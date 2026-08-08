# Yaesu HRI-200 — Serial Protocol Documentation

Reverse-engineered documentation of the control protocol used between the
WIRES-X PC software and the Yaesu HRI-200 interface, sufficient to drive
PTT, squelch detection, frequency setting and audio from Linux —
independent of any particular application.

The protocol was previously noted as unknown in
[sm0svx/svxlink issue #111](https://github.com/sm0svx/svxlink/issues/111),
open since 2015.

**Status:** working. PTT, COS, audio and the full `D1M` channel
configuration — frequency, FM/digital mode, narrow, CTCSS, DCS and power —
verified end-to-end on a Raspberry Pi 4, and running continuously as a
SvxLink node since.

Two radios have been driven: an **FTM-400DEXP**, which identifies itself and
takes its channel configuration from the host, and an **FT-7800R**, which does
not identify itself at all and is tuned by hand. Both work; see section 4.

---

## 1. Test environment

Everything below was observed on this configuration:

| Component | Details |
|---|---|
| Interface | Yaesu HRI-200, firmware build timestamp **2015-04-13 13:38:24** |
| Radio | Yaesu **FTM-400DEXP**, reported as `FTM-400DEXP  B3 Ver1.90020141217` |
| Second radio | Yaesu **FT-7800R** — analogue, does not identify itself |
| Radio mode | **Analogue FM** (no C4FM tested) |
| Host | Raspberry Pi 4 |
| OS | **Raspberry Pi OS Lite 64-bit**, Debian 13 (Trixie), kernel 6.12, arm64 |
| Reference capture | WIRES-X on Windows, captured with USBPcap |
| Cable | CT-174 (10-pin mini-DIN) to RADIO 1 |
| RF | Dummy load throughout |

### Host software requirements

Starting from a clean Raspberry Pi OS Lite 64-bit image, the complete set of
packages installed was:

```bash
sudo apt install -y usbutils        # lsusb, for the initial USB survey
sudo apt install -y alsa-utils      # aplay, arecord, amixer, alsactl
sudo apt install -y python3-serial  # pySerial — the only runtime dependency
sudo apt install -y sox             # optional: level measurement only
```

`python3-serial` and `alsa-utils` are the only ones actually required at
runtime. `usbutils` was used for investigation and `sox` only for measuring
audio levels during setup.

Nothing else was needed. No kernel modules were built, no `snd-usb-audio`
quirks-table entry was required, no udev rules are needed for the CDC device
beyond group membership, and no vendor drivers exist or are necessary. Every
interface on the HRI-200 binds to an in-tree driver at boot.

The same code was also run unmodified on Ubuntu 24.04 (x86-64) and on
Windows 11 with pySerial, so nothing here is Pi- or ARM-specific. The Pi was
simply the target platform for the node.

Add your user to `dialout` to avoid running as root:

```bash
sudo usermod -aG dialout $USER    # log out and back in
```

### Audio configuration applied

The card enumerates as ALSA card name `codec`. Use the **name**, not the
index — the index moves depending on boot order and what else is attached.

Two changes were made from the defaults:

```bash
# 1. Bass Boost off — it colours transmit audio and has no place in an FM node
amixer -c codec sset 'Bass Boost' off

# 2. Speaker (TX level) raised from the default 27 to near maximum.
#    At the default -20 dB the transmitted audio was barely audible.
amixer -c codec sset Speaker 47

sudo alsactl store    # persist across reboots
```

`PCM` (which is the **capture** control despite the name) started at its
default of 31/55, giving 0.39 peak amplitude on open-squelch noise. That was
enough for audio to pass but **not enough for reliable DTMF decoding**: at 31
the decoder dropped digits, and at 45 (+14 dB) a ten-digit sequence came
through without error. Speech then measured RMS -21.8 dBFS with peaks at
-5.7 dBFS, which is a sensible working point.

Measure rather than guess, and **speak into the handheld during the
recording** — measuring the noise floor says nothing about where speech lands:

```bash
arecord -D plughw:CARD=codec,DEV=0 -f S16_LE -r 48000 -c 1 -d 5 /tmp/rx.wav
sox /tmp/rx.wav -n stats
```

Use `stats`, not `stat`. The older `stat` reports a `Midline amplitude` that
looks like a DC offset and is not one; `stats` gives the real `DC offset`,
which measured -0.000017, i.e. none.

`Mic` is a playback-side sidetone control, muted by default, and was left
alone.

Resulting mixer state:

| Control | Direction | Value | Notes |
|---|---|---|---|
| `PCM` | capture | 45/55 (+14 dB) | raised from 31 — 31 was too low for DTMF |
| `Speaker` | playback | 47/47 (0 dB) | raised from 27 (-20 dB) |
| `Mic` | playback | muted | default, unchanged |
| `Bass Boost` | playback | off | changed from on |

Note that `Speaker` ended up at maximum for this radio. Levels were set by
increasing until speech sounded correct, so there may be no headroom left —
verify with a clean 1 kHz tone, where overdeviation is far easier to hear than
on speech, and back off if it sounds rough.

**Levels are radio-specific, and by more than one might expect.** Measured on
the same box and the same cable:

| | `Speaker` (TX) | `PCM` (RX) |
|---|---|---|
| FTM-400DEXP | 47/47 | 45/55 |
| FT-7800R | 26/47 | 40/55 |

That is roughly 21 dB less drive into the FT-7800R for comparable deviation.
Do not carry a working setting from one radio to another.

### The squelch-open transient

Every time the radio unmutes its AF stage the capture stream takes a hard
step. It is worth documenting because software that watches for clipping —
SvxLink's distortion detector, for one — will flag it, and the obvious
response of turning the level down is exactly wrong.

Measured on a ten-second recording containing one squelch opening:

```
signal on the noise floor      about +/-100
falls to full scale            in 8 samples, 170 us
recovers                       smooth monotonic exponential
time constant                  61 ms
implied high-pass corner       2.6 Hz
speech in the same recording   peaks 3000-10000, RMS -30 dBFS
```

A clean exponential decay of that shape is a DC step through an AC-coupled
path, not a digital glitch: a dropped USB frame gives isolated samples with no
structure, and a genuinely overdriven input clips on the waveform peaks rather
than once at the transition.

Two consequences. The transient **saturates regardless of gain** — raising
`PCM` by 14 dB left the count of full-scale samples at exactly 2 in 240 000,
because something already at the rail cannot go further. And it is harmless:
it lands before the useful audio, and DTMF, squelch and relayed speech were
all unaffected.

If it must be suppressed, delay acting on the squelch-open indication by about
100 ms so the downstream audio gate opens after the thump has decayed. The
cost is a clipped first syllable, which is usually the worse trade.

The radio must be in **HRI-200 node mode**: power on while holding
`[D/X]` + `[GM]`. The display then shows `HRI-200`. Holding `[D/X]` alone
gives PDN (Portable Digital Node) mode, which is a different function and
will not work.

### On firmware versions

The device reports a build timestamp, not a version string. The unit tested
reports `20150413133824`. Yaesu's only published firmware update package is
labelled `1.01` and its installer was built 2015-07-13, so the tested unit
may be running factory firmware rather than 1.01. There is no way to tell
from the wire protocol. No behavioural differences are known.

### What is radio-independent

`M00`, `R6423`, the `P`/`B` poll pair and the `D1P` status pushes are handled
by the HRI-200's own MCU and should behave identically regardless of which
radio is attached.

`D1M` is decoded field by field in section 7 — but against an FTM-400D, and
it only applies to radios that identify themselves at all. An **FT-7800R**
never answers `D1V0000`, so it is never sent `D1M`; it works as a node radio
with its frequency set by hand. See section 4.

So the layout remains verified against exactly one model. Treat it as
provisional for anything else (FTM-100D, DR-1X, …).

Audio levels **are** radio-specific regardless of controllability — see
section 1.

Reports from other radio models are welcome — see section 9.

---

## 2. USB topology

The HRI-200 presents as a hub with two downstream devices. **No vendor
drivers are needed on Linux**; all interfaces are standard classes and bind
automatically.

```
0451:2046   TI TUSB2046 hub
├── 26aa:0002  "HRI-200 Communication device A"
│   └── CDC ACM  →  cdc_acm  →  /dev/ttyACM0        ← control protocol
└── 26aa:0003  "HRI-200 A(CH1) USB Audio codec"
    ├── If 0-2: USB Audio Class 1.0 → snd-usb-audio ← audio
    └── If 3:   HID → usbhid → /dev/hidraw*
```

The `A(CH1)` naming suggests a second channel exists when RADIO 2 is used.
Not tested.

### Audio interface

Standard UAC 1.0. Works with no configuration.

| Direction | Endpoint | Format |
|---|---|---|
| Capture (RX from radio) | `0x81` isochronous | 1 ch mono, 16-bit, 8–48 kHz |
| Playback (TX to radio) | `0x02` isochronous | 2 ch stereo, 16-bit, 8–48 kHz |

ALSA mixer controls, with names that do not match their function:

| Control | Actual direction | Notes |
|---|---|---|
| `PCM` | **capture** (`cvolume`) | RX level from radio. Range 0–55 |
| `Speaker` | playback | TX level to radio. Range 0–47 |
| `Mic` | playback | Sidetone/monitor. Muted by default; leave it |
| `Bass Boost` | playback | Turn **off** — it colours TX audio |

### HID interface — a dead end

The HID interface is a generic consumer-control descriptor (31 bytes):
volume up, volume down, mute. **Input reports only** — there is no output
or feature report, and no interrupt OUT endpoint, so nothing can be sent
to the device this way.

It is not a CM108-style GPIO interface. PTT and COS are not available here.

---

## 3. Framing

```
0x01  <ASCII payload>  0x04
SOH                    EOT
```

No checksum, no length prefix on the frame itself, no escaping. Payload is
always printable ASCII. Port settings: 38400 baud (the device is CDC ACM,
so the rate is nominal).

### Critical: DTR/RTS must be low

pySerial asserts DTR and RTS on open by default. The HRI-200's MCU treats
this as a reset — the radio restarts and loses its frequency. Open the port
with both lines deasserted:

```python
ser = serial.Serial()
ser.port = "/dev/ttyACM0"
ser.baudrate = 38400
ser.timeout = 0
ser.dtr = False
ser.rts = False
ser.open()
```

This was the cause of a long-standing "radio reboots randomly" symptom
during development.

---

## 4. Startup sequence

| Step | Host → HRI-200 | HRI-200 → Host | Meaning |
|---|---|---|---|
| 1 | `M00` | `M00` | Handshake. **Mandatory** |
| 2 | `R6423` | `R<hex>` | Device information |
| 3 | `P010000` | `B0 0    0000000` | First poll |
| 4 | `D1V0000` | `D1V0020<radio id>` | Radio identification |
| 5 | `D1M….` | `D1M….` | Frequency setting |
| 6 | `D1B00010` | `D1B00010` | Configuration, echoed |
| 7 | `D1C0000` | `D1C000500000` | Radio status poll |

### `M00` is mandatory

**The device answers nothing until `M00` has been acknowledged.** A blind
scan of all 256 single command bytes against an un-handshaken device
produces zero responses. This is why the protocol resisted guessing.

### `R6423` — device information

The payload after `R` is hex-encoded ASCII at an **odd offset**: skip the
first character, then decode pairwise.

```
00000,00000,XXXXXXXX,20150413133824
                     ^^^^^^^^^^^^^^ build timestamp YYYYMMDDHHMMSS
            ^^^^^^^^ device serial number
```

This is a query, not authentication — no prior knowledge of the serial
number is required.

### `D1V0000` — radio detection needs retries, and may never succeed

A controllable radio needs **several seconds** to be detected after startup.
In the reference capture, WIRES-X queried at t=1.0 s and t=2.1 s with no reply,
and only received a response at t=4.1 s on the third attempt. Reproduced on a
second unit, which also answered on the third attempt.

Poll `D1V0000` repeatedly, roughly every 1.2 s for up to ~10 s, keeping the
`P` poll running in between. A single query with a 3 s timeout fails
intermittently.

Response format: `D1V0020` followed by the radio identification string.

#### A plain analogue radio never answers

This is normal, not a fault. A WIRES-X capture with an **FT-7800R** attached:

```
  0.000  ->  M00                     handshake, acknowledged
  0.013  ->  R6423                   device info, answered
  1.041  ->  D1V0000                 no reply
  2.041  ->  D1V0000                 no reply
    ...                              37 attempts over 54 s
 55.119  ->  D1V0000                 no reply
```

Meanwhile `P010000` was polled 56 times and answered 56 times, so the box and
the link were healthy throughout. **WIRES-X never sent `D1M`** — with no radio
identified there is nothing to configure, so it simply kept asking.

The distinction is between two classes of radio:

| | Answers `D1V` | Frequency, power, tone | PTT, squelch, audio |
|---|---|---|---|
| FTM-400D, FTM-100D … | yes | set by the host with `D1M` | via the data connector |
| FT-7800R and similar | no | set on the radio | via the data connector |

PTT and squelch travel over the data connector's own lines and are reported
through the poll, so they work either way. Only the channel configuration
depends on the radio being controllable.

**A client must therefore treat a detection failure as informational, not
fatal.** Refusing to start leaves a perfectly usable node dead: the operator
tunes the radio by hand and everything else behaves normally. Skip `D1M` when
nothing identified — sending channel settings to a radio that cannot receive
them achieves nothing.

The same silence has one other cause worth reporting to the user: a
controllable radio in the wrong mode. On an FTM-400D, `[D/X]` alone gives PDN
mode, which looks similar and does not answer `D1V` either.

---

## 5. Poll, PTT and squelch

The host polls at roughly 1 Hz. The poll command carries the PTT state:

```
P010000     poll, PTT OFF
P100000     poll, PTT ON
P010010     shutdown (sent once when the software exits)
```

The response is always:

```
B<n> 0    0000000
```

where `<n>` is the **squelch state**: `0` closed, `1` open. The remaining
fields were constant (`0    0000000`) throughout the capture and are
undecoded.

### PTT must be held

PTT is asserted by *what you poll with*, not by a one-shot command. Stop
sending `P100000` and the transmitter drops.

Measured in the reference capture: TX started 19 ms after the first
`P100000`, and ended 29 ms after returning to `P010000`.

**Send the poll immediately on PTT state change** rather than waiting for
the next scheduled poll — otherwise PTT latency equals the poll interval.

---

## 6. Radio status

The device pushes `D1P0004<pppp>` unsolicited on state change, and answers
`D1C0000` with `D1C0005<ppppp>`.

Note the field layout — the length field is four characters, so the status
byte is at the **end** of the string:

```
D1P 0004 0025
    ^^^^      length field, hex 0x04 = 4-character payload
         ^^^^ payload
           ^^ status byte — read as the last two characters
```

Status byte bitfield:

| Value | Meaning |
|---|---|
| `00` | Idle |
| `01` | Carrier detected (brief, transitional) |
| `10` | **RX / squelch open** — tracks `B1` |
| `05` | TX starting |
| `25` | **TX active** (bit `0x20`) |

Squelch can be read either from the `B` digit in the poll response or from
bit `0x10` of the `D1P` pushes. The pushes are lower latency; the poll
response is a reliable fallback.

---

## 7. The D1M command — frequency, mode, tone and power

`D1M` carries the complete channel configuration. Everything the radio needs
to know about how to operate is in this one frame.

**None of it is persistent.** In HRI-200 node mode the radio is a slave —
the host owns the configuration and sets it on every startup. WIRES-X does
exactly the same.

**The box only reads `D1M` during initialisation.** Sending a new one mid
session has no effect. Changing any setting requires closing the port,
reconnecting and repeating the whole handshake. This is what makes field
mapping tedious but also completely reliable: one capture per setting, then
diff.

### Frame layout

```
D1M 0043 <67-character payload>
    ^^^^ length field, hex 0x43 = 67
```

Payload, with every decoded field marked:

```
 M 000  144.00000  ±  000.00000  N T CCC DDD 000 P 0  144.00000 + 000.00000 010887540002
 │ │    └─ freq A ┘ │  └offset A┘ │ │  │   │   │  │ │  └────────── VFO B ─────────────┘
 │ │               │             │ │  │   │   │  │ └ ?
 │ │               │             │ │  │   │   │  └ power
 │ │               │             │ │  │   │   └ ? (3 chars)
 │ │               │             │ │  │   └ DCS code
 │ │               │             │ │  └ CTCSS tone
 │ │               │             │ └ tone mode
 │ │               │             └ narrow
 │ │               └ shift sign
 │ └ ? (3 chars)
 └ operating mode
```

Payload indices (add 7 for the position in the complete frame):

| Index | Width | Field | Values |
|---|---|---|---|
| 0 | 1 | Operating mode | `4` FM, `7` digital (request), `5` digital (reply) |
| 1–3 | 3 | *undecoded* | always `000` |
| 4–12 | 9 | Frequency A | `NNN.NNNNN` |
| 13 | 1 | Shift sign | `+` / `-` — normalised by the box |
| 14–22 | 9 | Offset A | `NNN.NNNNN` |
| 23 | 1 | Narrow | `0` wide, `1` narrow |
| 24 | 1 | Tone mode | `1` off, `2` CTCSS, `3` DCS |
| 25–27 | 3 | CTCSS tone | integer part, truncated |
| 28–30 | 3 | DCS code | verbatim, three octal digits |
| 31–33 | 3 | *undecoded* | always `000` |
| 34 | 1 | Power | `0` high, `1` mid, `2` low |
| 35 | 1 | *undecoded* | always `0` |
| 36–66 | 31 | VFO B | same structure, not exercised |

Everything below was established by capturing one WIRES-X session per
setting and diffing the resulting frames. In every case exactly one field
changed.

### Operating mode — index 0

| Value | Meaning |
|---|---|
| `4` | FM |
| `7` | Digital — what the **host sends** |
| `5` | Digital — what the **box reports back** |

The box normalises `7` to `5` in its reply. As bits:

```
FM                4 = 0b100
digital request   7 = 0b111
digital state     5 = 0b101
```

Bit 2 is always set, bit 0 looks like the digital flag, and bit 1 appears to
be a "change mode" request the box clears once applied. A client should send
`7` and expect `5` back — this is not an error.

### Narrow — index 23

| Value | Meaning |
|---|---|
| `0` | Wide |
| `1` | Narrow |

Echoed unchanged. Observed in digital mode; the FTM-400D also has FM-N, so
it is presumably the same field there.

### Tone mode — index 24

| Value | Meaning |
|---|---|
| `1` | Off |
| `2` | CTCSS |
| `3` | DCS |

TSQL, TSQL-R, DCS-R and PAGER are untested and probably occupy further
values on this same field.

### CTCSS tone — indices 25–27

The **integer part of the tone, truncated**, three digits, zero padded:

```
 67.0 -> 067      88.5 -> 088      250.3 -> 250
 69.3 -> 069      71.9 -> 071      254.1 -> 254
```

Truncated, not rounded — `71.9` becomes `071`, not `072`.

This works because all 50 standard CTCSS tones have **unique integer
parts**, so the decimal carries no information. Convenient, but it also
means the format cannot express anything outside the standard list.

### DCS code — indices 28–30

The code exactly as written, three octal digits: `023`, `025`, `754`. No
transformation whatsoever. All 104 standard codes fit.

### Both tone fields persist independently

The CTCSS and DCS fields keep their values regardless of which mode is
active. Neither is ever cleared — only the mode flag decides which one the
radio uses.

This is why `023` looked like a constant in every capture taken before DCS
was tried: it is simply the lowest code in the list, sitting there as a
default. Likewise `088` appeared constant until CTCSS was exercised, and a
tone-off capture still reports whichever tone was selected last.

A client should therefore always populate both fields with something valid,
even when the corresponding mode is not selected.

### Power — index 34

| Value | Level |
|---|---|
| `0` | High |
| `1` | Mid |
| `2` | Low |

Note the scale is **inverted** — a higher digit means lower power.

Only the VFO A half changes; the equivalent position in the VFO B half was
constant across all reference captures.

### The reply

The box echoes the frame back with its actual state. Two positions differ
legitimately:

| Frame position | Payload index | Why |
|---|---|---|
| 20 | 13 | Shift sign is normalised — a requested `-` comes back as `+` |
| 7 | 0 | Digital mode `7` is reported back as `5` |

A difference anywhere else means an undocumented field has been found, and
is worth capturing.

### Working template

```python
FREQ_TEMPLATE = ("D1M0043{M}000{F}-000.00000{N}{T}{C}{D}000{P}0"
                 "{F}+000.00000010887540002")

MODE_FM, MODE_DIGITAL = "4", "7"
TONE_OFF, TONE_CTCSS, TONE_DCS = "1", "2", "3"
POWER_HIGH, POWER_MID, POWER_LOW = "0", "1", "2"


def build_d1m(mhz, mode=MODE_FM, narrow=False, power=POWER_LOW,
              tone_mode=TONE_OFF, ctcss=88.5, dcs=23):
    """Both tone fields are always populated - the radio keeps them
    independently of which mode is selected."""
    f = f"{mhz:09.5f}"                    # 145.28750, exactly 9 characters
    assert len(f) == 9
    cmd = (FREQ_TEMPLATE
           .replace("{M}", mode)
           .replace("{F}", f)
           .replace("{N}", "1" if narrow else "0")
           .replace("{T}", tone_mode)
           .replace("{C}", f"{int(ctcss):03d}")   # truncated, not rounded
           .replace("{D}", f"{int(dcs):03d}")
           .replace("{P}", power))
    body = cmd[3:]
    assert int(body[:4], 16) == len(body) - 4     # length field must agree
    return cmd
```

Verified byte-identical against reference captures for FM, digital, digital
narrow, all three power levels, seven CTCSS tones and three DCS codes.

### How these fields were found

The method is simple and works for anything still undecoded:

1. Set one thing on the radio manually
2. Capture a full WIRES-X session with USBPcap or usbmon
3. Extract the `D1M` frame the host sends
4. Diff it against a capture that differs only in that one setting

Every field documented above changed exactly one character. Nothing needed
to be guessed.

---

## 8. Implementing a client

The protocol needs three things from a client: framing, a handshake, and a
poll loop. Audio is entirely separate — it is a standard ALSA device.

### Minimum viable client

```python
import serial, time

SOH, EOT = 0x01, 0x04

def frame(payload):
    return bytes([SOH]) + payload.encode("ascii") + bytes([EOT])

# DTR/RTS low before open - see section 3
ser = serial.Serial()
ser.port, ser.baudrate, ser.timeout = "/dev/ttyACM0", 38400, 0
ser.dtr = ser.rts = False
ser.open()

ser.write(frame("M00"))          # mandatory handshake
# ... wait for M00 echo, then D1V0000 with retries, then D1M

ptt = False
while True:
    ser.write(frame("P100000" if ptt else "P010000"))
    # parse replies: B<n> -> squelch, D1P0004<pppp> -> status
    time.sleep(0.2)
```

### Poll rate

WIRES-X polls at roughly 1 Hz. That is enough to hold PTT, but it puts up to
one second of latency on squelch detection when relying on the `B` response.

Polling at 4–5 Hz is comfortable and gives better COS latency. 5 Hz has since
run continuously in service without the device objecting. Regardless of rate,
**send the poll immediately when PTT changes state** rather than waiting for
the next scheduled one — with that in place, measured PTT latency from request
to frame on the wire is under a millisecond.

### State to track

| State | Source |
|---|---|
| PTT asserted | your own — determines which poll command you send |
| Squelch open | `B<n>` digit, or `D1P` bit `0x10` |
| Transmitting | `D1P` bit `0x20` — useful as confirmation |
| Radio controllable | `D1V0000` responded during connect. **Not** the same as the radio being present or working — see section 4 |

The `D1P` pushes arrive unsolicited and are lower latency than the poll
response. Use them as the primary squelch source and the `B` digit as a
fallback — they agree in all observations.

### Audio

The audio interface is independent of the control protocol. It is plain
USB Audio Class 1.0 on ALSA card name `codec`:

```bash
arecord -D plughw:CARD=codec,DEV=0 -f S16_LE -r 48000 -c 1 out.wav
aplay   -D plughw:CARD=codec,DEV=0 in.wav
```

Any audio stack that speaks ALSA will work. Nothing in the control protocol
carries audio.

### Shutdown

Set PTT off, send one final `P010000`, and optionally `P010010` — which
WIRES-X sends once when it exits. Its exact effect is unverified; sending it
appears to make the device release the radio, which means the next session
needs the full `D1V0000` retry sequence.

### Known implementations

Two exist, both in this repository, and between them they exercise everything
documented above:

| | |
|---|---|
| `hri200-parrot.py` | A standalone repeater-in-a-box. No dependencies beyond pySerial. The shortest complete example of the protocol. |
| `hri200node.py` | A SvxLink node. Bridges PTT and squelch to SvxLink's pseudo-terminal drivers and serves a web configuration panel. |

The SvxLink integration needs no patches to SvxLink, because it already has
PTY drivers for both directions: `PTT_TYPE=PTY` makes it write `T` and `R` to
a pty for transmit control, and `SQL_DET=PTY` makes it read `O` and `Z` for
squelch. Translating those to and from the frames above is the whole job.

One implementation note that only shows up in practice: **the box reports your
own transmission back as a squelch event**, `D1P` bit `0x10` setting while bit
`0x20` is also set. Any client that relays squelch onwards must force it closed
while PTT is asserted and for a few hundred milliseconds afterwards, then flush
the serial input buffer. Without that the node keys itself in a loop. 400 ms is
comfortable; the box's own report clears within about 30 ms of the transmitter
dropping.

---

## 9. Not decoded

Contributions welcome, particularly captures from other radio models.

### Within `D1M`

| Payload index | Width | Status |
|---|---|---|
| 1–3 | 3 | Always `000` in every capture |
| 31–33 | 3 | Always `000` in every capture |
| 35 | 1 | Always `0` in every capture |
| 36–66 | 31 | VFO B half — never exercised |

Likely candidates for the remaining fields: channel step, shift direction,
AMS/auto mode, and whatever distinguishes the FTM-400D's DN and VW digital
modes.

### Tone mode values

Only three of the tone mode values at index 24 are known (`1` off, `2`
CTCSS, `3` DCS). The FTM-400D also offers **TSQL**, **TSQL-R**, **DCS-R**
and on some models **PAGER**. These almost certainly occupy further values
on the same field — one capture each would settle it.

### Elsewhere in the protocol

- `D1B00010` — purpose unknown, echoed verbatim
- Fields after `B<n>` in the poll response — constant in all observations
- `P010010` — sent once at shutdown; exact effect unverified
- The two leading `00000,00000` fields in the `R6423` response
- Channel B (`RADIO 2` port) — untested
- Anything C4FM-specific — out of scope for analogue FM linking

### How to help

The method that decoded everything in section 7 is straightforward and needs
no special tooling:

1. Change **one** setting on the radio
2. Capture a full WIRES-X session with USBPcap (Windows) or usbmon (Linux),
   filtered on the CDC device
3. Extract the `D1M` frame the host sends
4. Diff it against a capture that differs only in that one setting

Every field found so far changed exactly one character. Nothing had to be
guessed. Because the box only reads `D1M` at initialisation, each setting
needs its own full session — tedious, but unambiguous.

For the general protocol, a capture covering startup, 30 s idle, five keying
cycles and five squelch cycles is the most useful single artefact. The ASCII
framing makes it readable in Wireshark without any decoding.

**Captures from other radios are the highest-value contribution.** The `D1M`
layout above is confirmed on an FTM-400D only. An FTM-100D or DR-1X may use
different field positions or widths.

An FT-7800R has been tested and does not exercise `D1M` at all — it never
identifies itself, so the host never sends one. That answers a different
question, and a useful one, but it does not validate the field layout.

---

## 10. Programming mode

An internal switch places the device in Renesas boot mode. The USB identity
changes completely:

```
Normal:       26aa:0002 (CDC ACM) + 26aa:0003 (audio)
Programming:  045b:0025 "Generic Boot USB Direct" (Renesas/Hitachi)
              1 interface, bulk EP 0x01 OUT / 0x82 IN, 64 bytes
```

The MCU is a **Renesas (Hitachi) H8S/2370**, identified from strings in
Yaesu's firmware updater, which implements the standard Renesas boot-mode
serial protocol.

The firmware payload in the updater is obfuscated with a **32-bit block
cipher in ECB mode**, with two independent transforms alternating on 8-byte
boundaries. Not linear — a multiplicative hypothesis was tested and rejected.
The transform is implemented inline in the updater executable; no standard
crypto constants or imports are present.

This was **not needed** to implement the protocol, and is documented only
in case someone wants to go further.

---

## 11. Legal note

This documentation was produced by observing traffic between software and
hardware owned by the author, for the purpose of interoperability with
open-source software. In the EU this is expressly permitted under the
Software Directive (2009/24/EC, Article 6).

Yaesu's WIRES-X server end-user agreement prohibits modifying the WIRES-X
software or the HRI-200. Nothing here modifies either — no firmware was
altered and no Yaesu server was accessed. Anyone using this documentation
should form their own view on their own circumstances.

No Yaesu firmware, software or copyrighted material is redistributed here.

---

*Serial numbers have been replaced with placeholders. Verified against two
HRI-200 units, with an FTM-400DEXP and an FT-7800R in analogue FM mode, and
running continuously as a SvxLink node since. Your results may vary, and
reports of differences — especially from other radio models — are the most
useful contribution you can make.*
