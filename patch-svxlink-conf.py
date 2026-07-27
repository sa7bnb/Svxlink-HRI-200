#!/usr/bin/env python3
"""
patch-svxlink-conf.py - apply the HRI-200 node settings to svxlink.conf.

Section aware, idempotent and non-destructive. It rewrites the keys it cares
about and leaves the other three hundred lines of the stock Debian file, and
its comments, exactly where they are.

    ./patch-svxlink-conf.py --dry-run          # show the diff, change nothing
    sudo ./patch-svxlink-conf.py               # back up, patch, show the diff

A key that already exists in the target section is replaced in place, whether
it was commented out or not. A key that does not exist is appended to the end
of its section. Running it twice produces no second diff.
"""

import argparse
import difflib
import os
import re
import shutil
import sys
import time

DEFAULT_PATH = "/etc/svxlink/svxlink.conf"

# (section, key, value) - value None means "comment this key out"
CHANGES = [
    # Capture on the HRI-200 is 1 channel, playback is 2. The "plug" device in
    # /etc/asound.conf duplicates mono to stereo on the way out, so svxlink can
    # treat the box as mono in both directions.
    ("GLOBAL", "CARD_SAMPLE_RATE", "48000"),
    ("GLOBAL", "CARD_CHANNELS", "1"),

    ("SimplexLogic", "CALLSIGN", "@CALLSIGN@"),
    # Belt and braces. hri200d.py already refuses to report an open squelch
    # while PTT is asserted; no reason not to have svxlink enforce it too.
    ("SimplexLogic", "MUTE_RX_ON_TX", "1"),
    ("SimplexLogic", "IDENT_ONLY_AFTER_TX", "4"),
    # No CTCSS on this node, so do not announce one.
    ("SimplexLogic", "REPORT_CTCSS", None),

    ("Rx1", "AUDIO_DEV", "alsa:hri200"),
    ("Rx1", "AUDIO_CHANNEL", "0"),
    # The radio's hardware squelch has already made the decision and the
    # HRI-200 hands it to us. VOX on top of that only adds delay.
    ("Rx1", "SQL_DET", "PTY"),
    ("Rx1", "PTY_PATH", "/dev/shm/hri200_sql"),
    ("Rx1", "SQL_START_DELAY", "0"),
    ("Rx1", "SQL_DELAY", "0"),
    # The squelch flutters between words. This is the layer that should absorb
    # it, not the daemon.
    ("Rx1", "SQL_HANGTIME", "1500"),
    ("Rx1", "SQL_TAIL_ELIM", "300"),
    ("Rx1", "DEEMPHASIS", "0"),
    # Not used any more, and pointing at a serial port that is not there.
    ("Rx1", "SERIAL_PORT", None),
    ("Rx1", "SERIAL_PIN", None),
    ("Rx1", "DTMF_SERIAL", None),

    ("Tx1", "AUDIO_DEV", "alsa:hri200"),
    ("Tx1", "AUDIO_CHANNEL", "0"),
    ("Tx1", "PTT_TYPE", "PTY"),
    ("Tx1", "PTT_PTY", "/dev/shm/hri200_ptt"),
    # Measured on the reference capture: TX came up 19 ms after the first
    # P100000 frame. 300 ms covers scheduling jitter comfortably.
    ("Tx1", "TX_DELAY", "300"),
    # Independent of the daemon's own --tx-timeout. Two timers is correct.
    ("Tx1", "TIMEOUT", "300"),
    ("Tx1", "PREEMPHASIS", "0"),
    ("Tx1", "PTT_PORT", None),
    ("Tx1", "PTT_PIN", None),
]

SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def key_of(line):
    """Returns the config key on a line, commented out or not, else None."""
    m = re.match(r"^\s*#?\s*([A-Za-z_0-9]+)\s*=", line)
    return m.group(1) if m else None


def section_bounds(lines):
    """Maps section name -> (first line index, one past last non-blank line)."""
    bounds, cur, start = {}, None, None
    for i, line in enumerate(lines):
        m = SECTION_RE.match(line)
        if m:
            if cur is not None:
                bounds[cur] = (start, i)
            cur, start = m.group(1), i + 1
    if cur is not None:
        bounds[cur] = (start, len(lines))
    # trim trailing blank lines off each section so appends land tidily
    for name, (a, b) in bounds.items():
        while b > a and not lines[b - 1].strip():
            b -= 1
        bounds[name] = (a, b)
    return bounds


def apply_changes(lines, changes):
    lines = list(lines)
    notes = []
    for section, key, value in changes:
        bounds = section_bounds(lines)
        if section not in bounds:
            notes.append(f"  [!] section [{section}] not found - skipped {key}")
            continue
        a, b = bounds[section]

        hit = None
        for i in range(a, b):
            if key_of(lines[i]) == key:
                hit = i
                break

        if value is None:
            if hit is None:
                continue
            if lines[hit].lstrip().startswith("#"):
                continue
            lines[hit] = "#" + lines[hit]
            notes.append(f"  [{section}] {key} commented out")
        else:
            new = f"{key}={value}\n"
            if hit is None:
                lines.insert(b, new)
                notes.append(f"  [{section}] {key}={value} (added)")
            elif lines[hit] != new:
                lines[hit] = new
                notes.append(f"  [{section}] {key}={value}")
    return lines, notes


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--path", default=DEFAULT_PATH)
    ap.add_argument("--callsign", default="SA7BNB")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-h", "--help", action="help")
    a = ap.parse_args()

    if not os.path.exists(a.path):
        sys.exit(f"{a.path} does not exist")

    with open(a.path) as f:
        original = f.readlines()

    changes = [(s, k, None if v is None else v.replace("@CALLSIGN@", a.callsign))
               for s, k, v in CHANGES]
    patched, notes = apply_changes(original, changes)

    diff = list(difflib.unified_diff(
        original, patched, fromfile=a.path, tofile=a.path + " (patched)"))

    if not diff:
        print("Nothing to do - the file already has these settings.")
        return 0

    print("".join(diff))
    print("Summary:")
    for n in notes:
        print(n)

    if a.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    backup = f"{a.path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(a.path, backup)
    with open(a.path, "w") as f:
        f.writelines(patched)
    print(f"\nWritten. Backup: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
