#!/usr/bin/env python3
"""
hri200node.py - a complete SvxLink node for the Yaesu HRI-200.

One process, three jobs:

  1. Speaks the HRI-200 control protocol over /dev/ttyACM0
  2. Bridges PTT and squelch to SvxLink through its pseudo-terminal drivers
  3. Serves a small web panel for changing settings

Audio is deliberately not handled here. The HRI-200's codec is plain USB Audio
Class 1.0, so SvxLink opens it directly through ALSA - nothing is resampled
twice and no latency is added to the audio path.

    Tx1:  PTT_TYPE=PTY   PTT_PTY=/dev/shm/hri200_ptt   svxlink -> 'T' / 'R'
    Rx1:  SQL_DET=PTY    PTY_PATH=/dev/shm/hri200_sql  svxlink <- 'O' / 'Z'

Because the daemon and the panel share a process, changing frequency, power,
mode or tone needs no service restart. The box only reads D1M during
initialisation, so the node closes the serial port, repeats the handshake and
sends the new frame - which takes about four seconds and never interrupts
SvxLink. Only EchoLink changes require restarting svxlink itself.

USAGE
    hri200node.py              run the node and the web panel
    hri200node.py --setup         patch svxlink.conf and /etc/asound.conf
    hri200node.py --check         report on everything that has to be right
    hri200node.py --no-web        run the node only
    hri200node.py --wait-network  block until DNS answers, for ExecStartPre

SAFETY PROPERTY
    PTT is asserted by which poll frame is sent, not by a latching command. If
    this process dies, stalls or is killed, the HRI-200 drops the transmitter
    on its own within about a second. A software fault cannot leave the radio
    keyed.

PREREQUISITES
    Flash switch inside the box in NORMAL position - lsusb shows 26aa:0002 and
    26aa:0003. Radio in HRI-200 node mode: on an FTM-400D, power on holding
    [D/X] + [GM] until the display reads HRI-200. [D/X] alone gives PDN mode,
    which looks similar and does not work.
"""

import argparse
import difflib
import errno
import html
import os
import re
import secrets
import select
import shutil
import signal
import socket
import subprocess
import sys
import termios
import threading
import time
import tty
from functools import wraps

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

CONF = os.environ.get("HRI200_CONF", "/etc/hri200node.conf")
ECHOLINK_CONF = os.environ.get(
    "ECHOLINK_CONF", "/etc/svxlink/svxlink.d/ModuleEchoLink.conf")
SVXLINK_CONF = os.environ.get("SVXLINK_CONF", "/etc/svxlink/svxlink.conf")
ASOUND_CONF = os.environ.get("ASOUND_CONF", "/etc/asound.conf")

SOH, EOT = 0x01, 0x04
BAUD = 38400

# D1M carries the complete channel configuration. Every field below was
# established by capturing one WIRES-X session per setting and diffing the
# result; in each case exactly one character changed. See PROTOCOL.md s7.
#
#   {M} mode  {F} frequency  {N} narrow  {T} tone mode
#   {C} CTCSS {D} DCS        {P} power
#
# The VFO B half was constant across every reference capture and is passed
# through verbatim. The shift sign is normalised by the box: a '-' comes back
# as '+', which is expected rather than an error.
FREQ_TEMPLATE = ("D1M0043{M}000{F}-000.00000{N}{T}{C}{D}000{P}0"
                 "{F}+000.00000010887540002")

MODE_FM, MODE_DIGITAL = "4", "7"            # the box reports digital back as 5
TONE_OFF, TONE_CTCSS, TONE_DCS = "1", "2", "3"
POWER_CODE = {"high": "0", "mid": "1", "low": "2"}      # inverted scale

CTCSS_TONES = [
    67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8,
    97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3, 131.8,
    136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2, 165.5, 167.9, 171.3,
    173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5, 203.5,
    206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8, 250.3, 254.1,
]

DCS_CODES = [
    23, 25, 26, 31, 32, 36, 43, 47, 51, 53, 54, 65, 71, 72, 73, 74,
    114, 115, 116, 122, 125, 131, 132, 134, 143, 145, 152, 155, 156, 162,
    165, 172, 174, 205, 212, 223, 225, 226, 243, 244, 245, 246, 251, 252,
    255, 261, 263, 265, 266, 271, 274, 306, 311, 315, 325, 331, 332, 343,
    346, 351, 356, 364, 365, 371, 411, 412, 413, 423, 431, 432, 445, 446,
    452, 454, 455, 462, 464, 465, 466, 503, 506, 516, 523, 526, 532, 546,
    565, 606, 612, 624, 627, 631, 632, 654, 662, 664, 703, 712, 723, 731,
    732, 734, 743, 754,
]

BANDS = [(144.0, 146.0, "2 m"), (430.0, 440.0, "70 cm")]

DEFAULTS = {
    "FREQ": "434.5000",
    "PORT": "/dev/ttyACM0",
    "MODE": "fm",
    "POWER": "mid",
    "NARROW": "0",
    "TONE": "none",
    "CTCSS": "88.5",
    "DCS": "23",
    "PTT_PTY": "/dev/shm/hri200_ptt",
    "SQL_PTY": "/dev/shm/hri200_sql",
    "POLL_INTERVAL": "0.2",
    "RX_BLANK": "0.4",
    "TX_TIMEOUT": "300",
    "WEB_HOST": "0.0.0.0",
    "WEB_PORT": "8080",
    "WEB_USER": "svx",
    "WEB_PASSWORD": "password",
}

RADIO_KEYS = ("FREQ", "MODE", "POWER", "NARROW", "TONE", "CTCSS", "DCS")

KEPT = "\u2022" * 8         # shown when a password is stored but not revealed

# The identity an unconfigured node carries. Debian's own placeholders, so
# --check and the panel agree on what "not set yet" looks like.
PLACEHOLDER = {
    "node_callsign": "MYCALL",
    "callsign": "MYCALL-L",
    "sysopname": "MyName",
    "location": "[Svx] MyTown",
    "password": "MyPass",
}
UNSET_PASSWORDS = ("", "MyPass", "your_echolink_password")

VERBOSE = False
_log_lock = threading.Lock()


def log(msg):
    with _log_lock:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def dbg(msg):
    if VERBOSE:
        log(f"  . {msg}")


# ---------------------------------------------------------------------------
# Reading and writing key=value config files
# ---------------------------------------------------------------------------

def read_keys(path, keys=None):
    """Returns {key: value} for uncommented keys in path. Quotes stripped."""
    out = {}
    try:
        with open(path) as f:
            for line in f:
                m = re.match(r"^\s*([A-Za-z_0-9]+)\s*=\s*(.*?)\s*$", line)
                if m and (keys is None or m.group(1) in keys):
                    v = m.group(2)
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                        v = v[1:-1]
                    out[m.group(1)] = v
    except OSError:
        pass
    return out


def write_keys(path, values):
    """Replaces each key in place, appending any that are missing.

    Everything else in the file - comments, ordering, keys not listed - is
    preserved exactly. Written through a temporary file in the same directory
    and renamed, so a failure part way cannot leave a half-written config.
    """
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []
    except OSError as e:
        raise RuntimeError(f"Cannot read {path}: {e.strerror}")

    remaining = dict(values)
    for i, line in enumerate(lines):
        m = re.match(r"^\s*#?\s*([A-Za-z_0-9]+)\s*=", line)
        if m and m.group(1) in remaining:
            k = m.group(1)
            lines[i] = f"{k}={remaining.pop(k)}\n"
    for k, v in remaining.items():
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{k}={v}\n")

    _write_lines(path, lines)


def _write_lines(path, lines):
    """Writes lines to path, atomically when possible.

    The preferred route is a temporary file in the same directory followed by
    a rename, so an interruption cannot leave a half-written config. That
    needs write permission on the DIRECTORY, which the panel does not have:
    the config files live in /etc, and only the files themselves are group
    writable.

    So fall back to rewriting in place. That is not atomic, but the whole
    content is assembled first and written in a single call, which keeps the
    window down to one syscall.
    """
    data = "".join(lines)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w") as f:
            f.write(data)
        if os.path.exists(path):
            st = os.stat(path)
            os.chmod(tmp, st.st_mode & 0o7777)
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except PermissionError:
                pass
        os.replace(tmp, path)
        return
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if e.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
            raise RuntimeError(f"Cannot write {path}: {e.strerror}")

    try:
        with open(path, "w") as f:
            f.write(data)
    except OSError as e:
        raise RuntimeError(
            f"Cannot write {path}: {e.strerror}. The file must be writable by "
            f"uid {os.getuid()} - see the chgrp/chmod steps in install.sh.")


def read_section_key(path, section, key):
    """Reads one key from one section. CALLSIGN appears in three sections of
    svxlink.conf, so a plain grep would find the wrong one."""
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return None
    bounds = _section_bounds(lines)
    if section not in bounds:
        return None
    a, b = bounds[section]
    for i in range(a, b):
        m = re.match(r"^\s*([A-Za-z_0-9]+)\s*=\s*(.*?)\s*$", lines[i])
        if m and m.group(1) == key:
            v = m.group(2)
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            return v
    return None


def write_section_key(path, section, key, value):
    """Replaces one key inside one section, leaving the rest of the file
    byte for byte where it was."""
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError as e:
        raise RuntimeError(f"Cannot read {path}: {e.strerror}")
    bounds = _section_bounds(lines)
    if section not in bounds:
        raise RuntimeError(f"[{section}] not found in {path}")
    a, b = bounds[section]
    new = f"{key}={value}\n"
    for i in range(a, b):
        m = re.match(r"^\s*#?\s*([A-Za-z_0-9]+)\s*=", lines[i])
        if m and m.group(1) == key:
            if lines[i] == new:
                return False
            lines[i] = new
            break
    else:
        lines.insert(b, new)

    _write_lines(path, lines)
    return True


def load_conf():
    cfg = dict(DEFAULTS)
    cfg.update(read_keys(CONF))
    return cfg


# ---------------------------------------------------------------------------
# Building the D1M channel frame
# ---------------------------------------------------------------------------

def build_d1m(cfg, force=False):
    """Assembles a complete D1M frame from the config. Returns (frame, label).

    Raises ValueError with a readable message if anything is out of range, so
    the web panel can show it and the node can refuse to start on it.
    """
    try:
        mhz = float(str(cfg["FREQ"]).replace(",", "."))
    except (ValueError, KeyError):
        raise ValueError(f"{cfg.get('FREQ')!r} is not a frequency in MHz.")

    f = f"{mhz:09.5f}"
    if len(f) != 9:
        raise ValueError(f"{mhz:g} MHz does not fit the NNN.NNNNN field. "
                         "Three digits before the point, e.g. 434.5000")

    band = next((n for lo, hi, n in BANDS if lo <= mhz <= hi), None)
    if not band and not force:
        raise ValueError(f"{mhz:g} MHz is outside 144-146 and 430-440 MHz. "
                         "The radio will not transmit there.")

    mode = str(cfg.get("MODE", "fm")).lower()
    if mode not in ("fm", "digital"):
        raise ValueError("Mode must be fm or digital.")
    power = str(cfg.get("POWER", "mid")).lower()
    if power not in POWER_CODE:
        raise ValueError("Power must be high, mid or low.")
    tone = str(cfg.get("TONE", "none")).lower()
    if tone not in ("none", "ctcss", "dcs"):
        raise ValueError("Tone must be none, ctcss or dcs.")
    narrow = str(cfg.get("NARROW", "0")) in ("1", "true", "yes", "on")

    ctcss = float(cfg.get("CTCSS", 88.5))
    if tone == "ctcss" and not any(abs(ctcss - t) < 0.05 for t in CTCSS_TONES):
        raise ValueError(f"{ctcss:g} Hz is not a standard CTCSS tone.")
    dcs = int(cfg.get("DCS", 23))
    if tone == "dcs" and dcs not in DCS_CODES:
        raise ValueError(f"{dcs} is not a standard DCS code.")

    tone_code = {"none": TONE_OFF, "ctcss": TONE_CTCSS, "dcs": TONE_DCS}[tone]

    frame = (FREQ_TEMPLATE
             .replace("{M}", MODE_DIGITAL if mode == "digital" else MODE_FM)
             .replace("{F}", f)
             .replace("{N}", "1" if narrow else "0")
             .replace("{T}", tone_code)
             .replace("{C}", f"{int(ctcss):03d}")     # truncated, not rounded
             .replace("{D}", f"{int(dcs):03d}")
             .replace("{P}", POWER_CODE[power]))

    body = frame[3:]
    if int(body[:4], 16) != len(body) - 4:
        raise ValueError("Length field disagrees with the assembled payload.")

    bits = [f"{mhz:.4f} MHz" + (f" ({band})" if band else " [out of band]"),
            "C4FM" if mode == "digital" else "FM",
            f"power {power}", "narrow" if narrow else "wide"]
    if tone == "ctcss":
        bits.append(f"CTCSS {ctcss:.1f} Hz")
    elif tone == "dcs":
        bits.append(f"DCS {dcs:03d}")
    else:
        bits.append("no tone")
    return frame, ", ".join(bits)


# ---------------------------------------------------------------------------
# HRI-200 control protocol
# ---------------------------------------------------------------------------

class HRI:
    """Client for the HRI-200 control protocol.

    Framing   SOH(0x01) <ASCII payload> EOT(0x04)
    M00       handshake, mandatory - the box answers nothing without it
    R6423     device information, hex-encoded ASCII at an odd offset
    D1V0000   radio identification, needs several retries over ~4 s
    D1M....   channel configuration, read only during initialisation
    P010000   poll, PTT off        P100000   poll, PTT on
    B<n>...   poll reply, <n> is the squelch state
    D1P0004vv unsolicited status push, 0x10 = RX, 0x20 = TX
    """

    def __init__(self, port):
        self.port = port
        self.ser = None
        self.buf = bytearray()
        self.sql = False        # squelch open, as reported by the box
        self.tx = False         # the box says the transmitter is up
        self.ptt = False        # what we are asking for
        self.radio = None
        self.serial_no = None
        self.firmware = None

    # -- transport ----------------------------------------------------------

    def open(self):
        import serial
        # DTR and RTS must be low BEFORE the port is opened. pySerial raises
        # both by default and the MCU reads that as a reset: the radio reboots
        # and loses its configuration. Same mechanism as Arduino auto-reset.
        s = serial.Serial()
        s.port = self.port
        s.baudrate = BAUD
        s.timeout = 0
        s.dtr = False
        s.rts = False
        s.open()
        self.ser = s
        self.buf.clear()
        self.sql = self.tx = self.ptt = False

    def close(self, polite=True):
        if not self.ser:
            return
        try:
            if polite:
                self.ptt = False
                self.poll()
                time.sleep(0.1)
                self.send("P010010")        # what WIRES-X sends when it exits
                time.sleep(0.05)
            self.ser.close()
        except Exception:
            pass
        self.ser = None

    def fileno(self):
        return self.ser.fileno() if self.ser else -1

    def send(self, s):
        if self.ser:
            self.ser.write(bytes([SOH]) + s.encode("ascii") + bytes([EOT]))

    def frames(self):
        """Complete SOH..EOT frames received since the last call."""
        if not self.ser:
            return []
        try:
            d = self.ser.read(4096)
        except Exception:
            return []
        if d:
            self.buf += d
        out = []
        while True:
            try:
                i = self.buf.index(SOH)
                j = self.buf.index(EOT, i + 1)
            except ValueError:
                if len(self.buf) > 4096:            # resync on garbage
                    del self.buf[:-256]
                return out
            out.append(self.buf[i + 1:j].decode("latin1"))
            del self.buf[:j + 1]

    def expect(self, prefix, timeout=2.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for f in self.frames():
                if f.startswith(prefix):
                    return f
            time.sleep(0.02)
        return None

    def flush_input(self):
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        self.buf.clear()

    # -- protocol -----------------------------------------------------------

    def poll(self):
        self.send("P100000" if self.ptt else "P010000")

    def set_ptt(self, on):
        """Returns True if the state actually changed."""
        if on == self.ptt:
            return False
        self.ptt = on
        self.poll()          # immediately, or PTT latency = the poll interval
        return True

    def update(self):
        """Pumps received frames into sql / tx."""
        for f in self.frames():
            new = None
            if f.startswith("B") and len(f) > 1:
                new = f[1] == "1"
            elif f.startswith("D1P0004") and len(f) >= 11:
                # The status byte is the LAST two characters; the four after
                # D1P are a length field.
                try:
                    v = int(f[-2:], 16)
                except ValueError:
                    continue
                new = bool(v & 0x10)
                self.tx = bool(v & 0x20)
            if new is not None and new != self.sql:
                self.sql = new
                dbg(f"box squelch -> {'OPEN' if new else 'closed'}")

    def detect_radio(self, attempts=8, gap=1.2):
        """The box needs several seconds to notice the attached radio.

        In the reference capture WIRES-X got no reply at t=1.0 s or t=2.1 s
        and only succeeded at t=4.1 s. The poll is kept running throughout.
        """
        for i in range(1, attempts + 1):
            self.send("D1V0000")
            end = time.monotonic() + gap
            while time.monotonic() < end:
                for f in self.frames():
                    if f.startswith("D1V") and len(f) > 7:
                        return f[7:].strip()
                self.poll()
                time.sleep(0.15)
            if i == 2:
                log("  the box needs a few seconds ...")
        return None

    def handshake(self, d1m):
        """M00, device info, radio detection, channel configuration.

        Returns None on success or a human-readable reason on failure.
        """
        self.send("M00")
        if self.expect("M00") is None:
            return ("No response to M00. Flash switch in normal position? "
                    "Cable seated? Is something else holding the port?")
        log("[OK] M00 acknowledged")

        self.send("R6423")
        r = self.expect("R")
        if r:
            try:
                p = bytes.fromhex(r[2:]).decode("ascii", "replace").split(",")
                self.serial_no = p[2]
                d = p[3]
                self.firmware = (f"{d[0:4]}-{d[4:6]}-{d[6:8]} "
                                 f"{d[8:10]}:{d[10:12]}:{d[12:14]}")
                log(f"[OK] Serial {self.serial_no}, "
                    f"firmware built {self.firmware}")
            except Exception:
                pass

        radio = self.detect_radio()
        if not radio:
            return ("Radio does not respond. Does the display read HRI-200? "
                    "Power it fully off and on holding [D/X] + [GM], wait "
                    "five seconds, then retry. [D/X] alone gives PDN mode, "
                    "which will not work.")
        self.radio = radio
        log(f"[OK] Radio: {radio}")

        # Not persistent - in node mode the radio is a slave and the host owns
        # the configuration, so this goes out on every connection.
        self.send(d1m)
        if self.expect("D1M", 2.0):
            log("[OK] Channel configured")
        else:
            log("[!] No acknowledgement for the channel configuration")
        return None


# ---------------------------------------------------------------------------
# SvxLink's pseudo-terminals
# ---------------------------------------------------------------------------

class Pty:
    """One of SvxLink's PTYs, reached through the symlink it creates.

    SvxLink allocates the master, calls cfmakeraw() on it and symlinks the
    slave to the configured path. It recreates that symlink on every restart
    pointing at a different /dev/pts/N, so the target is re-checked and the
    slave reopened whenever it moves.
    """

    RETRY = 1.0

    def __init__(self, path, label):
        self.path = path
        self.label = label
        self.fd = None
        self.target = None
        self.next_try = 0.0
        self.last_err = None

    def _complain(self, kind, msg):
        """Logs a failure once, then stays quiet until the reason changes."""
        if self.last_err != kind:
            self.last_err = kind
            log(f"[{self.label}] {msg}")

    def close(self, why=""):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            log(f"[{self.label}] disconnected{': ' + why if why else ''}")
        self.fd = None
        self.target = None

    def ensure(self, now):
        """Opens or reopens the slave. Returns True if it just came up."""
        if self.fd is not None:
            try:
                if os.readlink(self.path) == self.target:
                    return False
            except OSError:
                pass
            self.close("svxlink recreated the pty")

        if now < self.next_try:
            return False
        self.next_try = now + self.RETRY

        try:
            target = os.readlink(self.path)
        except FileNotFoundError:
            self._complain("waiting",
                           f"{self.path} does not exist yet - is svxlink "
                           "running?")
            return False
        except OSError as e:
            if e.errno == errno.EINVAL:
                # Not a symlink. Almost always a stray regular file left by a
                # shell redirection such as `echo T > /dev/shm/hri200_ptt`.
                # svxlink cannot symlink over it, so its Tx setup failed - and
                # PttPty's destructor then dereferences a null pointer.
                self._complain("blocked",
                               f"{self.path} is a regular file, not a "
                               "symlink. Remove it and restart svxlink - a "
                               "plain file here stops svxlink creating its "
                               "pty and makes it segfault.")
            else:
                self._complain("readlink", f"{self.path}: {e.strerror}")
            return False

        try:
            fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except PermissionError:
            # /dev/shm is mode 1777, so with fs.protected_symlinks=1 the
            # kernel refuses to follow a symlink there unless the follower
            # owns it or owns the directory. Root is NOT exempt - that is the
            # point of the protection - so running under sudo fails where
            # running as the svxlink user succeeds.
            try:
                owner = os.lstat(self.path).st_uid
            except OSError:
                owner = "?"
            self._complain(
                "denied",
                f"permission denied opening {self.path} (-> {target}). "
                f"Running as uid {os.getuid()}, symlink owned by uid {owner}. "
                "With fs.protected_symlinks=1 even root cannot follow it. Run "
                "as the same user as svxlink.")
            return False
        except OSError as e:
            self._complain("open",
                           f"{self.path} -> {target}: {e.strerror}. svxlink "
                           "may have exited; it leaves a stale symlink behind "
                           "when it crashes.")
            return False
        self.last_err = None

        # svxlink already put the pair in raw mode. Doing it again is harmless
        # and guards against a canonical-mode slave, where a read would block
        # forever waiting for a newline that never comes.
        try:
            tty.setraw(fd)
        except termios.error:
            pass

        self.fd = fd
        self.target = target
        log(f"[{self.label}] connected: {self.path} -> {target}")
        return True

    def read(self):
        if self.fd is None:
            return b""
        try:
            data = os.read(self.fd, 256)
        except BlockingIOError:
            return b""
        except OSError as e:
            self.close(f"read error: {e.strerror}")
            return b""
        if not data:
            self.close("master closed")
        return data

    def write(self, data):
        if self.fd is None:
            return False
        try:
            os.write(self.fd, data)
            return True
        except BlockingIOError:
            return False
        except OSError as e:
            self.close(f"write error: {e.strerror}")
            return False


# ---------------------------------------------------------------------------
# Shared state between the node thread and the web thread
# ---------------------------------------------------------------------------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.reconfigure = threading.Event()
        self.connected = False
        self.radio = None
        self.serial_no = None
        self.firmware = None
        self.channel = ""
        self.squelch = False
        self.transmitting = False
        self.svxlink_linked = False
        self.error = None
        self.started = time.time()

    def snapshot(self):
        with self.lock:
            return {
                "connected": self.connected,
                "radio": self.radio,
                "serial_no": self.serial_no,
                "firmware": self.firmware,
                "channel": self.channel,
                "squelch": self.squelch,
                "transmitting": self.transmitting,
                "svxlink_linked": self.svxlink_linked,
                "error": self.error,
                "uptime": int(time.time() - self.started),
            }

    def set(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)


STATE = State()


# ---------------------------------------------------------------------------
# The node thread
# ---------------------------------------------------------------------------

def node_loop(state):
    """Serial handshake, poll loop, PTT and squelch bridging.

    Reconnects on its own if the box goes away, and reconnects deliberately
    when the web panel changes a radio setting - the box only reads D1M during
    initialisation, so a new channel means a new handshake.
    """
    hri = None
    cfg = load_conf()
    sql_pty = Pty(cfg["SQL_PTY"], "SQL")
    ptt_pty = Pty(cfg["PTT_PTY"], "PTT")

    sent_sql = None
    blank_until = 0.0
    flush_at = None
    next_poll = 0.0
    tx_started = 0.0
    retry_at = 0.0

    while not state.stop.is_set():
        now = time.monotonic()

        # ---- (re)connect to the box ---------------------------------------
        if hri is None or hri.ser is None:
            if now < retry_at:
                time.sleep(0.2)
                continue
            cfg = load_conf()
            try:
                d1m, label = build_d1m(cfg)
            except ValueError as e:
                state.set(error=f"Configuration rejected: {e}", connected=False)
                log(f"[FAIL] {e}")
                retry_at = now + 30
                continue

            log(f"Connecting to HRI-200 on {cfg['PORT']}")
            log(f"  {label}")
            hri = HRI(cfg["PORT"])
            try:
                hri.open()
            except Exception as e:
                state.set(error=f"Cannot open {cfg['PORT']}: {e}",
                          connected=False)
                log(f"[FAIL] Cannot open {cfg['PORT']}: {e}")
                hri = None
                retry_at = now + 10
                continue

            reason = hri.handshake(d1m)
            if reason:
                log(f"[FAIL] {reason}")
                state.set(error=reason, connected=False)
                hri.close(polite=False)
                hri = None
                retry_at = now + 15
                continue

            state.set(connected=True, radio=hri.radio, serial_no=hri.serial_no,
                      firmware=hri.firmware, channel=label, error=None)
            sent_sql = None
            state.reconfigure.clear()
            continue

        # ---- a radio setting changed --------------------------------------
        if state.reconfigure.is_set() and not hri.ptt:
            log("Channel change requested - reconnecting to apply it")
            state.reconfigure.clear()
            state.set(connected=False)
            hri.close()
            hri = None
            retry_at = time.monotonic() + 0.5
            continue

        # ---- svxlink's pseudo-terminals -----------------------------------
        if sql_pty.ensure(now):
            sent_sql = None                 # resend state to a fresh svxlink
        ptt_pty.ensure(now)
        state.set(svxlink_linked=ptt_pty.fd is not None
                  and sql_pty.fd is not None)

        if ptt_pty.fd is None and hri.ptt:
            log("[!] svxlink disconnected while keyed - dropping PTT")
            hri.set_ptt(False)
            blank_until = now + float(cfg["RX_BLANK"])
            flush_at = now + 0.3

        # ---- wait for something to happen ---------------------------------
        fds = [hri.fileno()]
        if ptt_pty.fd is not None:
            fds.append(ptt_pty.fd)
        deadlines = [next_poll - now, 0.25]
        if flush_at:
            deadlines.append(flush_at - now)
        if blank_until > now:
            deadlines.append(blank_until - now)
        try:
            select.select(fds, [], [], max(min(deadlines), 0.005))
        except (OSError, ValueError):
            pass
        now = time.monotonic()

        # ---- serial ---------------------------------------------------------
        try:
            hri.update()
        except Exception as e:
            log(f"[!] Serial error: {e} - reconnecting")
            state.set(connected=False, error=f"Serial error: {e}")
            hri.close(polite=False)
            hri = None
            retry_at = now + 5
            continue

        # ---- svxlink asking for PTT ---------------------------------------
        data = ptt_pty.read()
        if data:
            want = hri.ptt
            for c in data:
                if c == ord("T"):
                    want = True
                elif c == ord("R"):
                    want = False
            if hri.set_ptt(want):
                next_poll = now + float(cfg["POLL_INTERVAL"])
                if want:
                    tx_started = now
                    log("TX on")
                else:
                    log("TX off")
                    # The box reports our own transmission back as a squelch
                    # event. Blank it and drop what is already buffered.
                    blank_until = now + float(cfg["RX_BLANK"])
                    flush_at = now + 0.3
                    hri.sql = False
                state.set(transmitting=want)

        # ---- transmit timeout ---------------------------------------------
        tmo = float(cfg["TX_TIMEOUT"])
        if hri.ptt and tmo and now - tx_started > tmo:
            log(f"[!] TX timeout after {tmo:.0f} s - dropping PTT")
            hri.set_ptt(False)
            state.set(transmitting=False)
            blank_until = now + float(cfg["RX_BLANK"])
            flush_at = now + 0.3
            hri.sql = False

        # ---- deferred flush -------------------------------------------------
        if flush_at and now >= flush_at:
            hri.flush_input()
            hri.sql = False
            flush_at = None

        # ---- keep polling ---------------------------------------------------
        if now >= next_poll:
            hri.poll()
            next_poll = now + float(cfg["POLL_INTERVAL"])

        # ---- report squelch --------------------------------------------------
        # Never report an open squelch while transmitting, nor during the
        # blanking window after unkeying.
        open_now = hri.sql and not hri.ptt and now >= blank_until
        if open_now != sent_sql:
            if sql_pty.write(b"O" if open_now else b"Z"):
                sent_sql = open_now
                state.set(squelch=open_now)
                log(f"COS {'OPEN' if open_now else 'closed'}")

    log("Node stopping")
    if hri:
        hri.set_ptt(False)
        hri.close()
    sql_pty.write(b"Z")
    sql_pty.close()
    ptt_pty.close()


# ---------------------------------------------------------------------------
# Service control, through a narrow sudoers rule
# ---------------------------------------------------------------------------

def systemctl(action, unit):
    try:
        r = subprocess.run(["sudo", "-n", "systemctl", action, unit],
                           capture_output=True, text=True, timeout=45)
        return r.returncode == 0, (r.stderr or r.stdout).strip()
    except subprocess.TimeoutExpired:
        return False, f"systemctl {action} {unit} timed out"
    except OSError as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Web panel
# ---------------------------------------------------------------------------

def make_app():
    from flask import Flask, request, redirect, url_for, Response, session, jsonify

    app = Flask(__name__)
    app.secret_key = secrets.token_bytes(32)

    def protected(view):
        @wraps(view)
        def wrapper(*a, **kw):
            cfg = load_conf()
            user, pw = cfg.get("WEB_USER"), cfg.get("WEB_PASSWORD")
            auth = request.authorization
            if (not auth
                    or not secrets.compare_digest(auth.username or "", user)
                    or not secrets.compare_digest(auth.password or "", pw)):
                return Response("Authentication required.", 401,
                                {"WWW-Authenticate":
                                 'Basic realm="Node configuration"'})
            return view(*a, **kw)
        return wrapper

    @app.route("/")
    @protected
    def index():
        cfg = load_conf()
        el = read_keys(ECHOLINK_CONF,
                       {"CALLSIGN", "SYSOPNAME", "LOCATION", "PASSWORD"})
        node_cs = read_section_key(SVXLINK_CONF, "SimplexLogic", "CALLSIGN")
        notice = session.pop("notice", None)
        errors = session.pop("errors", [])
        form = session.pop("form", None)
        if form:
            cfg.update(form)
        token = secrets.token_urlsafe(24)
        session["token"] = token
        return render(cfg, el, node_cs or "", notice, errors, token)

    @app.route("/status")
    @protected
    def status():
        return jsonify(STATE.snapshot())

    @app.route("/save", methods=["POST"])
    @protected
    def save():
        if not secrets.compare_digest(request.form.get("token", ""),
                                      session.get("token", "")):
            session["errors"] = ["That form had expired. Nothing was changed "
                                 "- check the values and save again."]
            return redirect(url_for("index"))

        f = request.form
        errors = []
        radio = {
            "FREQ": f.get("freq", "").strip().replace(",", "."),
            "MODE": f.get("mode", "fm"),
            "POWER": f.get("power", "mid"),
            "NARROW": "1" if f.get("narrow") == "on" else "0",
            "TONE": f.get("tone", "none"),
            "CTCSS": f.get("ctcss", "88.5"),
            "DCS": f.get("dcs", "23"),
        }
        try:
            _, label = build_d1m(radio)
        except ValueError as e:
            errors.append(str(e))
            label = ""

        node_cs = f.get("node_callsign", "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{3,8}", node_cs):
            errors.append("Node callsign must be 3-8 letters and digits, with "
                          "no suffix - this is what SvxLink identifies with. "
                          "Example: SA0XXX")

        cs = f.get("callsign", "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{3,8}(-[LR])?", cs):
            errors.append("Callsign must be 3-8 letters and digits, "
                          "optionally followed by -L for a simplex link or "
                          "-R for a repeater. Example: SA0XXX-L")
        sysop = f.get("sysopname", "").strip()
        loc = f.get("location", "").strip()
        if not sysop:
            errors.append("Sysop name cannot be empty.")
        if not loc:
            errors.append("Location cannot be empty.")
        pw = f.get("password", "")
        new_pw = None if pw in ("", KEPT) else pw
        if new_pw is not None and len(new_pw) < 3:
            errors.append("EchoLink password looks too short.")

        if errors:
            session["errors"] = errors
            session["form"] = radio
            return redirect(url_for("index"))

        before = read_keys(ECHOLINK_CONF,
                           {"CALLSIGN", "SYSOPNAME", "LOCATION"})
        el = {"CALLSIGN": cs, "SYSOPNAME": sysop, "LOCATION": loc}
        if new_pw is not None:
            el["PASSWORD"] = new_pw

        old_node_cs = read_section_key(SVXLINK_CONF, "SimplexLogic", "CALLSIGN")
        try:
            write_keys(CONF, radio)
            write_keys(ECHOLINK_CONF, el)
            node_cs_changed = write_section_key(
                SVXLINK_CONF, "SimplexLogic", "CALLSIGN", node_cs)
        except RuntimeError as e:
            session["errors"] = [str(e)]
            return redirect(url_for("index"))

        done = [f"Radio set to {label}."]
        if node_cs_changed:
            done.insert(0, f"Node callsign set to {node_cs}"
                           f"{f' (was {old_node_cs})' if old_node_cs else ''}.")
        STATE.reconfigure.set()
        done.append("Reconnecting to the box to apply it, about four "
                    "seconds. SvxLink is not interrupted.")

        if (node_cs_changed or new_pw is not None
                or any(before.get(k) != v
                       for k, v in el.items() if k in before)):
            ok, msg = systemctl("restart", "svxlink")
            done.append("SvxLink restarted for the EchoLink changes; give it "
                        "about 25 seconds." if ok
                        else f"SvxLink did not restart: {msg}")

        session["notice"] = " ".join(done)
        return redirect(url_for("index"))

    return app


def render(cfg, el, node_cs, notice, errors, token):
    def e(v):
        return html.escape(str(v), quote=True)

    def sel(a, b):
        return " selected" if str(a) == str(b) else ""

    ctcss_opts = "".join(
        f'<option value="{t}"{sel(t, cfg.get("CTCSS"))}>{t:.1f} Hz</option>'
        for t in CTCSS_TONES)
    dcs_opts = "".join(
        f'<option value="{d}"{sel(d, cfg.get("DCS"))}>D{d:03d}</option>'
        for d in DCS_CODES)

    err_html = ""
    if errors:
        items = "".join(f"<li>{e(x)}</li>" for x in errors)
        err_html = ('<div class="msg msg-bad"><b>Nothing was saved.</b>'
                    f'<ul>{items}</ul></div>')
    ok_html = f'<div class="msg msg-ok">{e(notice)}</div>' if notice else ""

    stored = el.get("PASSWORD", "")
    has_pw = stored not in UNSET_PASSWORDS
    pw_hint = ("A password is stored. Leave this alone to keep it."
               if has_pw else
               "No password stored yet. EchoLink cannot log in without one.")
    narrow = str(cfg.get("NARROW", "0")) in ("1", "true", "yes", "on")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Node configuration</title>
<style>
  :root {{
    --case-edge:#15171a; --face:#2d3138; --face-edge:#3a3f47;
    --legend:#8c939c; --text:#dfe3e8;
    --lcd-bg:#171204; --lcd:#ffb224; --lcd-ghost:rgba(255,178,36,.14);
    --steel:#6ea3c0; --bad:#e0653c; --good:#7fb069;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:24px 16px 64px; background:var(--case-edge);
          color:var(--text);
          font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }}
  .rack {{ max-width:660px; margin:0 auto; }}

  .readout {{ background:var(--lcd-bg); border:1px solid #000;
              border-radius:3px; padding:22px 20px 16px; margin-bottom:6px;
              box-shadow:inset 0 2px 14px rgba(0,0,0,.85); }}
  .readout .freq {{ font:600 clamp(38px,11vw,62px)/1 ui-monospace,
                    "DejaVu Sans Mono",Menlo,Consolas,monospace;
                    font-variant-numeric:tabular-nums; color:var(--lcd);
                    text-shadow:0 0 18px rgba(255,178,36,.35); }}
  .readout .freq small {{ font-size:.34em; letter-spacing:.18em;
                          margin-left:.5em; opacity:.75; }}
  .tags {{ margin-top:12px; display:flex; flex-wrap:wrap; gap:6px 8px;
           font:600 11px/1 ui-monospace,"DejaVu Sans Mono",monospace;
           letter-spacing:.14em; }}
  .tags span {{ color:var(--lcd); border:1px solid rgba(255,178,36,.3);
                padding:5px 8px; border-radius:2px; }}
  .tags span.off {{ color:var(--lcd-ghost); border-color:var(--lcd-ghost); }}

  .live {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:6px; }}
  .lamp {{ flex:1 1 120px; background:var(--face);
           border:1px solid var(--face-edge); padding:9px 11px;
           border-radius:2px; font-size:12px; color:var(--legend); }}
  .lamp b {{ display:block; margin-top:3px; font-size:13px; color:var(--text);
             font-weight:600; }}
  .lamp.on b {{ color:var(--good); }}
  .lamp.tx b {{ color:var(--bad); }}
  .lamp.bad b {{ color:var(--bad); }}

  .module {{ background:var(--face); border:1px solid var(--face-edge);
             border-top:none; padding:20px; }}
  .module:last-of-type {{ border-radius:0 0 4px 4px; }}
  .legend {{ font:600 11px/1 system-ui,sans-serif; letter-spacing:.2em;
             text-transform:uppercase; color:var(--legend); margin:0 0 16px;
             padding-bottom:10px; border-bottom:1px solid var(--face-edge); }}
  .row {{ display:flex; flex-wrap:wrap; gap:14px; }}
  .row > * {{ flex:1 1 180px; }}
  label.field {{ display:block; margin-bottom:14px; }}
  label.field > span {{ display:block; font-size:12px; color:var(--legend);
                        margin-bottom:5px; }}
  input[type=text], input[type=password], select {{
    width:100%; padding:9px 10px; background:#1b1e22; color:var(--text);
    border:1px solid #454b54; border-radius:3px;
    font:15px system-ui,sans-serif; }}
  input:focus, select:focus {{ outline:2px solid var(--steel);
                               outline-offset:1px; border-color:var(--steel); }}
  .hint {{ font-size:12px; color:var(--legend); margin-top:5px; }}
  .hint.warn {{ color:var(--bad); }}
  .check {{ display:flex; align-items:center; gap:9px; margin:4px 0 14px; }}
  .check input {{ width:17px; height:17px; accent-color:var(--steel); }}

  .actions {{ display:flex; gap:12px; flex-wrap:wrap; margin-top:18px; }}
  button {{ padding:11px 22px; border-radius:3px; cursor:pointer;
            font:600 14px system-ui,sans-serif; }}
  .save {{ background:var(--steel); color:#10161a; border:none; }}
  .save:hover {{ filter:brightness(1.12); }}
  .reset {{ background:transparent; color:var(--legend);
            border:1px solid #4a5058; }}
  .reset:hover {{ color:var(--text); border-color:var(--legend); }}

  .msg {{ padding:13px 16px; border-radius:3px; margin-bottom:14px;
          border-left:3px solid; }}
  .msg-ok {{ background:#1e2a1c; border-color:var(--good); }}
  .msg-bad {{ background:#2b1c17; border-color:var(--bad); }}
  .msg ul {{ margin:8px 0 0; padding-left:20px; }}
  footer {{ margin-top:22px; font-size:12px; color:#6b727a; line-height:1.6; }}
  .colophon {{ margin-top:20px; padding-top:14px;
               border-top:1px solid var(--face-edge);
               display:flex; justify-content:space-between; gap:12px;
               flex-wrap:wrap; font-size:11px; color:#6b727a;
               letter-spacing:.09em; text-transform:uppercase; }}
  .colophon b {{ color:var(--legend); font-weight:600; letter-spacing:.09em; }}
  .colophon a {{ color:var(--steel); text-decoration:none; }}
  .colophon a:hover {{ text-decoration:underline; }}
  @media (prefers-reduced-motion:reduce) {{ * {{ transition:none !important; }} }}
</style>
</head>
<body>
<div class="rack">
  {ok_html}{err_html}

  <div class="readout">
    <div class="freq"><span id="rFreq">{e(cfg.get('FREQ'))}</span><small>MHz</small></div>
    <div class="tags">
      <span id="rMode">FM</span>
      <span id="rWidth">WIDE</span>
      <span id="rTone" class="off">NO TONE</span>
      <span id="rPower">PWR MID</span>
    </div>
  </div>

  <div class="live">
    <div class="lamp" id="lRadio">Radio<b>...</b></div>
    <div class="lamp" id="lSvx">SvxLink<b>...</b></div>
    <div class="lamp" id="lSql">Squelch<b>...</b></div>
    <div class="lamp" id="lTx">Transmitter<b>...</b></div>
  </div>

  <form method="post" action="/save" id="cfg">
  <input type="hidden" name="token" value="{e(token)}">

  <div class="module">
    <p class="legend">Station</p>
    <label class="field"><span>Node callsign</span>
      <input type="text" name="node_callsign" value="{e(node_cs)}"
             autocomplete="off" spellcheck="false"
             placeholder="SA0XXX">
      <div class="hint">{'<span style="color:var(--bad)">Not set yet - the node identifies as MYCALL until you change this.</span>' if node_cs in ('', PLACEHOLDER["node_callsign"]) else 'What SvxLink identifies with, no suffix. Changing it restarts SvxLink.'}</div>
    </label>
  </div>

  <div class="module">
    <p class="legend">Radio</p>
    <div class="row">
      <label class="field"><span>Frequency, MHz</span>
        <input type="text" name="freq" id="freq" value="{e(cfg.get('FREQ'))}"
               inputmode="decimal" autocomplete="off"></label>
      <label class="field"><span>Power</span>
        <select name="power" id="power">
          <option value="low"{sel('low', cfg.get('POWER'))}>Low</option>
          <option value="mid"{sel('mid', cfg.get('POWER'))}>Mid</option>
          <option value="high"{sel('high', cfg.get('POWER'))}>High</option>
        </select></label>
    </div>
    <div class="row">
      <label class="field"><span>Modulation</span>
        <select name="mode" id="mode">
          <option value="fm"{sel('fm', cfg.get('MODE'))}>FM</option>
          <option value="digital"{sel('digital', cfg.get('MODE'))}>Digital (C4FM)</option>
        </select>
        <div class="hint" id="modeHint"></div></label>
      <label class="field"><span>Access tone</span>
        <select name="tone" id="tone">
          <option value="none"{sel('none', cfg.get('TONE'))}>None</option>
          <option value="ctcss"{sel('ctcss', cfg.get('TONE'))}>CTCSS, analogue</option>
          <option value="dcs"{sel('dcs', cfg.get('TONE'))}>DCS, digital</option>
        </select></label>
    </div>
    <div class="row">
      <label class="field" id="ctcssWrap"><span>CTCSS tone</span>
        <select name="ctcss" id="ctcss">{ctcss_opts}</select></label>
      <label class="field" id="dcsWrap"><span>DCS code</span>
        <select name="dcs" id="dcs">{dcs_opts}</select></label>
    </div>
    <label class="check">
      <input type="checkbox" name="narrow" id="narrow"{' checked' if narrow else ''}>
      <span>Narrow deviation</span></label>
  </div>

  <div class="module">
    <p class="legend">EchoLink</p>
    <div class="row">
      <label class="field"><span>Callsign</span>
        <input type="text" name="callsign" value="{e(el.get('CALLSIGN', 'SA0XXX-L'))}"
               autocomplete="off" spellcheck="false">
        <div class="hint">-L for a simplex link, -R for a repeater. Registered
          separately from your personal callsign, with its own password.</div></label>
      <label class="field"><span>Password</span>
        <input type="password" name="password" value="{KEPT if has_pw else ''}"
               autocomplete="new-password">
        <div class="hint">{e(pw_hint)}</div></label>
    </div>
    <div class="row">
      <label class="field"><span>Sysop name</span>
        <input type="text" name="sysopname" value="{e(el.get('SYSOPNAME', ''))}"></label>
      <label class="field"><span>Location</span>
        <input type="text" name="location" value="{e(el.get('LOCATION', ''))}">
        <div class="hint">Shown in the EchoLink directory.</div></label>
    </div>
  </div>

  <div class="actions">
    <button type="submit" class="save">Save and apply</button>
    <button type="button" class="reset" id="defaults">Load defaults</button>
  </div>
  </form>

  <footer>
    Radio changes reconnect to the box, which takes about four seconds and does
    not interrupt SvxLink. EchoLink changes restart SvxLink, which takes the
    node off the air for roughly 25 seconds.<br>
    This page speaks plain HTTP and holds your EchoLink password. Keep it on
    your own network.
  </footer>

  <div class="colophon">
    <span><b>SA7BNB</b> &middot; Anders Isaksson &middot; Sweden</span>
    <span><a href="https://github.com/sa7bnb/Svxlink-HRI-200"
             target="_blank" rel="noopener">HRI-200 SvxLink node</a></span>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const DEF = {{
  freq:"434.5000", mode:"fm", tone:"none", power:"mid",
  ctcss:"88.5", dcs:"23", narrow:false,
  node_callsign:"{PLACEHOLDER["node_callsign"]}",
  callsign:"{PLACEHOLDER["callsign"]}",
  sysopname:"{PLACEHOLDER["sysopname"]}",
  location:"{PLACEHOLDER["location"]}",
  password:"{PLACEHOLDER["password"]}"
}};

function paint() {{
  const f = parseFloat(($("freq").value || "0").replace(",", "."));
  $("rFreq").textContent = isNaN(f) ? "---.----" : f.toFixed(4);
  const dig = $("mode").value === "digital";
  $("rMode").textContent = dig ? "C4FM" : "FM";
  $("modeHint").textContent = dig
    ? "SvxLink cannot decode C4FM. The node will key up and pass no usable audio."
    : "";
  $("modeHint").className = dig ? "hint warn" : "hint";
  $("rWidth").textContent = $("narrow").checked ? "NARROW" : "WIDE";
  $("rPower").textContent = "PWR " + $("power").value.toUpperCase();
  const t = $("tone").value;
  $("ctcssWrap").style.display = t === "ctcss" ? "" : "none";
  $("dcsWrap").style.display   = t === "dcs"   ? "" : "none";
  const rt = $("rTone");
  if (t === "ctcss")    {{ rt.textContent = "CTCSS " + $("ctcss").value; rt.className=""; }}
  else if (t === "dcs") {{ rt.textContent = "DCS D" + String($("dcs").value).padStart(3,"0"); rt.className=""; }}
  else                  {{ rt.textContent = "NO TONE"; rt.className="off"; }}
}}
$("cfg").addEventListener("input", paint);
$("cfg").addEventListener("change", paint);

$("defaults").addEventListener("click", () => {{
  if (!confirm("Reset every field, including the callsign and the EchoLink "
             + "credentials, back to placeholders?\\n\\nUseful before taking an "
             + "image. Nothing is saved until you choose Save and apply.")) return;
  for (const [k,v] of Object.entries(DEF)) {{
    const el = $("cfg").elements[k];
    if (!el) continue;
    if (el.type === "checkbox") el.checked = v; else el.value = v;
  }}
  paint();
}});

function lamp(id, label, value, cls) {{
  const el = $(id);
  el.className = "lamp" + (cls ? " " + cls : "");
  el.innerHTML = label + "<b>" + value + "</b>";
}}
async function poll() {{
  try {{
    const s = await (await fetch("/status")).json();
    lamp("lRadio","Radio", s.connected ? (s.radio || "connected") : "not connected",
         s.connected ? "on" : "bad");
    lamp("lSvx","SvxLink", s.svxlink_linked ? "linked" : "waiting",
         s.svxlink_linked ? "on" : "bad");
    lamp("lSql","Squelch", s.squelch ? "OPEN" : "closed", s.squelch ? "on" : "");
    lamp("lTx","Transmitter", s.transmitting ? "KEYED" : "idle",
         s.transmitting ? "tx" : "");
  }} catch (e) {{
    lamp("lRadio","Radio","unreachable","bad");
  }}
}}
paint(); poll(); setInterval(poll, 2000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# --setup : patch svxlink.conf and /etc/asound.conf
# ---------------------------------------------------------------------------

SVX_CHANGES = [
    ("GLOBAL", "CARD_SAMPLE_RATE", "48000"),
    # Capture on the box is 1 channel, playback is 2. The plug device in
    # asound.conf duplicates mono to stereo on the way out.
    ("GLOBAL", "CARD_CHANNELS", "1"),
    ("SimplexLogic", "MUTE_RX_ON_TX", "1"),
    ("SimplexLogic", "IDENT_ONLY_AFTER_TX", "4"),
    ("SimplexLogic", "SHORT_IDENT_INTERVAL", "10"),
    ("SimplexLogic", "LONG_IDENT_INTERVAL", "60"),
    ("SimplexLogic", "REPORT_CTCSS", None),
    ("Rx1", "AUDIO_DEV", "alsa:hri200"),
    ("Rx1", "AUDIO_CHANNEL", "0"),
    # The radio's hardware squelch has already decided and the box hands it to
    # us. VOX on top of that only adds delay and false triggers.
    ("Rx1", "SQL_DET", "PTY"),
    ("Rx1", "PTY_PATH", "@SQL@"),
    ("Rx1", "SQL_START_DELAY", "0"),
    ("Rx1", "SQL_DELAY", "0"),
    ("Rx1", "SQL_HANGTIME", "1500"),
    ("Rx1", "SQL_TAIL_ELIM", "300"),
    ("Rx1", "DEEMPHASIS", "0"),
    ("Rx1", "SERIAL_PORT", None),
    ("Rx1", "SERIAL_PIN", None),
    ("Rx1", "DTMF_SERIAL", None),
    ("Tx1", "AUDIO_DEV", "alsa:hri200"),
    ("Tx1", "AUDIO_CHANNEL", "0"),
    ("Tx1", "PTT_TYPE", "PTY"),
    ("Tx1", "PTT_PTY", "@PTT@"),
    # Measured: TX came up 19 ms after the first P100000. 300 ms covers
    # scheduling jitter comfortably.
    ("Tx1", "TX_DELAY", "300"),
    ("Tx1", "TIMEOUT", "300"),
    ("Tx1", "PREEMPHASIS", "0"),
    ("Tx1", "PTT_PORT", None),
    ("Tx1", "PTT_PIN", None),
]

ASOUND = """pcm.hri200 {
    type plug
    slave.pcm "hw:CARD=codec,DEV=0"
    hint.description "Yaesu HRI-200"
}

ctl.hri200 {
    type hw
    card codec
}
"""

SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def _section_bounds(lines):
    bounds, cur, start = {}, None, None
    for i, line in enumerate(lines):
        m = SECTION_RE.match(line)
        if m:
            if cur is not None:
                bounds[cur] = (start, i)
            cur, start = m.group(1), i + 1
    if cur is not None:
        bounds[cur] = (start, len(lines))
    for name, (a, b) in bounds.items():
        while b > a and not lines[b - 1].strip():
            b -= 1
        bounds[name] = (a, b)
    return bounds


def patch_svxlink_conf(path, callsign, ptt, sql, dry_run=False):
    """Section-aware, idempotent patch of svxlink.conf.

    A key already present in the target section is replaced in place, whether
    commented out or not; a missing key is appended to that section. The other
    three hundred lines of Debian's file, and its comments, are untouched.
    """
    if not os.path.exists(path):
        return f"{path} does not exist - is svxlink-server installed?"

    with open(path) as f:
        original = f.readlines()
    lines = list(original)

    changes = [(s, k, v) for s, k, v in SVX_CHANGES]
    changes.insert(2, ("SimplexLogic", "CALLSIGN", callsign))

    for section, key, value in changes:
        if value is not None:
            value = value.replace("@PTT@", ptt).replace("@SQL@", sql)
        bounds = _section_bounds(lines)
        if section not in bounds:
            log(f"  [!] section [{section}] not found - skipped {key}")
            continue
        a, b = bounds[section]
        hit = None
        for i in range(a, b):
            m = re.match(r"^\s*#?\s*([A-Za-z_0-9]+)\s*=", lines[i])
            if m and m.group(1) == key:
                hit = i
                break
        if value is None:
            if hit is not None and not lines[hit].lstrip().startswith("#"):
                lines[hit] = "#" + lines[hit]
        else:
            new = f"{key}={value}\n"
            if hit is None:
                lines.insert(b, new)
            else:
                lines[hit] = new

    diff = list(difflib.unified_diff(original, lines,
                                     fromfile=path, tofile=path + " (patched)"))
    if not diff:
        log(f"[OK] {path} already has these settings")
        return None
    if dry_run:
        print("".join(diff))
        return None

    backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, backup)
    with open(path, "w") as f:
        f.writelines(lines)
    log(f"[OK] {path} patched, backup at {backup}")
    return None


def do_setup(args):
    cfg = load_conf()
    # Optional on purpose: an image built for someone else must not carry a
    # callsign. MYCALL is Debian's own placeholder, and --check flags it.
    callsign = args.callsign or "MYCALL"
    if not args.callsign:
        log("[!] No --callsign given. The node will identify as MYCALL until "
            "you set it in the web panel.")

    err = patch_svxlink_conf(SVXLINK_CONF, callsign,
                             cfg["PTT_PTY"], cfg["SQL_PTY"], args.dry_run)
    if err:
        sys.exit(err)

    if args.dry_run:
        log("--dry-run: /etc/asound.conf not written")
        return 0

    if os.path.exists(ASOUND_CONF) and "hri200" in open(ASOUND_CONF).read():
        log(f"[OK] {ASOUND_CONF} already defines hri200")
    else:
        if os.path.exists(ASOUND_CONF):
            shutil.copy2(ASOUND_CONF, ASOUND_CONF + ".bak")
        with open(ASOUND_CONF, "w") as f:
            f.write(ASOUND)
        log(f"[OK] {ASOUND_CONF} written")

    log("")
    log("Now: set the mixer levels, install the sound files, and check with")
    log("  hri200node.py --check")
    return 0


# ---------------------------------------------------------------------------
# --wait-network : hold off until name resolution actually works
# ---------------------------------------------------------------------------

def do_wait_network(host, timeout, floor):
    """Waits until a hostname resolves, then returns.

    systemd's network-online.target only means an interface has an address.
    On a Pi with NetworkManager, and especially over wifi, the resolver can
    stay unusable for several seconds after that - long enough for svxlink to
    try servers.echolink.org, fail, and sit there up but not linked.

    A fixed sleep would work but costs the same time on every boot whether it
    is needed or not. This returns as soon as DNS answers, and always exits
    zero after the timeout: a node that starts without EchoLink is better than
    one that never starts at all.
    """
    start = time.monotonic()
    if floor > 0:
        time.sleep(floor)          # an interface can have an address before it routes
    attempt = 0
    while time.monotonic() - start < timeout:
        try:
            socket.getaddrinfo(host, None)
            log(f"[OK] Network ready after {time.monotonic() - start:.1f} s "
                f"({host} resolves)")
            return 0
        except socket.gaierror:
            attempt += 1
            if attempt == 5:
                log(f"  waiting for DNS ({host}) ...")
            time.sleep(1.0)
    log(f"[!] {host} did not resolve within {timeout:.0f} s. Starting anyway - "
        "EchoLink may report a DNS failure and stay offline until restarted.")
    return 0


# ---------------------------------------------------------------------------
# --check : say what is wrong before it wastes an evening
# ---------------------------------------------------------------------------

def do_check():
    cfg = load_conf()
    bad = 0

    def report(ok, label, detail=""):
        nonlocal bad
        if not ok:
            bad += 1
        print(f"  {'OK  ' if ok else 'FAIL'}  {label}"
              + (f"\n          {detail}" if detail and not ok else ""))

    print("\nHardware")
    try:
        lsusb = subprocess.run(["lsusb"], capture_output=True, text=True).stdout
    except OSError:
        lsusb = ""
    report("26aa:0002" in lsusb, "HRI-200 control interface present",
           "Not on the USB bus. Powered? Cable seated? If lsusb shows "
           "045b:0025 the internal flash switch is in programming position.")
    report("26aa:0003" in lsusb, "HRI-200 audio interface present")
    report(os.path.exists(cfg["PORT"]), f"{cfg['PORT']} exists")
    report(os.access(cfg["PORT"], os.R_OK | os.W_OK),
           f"{cfg['PORT']} is readable and writable by uid {os.getuid()}",
           "Add this user to the dialout group.")

    # A file that exists, has the right permissions and is zero bytes is the
    # signature of a power cut shortly after writing: ext4 commits metadata
    # before contents. systemd reads an empty unit file as "masked", which is
    # a confusing way to find out an hour later.
    truncated = [p for p in (CONF, ASOUND_CONF, SVXLINK_CONF,
                             "/usr/local/bin/hri200node.py",
                             "/etc/systemd/system/hri200node.service")
                 if os.path.isfile(p) and os.path.getsize(p) == 0]
    report(not truncated, "no zero-length config files",
           "These exist but are empty: " + ", ".join(truncated) +
           ". Almost always a power cut shortly after installing - metadata "
           "reached the card, contents did not. Re-run install.sh, and shut "
           "down with 'sudo shutdown -h now' rather than pulling the plug.")

    print("\nAudio")
    try:
        aplay = subprocess.run(["aplay", "-L"], capture_output=True,
                               text=True).stdout
    except OSError:
        aplay = ""
    report("hri200" in aplay, "ALSA device 'hri200' is defined",
           f"Missing or malformed {ASOUND_CONF}. Run --setup.")

    print("\nSvxLink")
    report(os.path.exists(SVXLINK_CONF), f"{SVXLINK_CONF} exists")
    drop = "/etc/systemd/system/svxlink.service.d/wait-for-network.conf"
    report(os.path.exists(drop), "svxlink waits for the network before starting",
           "Without it svxlink can win a race against the resolver at boot and "
           "come up with EchoLink offline. See install.sh.")
    svx = read_keys(SVXLINK_CONF)
    report(svx.get("SQL_DET") == "PTY", "Rx1 SQL_DET=PTY", "Run --setup.")
    report(svx.get("PTT_TYPE") == "PTY", "Tx1 PTT_TYPE=PTY", "Run --setup.")
    sounds = "/usr/share/svxlink/sounds"
    lang = svx.get("DEFAULT_LANG", "en_US")
    report(os.path.isdir(f"{sounds}/{lang}"),
           f"sound files present at {sounds}/{lang}",
           "The node will transmit carrier with silence on it. The archive "
           "unpacks as en_US-heather-16k and needs a symlink to en_US.")

    for label, path in (("PTT", cfg["PTT_PTY"]), ("SQL", cfg["SQL_PTY"])):
        if os.path.islink(path):
            report(True, f"{label} pty {path} is a symlink")
        elif os.path.exists(path):
            report(False, f"{label} pty {path} is a symlink",
                   "It is a regular file - almost certainly left by a shell "
                   "redirection. Remove it and restart svxlink, or svxlink "
                   "will segfault when setting up its transmitter.")
        else:
            report(False, f"{label} pty {path} exists",
                   "svxlink creates it at startup. Is svxlink running?")

    node_cs = read_section_key(SVXLINK_CONF, "SimplexLogic", "CALLSIGN")
    report(node_cs not in (None, "", PLACEHOLDER["node_callsign"]),
           "node callsign set",
           "Still MYCALL. Set it in the web panel - transmitting without your "
           "own callsign is not legal anywhere.")

    print("\nEchoLink")
    el = read_keys(ECHOLINK_CONF, {"CALLSIGN", "PASSWORD"})
    report(el.get("CALLSIGN") not in (None, PLACEHOLDER["callsign"]),
           "EchoLink callsign set")
    report(el.get("PASSWORD") not in (None,) + UNSET_PASSWORDS,
           "EchoLink password set")

    print("\nChannel")
    try:
        _, label = build_d1m(cfg)
        report(True, label)
    except ValueError as e:
        report(False, "channel configuration valid", str(e))

    print(f"\n{'Everything checks out.' if not bad else f'{bad} problem(s).'}\n")
    return 1 if bad else 0


# ---------------------------------------------------------------------------

def main():
    global VERBOSE
    ap = argparse.ArgumentParser(
        description="HRI-200 SvxLink node with a web configuration panel")
    ap.add_argument("--setup", action="store_true",
                    help="patch svxlink.conf and /etc/asound.conf, then exit")
    ap.add_argument("--check", action="store_true",
                    help="report on everything that has to be right, then exit")
    ap.add_argument("--callsign", help="for --setup")
    ap.add_argument("--dry-run", action="store_true", help="for --setup")
    ap.add_argument("--no-web", action="store_true",
                    help="run the node without the web panel")
    ap.add_argument("--wait-network", action="store_true",
                    help="block until DNS answers, then exit 0. For "
                         "ExecStartPre, so services do not race the resolver")
    ap.add_argument("--wait-host", default="servers.echolink.org",
                    help="hostname to resolve for --wait-network")
    ap.add_argument("--wait-timeout", type=float, default=90.0,
                    help="give up waiting after this many seconds")
    ap.add_argument("--wait-floor", type=float, default=3.0,
                    help="always wait at least this long first")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    VERBOSE = a.verbose

    if a.wait_network:
        return do_wait_network(a.wait_host, a.wait_timeout, a.wait_floor)
    if a.setup:
        return do_setup(a)
    if a.check:
        return do_check()

    cfg = load_conf()

    if not a.no_web:
        try:
            app = make_app()
        except ImportError:
            sys.exit("Flask missing: sudo apt install -y python3-flask")
        host, port = cfg["WEB_HOST"], int(cfg["WEB_PORT"])
        if cfg["WEB_PASSWORD"] == DEFAULTS["WEB_PASSWORD"]:
            log("[!] The web panel is still on its default password. Anyone "
                "who can reach it can change what your transmitter does. "
                f"Set WEB_PASSWORD in {CONF}.")
        threading.Thread(
            target=lambda: app.run(host=host, port=port, threaded=True),
            daemon=True).start()
        log(f"Web panel on http://{host}:{port}/ as user {cfg['WEB_USER']}")

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: STATE.stop.set())

    node_loop(STATE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
