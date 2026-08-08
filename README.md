# Installing the node

One repository, one script. From a blank Raspberry Pi to a working SvxLink node.



Everything you need to set afterwards is on that page: callsign, EchoLink,
frequency, power and tone. The readout shows what the radio will be set to; the
lamps below show what it is actually doing.

> **Work into a dummy load** until you have a coordinated frequency. The node
> keys a real transmitter as soon as it starts.

---

## Two ways in

**Flash the ready-made image** — everything below is already done. Download it
from [Google Drive](https://drive.google.com/drive/folders/1ayyVbCobbVznq5wLdfnXmBkhqYQB8LQv?usp=drive_link),
write it to a card with Raspberry Pi Imager, boot, and go straight to section 3
to fill in your callsign and EchoLink details.

**Or install on your own Raspberry Pi OS**, which is sections 1 to 5. Do this
if you already have a Pi set up the way you like it, or if you want to read
what the script does before it does it.

Either way the logins are the same:

## Logins

| | User | Password |
|---|---|---|
| SSH / console | `svx` | `password` |
| Web panel, port 8080 | `svx` | `password` |

Two separate accounts that happen to share a name and a password. **Change
both.** The SSH one with `passwd`, the panel one in `/etc/hri200node.conf`
followed by `sudo systemctl restart hri200node`.

Anyone who reaches port 8080 can change what your transmitter does, and the
page is unencrypted. The firewall keeps it to your local network — keep it
there.

---

## What you need

- **HRI-200** with the internal flash switch in its **normal** position
- A **radio** the box supports, with its CT-174 cable. FTM-400D and FT-7800R
  verified
- A **Raspberry Pi** with a spare USB port and network
- An **EchoLink account with the `-L` suffix**, validated. Optional, but
  validation is manual and takes days, so start it now

---

## Two kinds of radio

**Radios that identify themselves** — an FTM-400D and similar — are tuned from
the web panel. The host owns the frequency, power and tone, and sets them at
every startup.

**Plain analogue radios** — an FT-7800R, for instance — never answer the
identification query, so the panel cannot tune them. Set the frequency on the
radio itself. Everything else works normally: PTT, squelch, audio, DTMF,
EchoLink. The panel detects this, says so, and greys out the controls that
would have no effect.

Neither is better; the second is just less automatic.

Tell the panel which you have — the **Radio** box has a button per model. It
loads that radio's audio levels, and for one known not to identify itself it
also skips most of the detection wait, cutting about seven seconds off every
startup. The radio is still asked, briefly, so swapping one in later corrects
itself. Choose **Other** if yours is not listed and let detection decide.

---

## 1. Radio first

*Sections 1 and 2 are for installing yourself. If you flashed the image, skip
to section 3 — but do read the radio-mode note below, because that part is not
something an image can do for you.*

Power the FTM-400D on **while holding `[D/X]` + `[GM]`** until the display
reads `HRI-200`, then plug the box into the Pi.

`[D/X]` alone gives PDN mode. It looks similar and does not work. An analogue
radio has no such mode and needs nothing done to it.

> **This does not survive a power cut.** Everything else starts itself; the
> radio's mode does not.

Do this before installing — the script sets the mixer levels only if it can see
the sound card.

```bash
lsusb | grep 26aa
```

Two lines, `26aa:0002` and `26aa:0003`. If you see `045b:0025` the flash switch
is in programming position and nothing below will work.

## 2. Install

```bash
ssh svx@<pi-address>

sudo apt install -y git
git clone https://github.com/sa7bnb/Svxlink-HRI-200.git
cd Svxlink-HRI-200
chmod +x install.sh
sudo ./install.sh
```

It upgrades the system first, so on a fresh image expect about fifteen minutes.
Nothing prompts. It ends with a summary and a health report.

The installer handles packages, sound files, ALSA, mixer levels,
`svxlink.conf`, systemd, the firewall and automatic updates.

If it says a reboot is needed, **do it when you are at the radio** — afterwards
the FTM-400D needs `[D/X]` + `[GM]` again.

## 3. Configure in the panel

```
http://<pi-address>:8080/
```

**Station** box first: your callsign, no suffix. Until it is set the node
identifies as `MYCALL`, which is not legal to transmit under anywhere.

**Radio**: pick your model. This loads its audio levels. If the panel reports
that the radio does not identify itself, that is expected on an analogue set,
and the frequency, power and tone controls below are inert — tune the radio.

Then **EchoLink**: callsign with `-L`, password, sysop name, location. The `-L`
account is registered separately from your personal callsign and has its own
password.

Keep the frequency in the location string — `[Svx] 434.500, Yourtown` — because
that is what other operators read in the EchoLink directory to know where to
call you. Change frequency later and this does not follow automatically;
`--check` will tell you if the two disagree.

**Save and apply.** EchoLink changes restart SvxLink, so allow 25 seconds.

## 4. Test it

With a dummy load, key up from a handheld and send `1` `#` as DTMF — hold PTT,
press `1`, press `#`, release. The parrot should answer by voice and play your
audio back.

| From the radio | |
|---|---|
| `*` | Status: callsign and time |
| `0#` | Help: lists the modules |
| `1#` | Parrot |
| `2#` then `9999#` | EchoLink `*ECHOTEST*`, a round trip over the network |
| `#` | Leave the module |

`9999` is the one that tells you how you sound to others — the local parrot
plays back audio that never left the node.

## 5. Reboot and confirm

```bash
sudo reboot
```

Then, with the radio back in HRI-200 mode:

```bash
systemctl is-active svxlink hri200node
sudo -u svxlink /usr/local/bin/hri200node.py --check
```

---

## Installer options

```bash
sudo FREQ=145.2875 WEB_PASSWORD=hunter2 LAN=192.168.0.0/24 ./install.sh
```

| | Default | |
|---|---|---|
| `FREQ` | `434.5000` | Operating frequency, MHz |
| `WEB_USER` / `WEB_PASSWORD` | `svx` / `password` | Panel login |
| `LAN` | `192.168.1.0/24` | Which network may reach the panel |
| `SSH_PORT` | `22` | Which port the firewall opens for SSH |
| `SOUNDS_VER` | `24.02` | Must match your SvxLink version |
| `ENABLE_FIREWALL` | `yes` | |
| `ENABLE_UPDATES` | `yes` | |
| `FULL_UPGRADE` | `yes` | Skip the initial system upgrade |
| `HOLD_SVXLINK` | `yes` | Let future upgrades move SvxLink |

Re-running is safe. It backs up what it replaces and leaves
`/etc/hri200node.conf` alone once it exists.

Passing a callsign as an argument — `sudo ./install.sh SA0XXX` — sets it at
install time instead of in the panel.

### What the installer sets up

**Firewall.** SSH and port 8080 from your LAN only; UDP 5198–5199 from
anywhere, since EchoLink connections arrive from arbitrary nodes. Everything
else inbound denied. The SSH rule goes in before the firewall is enabled, but
**open a second terminal and confirm you can still log in before closing the
first.**

**Automatic updates**, with SvxLink excluded and no unattended reboots — a
reboot needs someone to put the radio back into HRI-200 mode.

**SvxLink pinned** with `apt-mark hold`, so no upgrade can change its
configuration format under you. The cost: it gets no security updates either.
To upgrade deliberately:

```bash
sudo apt-mark unhold svxlink-server svxlink-calibration-tools
sudo apt install --only-upgrade svxlink-server
sudo -u svxlink /usr/local/bin/hri200node.py --check
sudo apt-mark hold svxlink-server svxlink-calibration-tools
```

**A DNS wait** before both services start. `network-online.target` only means
an interface has an address; the resolver can be unusable for seconds
afterwards, which otherwise leaves the node up but not linked to EchoLink.

---

## Updating

```bash
cd ~/Svxlink-HRI-200
git pull
sudo install -m755 hri200node.py /usr/local/bin/hri200node.py
sudo systemctl restart hri200node
```

**The install step is the one people forget.** The service runs the copy in
`/usr/local/bin`, so editing the clone — or rebooting — changes nothing.

---

## Day to day

Radio settings — frequency, power, mode, tone — apply in about four seconds
without restarting anything, on a radio that can be tuned remotely. On an
analogue set they are ignored and the panel says so. EchoLink settings restart
SvxLink, roughly 25 seconds off the air.

Everything is also editable by hand in `/etc/hri200node.conf`, followed by
`sudo systemctl restart hri200node`.

**When something is wrong:**

```bash
sudo -u svxlink /usr/local/bin/hri200node.py --check
```

That walks the whole chain and names what is wrong rather than that something
is.

**Logs live in two places**, which catches people out:

```bash
journalctl -u hri200node -f      # the node and the panel
sudo tail -f /var/log/svxlink    # SvxLink logs to a FILE, not the journal
```

### Audio levels

The panel's **Audio** box has two sliders, applied on save without restarting
anything. Picking a radio loads its starting point.

| | Transmit | Receive | Tunable from the panel |
|---|---|---|---|
| FTM-400D | 47 | 45 | yes |
| FT-7800R | 26 | 40 | no — set on the radio |
| Other | unchanged | unchanged | detected |

Presets are a place to start, not a specification. The gap between those two is
about 21 dB of drive on the same box and cable, which is not something you
would guess at — so measure rather than assume. Levels are stored once, not per
radio, so swapping sets means picking the button again.

To measure, stop the services — they hold the card — and **speak into the
handheld during the recording**:

```bash
sudo systemctl stop hri200node svxlink
arecord -D plughw:CARD=codec,DEV=0 -f S16_LE -r 48000 -c 1 -d 5 /tmp/rx.wav
sox /tmp/rx.wav -n stats
sudo systemctl start svxlink hri200node
```

Aim for `RMS lev dB` around −18 to −22 and `Pk lev dB` around −6 to −3. Too low
and DTMF stops decoding; too high and transmit audio distorts.

From the shell instead:

```bash
sudo amixer -c codec sset 'Bass Boost' off
sudo amixer -c codec sset Speaker 47      # transmit, out to the radio
sudo amixer -c codec sset PCM 45          # receive, in from the radio
sudo alsactl store
```

The control names do not describe their functions. The factory `Speaker 27` is
−20 dB and far too quiet.

---

## Before it goes on an antenna

**Frequency.** `434.5000` is a placeholder. Swedish internet gateways normally
live in 433.000–434.750 MHz. Do not use the 2 m repeater segment.

**Coordination.** An unattended transmitter needs a coordinated channel, and
what is free depends on what already exists within range — which you cannot
tell from your own receiver. In Sweden that means SSA's frequency coordinator,
and PTS's regulations govern unattended operation and identification.

**Port forwarding**, if the node should be reachable from outside. Forward UDP
**5198** and **5199** to the Pi in your router. Without it you can connect out
and hear the other station, but nobody can reach you — and nothing in the log
says so until someone tries.

**Never forward port 8080.** From outside, tunnel it:

```bash
ssh -L 8080:localhost:8080 svx@<pi-address>
```

---

## Building a distributable image

Install without a callsign, then use **Load defaults** in the panel followed by
**Save and apply** to clear the identity. Then:

```bash
# Wifi, if the image should not carry your network
sudo rm -f /etc/NetworkManager/system-connections/*.nmconnection

# SSH host keys - otherwise every node from this image shares an identity
sudo rm -f /etc/ssh/ssh_host_*
sudo systemctl enable regenerate_ssh_host_keys 2>/dev/null || true

# Logs and history
sudo rm -f /var/log/svxlink /var/log/svxlink.*
sudo journalctl --rotate && sudo journalctl --vacuum-time=1s
history -c && rm -f ~/.bash_history

sync
sudo shutdown -h now
```

**Wait for the LED to go out before pulling the power.** Ext4 writes metadata
before contents, so a power cut here leaves files that exist and are empty —
systemd reads a zero-length unit file as *masked*, and SvxLink fails with
`Unknown PCM hri200`. `--check` looks for exactly this; the cure is to re-run
`install.sh`.

Every node flashed from the image shares both passwords. Say so wherever you
publish it, or set a random one per image:

```bash
sudo WEB_PASSWORD=$(head -c 9 /dev/urandom | base64 | tr -d '+/=') ./install.sh
grep WEB_PASSWORD /etc/hri200node.conf
```

Shrink it with [PiShrink](https://github.com/Drewsif/PiShrink), then **flash it
and boot it before publishing**. The first-boot expansion is the part that can
fail silently.

The image linked at the top of this page was built exactly this way.

The ALSA levels do travel with the image.

---

## About the default password

The installer sets the panel to `svx` / `password` unless you say otherwise,
and warns about it at the end. On a home LAN behind a router that may be a fair
trade — but it should be a decision, not an accident.

```bash
sudo sed -i 's/^WEB_PASSWORD=.*/WEB_PASSWORD=something-better/' /etc/hri200node.conf
sudo systemctl restart hri200node
```

The Pi's own login is separate and changed with `passwd`. If you set them the
same — which the defaults do — remember that changing one leaves the other.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `journalctl -u svxlink` looks empty | Wrong log. Use `/var/log/svxlink` |
| `Internal Server Error` on the panel | `journalctl -u hri200node -n 30` has the traceback |
| Panel: `Cannot write ...: Permission denied` | Old `hri200node.py` — see Updating |
| Edits do nothing | Editing the clone, not `/usr/local/bin` |
| Panel login rejected | Not the Pi's password — see `WEB_USER` in `/etc/hri200node.conf` |
| Panel says the radio does not identify itself | Normal for a plain analogue set — tune it by hand. On an FTM-400D it means PDN mode instead of HRI-200 mode |
| Frequency and power do nothing | Same: the radio cannot be tuned remotely |
| `location agrees with the operating frequency` fails | You changed frequency but not the EchoLink location string |
| `No response to M00` | Flash switch in programming position, or another program holds the port |
| `svxlink` SEGV, restart loop | A regular file at a PTY path. `sudo rm -f /dev/shm/hri200_*` |
| Node transmits silence | Sound files missing, or the `en_US` symlink absent |
| Transmit barely audible | `Speaker` still at the factory 27 |
| Transmit distorted | `Speaker` too high — an FT-7800R wants about 26, not 47 |
| `INCORRECT PASSWORD` | The `-L` account has its own password; changes take minutes to propagate |
| `DNS query failed` at boot | Drop-in missing from `/etc/systemd/system/svxlink.service.d/` |
| Slow start with no internet | Expected: 30 s for DNS, then it starts anyway |
| DTMF not decoding | Receive level. `amixer -c codec sset PCM 45` |
| Radio dead after a power cut | Expected. `[D/X]` + `[GM]` |
| SSH host key changed | You reinstalled. `ssh-keygen -R <address>` |
| Unit "masked", `Unknown PCM hri200` | Zero-length files from an unclean shutdown. Re-run `install.sh` |

### Three that are not obvious

**Never `>` a PTY path.** `echo T > /dev/shm/hri200_ptt` creates a regular file
if the symlink is absent. SvxLink then cannot `symlink()` over it and its
`PttPty` destructor dereferences a null pointer — `SIGSEGV` with no useful
message. Remove **both** symlinks and restart.

**Do not run the node under `sudo`.** `/dev/shm` is mode `1777`, and with
`fs.protected_symlinks=1` the kernel will not follow a symlink there unless the
follower owns it or the directory. Root is not exempt. `sudo -u svxlink` works
where `sudo` fails.

**`Distortion detected` is not about your level.** It fires at every squelch
opening: the radio's AF stage unmuting, full scale in eight samples, then a
61 ms exponential recovery — a DC step through the audio path's AC coupling.
The transient saturates regardless of gain, so turning the level down only
makes the node deaf. Detail in [PROTOCOL.md](PROTOCOL.md), section 1.

---

## Removing it

```bash
sudo systemctl disable --now hri200node
sudo rm -f /etc/systemd/system/hri200node.service /etc/sudoers.d/hri200node \
           /usr/local/bin/hri200node.py /etc/hri200node.conf
sudo systemctl daemon-reload
```

`svxlink.conf` and `asound.conf` have timestamped backups from the installer.
The radio and the box are untouched — no firmware was modified, so returning to
WIRES-X is just stopping the services.

---

## Thanks

To Tobias Blomberg, **SM0SVX**, for [SvxLink](https://www.svxlink.org/) — two
decades of work, given away, and still maintained.

None of this required patching it. The two pseudo-terminal drivers everything
here depends on, `PTT_TYPE=PTY` and `SQL_DET=PTY`, were written in 2014 for
nobody in particular: a generic hook for hardware that did not exist yet. A
decade later they turned out to be exactly the right shape for a Yaesu
interface nobody had managed to drive from Linux.

Everything above the serial port is his — the modules, the DTMF handling, the
EchoLink implementation, the identification logic. This project only teaches
SvxLink how to talk to one more piece of hardware.
