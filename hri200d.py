#!/usr/bin/env python3
"""
hri200d.py - SvxLink interface daemon for the Yaesu HRI-200.

Exposes the HRI-200's PTT and squelch to SvxLink through SvxLink's own
pseudo-terminal drivers. No patches to SvxLink are required.

    Tx1:  PTT_TYPE=PTY   PTT_PTY=/dev/shm/hri200_ptt   svxlink -> 'T' / 'R'
    Rx1:  SQL_DET=PTY    PTY_PATH=/dev/shm/hri200_sql  svxlink <- 'O' / 'Z'

Audio is deliberately NOT handled here. The HRI-200's USB audio codec is a
plain UAC 1.0 device, so SvxLink opens it directly through ALSA. This daemon
only ever touches /dev/ttyACM0.

    sudo apt install -y python3-serial
    ./hri200d.py --freq 145.2875

PREREQUISITES
    * Flash switch inside the box in NORMAL position
      (lsusb shows 26aa:0002 and 26aa:0003)
    * Radio in HRI-200 node mode. On an FTM-400D: power on while holding
      [D/X] + [GM] until the display shows HRI-200. [D/X] alone gives PDN
      mode, which will not work.
    * svxlink running, so that the PTY symlinks exist. If they do not, this
      daemon waits for them and connects when they appear.

PROTOCOL
    See PROTOCOL.md. Summary:
    Framing   SOH(0x01) <ASCII payload> EOT(0x04)
    M00       handshake, mandatory - the box answers nothing without it
    D1V0000   radio identification, needs several retries
    D1M....   frequency setting, not persistent - set on every startup
    P010000   poll, PTT OFF       P100000   poll, PTT ON
    B<n>...   poll reply, <n> is the squelch state (0 closed, 1 open)
    D1P0004vv status push. Value is the LAST TWO characters. 0x10 = RX,
              0x20 = TX.

SAFETY PROPERTY
    PTT is held by the poll, not latched by a one-shot command. If this
    daemon dies, stalls or is killed, the HRI-200 drops the transmitter on
    its own within about a second. A crash cannot leave the radio keyed.
"""

import argparse
import errno
import os
import select
import signal
import sys
import termios
import time
import tty

try:
    import serial
except ImportError:
    sys.exit("pySerial missing: sudo apt install -y python3-serial")

SOH, EOT = 0x01, 0x04
BAUD = 38400

# Captured verbatim from a WIRES-X session with an FTM-400D. The flag fields
# (010880230002 / 010887540002) are undecoded and probably encode CTCSS,
# power level, channel step and mode. They may differ on other radios.
# {F} is replaced with the frequency as exactly 9 characters: NNN.NNNNN
FREQ_TEMPLATE = ("D1M00434000{F}+000.00000010880230002"
                 "0{F}+000.00000010887540002")

# Amateur allocations in IARU Region 1 that an FTM-400D will transmit on
BANDS = [(144.0, 146.0, "2 m"), (430.0, 440.0, "70 cm")]

VERBOSE = False


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def dbg(msg):
    if VERBOSE:
        log(f"  . {msg}")


# ---------------------------------------------------------------------------
# HRI-200 control protocol
# ---------------------------------------------------------------------------

class HRI:
    """Minimal client for the HRI-200 control protocol."""

    def __init__(self, port):
        # DTR and RTS must be low BEFORE the port is opened. pySerial raises
        # both by default, and the MCU reads that as a reset: the radio
        # reboots and loses its frequency. Same mechanism as Arduino
        # auto-reset. Setting the attributes before open() applies them at
        # open time.
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = BAUD
        self.ser.timeout = 0
        self.ser.dtr = False
        self.ser.rts = False
        try:
            self.ser.open()
        except serial.SerialException as e:
            sys.exit(f"Cannot open {port}: {e}")
        self.buf = bytearray()
        self.sql = False        # squelch open, as reported by the box
        self.tx = False         # box reports the transmitter is up
        self.ptt = False        # what we are asking for

    def fileno(self):
        return self.ser.fileno()

    def send(self, s):
        self.ser.write(bytes([SOH]) + s.encode("ascii") + bytes([EOT]))

    def frames(self):
        """Returns any complete SOH..EOT frames received since last call."""
        d = self.ser.read(4096)
        if d:
            self.buf += d
        out = []
        while True:
            try:
                i = self.buf.index(SOH)
                j = self.buf.index(EOT, i + 1)
            except ValueError:
                if len(self.buf) > 4096:          # resync on garbage
                    del self.buf[:-256]
                return out
            out.append(self.buf[i + 1:j].decode("latin1"))
            del self.buf[:j + 1]

    def expect(self, prefix, timeout=2.0):
        """Waits for a frame starting with prefix. Returns None on timeout."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for f in self.frames():
                if f.startswith(prefix):
                    return f
            time.sleep(0.02)
        return None

    def poll(self):
        """One poll frame. PTT is asserted by which variant we send."""
        self.send("P100000" if self.ptt else "P010000")

    def set_ptt(self, on):
        """Returns True if the state actually changed."""
        if on == self.ptt:
            return False
        self.ptt = on
        # Send immediately rather than waiting for the next scheduled poll -
        # otherwise PTT latency equals the poll interval
        self.poll()
        return True

    def update(self):
        """Pumps frames. Returns True if the reported squelch state changed."""
        changed = False
        for f in self.frames():
            new = None
            if f.startswith("B") and len(f) > 1:
                new = f[1] == "1"
            elif f.startswith("D1P0004") and len(f) >= 11:
                # The status byte is the LAST two characters. The four
                # characters after D1P are a length field.
                try:
                    v = int(f[-2:], 16)
                except ValueError:
                    continue
                new = bool(v & 0x10)
                self.tx = bool(v & 0x20)
            if new is not None and new != self.sql:
                self.sql = new
                changed = True
                dbg(f"box squelch -> {'OPEN' if new else 'closed'}")
        return changed

    def flush_input(self):
        """Discards everything currently in the serial input buffer."""
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        self.buf.clear()

    def close(self):
        try:
            self.ptt = False
            self.poll()
            time.sleep(0.1)
            self.send("P010010")      # what WIRES-X sends when it exits
            time.sleep(0.05)
            self.ser.close()
        except Exception:
            pass


def build_freq(mhz, force=False):
    """Builds a D1M frame by substituting the frequency into the template."""
    f = f"{mhz:09.5f}"
    if len(f) != 9:
        sys.exit(f"{mhz} does not fit the NNN.NNNNN field ({f!r}). "
                 "Use three integer digits, e.g. 145.28750")

    band = next((n for lo, hi, n in BANDS if lo <= mhz <= hi), None)
    if band:
        log(f"Frequency: {mhz:.5f} MHz ({band})")
    elif force:
        log(f"[!] {mhz:.5f} MHz is outside 144-146 / 430-440 MHz. "
            "Requires a MARS-modified radio.")
    else:
        sys.exit(f"{mhz} MHz is outside the amateur bands in IARU Region 1. "
                 "Add --force if your radio is MARS-modified.")

    cmd = FREQ_TEMPLATE.replace("{F}", f)
    body = cmd[3:]
    if int(body[:4], 16) != len(body) - 4:
        sys.exit("Length field does not match the template payload.")
    return cmd


def detect_radio(h, attempts=8, gap=1.2):
    """Queries D1V0000 repeatedly until the radio answers.

    The box needs several seconds to detect the attached radio after startup.
    In the reference capture WIRES-X got no reply at t=1.0 s or t=2.1 s and
    only succeeded at t=4.1 s on the third attempt. The poll is kept running
    throughout.
    """
    for i in range(1, attempts + 1):
        h.send("D1V0000")
        end = time.monotonic() + gap
        while time.monotonic() < end:
            for f in h.frames():
                if f.startswith("D1V") and len(f) > 7:
                    return f[7:].strip()
            h.poll()
            time.sleep(0.15)
        if i == 2:
            log("  the box needs a few seconds ...")
    return None


def handshake(h, freq_cmd):
    """M00 handshake, device info, radio detection, frequency setting."""
    # M00 is mandatory. Until it has been acknowledged the box ignores
    # everything - a blind scan of all 256 command bytes returns nothing.
    h.send("M00")
    if h.expect("M00") is None:
        log("[FAIL] No response to M00.")
        log("       Flash switch in normal position? Cable connected?")
        return False
    log("[OK] M00 acknowledged")

    h.send("R6423")
    r = h.expect("R")
    if r:
        # Hex-encoded ASCII at an odd offset: skip one character, then decode
        # pairwise. Yields "00000,00000,<serial>,<build timestamp>"
        try:
            p = bytes.fromhex(r[2:]).decode("ascii", "replace").split(",")
            d = p[3]
            log(f"[OK] Serial {p[2]}, firmware built "
                f"{d[0:4]}-{d[4:6]}-{d[6:8]} {d[8:10]}:{d[10:12]}:{d[12:14]}")
        except Exception:
            pass

    radio = detect_radio(h)
    if not radio:
        log("[FAIL] Radio does not respond after several attempts.")
        log("       Does the display show HRI-200? Power the radio fully off")
        log("       and on again holding [D/X] + [GM], wait 5 s, then retry.")
        return False
    log(f"[OK] Radio: {radio}")

    # The frequency is not stored in the radio. In node mode the radio is a
    # slave and the host owns the frequency, so it must be set every time.
    h.send(freq_cmd)
    if h.expect("D1M", 2.0):
        log("[OK] Frequency set")
    else:
        log("[!] No acknowledgement for the frequency setting")
    return True


# ---------------------------------------------------------------------------
# SvxLink PTY plumbing
# ---------------------------------------------------------------------------

class Pty:
    """One of SvxLink's PTYs, accessed through the symlink it creates.

    SvxLink allocates the master, calls cfmakeraw() on it and symlinks the
    slave to the configured path. It recreates the symlink on every restart,
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
        self.last_err = None      # so a repeating failure is logged once

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
            self._complain("waiting", f"{self.path} does not exist yet - "
                                      "is svxlink running?")
            return False
        except OSError as e:
            if e.errno == errno.EINVAL:
                # Not a symlink. Almost always a stray regular file left by a
                # shell redirection such as `echo T > /dev/shm/hri200_ptt`.
                # svxlink cannot symlink over it, so its own Tx setup failed.
                self._complain("blocked",
                               f"{self.path} is not a symlink. Remove it "
                               "and restart svxlink - a plain file here "
                               "stops svxlink creating its pty.")
            else:
                self._complain("readlink", f"{self.path}: {e.strerror}")
            return False

        try:
            fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except PermissionError:
            # /dev/shm is 1777, so with fs.protected_symlinks=1 the kernel
            # refuses to follow a symlink there unless the follower owns it
            # or owns the directory. Root is NOT exempt. Running this daemon
            # under sudo therefore fails where running it as the svxlink user
            # succeeds - the opposite of what one expects.
            self._complain(
                "denied",
                f"permission denied opening {self.path} (-> {target}). "
                f"Running as uid {os.getuid()}; the symlink is owned by uid "
                f"{os.lstat(self.path).st_uid}. With fs.protected_symlinks=1 "
                "even root cannot follow it. Run as the same user as "
                "svxlink, e.g. sudo -u svxlink, or use the systemd unit.")
            return False
        except OSError as e:
            self._complain("open", f"{self.path} -> {target}: {e.strerror} "
                                   "(svxlink may have exited; a stale symlink "
                                   "is left behind when it crashes)")
            return False
        self.last_err = None

        # svxlink already put the pair in raw mode; doing it again is
        # harmless and protects against a canonical-mode slave, where a read
        # would block forever waiting for a newline that never comes.
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
        if not data:                     # EOF - master closed
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

class Stopper:
    def __init__(self):
        self.stop = False
        signal.signal(signal.SIGINT, self._hit)
        signal.signal(signal.SIGTERM, self._hit)

    def _hit(self, *_):
        self.stop = True


def main():
    global VERBOSE

    ap = argparse.ArgumentParser(
        description="SvxLink interface daemon for the Yaesu HRI-200",
        add_help=False)
    ap.add_argument("--freq", type=float, required=True,
                    help="operating frequency in MHz, e.g. 145.2875")
    ap.add_argument("--port", default="/dev/ttyACM0",
                    help="HRI-200 serial port (default /dev/ttyACM0)")
    ap.add_argument("--ptt-pty", default="/dev/shm/hri200_ptt",
                    help="must match Tx1/PTT_PTY in svxlink.conf")
    ap.add_argument("--sql-pty", default="/dev/shm/hri200_sql",
                    help="must match Rx1/PTY_PATH in svxlink.conf")
    ap.add_argument("--poll-interval", type=float, default=0.2,
                    help="seconds between polls (default 0.2 = 5 Hz)")
    ap.add_argument("--rx-blank", type=float, default=0.4,
                    help="seconds to keep the squelch reported closed after "
                         "unkeying (default 0.4)")
    ap.add_argument("--tx-timeout", type=float, default=300.0,
                    help="force PTT off after this many seconds (0=off)")
    ap.add_argument("--force", action="store_true",
                    help="allow frequencies outside the amateur bands")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-h", "--help", action="help")
    a = ap.parse_args()
    VERBOSE = a.verbose

    freq_cmd = build_freq(a.freq, a.force)
    stopper = Stopper()

    h = HRI(a.port)
    log(f"Connecting to HRI-200 on {a.port}")
    if not handshake(h, freq_cmd):
        h.close()
        return 1

    sql_pty = Pty(a.sql_pty, "SQL")
    ptt_pty = Pty(a.ptt_pty, "PTT")
    log(f"Waiting for svxlink: {a.ptt_pty} and {a.sql_pty}")

    sent_sql = None          # last O/Z we pushed, None forces a resend
    blank_until = 0.0        # report squelch closed until this time
    flush_at = None          # one-shot serial flush after unkeying
    next_poll = 0.0
    tx_started = 0.0

    try:
        while not stopper.stop:
            now = time.monotonic()

            if sql_pty.ensure(now):
                sent_sql = None          # resend state to a fresh svxlink
            ptt_pty.ensure(now)

            # svxlink went away while we were transmitting
            if ptt_pty.fd is None and h.ptt:
                log("[!] svxlink disconnected while keyed - dropping PTT")
                h.set_ptt(False)
                blank_until = now + a.rx_blank
                flush_at = now + 0.3

            # ---- wait for something to happen -----------------------------
            fds = [h.fileno()]
            if ptt_pty.fd is not None:
                fds.append(ptt_pty.fd)
            deadlines = [next_poll - now]
            if flush_at:
                deadlines.append(flush_at - now)
            if blank_until > now:
                deadlines.append(blank_until - now)
            timeout = min([d for d in deadlines] + [0.25])
            timeout = max(timeout, 0.005)
            try:
                select.select(fds, [], [], timeout)
            except (OSError, ValueError):
                pass
            now = time.monotonic()

            # ---- serial: squelch and status -------------------------------
            h.update()

            # ---- svxlink asking for PTT -----------------------------------
            data = ptt_pty.read()
            if data:
                want = h.ptt
                for c in data:
                    if c == ord("T"):
                        want = True
                    elif c == ord("R"):
                        want = False
                if h.set_ptt(want):
                    next_poll = now + a.poll_interval
                    if want:
                        tx_started = now
                        log("TX on")
                    else:
                        log("TX off")
                        # The box reports our own transmission back as a
                        # squelch event. Blank the squelch and drop whatever
                        # is already in the serial buffer.
                        blank_until = now + a.rx_blank
                        flush_at = now + 0.3
                        h.sql = False

            # ---- transmit timeout -----------------------------------------
            if h.ptt and a.tx_timeout and now - tx_started > a.tx_timeout:
                log(f"[!] TX timeout after {a.tx_timeout:.0f} s - dropping PTT")
                h.set_ptt(False)
                blank_until = now + a.rx_blank
                flush_at = now + 0.3
                h.sql = False

            # ---- deferred serial flush ------------------------------------
            if flush_at and now >= flush_at:
                h.flush_input()
                h.sql = False
                flush_at = None

            # ---- keep the poll running ------------------------------------
            if now >= next_poll:
                h.poll()
                next_poll = now + a.poll_interval

            # ---- report squelch to svxlink --------------------------------
            # Never report an open squelch while transmitting, or while the
            # blanking window after unkeying is still running.
            open_now = h.sql and not h.ptt and now >= blank_until
            if open_now != sent_sql:
                if sql_pty.write(b"O" if open_now else b"Z"):
                    sent_sql = open_now
                    log(f"COS {'OPEN' if open_now else 'closed'}")

    finally:
        log("Shutting down")
        h.set_ptt(False)
        sql_pty.write(b"Z")
        sql_pty.close()
        ptt_pty.close()
        h.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
