# Installing the node

One repository, one script. Fifteen minutes from a blank Raspberry Pi to a node
that comes up on its own after a power cut.

**Reference system:** Raspberry Pi 4, Raspberry Pi OS Lite 64-bit (Debian 13
"Trixie"), SvxLink 24.02 from Debian, HRI-200, FTM-400D.

> **Work into a dummy load** until you have coordinated a frequency. The node
> keys a real transmitter as soon as it starts.

---

## What you need

| | |
|---|---|
| Interface | HRI-200 with the internal flash switch in **normal** position |
| Radio | One the box supports, with its CT-174 cable. FTM-400D verified |
| Host | Raspberry Pi with a spare USB port and network |
| EchoLink | An account with the `-L` suffix, **validated**. Optional, but validation is manual and takes days — start it now |
| Frequency | See "Before it goes on an antenna" below |

---

## 1. Connect the hardware first

Power the FTM-400D on **while holding `[D/X]` + `[GM]`** until the display
reads `HRI-200`, then plug the box into the Pi.

`[D/X]` alone gives PDN mode. It looks similar and does not work — this is the
most common reason for "the radio does not respond", and worth being certain
about before blaming software.

> **This does not survive a power cut.** Everything else on the node starts
> itself; the radio's mode does not. A UPS on the radio is more effective than
> anything you can do in software.

Doing this before running the installer matters: it sets the mixer levels only
if it can see the sound card, and skips that step with a warning otherwise.

```bash
lsusb | grep 26aa
```

Two lines — `26aa:0002` and `26aa:0003`. If you see `045b:0025` instead, the
internal flash switch is in programming position and nothing below will work.

## 2. Clone and run

```bash
sudo apt install -y git
git clone https://github.com/sa7bnb/Svxlink-HRI-200.git
cd Svxlink-HRI-200
chmod +x install.sh
sudo ./install.sh
```

That is the whole command. Everything else — your callsign, EchoLink, the
frequency — is set afterwards in the web panel, which means the same image
works for anybody.

Without a suffix: this is what SvxLink identifies with, not the EchoLink `-L`
callsign. Leave it out and the node identifies as `MYCALL` until someone fills
in the panel's Station box, and `--check` flags it until they do.

It starts with a full system upgrade, so on a fresh Raspberry Pi OS image with
a few dozen pending packages the whole thing can take fifteen minutes. Nothing
prompts — a config-file question would otherwise hang the script on a machine
nobody is watching, and existing files are kept rather than replaced.

Then it prints a summary with the panel address and a `--check` report.

If the upgrade installed a new kernel it says so at the end. **Do that reboot
when you are at the radio**, because afterwards the FTM-400D needs `[D/X]` +
`[GM]` again and until then the node is up but deaf.

### Changing the defaults

Environment variables, if you want something other than the defaults:

```bash
sudo FREQ=145.2875 WEB_PASSWORD=hunter2 LAN=192.168.0.0/24 ./install.sh
```

| Variable | Default | |
|---|---|---|
| `FREQ` | `434.5000` | Operating frequency in MHz |
| `WEB_USER` | `svx` | Panel login |
| `WEB_PASSWORD` | `password` | Panel password |
| `LAN` | `192.168.1.0/24` | Which network may reach the panel |
| `SOUNDS_VER` | `24.02` | Must match your SvxLink version |
| `SSH_PORT` | `22` | Which port the firewall opens for SSH |
| `ENABLE_FIREWALL` | `yes` | Set to `no` to leave ufw alone |
| `ENABLE_UPDATES` | `yes` | Set to `no` to skip unattended-upgrades |
| `FULL_UPGRADE` | `yes` | Set to `no` to skip the initial system upgrade |
| `HOLD_SVXLINK` | `yes` | Set to `no` to let future upgrades move SvxLink |

Running it again later is safe. It backs up anything it replaces and leaves
`/etc/hri200node.conf` alone once it exists.

### What the firewall ends up allowing

| Port | | From |
|---|---|---|
| 22/tcp | SSH | your LAN, plus the address you are installing from |
| 8080/tcp | Web panel | your LAN only |
| 5198/udp | EchoLink audio | anywhere — connections arrive from arbitrary nodes |
| 5199/udp | EchoLink control | anywhere |

Everything else inbound is denied; outbound is unrestricted, which covers TCP
5200 to the EchoLink directory server and 5300 to a reflector.

The SSH rule goes in **before** the firewall is enabled, and the installer
tries to detect the address you are connecting from so that case is covered
too. Even so: **open a second terminal and confirm you can still log in before
closing the first.** A default-deny firewall on a machine in a cupboard is an
unforgiving thing to get wrong.

```bash
sudo ufw status numbered
```

### SvxLink is pinned

The installer runs a full system upgrade first, then pins SvxLink with
`apt-mark hold`. Nothing after that — a manual `apt full-upgrade`, an automatic
security update, an accidental `apt dist-upgrade` — can move it.

The reason is that a version bump can change the configuration format and take
a working node off the air, and you find out when somebody cannot connect
rather than when it happens. If SvxLink is already installed when you run the
script, it is held *before* the upgrade too.

The cost is real and worth stating: **a held package gets no security updates
either.** SvxLink listens on the network, so that is a deliberate trade rather
than a free one. To upgrade later, do it when you can watch it:

```bash
sudo apt-mark unhold svxlink-server svxlink-calibration-tools
sudo apt install --only-upgrade svxlink-server
sudo -u svxlink /usr/local/bin/hri200node.py --check
sudo apt-mark hold svxlink-server svxlink-calibration-tools
```

`apt-mark showhold` lists what is currently pinned.

### Automatic security updates

Everything else updates itself overnight, with two deliberate exceptions.

**SvxLink is excluded**, as above. **Nothing reboots unattended**, because a
reboot needs someone to put the radio back into HRI-200 mode by hand. Check for
a pending one when you are next at the radio:

```bash
ls /var/run/reboot-required 2>/dev/null && cat /var/run/reboot-required.pkgs
```

APT silently ignores configuration it cannot parse, so verify the merged result
rather than the file:

```bash
apt-config dump | grep -i unattended
systemctl list-timers apt-daily apt-daily-upgrade --no-pager
```

## 3. Open the panel

```
http://<pi-address>:8080/
```

Log in with `svx` / `password`, or whatever you set.

Start with the **Station** box at the top: your callsign, no suffix. Until that
is set the node identifies as `MYCALL`, which is not legal to transmit under
anywhere. Saving it restarts SvxLink, so allow about 25 seconds.

Then fill in the EchoLink section — callsign with `-L`, password, sysop name
and location — and choose **Save and apply**.

The panel shows four live lamps: radio, SvxLink, squelch and transmitter. Key
up from a handheld and the squelch lamp should turn green within a second.

## 4. Prove it end to end

With a dummy load connected, send `1` `#` as DTMF — hold PTT, press `1`, press
`#`, release. The parrot should announce itself by voice, then play your audio
back.

Commands from the radio:

| | |
|---|---|
| `*` | Status: callsign and time |
| `0#` | Help: lists the modules |
| `1#` | Parrot, local |
| `2#` then `9999#` | EchoLink `*ECHOTEST*` — a round trip over the network |
| `#` | Leave the module, or disconnect |

`9999` is the one that tells you how you actually sound to others: it exercises
the transmit level and the codec. The local parrot cannot, because that audio
never leaves the node.

### Startup order and the network wait

Both services wait for name resolution before starting, because
`network-online.target` only means an interface has an address — the resolver
can still be unusable for several seconds afterwards, especially over wifi.
Losing that race puts `No IP addresses were returned for the EchoLink directory
server DNS query` in the log and leaves the node up but not linked, with no
further attempt.

Rather than a fixed sleep, both units call `hri200node.py --wait-network`,
which returns the moment DNS answers — milliseconds on a healthy boot.

They wait with different patience, deliberately:

| | Waits up to | Because |
|---|---|---|
| `svxlink` | 90 s | It needs the directory server, and there is no point starting without it |
| `hri200node` | 30 s | PTT, squelch and the local modules work with the internet down; a broken WAN should not keep the repeater function offline |

Both start anyway when the timeout expires. A node that comes up without
EchoLink is better than one that never comes up.

## 5. Reboot, and check it comes back

```bash
sudo reboot
```

Then, with the radio back in HRI-200 mode:

```bash
systemctl is-active svxlink hri200node
sudo grep -A4 "EchoLink directory" /var/log/svxlink | tail -8
```

The EchoLink greeting should be there without you touching anything.

---

## Updating

```bash
cd ~/Svxlink-HRI-200
git pull
sudo install -m755 hri200node.py /usr/local/bin/hri200node.py
sudo systemctl restart hri200node
```

**Installing to `/usr/local/bin` is the step people forget.** The service runs
the installed copy, so editing the file in the clone — or even rebooting —
changes nothing on its own.

If `install.sh` itself changed, re-run it. It is idempotent and leaves your
configuration alone.

---

## Using it day to day

**The node callsign** lives in the panel's Station box. It is written into
`[SimplexLogic]` in `svxlink.conf` and restarts SvxLink, so it takes about 25
seconds. The two other `CALLSIGN` keys in that file, under `[RepeaterLogic]`
and `[ReflectorLogic]`, are deliberately left alone.

**Radio settings — frequency, power, mode, tone — apply in about four
seconds.** The box only reads its channel configuration during initialisation,
so the node closes the serial port, repeats the handshake and sends a new
frame. SvxLink is never interrupted and nothing restarts.

**EchoLink settings restart SvxLink**, which takes the node off the air for
roughly 25 seconds.

Everything can also be edited by hand:

```bash
sudo nano /etc/hri200node.conf
sudo systemctl restart hri200node
```

### When something is wrong

```bash
sudo -u svxlink /usr/local/bin/hri200node.py --check
```

That walks the whole chain — USB, serial permissions, the ALSA device,
SvxLink's settings, the network drop-in, the PTY symlinks, the sound files, the
callsign, EchoLink, and the channel configuration — and names what is wrong
rather than that something is.

Logs live in two places, which catches people out:

```bash
journalctl -u hri200node -f      # the node and the panel
sudo tail -f /var/log/svxlink    # SvxLink logs to a FILE, not the journal
```

`journalctl -u svxlink` shows only systemd's bookkeeping and looks empty even
when everything is fine.

### Audio levels

The installer sets them, but only if the sound card was present when it ran.
Check:

```bash
amixer -c codec | grep -A5 "'Speaker'"
```

`Playback 27 [-20.00dB]` is the factory default and far too quiet — it should
read `47 [0.00dB]`. To set them:

```bash
sudo amixer -c codec sset 'Bass Boost' off
sudo amixer -c codec sset Speaker 47      # transmit
sudo amixer -c codec sset PCM 45          # receive
sudo alsactl store
```

The control names do not match their functions: `Speaker` is the level **out**
to the radio, `PCM` the level **in**.

To verify, stop the services — they hold the card — and **speak into the
handheld during the recording**:

```bash
sudo systemctl stop hri200node svxlink
arecord -D plughw:CARD=codec,DEV=0 -f S16_LE -r 48000 -c 1 -d 5 /tmp/rx.wav
sox /tmp/rx.wav -n stats
sudo systemctl start svxlink hri200node
```

Aim for `RMS lev dB` around −18 to −22 and `Pk lev dB` around −6 to −3.

---

## Building a distributable image

The point of leaving the callsign out is that an image can be handed to another
operator without carrying yours.

```bash
sudo ./install.sh
```

Then, before shutting down to take the image, clear anything personal. None of
this is done for you, because guessing wrong about what to delete on someone's
working node would be worse than leaving it:

```bash
# Your EchoLink credentials
sudo sed -i 's/^CALLSIGN=.*/CALLSIGN=MYCALL-L/;s/^PASSWORD=.*/PASSWORD=MyPass/;\
             s/^SYSOPNAME=.*/SYSOPNAME=MyName/;s/^LOCATION=.*/LOCATION=[Svx] MyTown/' \
        /etc/svxlink/svxlink.d/ModuleEchoLink.conf

# Wifi credentials, if the image should not carry your network
sudo rm -f /etc/NetworkManager/system-connections/*.nmconnection

# SSH host keys - otherwise every node from this image shares an identity
sudo rm -f /etc/ssh/ssh_host_*
sudo systemctl enable regenerate_ssh_host_keys 2>/dev/null || true

# Logs and shell history
sudo rm -f /var/log/svxlink /var/log/svxlink.*
sudo journalctl --rotate && sudo journalctl --vacuum-time=1s
history -c && rm -f ~/.bash_history

sync
sudo shutdown -h now
```

**Shut down properly, and wait for the LED to go out before pulling the
power.** Ext4 writes metadata before contents and commits its journal every
five seconds, so a power cut shortly after an install leaves files that exist,
with the right owner and permissions, and no contents. systemd reads a
zero-length unit file as *masked*, and SvxLink fails with `Unknown PCM hri200`
because `asound.conf` is empty — a confusing set of symptoms for a node that
was working minutes earlier.

`hri200node.py --check` looks for exactly this and names the files. The cure is
to re-run `install.sh`, which is idempotent.

Whoever flashes the image opens the panel, fills in the Station box and the
EchoLink box, and is running. `--check` reports the callsign as unset until
they do.

Consider changing `WEB_PASSWORD` in `/etc/hri200node.conf` per image, or at
minimum telling recipients to change it — otherwise every node from that image
shares one password.

The ALSA levels **do** travel with the image, since `alsactl` state is keyed on
the card name and the HRI-200 always enumerates as `codec`.

---

## Before it goes on an antenna

**Frequency.** The `434.5000` default is a placeholder. Swedish internet
gateways normally live in 433.000–434.750 MHz; note that 433.050–434.790 is
shared with ISM devices, so the noise floor is real, especially near 433.92.
Do not use the 2 m repeater segment.

**Coordination.** An unattended transmitter needs a coordinated channel, and
what is free depends on what already exists within range — which you cannot
determine from your own receiver. In Sweden that means SSA's frequency
coordinator, and PTS's regulations govern unattended operation and
identification intervals.

**Port forwarding**, if the node should be reachable from outside. Outbound
EchoLink works with no configuration, which is why login succeeds before you
have done anything. Inbound needs UDP **5198** and **5199** forwarded to the Pi
in your router — the installer opened them in the Pi's own firewall, but the
router is a separate layer. Without them you can connect out and hear the other
station, but nobody can reach you, and nothing in the log says so until someone
tries. CGNAT from your ISP defeats this regardless of router settings.

**Never forward port 8080.** The panel speaks plain HTTP and holds your
EchoLink password. From outside, tunnel it:

```bash
ssh -L 8080:localhost:8080 pi@192.168.1.120
```

---

## About the default password

The installer sets `svx` / `password` unless you say otherwise, and warns about
it at the end. On a home LAN behind a router that may be a fair trade — but it
should be a decision, not an accident. Anyone who can reach port 8080 can
change what your transmitter does, and can read the EchoLink password off the
wire because it is unencrypted.

```bash
sudo sed -i 's/^WEB_PASSWORD=.*/WEB_PASSWORD=something-better/' /etc/hri200node.conf
sudo systemctl restart hri200node
```

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `journalctl -u svxlink` looks empty | Wrong log. Use `/var/log/svxlink` |
| Panel: `Cannot write ...: Permission denied` | Running an old `hri200node.py` — see Updating |
| Changes to the file do nothing | Editing the clone, not `/usr/local/bin` |
| `No response to M00` | Flash switch in programming position, or another program holds the port |
| `Radio does not respond` | PDN mode instead of HRI-200 mode |
| `svxlink` SEGV, restart loop | A regular file sits at a PTY path. `sudo rm -f /dev/shm/hri200_*`, then restart |
| Node transmits silence | Sound files missing, or the `en_US` symlink absent |
| Transmit barely audible | `Speaker` still at the factory 27, i.e. −20 dB |
| `INCORRECT PASSWORD` | The `-L` account has its own password; changes take minutes to propagate |
| `DNS query failed` at boot | The drop-in is missing — check `ls /etc/systemd/system/svxlink.service.d/` |
| Slow start with no internet | Expected: it waits up to 30 s for DNS, then starts anyway |
| `Distortion detected` | Harmless — see below |
| DTMF not decoding | Receive level. `amixer -c codec sset PCM 45` |
| Radio dead after a power cut | Expected. `[D/X]` + `[GM]` |
| SSH host key changed | You reinstalled. `ssh-keygen -R <address>` |
| Unit "masked", `Unknown PCM hri200`, `--check` silent | Zero-length files from an unclean shutdown. Re-run `install.sh` |

### Never use `>` against the PTY paths

`echo T > /dev/shm/hri200_ptt` creates a **regular file** if the symlink is not
there. SvxLink then cannot `symlink()` over it, `PttPty::initialize()` fails,
and its destructor dereferences a null pointer — `SIGSEGV` and a restart loop
with no useful error message. `SquelchPty` has the null check that `PttPty`
lacks, so the receiver survives and only the transmitter dies.

Recovery: remove **both** symlinks — `symlink()` also fails against a stale one
— then `sudo systemctl reset-failed svxlink` and start again.

### Do not run the node under `sudo`

`/dev/shm` is mode `1777`, and with `fs.protected_symlinks=1` the kernel
refuses to follow a symlink there unless the follower owns the symlink or the
directory. **Root is not exempt** — that is the point of the protection. So
`sudo hri200node.py` fails with `EACCES` where `sudo -u svxlink hri200node.py`
succeeds, which is the opposite of what one expects. The systemd unit runs as
`svxlink` and is unaffected.

### "Distortion detected" is not about your level

It fires at every squelch opening. A recording shows the radio's AF stage
unmuting: full scale in eight samples, then a smooth exponential recovery with
a 61 ms time constant — a 2.6 Hz high-pass corner, i.e. a DC step through the
audio path's AC coupling. Speech in the same recording peaks nowhere near
clipping.

Lowering the level does not remove it — the transient saturates regardless of
gain — and only makes the node deaf. Ignore it. Full detail in
[PROTOCOL.md](PROTOCOL.md), section 1.

---

## Removing it

```bash
sudo systemctl disable --now hri200node
sudo rm -f /etc/systemd/system/hri200node.service /etc/sudoers.d/hri200node \
           /usr/local/bin/hri200node.py /etc/hri200node.conf
sudo systemctl daemon-reload
```

`svxlink.conf` and `asound.conf` have timestamped backups beside them from the
installer. The radio and the box are untouched — no firmware was modified, so
returning to WIRES-X is just stopping the services.
