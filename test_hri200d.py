#!/usr/bin/env python3
"""End-to-end test: fake HRI-200 hardware + fake svxlink PTYs vs hri200d.py"""
import os, pty, select, subprocess, sys, termios, time, tty

SOH, EOT = 0x01, 0x04
FAILS = []

def frame(p): return bytes([SOH]) + p.encode() + bytes([EOT])

def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond: FAILS.append(msg)

# ---- fake svxlink pty (master side, cfmakeraw, symlink to slave) ----------
def svx_pty(link):
    m, s = pty.openpty()
    attr = termios.tcgetattr(m); tty.setraw(m); termios.tcsetattr(m, termios.TCSANOW, attr)
    import tty as _t
    _t.setraw(m)
    path = os.ttyname(s)
    os.close(s)
    if os.path.islink(link) or os.path.exists(link): os.unlink(link)
    os.symlink(path, link)
    os.set_blocking(m, False)
    return m

# ---- fake HRI-200 --------------------------------------------------------
hri_m, hri_s = pty.openpty()
tty.setraw(hri_m)
hri_port = os.ttyname(hri_s)
os.set_blocking(hri_m, False)

PTT_LINK, SQL_LINK = "/tmp/t_ptt", "/tmp/t_sql"
ptt_m = svx_pty(PTT_LINK)
sql_m = svx_pty(SQL_LINK)

proc = subprocess.Popen(
    [sys.executable, "/mnt/user-data/outputs/hri200d.py", "--freq", "145.2875",
     "--port", hri_port, "--ptt-pty", PTT_LINK, "--sql-pty", SQL_LINK,
     "--rx-blank", "0.3", "--poll-interval", "0.2", "-v"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

buf = bytearray()
seen = {"M00": False, "R6423": False, "D1V": False, "D1M": False}
polls = []          # (time, ptt_bool)
sql_chars = []
daemon_log = []

t0 = time.monotonic()
stage = 0
state = {"radio_ready_at": t0 + 2.0}   # box "detects" radio only after 2 s

def hri_read():
    """Pump the fake HRI-200: parse frames, answer them."""
    try: d = os.read(hri_m, 4096)
    except BlockingIOError: return
    except OSError: return
    buf.extend(d)
    while True:
        try:
            i = buf.index(SOH); j = buf.index(EOT, i+1)
        except ValueError: return
        p = buf[i+1:j].decode("latin1"); del buf[:j+1]
        now = time.monotonic()
        if p == "M00":
            seen["M00"] = True; os.write(hri_m, frame("M00"))
        elif p.startswith("R64"):
            seen["R6423"] = True
            payload = "00000,00000,12345678,20150413133824".encode().hex().upper()
            os.write(hri_m, frame("R" + "0" + payload))
        elif p == "D1V0000":
            if now >= state["radio_ready_at"]:
                seen["D1V"] = True
                os.write(hri_m, frame("D1V0020FTM-400DEXP  B3 Ver1.90020141217"))
        elif p.startswith("D1M"):
            seen["D1M"] = True; os.write(hri_m, frame(p))
        elif p.startswith("P"):
            ptt = p[1] == "1"
            polls.append((now, ptt))
            os.write(hri_m, frame("B%d 0    0000000" % (1 if state.get("sql") else 0)))

def push_status(val):
    os.write(hri_m, frame("D1P0004%04X" % val))

try:
    deadline = t0 + 14
    script = [
        (3.5,  "open_sql"),
        (5.0,  "close_sql"),
        (6.5,  "svx_key"),
        (8.0,  "svx_unkey"),
        (8.15, "tx_report_clears"),
        (9.0,  "svx_restart"),
        (10.5, "open_sql2"),
        (11.5, "svx_dies_keyed"),
        (13.0, "done"),
    ]
    si = 0
    global_ptt = None
    while time.monotonic() < deadline:
        watch = [hri_m, proc.stdout] + [f for f in (ptt_m, sql_m) if f >= 0]
        r, _, _ = select.select(watch, [], [], 0.05)
        if hri_m in r: hri_read()
        if sql_m >= 0 and sql_m in r:
            try: sql_chars.append((time.monotonic()-t0, os.read(sql_m, 64).decode()))
            except OSError: pass
        if proc.stdout in r:
            line = proc.stdout.readline()
            if line: daemon_log.append(line.rstrip())
        now = time.monotonic() - t0
        if si < len(script) and now >= script[si][0]:
            ev = script[si][1]; si += 1
            if ev == "open_sql":
                state["sql"] = True; push_status(0x10); print(f"[{now:5.1f}] radio: squelch OPEN")
            elif ev == "close_sql":
                state["sql"] = False; push_status(0x00); print(f"[{now:5.1f}] radio: squelch closed")
            elif ev == "svx_key":
                print(f"[{now:5.1f}] svxlink: writing 'T'"); os.write(ptt_m, b"T")
                state["key_t"] = now
            elif ev == "svx_unkey":
                print(f"[{now:5.1f}] svxlink: writing 'R'"); os.write(ptt_m, b"R")
                # box reports our own transmission as squelch, as it really does
                state["sql"] = True; push_status(0x10)
                state["unkey_t"] = now
            elif ev == "tx_report_clears":
                state["sql"] = False; push_status(0x00)
            elif ev == "svx_restart":
                print(f"[{now:5.1f}] svxlink: restarting (new ptys)")
                os.close(ptt_m); os.close(sql_m)
                ptt_m = svx_pty(PTT_LINK); sql_m = svx_pty(SQL_LINK)
            elif ev == "svx_dies_keyed":
                print(f"[{now:5.1f}] svxlink: keying then dying")
                os.write(ptt_m, b"T"); time.sleep(0.3)
                os.close(ptt_m); os.close(sql_m)
                ptt_m = sql_m = -1
            elif ev == "open_sql2":
                state["sql"] = True; push_status(0x10); print(f"[{now:5.1f}] radio: squelch OPEN")
            elif ev == "done":
                break
finally:
    proc.terminate()
    try: proc.wait(timeout=3)
    except subprocess.TimeoutExpired: proc.kill()
    try:
        for line in proc.stdout: daemon_log.append(line.rstrip())
    except Exception: pass

print("\n--- daemon log ---")
for l in daemon_log: print("   " + l)
print("\n--- sql pty traffic (t, chars) ---")
for t, c in sql_chars: print(f"   {t:5.2f}  {c!r}")

print("\n--- assertions ---")
check(seen["M00"], "M00 handshake sent")
check(seen["R6423"], "device info queried")
check(seen["D1V"], "radio detected (with retries past the 2 s delay)")
check(seen["D1M"], "frequency frame sent")
check(len(polls) > 20, f"poll loop running ({len(polls)} polls in ~12 s)")

seq = "".join(c for _, c in sql_chars)
check(seq.startswith("ZOZ"), f"initial closed, then open, then close: {seq!r}")
check(seq.count("O") == 2, f"exactly two COS openings reported: {seq!r}")

# PTT must have been asserted then released
ptt_on = [t for t, p in polls if p]
check(len(ptt_on) > 0, "PTT asserted after 'T'")
if ptt_on and "key_t" in state:
    lat = (ptt_on[0] - t0) - state["key_t"]
    check(lat < 0.05, f"PTT latency {lat*1000:.0f} ms (must be well under one poll interval)")
last_poll_ptt = polls[-1][1] if polls else True
check(not last_poll_ptt, "PTT released after 'R'")

# The self-triggered squelch right after unkeying must NOT reach svxlink
after_unkey = [c for t, c in sql_chars if "unkey_t" in state and state["unkey_t"] < t < state["unkey_t"] + 1.0]
check("O" not in "".join(after_unkey), f"no false COS from our own TX: {after_unkey}")
log_txt = "\n".join(daemon_log)
check(log_txt.count("[PTT] connected") >= 2, "reconnected to svxlink after its restart")
check("disconnected while keyed" in log_txt, "dropped PTT when svxlink died mid-transmission")
tail_ptt = [p for t, p in polls[-6:]]
check(not any(tail_ptt), "poll returned to PTT-off after svxlink died")

print(f"\n{'ALL TESTS PASSED' if not FAILS else str(len(FAILS)) + ' FAILURES'}")
sys.exit(1 if FAILS else 0)
