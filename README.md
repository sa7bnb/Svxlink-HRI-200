# hri200-svxlink

Run a Yaesu HRI-200 as an ordinary [SvxLink](https://github.com/sm0svx/svxlink)
node — EchoLink, SvxReflector, parrot, DTMF control — on Linux, with no
Windows, no WIRES-X, and no contact with Yaesu's servers.

Nothing is modified: no firmware, no kernel modules, no patches to SvxLink. The
box's serial protocol is documented in [PROTOCOL.md](PROTOCOL.md), and a single
userspace daemon translates between it and SvxLink's existing pseudo-terminal
drivers.

> ### Status: working, but early
>
> This is under active development and should be treated as such. It runs — the
> reference node survives reboots unattended and has been checked end to end
> through EchoLink's `*ECHOTEST*` loopback — but it has been exercised on
> exactly **one** combination of hardware: a Raspberry Pi 4 running Raspberry Pi
> OS Lite 64-bit (Debian 13), SvxLink 24.02, an HRI-200, and an FTM-400D.
>
> Parts of the protocol are still undecoded, several fields in the frequency
> frame are guesses copied verbatim from a capture, and no other radio model has
> been tried yet. Expect rough edges, and expect things to change.
>
> If you put this on the air, watch it rather than trusting it, and keep an eye
> on the log for the first while. Bug reports and results from other hardware
> are the most useful thing you can contribute.

---

## Why this exists

The HRI-200 is Yaesu's WIRES-X interface. It only ever spoke to one piece of
closed Windows software, and the protocol between them was undocumented — the
question was raised in the SvxLink project in 2015 and left open. When the
WIRES-X PC is retired, the box becomes a paperweight.

It turns out the hardware is far more open than the software around it. The
audio side is plain USB Audio Class 1.0, so ALSA drives it with no help at all.
Only the control channel — PTT, squelch, frequency — needed reverse
engineering, and that is a small ASCII protocol over a CDC serial port.

As far as I have been able to find, this is the first time an HRI-200 has been
made to work outside WIRES-X. If someone got there earlier, I would like to
hear about it.

---

## How it fits together

```
FTM-400D ──CT-174──▶ HRI-200 ──USB──▶ Raspberry Pi
                                       │
                     /dev/ttyACM0 ─────┤────▶ hri200d.py
                     (control)         │        │
                                       │        ├─ hri200_ptt ◀── 'T'/'R' ── svxlink
                                       │        └─ hri200_sql ──▶ 'O'/'Z' ──▶ svxlink
                                       │
                     ALSA "codec" ─────┴───────── audio ──────────────────── svxlink
```

The split is deliberate:

- **Audio never passes through the daemon.** SvxLink opens the codec directly
  through ALSA. Nothing is resampled twice, nothing is copied through Python,
  and no latency is added to the audio path.
- **The daemon owns only `/dev/ttyACM0`.** It performs the handshake, runs the
  poll loop, sets the frequency at startup, and converts PTT and COS.

A useful safety property falls out of the protocol: **PTT is asserted by what
you poll with, not by a latching command.** If the daemon crashes, stalls or is
killed, the HRI-200 drops the transmitter on its own within about a second. A
software fault cannot leave the radio keyed.

---

## What you need

| | |
|---|---|
| Interface | HRI-200, internal flash switch in **normal** position |
| Radio | One the box supports, with its CT-174 cable. FTM-400D verified |
| Host | Anything running Linux with a spare USB port. Pi 4 verified |
| SvxLink | 14.08 or later — that is when both PTY drivers landed |
| EchoLink | An account with the `-L` suffix, validated. Optional |

---

## Quick start

```bash
sudo apt install -y svxlink-server python3-serial alsa-utils sox

sudo install -m755 hri200d.py /usr/local/bin/hri200d.py
sudo install -m644 hri200d.service /etc/systemd/system/hri200d.service
sudo python3 patch-svxlink-conf.py --callsign YOURCALL

echo 'FREQ=434.5000
PORT=/dev/ttyACM0
OPTS=-v' | sudo tee /etc/default/hri200d

sudo systemctl daemon-reload
sudo systemctl enable --now svxlink hri200d
```

That skips the sound files, the ALSA device and several traps that will
otherwise cost you an evening. **Read [INSTALL.md](INSTALL.md)** — it is a
clean-machine walkthrough with each trap named where it bites.

Put the radio into node mode first: power the FTM-400D on **while holding
`[D/X]` + `[GM]`** until the display reads `HRI-200`. `[D/X]` alone gives PDN
mode, which looks similar and does not work.

---

## Repository contents

| File | |
|---|---|
| [`INSTALL.md`](INSTALL.md) | Full walkthrough, bring-up order and troubleshooting |
| [`PROTOCOL.md`](PROTOCOL.md) | The serial protocol, frame by frame |
| `hri200d.py` | The daemon |
| `hri200d.service` | systemd unit |
| `patch-svxlink-conf.py` | Section-aware, idempotent patcher for `svxlink.conf` |
| `test_hri200d.py` | End-to-end test against a simulated box and simulated PTYs |
| `hri200-parrot.py` | Standalone repeater-in-a-box; useful for isolating faults |

`hri200-parrot.py` needs no SvxLink at all. Run it first — if the box and radio
do not work there, nothing above them will.

---

## The protocol, in brief

Full detail in [PROTOCOL.md](PROTOCOL.md). The essentials:

```
Framing     SOH(0x01) <ASCII payload> EOT(0x04) at 38400 baud
M00         handshake — mandatory, the box answers nothing until it is done
D1V0000     radio identification, needs several retries over ~4 s
D1M....     frequency, not persistent — the host sets it at every startup
P010000     poll, PTT off        P100000     poll, PTT on
B<n>...     poll reply, <n> is the squelch state
D1P0004vv   unsolicited status push, 0x10 = RX, 0x20 = TX
```

Two things that are easy to get wrong. **DTR and RTS must be low before the
port is opened** — raising them resets the MCU and the radio reboots, the same
mechanism as Arduino auto-reset. And **the box reports your own transmission
back as a squelch event**, so the squelch must be forced closed while PTT is
asserted and for a moment afterwards, or the node keys itself in a loop.

---

## Limitations

This replaces WIRES-X rather than sitting alongside it.

- **Analogue FM only.** C4FM audio is AMBE, not PCM. Neither SvxLink nor this
  daemon can do anything with it.
- **No WIRES-X.** No rooms, no nodes, no `X` button. The radio becomes a node
  radio and you talk to it with a second set.
- **One frequency, simplex.** The host owns the frequency in node mode.
- **The WIRES-X software cannot run at the same time** — it takes the serial
  port, or you take it from the software.
- **The radio's mode does not survive a power cut.** Everything else starts
  itself; `[D/X]` + `[GM]` is manual. A UPS on the radio helps more than
  anything in software.

Going back to WIRES-X is just stopping the two services. Nothing here is
persistent and no firmware is touched.

---

## Things that cost me time

Recorded here because none of them are obvious and all of them are silent.

**`fs.protected_symlinks` makes `sudo` worse, not better.** `/dev/shm` is mode
`1777`, and the kernel will not follow a symlink there unless the follower owns
the symlink or the directory. Root is not exempt — that is the point of the
protection. So `sudo ./hri200d.py` fails with `EACCES` where `sudo -u svxlink
./hri200d.py` succeeds. Use the systemd unit and the question does not arise.

**Never `>` a PTY path.** `echo T > /dev/shm/hri200_ptt` creates a regular file
if the symlink is absent. SvxLink then cannot `symlink()` over it,
`PttPty::initialize()` fails, and its destructor dereferences a null pointer —
`SIGSEGV` and a restart loop with no useful error. `SquelchPty` has the null
check that `PttPty` lacks, so the receiver survives and only the transmitter
dies. Patch pending upstream.

**SvxLink logs to `/var/log/svxlink`, not the journal.** `journalctl -u
svxlink` shows only systemd's bookkeeping and looks empty when everything is
fine.

**The sound files are a separate download**, and the archive's directory
(`en_US-heather-16k`) does not match `DEFAULT_LANG=en_US`, so it needs a
symlink. Take the release matching your SvxLink version, not the newest. Get
this wrong and the node transmits carrier with silence on it.

**`Distortion detected` is not about your level.** It fires at every squelch
opening. A recording shows the radio's AF stage unmuting: full scale in eight
samples, then a smooth exponential recovery with a 61 ms time constant — a
2.6 Hz high-pass corner, i.e. a DC step through the audio path's AC coupling.
Speech in the same recording peaks nowhere near clipping. Lowering the level
only makes the node deaf.

---

## Roadmap

- Decode the remaining fields in the frequency frame — modulation mode, power
  level and channel step appear to be in there (Fixed 20260728)
- Test with other radios. An FT-7800R is next

---

## Credits

[SvxLink](https://github.com/sm0svx/svxlink) by Tobias Blomberg, SM0SVX. The
PTY drivers this integration relies on are his, and they are the reason no
patching was needed.

Contributions welcome, particularly reports from other radio models.

---

*SA7BNB*
