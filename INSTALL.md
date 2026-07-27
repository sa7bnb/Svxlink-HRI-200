# Installing an HRI-200 SvxLink node

A clean-machine walkthrough, from a blank Raspberry Pi to a node that comes up
on its own after a power cut.

Every step here was verified on real hardware during the first bring-up. Where
a step exists only to avoid a specific trap, the trap is named — those are the
ones worth reading rather than pasting.

**Reference system:** Raspberry Pi 4, Raspberry Pi OS Lite 64-bit (Debian 13
"Trixie"), SvxLink 24.02-5 from Debian, HRI-200 with an FTM-400D.

Every value below is taken from a node that is running, has survived several
reboots unattended, and has been checked end to end through EchoLink's
`*ECHOTEST*` loopback.

**Work into a dummy load until section 10.** The node keys a real transmitter
from section 9 onwards.

---

## 0. Before you start

You need:

- HRI-200 with the internal flash switch in **normal** position
- A radio the box supports, with its CT-174 cable — an FTM-400D here
- Raspberry Pi with a spare USB port and network
- An EchoLink account **with the `-L` suffix**, validated. This is a separate
  registration from your personal callsign and has its own password.
  Validation is manual and takes days, so start it now if you have not.
- A frequency. See section 12 before transmitting into an antenna.

Files from this repository:

```
hri200d.py             the daemon
hri200d.service        systemd unit
patch-svxlink-conf.py  applies the node settings to svxlink.conf
hri200-parrot.py       standalone test, useful for isolating faults
```

---

## 1. Copy the files across

From your workstation. Quote any path containing parentheses or spaces:

```bash
scp hri200d.py hri200d.service patch-svxlink-conf.py hri200-parrot.py \
    pi@192.168.1.120:~/
```

An SSH key saves time — you will log in many times during bring-up:

```bash
ssh-copy-id pi@192.168.1.120
```

---

## 2. Put the radio into node mode

Power the FTM-400D on **while holding `[D/X]` + `[GM]`** until the display
shows `HRI-200`.

`[D/X]` alone gives PDN mode. It looks similar and does not work. This is the
single most common reason for "the radio does not respond", and it is worth
being certain about before blaming anything in software.

> **This does not survive a power cut.** Every other part of the node starts
> itself; the radio's mode does not. A UPS on the radio is more effective than
> anything you can do in software.

---

## 3. Check the hardware is visible

```bash
lsusb | grep 26aa
aplay -l | grep -i codec
ls -l /dev/ttyACM*
```

Expected: `26aa:0002` (control) and `26aa:0003` (audio), a card named `codec`,
and `/dev/ttyACM0` in group `dialout`.

If `lsusb` shows `045b:0025` instead, the internal flash switch is in
programming position. Nothing below will work until that is changed.

---

## 4. Install SvxLink

```bash
sudo apt update
sudo apt install -y svxlink-server python3-serial alsa-utils sox
apt policy svxlink-server
```

Debian 13 ships 24.02-5. Anything from 14.08 onwards has the two PTY drivers
this integration needs.

Debian's package creates the `svxlink` user and puts it in `dialout` and
`audio` already. Confirm rather than assume:

```bash
groups svxlink
```

If `dialout` is missing, `sudo usermod -aG dialout svxlink`.

### Check for a private mount namespace

```bash
systemctl show svxlink -p PrivateTmp -p PrivateMounts
```

Both must be `no`. If either is `yes`, svxlink and the daemon would see
different `/dev/shm` and the PTY symlinks would never line up. Turn it off in a
drop-in, or move both PTYs to `/var/lib/svxlink/` in section 7.

---

## 5. Install the sound files

These are not in Debian. Without them the node transmits carrier with silence
on it — it looks like it works, and you hear nothing.

```bash
cd /tmp
wget https://github.com/sm0svx/svxlink-sounds-en_US-heather/releases/download/24.02/svxlink-sounds-en_US-heather-16k-24.02.tar.bz2
sudo tar xjf svxlink-sounds-en_US-heather-16k-24.02.tar.bz2 -C /usr/share/svxlink/sounds/
sudo chown -R root:root /usr/share/svxlink/sounds/en_US-heather-16k
sudo ln -sfn en_US-heather-16k /usr/share/svxlink/sounds/en_US
ls /usr/share/svxlink/sounds/en_US/
```

Two traps here, both silent:

**Match the version to your server.** Take 24.02 for SvxLink 24.02, not the
newest release. Clip names track the server version, and a mismatch produces
`Could not find audio clip` for whatever changed.

**The archive's directory is `en_US-heather-16k`, but the config says
`DEFAULT_LANG=en_US`.** Hence the symlink. Without it every clip is missing.

The listing should show `Core`, `Default`, `Parrot`, `EchoLink`, `Help` and
others. 16k is correct even though the card runs at 48 kHz — SvxLink resamples
internally.

---

## 6. Name the sound card

The card index moves between boots. Pin it by name:

```bash
sudo tee /etc/asound.conf >/dev/null <<'EOF'
pcm.hri200 {
    type plug
    slave.pcm "hw:CARD=codec,DEV=0"
    hint.description "Yaesu HRI-200"
}

ctl.hri200 {
    type hw
    card codec
}
EOF

aplay -L | grep -A2 hri200
```

`type plug` handles the channel mismatch: capture on the box is 1 channel,
playback is 2, and SvxLink can then treat it as mono in both directions.

Set the mixer. The control names do not match their functions — `Speaker` is
the **transmit** level out to the radio, `PCM` is the **receive** level in:

```bash
amixer -c codec sset 'Bass Boost' off
amixer -c codec sset Speaker 47      # transmit, 47 is the maximum
amixer -c codec sset PCM 45          # receive, +14 dB; factory default is 31
amixer -c codec sset Mic 0 off       # unused, keep it out of the way
sudo alsactl store
```

`PCM 45` rather than the factory 31 — the reference box needed +14 dB before
DTMF decoded reliably. Section 10 covers how to check it on your own.

Keep a plain-text copy, because `alsactl` state does not survive a rebuild:

```bash
amixer -c codec > ~/mixer-settings.txt
```

Prove the audio path opens, with a dummy load connected:

```bash
speaker-test -D hri200 -c 1 -t sine -f 1000 -l 1
```

---

## 7. Install the daemon

```bash
cd ~
sudo install -m755 hri200d.py /usr/local/bin/hri200d.py
sudo install -m644 hri200d.service /etc/systemd/system/hri200d.service

sudo tee /etc/default/hri200d >/dev/null <<'EOF'
FREQ=145.2875
PORT=/dev/ttyACM0
OPTS=-v
EOF

sudo systemctl daemon-reload
```

`FREQ` is the only place the frequency lives. The radio does not store it — in
node mode the host owns it and sets it at every startup.

---

## 8. Configure SvxLink

Do not overwrite `/etc/svxlink/svxlink.conf`. It is 323 lines of Debian
defaults and `CFG_DIR` wiring that the modules depend on. Patch it:

```bash
sudo python3 ~/patch-svxlink-conf.py --dry-run --callsign SA0XXX
sudo python3 ~/patch-svxlink-conf.py --callsign SA0XXX
```

The script is section aware and idempotent, takes a timestamped backup, and
shows a diff. It sets, among others:

| Section | Key | Value |
|---|---|---|
| `GLOBAL` | `CARD_SAMPLE_RATE` / `CARD_CHANNELS` | `48000` / `1` |
| `SimplexLogic` | `CALLSIGN` | yours, no suffix |
| `Rx1` | `SQL_DET` / `PTY_PATH` | `PTY` / `/dev/shm/hri200_sql` |
| `Tx1` | `PTT_TYPE` / `PTT_PTY` | `PTY` / `/dev/shm/hri200_ptt` |

Note the asymmetric key names — `PTY_PATH` on the receiver, `PTT_PTY` on the
transmitter. That is upstream's doing.

### Wait for DNS before starting

EchoLink resolves `servers.echolink.org` a second or two after startup.
`network.target` only means the stack is initialised, and even
`network-online.target` waits for an address rather than for DNS to be usable.
Losing that race gives `No IP addresses were returned for the EchoLink
directory server DNS query` and a node that is up but not linked.

```bash
sudo mkdir -p /etc/systemd/system/svxlink.service.d

sudo tee /etc/systemd/system/svxlink.service.d/wait-for-network.conf >/dev/null <<'EOF'
[Unit]
After=network-online.target nss-lookup.target
Wants=network-online.target

[Service]
# network-online.target waits for an address, not for DNS to be usable.
ExecStartPre=/bin/sleep 20
EOF

sudo systemctl daemon-reload
sudo systemctl show svxlink -p After | tr ' ' '\n' | grep -i network
```

Twenty seconds costs nothing on a node that needs the radio put into HRI-200
mode by hand anyway.

### EchoLink

Edit `/etc/svxlink/svxlink.d/ModuleEchoLink.conf`:

```ini
CALLSIGN=SA0XXX-L
PASSWORD=your_echolink_password
SYSOPNAME=Your Name
LOCATION=[Svx] 434.5000, Yourtown
```

The `-L` account has its own password. Changing the password on your personal
callsign does not change it here, and EchoLink's directory servers take a few
minutes to propagate a change — `INCORRECT PASSWORD` immediately after a
change may simply mean "not yet".

Check the file has one `PASSWORD` line, no quotes, no trailing carriage return:

```bash
grep -c "^PASSWORD" /etc/svxlink/svxlink.d/ModuleEchoLink.conf   # must be 1
grep "^PASSWORD" /etc/svxlink/svxlink.d/ModuleEchoLink.conf | cat -A | tail -c 20
```

Ending in `^M$` means Windows line endings, and the carriage return becomes
part of the password. Fix with `sudo sed -i 's/\r$//' <file>`.

---

## 9. First start

**The log is `/var/log/svxlink`, not the journal.** `journalctl -u svxlink`
shows only systemd's bookkeeping and looks empty even when everything is fine.
This wastes a surprising amount of time.

```bash
sudo systemctl start svxlink
sleep 25
sudo tail -40 /var/log/svxlink
ls -l /dev/shm/hri200_*
```

You want module loading with no `*** ERROR`, then `EchoLink directory status
changed to ON` followed by the server's greeting. And two symlinks owned by
`svxlink` pointing at `/dev/pts/N`.

> ### Never use `>` against the PTY paths
>
> `echo T > /dev/shm/hri200_ptt` creates a **regular file** if the symlink is
> not there. SvxLink then cannot `symlink()` over it, `PttPty::initialize()`
> fails, and its destructor dereferences a null pointer — `status=11/SEGV` and
> a restart loop, with no useful error message. `SquelchPty` has the null check
> that `PttPty` lacks, so the receiver survives and only the transmitter dies.
>
> Recovery: `sudo rm -f /dev/shm/hri200_*`, `sudo systemctl reset-failed
> svxlink`, start again. Remove **both** — `symlink()` also fails against a
> stale one.

Then the daemon:

```bash
sudo systemctl start hri200d
journalctl -u hri200d -n 20 --no-pager
```

Expected:

```
[OK] M00 acknowledged
[OK] Serial ........, firmware built 2015-04-13 13:38:24
  the box needs a few seconds ...
[OK] Radio: FTM-400DEXP  B3 Ver1.90020141217
[OK] Frequency set
[SQL] connected: /dev/shm/hri200_sql -> /dev/pts/0
[PTT] connected: /dev/shm/hri200_ptt -> /dev/pts/1
COS closed
```

> ### Do not run the daemon under `sudo`
>
> `/dev/shm` is mode `1777`, and with `fs.protected_symlinks=1` the kernel
> refuses to follow a symlink in such a directory unless the follower owns the
> symlink or the directory. **Root is not exempt** — that is the whole point of
> the protection. So `sudo ./hri200d.py` gets `EACCES` where `sudo -u svxlink
> ./hri200d.py` succeeds, which is the opposite of what one expects.
>
> The systemd unit runs as `svxlink` and is unaffected. For foreground testing
> use `sudo -u svxlink /usr/local/bin/hri200d.py --freq ... -v` — and note that
> `svxlink` cannot read your home directory, so install to `/usr/local/bin`
> first.

---

## 10. Prove it end to end

With a dummy load, key up from a handheld on the node frequency.

```bash
sudo tail -f /var/log/svxlink
```

`Rx1: The squelch is OPEN` means the COS path works. Then send `1` `#` as DTMF
— hold PTT, press `1`, press `#`, release — and the parrot should activate by
voice and play your audio back.

Useful commands from the radio:

| Command | Effect |
|---|---|
| `*` | status: callsign and time |
| `0#` | help: lists the modules |
| `1#` | parrot, local — plays back before the audio is coded for the network |
| `2#` | EchoLink menu |
| `2#` then `9999#` | EchoLink `*ECHOTEST*` — round trip over the network |
| `#` | leave the module, or disconnect |

`9999` is the one that tells you how you actually sound to others, since it
exercises the transmit level and the codec. The local parrot cannot: it plays
back audio that never left the node.

### Levels

If DTMF does not decode, measure. Stop both services first — they hold the
card — and **speak into the handheld during the five seconds**:

```bash
sudo systemctl stop hri200d svxlink
arecord -D plughw:CARD=codec,DEV=0 -f S16_LE -r 48000 -c 1 -d 5 /tmp/rx.wav
sox /tmp/rx.wav -n stats
```

Aim for `RMS lev dB` around −18 to −22 and `Pk lev dB` around −6 to −3. Adjust
with `amixer -c codec sset PCM <0-55>` and `sudo alsactl store`.

Use `stats`, not `stat`. The older `stat` reports a misleading `Midline
amplitude` that looks like a DC offset and is not one.

### About "Distortion detected"

`Rx1: Distortion detected! Please lower the input volume!` appears at every
squelch opening. **Do not follow its advice.** It is a real observation of
something real, but not of your level.

Analysing a ten-second recording shows a single event, at the exact moment the
radio unmutes its AF stage. The signal sits on the noise floor at about ±100,
then falls to full scale in eight samples — 170 us — and recovers along a
perfectly smooth monotonic exponential with a time constant of 61 ms. That is
a 2.6 Hz high-pass corner: a DC step through the AC coupling in the audio path.
A dropped USB packet does not look like that.

Speech in the same recording peaks between 3000 and 10000, RMS around
-30 dBFS — nowhere near clipping. So the level is fine, and lowering it only
makes the node deaf while leaving the transient exactly where it was.

This also explains an earlier red herring: the count of full-scale samples
stayed at exactly 2 across a 14 dB gain increase. Not because the spikes came
after the A/D, but because something already saturated cannot saturate harder.

If the log noise ever bothers you, the fix is to delay the `O` sent to svxlink
by ~100 ms so its audio gate opens after the thump has decayed — at the cost of
clipping the first syllable. Rarely worth it.

---

## 11. Enable at boot, then actually reboot

```bash
sudo systemctl enable svxlink hri200d
sudo reboot
```

Afterwards, with the radio back in HRI-200 mode:

```bash
uptime
systemctl is-active svxlink hri200d
ls -l /dev/shm/hri200_*
sudo grep -A4 "EchoLink directory" /var/log/svxlink | tail -8
```

The EchoLink greeting should be there without you touching anything. If it says
the DNS lookup failed, the drop-in from section 8 is missing or was saved
incorrectly — check `ls -l /etc/systemd/system/svxlink.service.d/`.

Race conditions are unreliable by nature. One clean boot does not prove the
drop-in is unnecessary; it may simply have got lucky.

---

## 12. Before it goes on an antenna

**Frequency.** `145.2875` in the examples is a placeholder, and a poor one — it
sits in the 2 m repeater segment. Swedish internet gateways normally live in
433.000–434.750 MHz. Note that 433.050–434.790 is shared with ISM devices;
amateurs have priority but the noise floor is real, especially near 433.92.

**Coordination.** An unattended transmitter needs a coordinated channel. What
is free depends on what already exists within range, which you cannot determine
from your own receiver. In Sweden that means SSA's frequency coordinator, and
PTS's regulations govern unattended operation and identification intervals.

Changing frequency touches one file:

```bash
sudo sed -i 's/^FREQ=.*/FREQ=434.5000/' /etc/default/hri200d
sudo systemctl restart hri200d
```

**Identification** lives in `[SimplexLogic]`, in **minutes**. Debian ships both
at 60, which means the short and long idents collide and you never hear the
difference. The reference node uses:

```ini
SHORT_IDENT_INTERVAL=10
LONG_IDENT_INTERVAL=60
```

To hear one now, drop `SHORT_IDENT_INTERVAL` to `1` temporarily, key up once,
and wait. Edit the file rather than reaching for `sed` — a bare
`sed -i 's/^SHORT_IDENT_INTERVAL=.*/.../'` rewrites the same key in
`[RepeaterLogic]` too. Harmless while that logic is unused, but untidy.

---

## 13. Port forwarding

Outbound works without any configuration, which is why login succeeds before
you have done anything. Inbound needs the router:

| Port | Protocol | To |
|---|---|---|
| 5198 | UDP | the Pi |
| 5199 | UDP | the Pi |

Without these you can connect out and hear the other station, but nobody can
reach you — and nothing in the log says so until someone tries. CGNAT from your
ISP defeats this entirely regardless of router settings.

---

## 14. Firewall

Do SSH first, or you will lock yourself out of a machine in a cupboard.

```bash
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from 192.168.1.0/24 to any port 22 proto tcp comment 'SSH from LAN'
sudo ufw allow 5198/udp comment 'EchoLink audio'
sudo ufw allow 5199/udp comment 'EchoLink control'
sudo ufw enable
```

Open a **second** SSH session and confirm you can get in before closing the
first. Check what else was listening with `sudo ss -tlnp` — VNC, Samba or a web
dashboard all need their own rules.

---

## 15. Automatic updates

```bash
sudo apt install -y unattended-upgrades

sudo tee /etc/apt/apt.conf.d/52unattended-local >/dev/null <<'EOF'
// Reboots need the FTM-400D put back into HRI-200 mode by hand, so never
// reboot unattended.
Unattended-Upgrade::Automatic-Reboot "false";

// Do not auto-upgrade svxlink. A config format change between versions
// would take the node off the air silently.
Unattended-Upgrade::Package-Blacklist {
    "svxlink-server";
    "svxlink-calibration-tools";
    "libasyncaudio.*";
    "libasynccore.*";
    "libasynccpp.*";
    "libasyncqt.*";
};

Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
EOF

apt-config dump | grep -i unattended
systemctl list-timers apt-daily apt-daily-upgrade --no-pager
```

APT silently ignores files it cannot parse, so `apt-config dump` is the check
that matters — it shows the merged result. The blacklist should appear as seven
entries, and both timers should have a `NEXT` time.

Check for pending reboots when you are next at the radio:

```bash
ls -l /var/run/reboot-required 2>/dev/null && cat /var/run/reboot-required.pkgs
```

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `journalctl -u svxlink` looks empty | Wrong log. Use `/var/log/svxlink` |
| `status=11/SEGV`, restart loop | Regular file at a PTY path. `rm -f /dev/shm/hri200_*` |
| Daemon: `permission denied` as root | `fs.protected_symlinks`. Run as `svxlink` |
| Daemon stuck at `Waiting for svxlink` | Run with `-v`; it now names the reason |
| `No response to M00` | Flash switch in programming position, or port held by something else |
| `Radio does not respond` | PDN mode instead of HRI-200 mode |
| Every audio clip missing | Sound package absent, or `en_US` symlink missing |
| Node transmits silence | Same as above |
| `INCORRECT PASSWORD` | `-L` account has its own password; allow minutes to propagate |
| `DNS query` failed at boot | Drop-in from section 8 missing |
| `Distortion detected` | Harmless. See section 10 |
| DTMF not decoding | Level. Measure with `sox ... stats` |
| Radio dead after power cut | Expected. `[D/X]` + `[GM]` |

Isolate faults by layer. `hri200-parrot.py` exercises the box and radio with no
SvxLink involved; if that fails, nothing above it can work. Conversely, with
svxlink running and the daemon stopped, `printf 'O' > /dev/shm/hri200_sql`
tests the SvxLink half with no hardware involved — but only when the symlink
already exists.
