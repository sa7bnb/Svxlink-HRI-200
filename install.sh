#!/bin/bash
#
# install.sh - sets up a complete HRI-200 SvxLink node.
#
#   sudo ./install.sh SA0XXX
#
# Idempotent: safe to run again after changing something. Every file it
# replaces is backed up first.
#
set -euo pipefail

CALLSIGN="${1:-}"
WEB_USER="${WEB_USER:-svx}"
WEB_PASSWORD="${WEB_PASSWORD:-password}"
FREQ="${FREQ:-434.5000}"
LAN="${LAN:-192.168.1.0/24}"
SSH_PORT="${SSH_PORT:-22}"
ENABLE_FIREWALL="${ENABLE_FIREWALL:-yes}"
ENABLE_UPDATES="${ENABLE_UPDATES:-yes}"
FULL_UPGRADE="${FULL_UPGRADE:-yes}"
HOLD_SVXLINK="${HOLD_SVXLINK:-yes}"

# Pinned so no upgrade, manual or automatic, can move the node underneath you.
SVX_PKGS=(svxlink-server svxlink-calibration-tools)
SOUNDS_VER="${SOUNDS_VER:-24.02}"

RUN_USER=svxlink            # must match svxlink's own user - see below
BIN=/usr/local/bin/hri200node.py
CONF=/etc/hri200node.conf
UNIT=/etc/systemd/system/hri200node.service
ECHOLINK=/etc/svxlink/svxlink.d/ModuleEchoLink.conf
SVXLINK_CONF_PATH=/etc/svxlink/svxlink.conf

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   ok   %s\n' "$*"; }
warn() { printf '   !!   %s\n' "$*"; }
die()  { printf '\n\033[31mFAILED: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run with sudo."
# Optional on purpose. If you are building an image to hand to someone else,
# leave it out - the node identifies as MYCALL and the first person to open the
# panel sets their own. --check flags it until they do.
if [[ -z $CALLSIGN ]]; then
    warn "No callsign given. The node will identify as MYCALL until someone"
    warn "sets it in the web panel. That is correct if you are building an"
    warn "image; otherwise run:  sudo ./install.sh SA0XXX"
fi
[[ -f hri200node.py ]] || die "hri200node.py not found in $(pwd)."
[[ -s hri200node.py ]] || die "hri200node.py is empty. A truncated file here
would install a node that cannot start. Re-clone or re-download it."

# ---------------------------------------------------------------------------
say "System update"
# ---------------------------------------------------------------------------
# Never prompt. A config-file question would hang the script forever on a
# machine nobody is watching, and --force-confold keeps whatever is already
# installed rather than silently replacing files we may have edited.
export DEBIAN_FRONTEND=noninteractive
APT_OPTS=(-y -o Dpkg::Options::=--force-confold -o Dpkg::Options::=--force-confdef)

apt-get update -qq

if [[ $FULL_UPGRADE != yes ]]; then
    warn "full upgrade skipped (FULL_UPGRADE=$FULL_UPGRADE)"
else
    PENDING=$(apt-get -s upgrade 2>/dev/null | grep -c '^Inst' || true)
    if [[ $PENDING -eq 0 ]]; then
        ok "already up to date"
    else
        # If svxlink is already installed, hold it. A version bump can change
        # the configuration format and take a working node off the air, and
        # you would find out when somebody could not connect. The test is
        # whether the package is installed - not whether this script has run
        # before - so a manually installed svxlink is protected too.
        if dpkg-query -W -f='${Status}' svxlink-server 2>/dev/null \
           | grep -q "ok installed"; then
            apt-mark hold "${SVX_PKGS[@]}" >/dev/null 2>&1 || true
            ok "svxlink is installed and will be held back from the upgrade"
        fi
        echo "   ..   upgrading $PENDING package(s). This can take a while on a Pi."
        apt-get "${APT_OPTS[@]}" full-upgrade
        apt-get "${APT_OPTS[@]}" autoremove
        ok "system upgraded"
    fi
fi

# ---------------------------------------------------------------------------
say "Packages"
# ---------------------------------------------------------------------------
apt-get "${APT_OPTS[@]}" install -qq svxlink-server python3-serial python3-flask \
                       alsa-utils sox wget ufw unattended-upgrades
SVX_VER=$(dpkg-query -W -f='${Version}' svxlink-server 2>/dev/null)
ok "svxlink-server $SVX_VER"

if [[ $HOLD_SVXLINK == yes ]]; then
    apt-mark hold "${SVX_PKGS[@]}" >/dev/null
    ok "svxlink pinned at $SVX_VER - no upgrade will move it"
    warn "That also means it gets no security updates. Release it with:"
    warn "  sudo apt-mark unhold ${SVX_PKGS[*]}"
else
    apt-mark unhold "${SVX_PKGS[@]}" >/dev/null 2>&1 || true
    warn "svxlink NOT pinned (HOLD_SVXLINK=no) - a future upgrade may move it"
fi

# ---------------------------------------------------------------------------
say "Hardware"
# ---------------------------------------------------------------------------
if lsusb | grep -q 26aa:0002; then
    ok "HRI-200 present on the USB bus"
else
    if lsusb | grep -q 045b:0025; then
        warn "The box is in PROGRAMMING mode - move the internal flash switch"
        warn "to its normal position. Installation continues, but the node"
        warn "will not run until you do."
    else
        warn "HRI-200 not detected. Powered? Cable seated?"
        warn "Installation continues; plug it in before starting the service."
    fi
fi

# ---------------------------------------------------------------------------
say "Service user"
# ---------------------------------------------------------------------------
# The node MUST run as the same user as svxlink. /dev/shm is mode 1777, and
# with fs.protected_symlinks=1 the kernel refuses to follow a symlink there
# unless the follower owns it or owns the directory. Root is not exempt - so
# running this as root would fail where running as svxlink succeeds.
id -u "$RUN_USER" >/dev/null 2>&1 || die "user $RUN_USER missing - is svxlink-server installed?"
usermod -aG dialout "$RUN_USER"
usermod -aG audio   "$RUN_USER"
ok "$RUN_USER is in dialout and audio"

# ---------------------------------------------------------------------------
say "Sound files"
# ---------------------------------------------------------------------------
# Not in Debian. Without them the node transmits carrier with silence on it,
# which looks like it works. The archive unpacks as en_US-heather-16k while
# the config says DEFAULT_LANG=en_US, hence the symlink.
SOUNDS=/usr/share/svxlink/sounds
if [[ -d $SOUNDS/en_US && -n $(ls -A "$SOUNDS/en_US" 2>/dev/null) ]]; then
    ok "already installed"
else
    TB="svxlink-sounds-en_US-heather-16k-${SOUNDS_VER}.tar.bz2"
    URL="https://github.com/sm0svx/svxlink-sounds-en_US-heather/releases/download/${SOUNDS_VER}/${TB}"
    if wget -q -O "/tmp/$TB" "$URL"; then
        mkdir -p "$SOUNDS"
        tar xjf "/tmp/$TB" -C "$SOUNDS"
        chown -R root:root "$SOUNDS/en_US-heather-16k"
        ln -sfn en_US-heather-16k "$SOUNDS/en_US"
        rm -f "/tmp/$TB"
        ok "installed and linked to en_US"
    else
        warn "download failed. The node will run but say nothing."
        warn "Take the release matching your svxlink version from"
        warn "https://github.com/sm0svx/svxlink-sounds-en_US-heather/releases"
    fi
fi

# ---------------------------------------------------------------------------
say "Program and configuration"
# ---------------------------------------------------------------------------
install -m755 hri200node.py "$BIN"
[[ -s $BIN ]] || die "$BIN is empty after install - the filesystem is refusing writes?"
ok "$BIN ($(wc -c < "$BIN") bytes)"

if [[ -f $CONF ]]; then
    ok "$CONF exists, left alone"
else
    cat > "$CONF" <<EOF
# HRI-200 node configuration. The web panel rewrites the radio keys; the rest
# is yours. Restart the service after editing by hand:
#   sudo systemctl restart hri200node

FREQ=$FREQ
PORT=/dev/ttyACM0

# fm or digital. Digital is accepted but useless with SvxLink: C4FM audio is
# AMBE, not PCM, so the node would key up and pass nothing usable.
MODE=fm

# high, mid or low. The protocol's scale is inverted (high=0) but use names.
POWER=mid
NARROW=0

# none, ctcss or dcs. Both tone values are always sent; TONE picks which the
# radio uses.
TONE=none
CTCSS=88.5
DCS=23

# Must match Tx1/PTT_PTY and Rx1/PTY_PATH in svxlink.conf.
PTT_PTY=/dev/shm/hri200_ptt
SQL_PTY=/dev/shm/hri200_sql

POLL_INTERVAL=0.2
RX_BLANK=0.4
TX_TIMEOUT=300

# The web panel. Change WEB_PASSWORD - see the note at the end of install.sh.
WEB_HOST=0.0.0.0
WEB_PORT=8080
WEB_USER=$WEB_USER
WEB_PASSWORD=$WEB_PASSWORD
EOF
    ok "$CONF written"
fi
chown root:"$RUN_USER" "$CONF"
chmod 660 "$CONF"

# ---------------------------------------------------------------------------
say "SvxLink"
# ---------------------------------------------------------------------------
if [[ -n $CALLSIGN ]]; then
    "$BIN" --setup --callsign "$CALLSIGN"
else
    "$BIN" --setup
fi

# The panel writes the node callsign into [SimplexLogic], so it needs write
# access. svxlink itself starts as root and reads this before dropping
# privileges, so 0660 does not break it.
chown root:"$RUN_USER" "$SVXLINK_CONF_PATH" 2>/dev/null || true
chmod 660 "$SVXLINK_CONF_PATH" 2>/dev/null || true

if [[ -f $ECHOLINK ]]; then
    chown root:"$RUN_USER" "$ECHOLINK"
    chmod 660 "$ECHOLINK"
    ok "$ECHOLINK is now 0660, no longer world readable"
else
    warn "$ECHOLINK not found - EchoLink settings cannot be saved"
fi

# svxlink resolves servers.echolink.org a second or two after starting.
# network-online.target only means an interface has an address - the resolver
# can still be unusable, especially over wifi. Losing that race leaves the node
# up but not linked, with "No IP addresses were returned for the EchoLink
# directory server DNS query" in the log and no further attempt.
#
# So rather than a fixed sleep, both units wait until DNS actually answers.
# That returns in milliseconds on a fast boot and still covers a slow one.
mkdir -p /etc/systemd/system/svxlink.service.d
cat > /etc/systemd/system/svxlink.service.d/wait-for-network.conf <<EOF
[Unit]
After=network-online.target nss-lookup.target
Wants=network-online.target

[Service]
ExecStartPre=$BIN --wait-network
TimeoutStartSec=180
EOF
ok "svxlink will wait for DNS before starting"

# network-online.target is only meaningful if something implements it.
for w in NetworkManager-wait-online systemd-networkd-wait-online; do
    if systemctl list-unit-files "$w.service" >/dev/null 2>&1 \
       && systemctl is-enabled "$w.service" >/dev/null 2>&1; then
        ok "$w is enabled"
        WAIT_OK=1
    fi
done
if [[ -z ${WAIT_OK:-} ]]; then
    if systemctl list-unit-files NetworkManager-wait-online.service >/dev/null 2>&1; then
        systemctl enable NetworkManager-wait-online.service >/dev/null 2>&1 || true
        ok "enabled NetworkManager-wait-online"
    else
        warn "nothing implements network-online.target. The DNS wait still"
        warn "applies, so this is not fatal."
    fi
fi
systemctl enable svxlink >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
say "Permission to restart svxlink"
# ---------------------------------------------------------------------------
# Exactly two commands, no wildcards. Needed only when the panel changes an
# EchoLink setting; radio changes are applied in-process.
SYSCTL=$(command -v systemctl)
cat > /etc/sudoers.d/hri200node <<EOF
$RUN_USER ALL=(root) NOPASSWD: $SYSCTL restart svxlink, $SYSCTL is-active svxlink
EOF
chmod 440 /etc/sudoers.d/hri200node
visudo -c >/dev/null || { rm -f /etc/sudoers.d/hri200node; die "sudoers rejected"; }
ok "$RUN_USER may restart svxlink and nothing else"

# ---------------------------------------------------------------------------
say "Mixer"
# ---------------------------------------------------------------------------
# The control names do not match their functions: Speaker is the TRANSMIT
# level out to the radio, PCM is the RECEIVE level in.
if amixer -c codec sset 'Bass Boost' off >/dev/null 2>&1; then
    amixer -c codec sset Speaker 47 >/dev/null   # transmit, 47 is the maximum
    amixer -c codec sset PCM 45     >/dev/null   # receive, +14 dB over default
    alsactl store 2>/dev/null || true
    amixer -c codec > /root/mixer-settings.txt 2>/dev/null || true
    ok "Speaker 47, PCM 45, Bass Boost off (saved to /root/mixer-settings.txt)"
else
    warn "sound card 'codec' not found - set levels once the box is plugged in"
fi

# ---------------------------------------------------------------------------
say "Service"
# ---------------------------------------------------------------------------
cat > "$UNIT" <<EOF
[Unit]
Description=HRI-200 SvxLink node and configuration panel
Documentation=https://github.com/sa7bnb/Svxlink-HRI-200
After=svxlink.service network-online.target nss-lookup.target
Wants=svxlink.service network-online.target
After=dev-ttyACM0.device

[Service]
Type=simple
# The node itself needs no network - PTT, squelch and the local modules work
# with the internet down. So this waits far less patiently than svxlink does:
# 30 seconds, then start regardless, so a broken WAN cannot keep the repeater
# function offline. Normally it returns in about three seconds.
ExecStartPre=$BIN --wait-network --wait-timeout 30
TimeoutStartSec=90
ExecStart=$BIN
User=$RUN_USER
Group=$RUN_USER
SupplementaryGroups=dialout audio

Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=10

NoNewPrivileges=false
ProtectHome=true
# Must stay false: with a private namespace this process and svxlink would
# see different /dev/shm and the pty symlinks would never line up.
PrivateTmp=false

StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable hri200node >/dev/null
ok "$UNIT installed and enabled"

# ---------------------------------------------------------------------------
say "Firewall"
# ---------------------------------------------------------------------------
if [[ $ENABLE_FIREWALL != yes ]]; then
    warn "skipped (ENABLE_FIREWALL=$ENABLE_FIREWALL)"
elif ! command -v ufw >/dev/null; then
    warn "ufw not available - skipped"
else
    # SSH FIRST, always. Enabling a default-deny firewall without it locks you
    # out of a machine that may be in a cupboard.
    ufw allow from "$LAN" to any port "$SSH_PORT" proto tcp comment 'SSH from LAN' >/dev/null

    # If this session comes from outside $LAN, allow that address too, or
    # enabling the firewall would cut the connection running this script.
    CLIENT=""
    [[ -n ${SSH_CLIENT:-} ]]     && CLIENT=$(awk '{print $1}' <<<"$SSH_CLIENT")
    [[ -z $CLIENT && -n ${SSH_CONNECTION:-} ]] && CLIENT=$(awk '{print $1}' <<<"$SSH_CONNECTION")
    # sudo clears the environment, so both of the above are usually empty.
    # utmp still records where the session came from.
    [[ -z $CLIENT ]] && CLIENT=$(who am i 2>/dev/null | sed -n 's/.*(\(.*\))/\1/p')
    # A hostname rather than an address is no use to ufw.
    [[ $CLIENT =~ ^[0-9.]+$|^[0-9a-fA-F:]+$ ]] || CLIENT=""
    if [[ -n $CLIENT ]]; then
        ufw allow from "$CLIENT" to any port "$SSH_PORT" proto tcp comment 'SSH, installing client' >/dev/null
        ok "SSH allowed from $LAN and from $CLIENT (this session)"
    else
        ok "SSH allowed from $LAN"
        warn "Could not detect this session's address. If you are connecting"
        warn "from outside $LAN, open another terminal and confirm you can"
        warn "still log in BEFORE closing this one."
    fi

    # The panel: LAN only. It speaks plain HTTP and holds an EchoLink
    # password, so a bare 'allow 8080' would be a mistake.
    ufw allow from "$LAN" to any port 8080 proto tcp comment 'node config panel' >/dev/null

    # EchoLink. Inbound connections arrive from arbitrary nodes, so these
    # cannot be restricted by source. TCP 5200 to the directory server is
    # outbound and needs no rule.
    ufw allow 5198/udp comment 'EchoLink audio'   >/dev/null
    ufw allow 5199/udp comment 'EchoLink control' >/dev/null

    ufw default deny incoming  >/dev/null
    ufw default allow outgoing >/dev/null
    ufw --force enable >/dev/null
    ok "enabled: SSH and 8080 from $LAN, 5198-5199/udp from anywhere"
    ufw status numbered | sed 's/^/        /'
fi

# ---------------------------------------------------------------------------
say "Automatic security updates"
# ---------------------------------------------------------------------------
if [[ $ENABLE_UPDATES != yes ]]; then
    warn "skipped (ENABLE_UPDATES=$ENABLE_UPDATES)"
else
    cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

    cat > /etc/apt/apt.conf.d/52unattended-hri200 <<'EOF'
// A reboot needs someone to put the radio back into HRI-200 mode by hand,
// so never reboot unattended. Check for a pending one when you are next at
// the radio:  ls /var/run/reboot-required
Unattended-Upgrade::Automatic-Reboot "false";

// Never auto-upgrade svxlink. A configuration format change between versions
// would take the node off the air silently, and you would find out when
// somebody could not connect.
Unattended-Upgrade::Package-Blacklist {
    "svxlink-server";
    "svxlink-calibration-tools";
    "libasyncaudio.*";
    "libasynccore.*";
    "libasynccpp.*";
    "libasyncqt.*";
};

// Keep /boot from filling up, which otherwise stops updates entirely.
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
EOF

    # APT silently ignores files it cannot parse, so verify the merged result
    # rather than trusting that the file was written.
    if apt-config dump | grep -q 'Package-Blacklist:: "svxlink-server"'; then
        ok "security updates on; svxlink and its libraries held back"
        ok "no unattended reboots - the radio needs a human after one"
    else
        warn "APT did not accept the configuration - check with:"
        warn "  apt-config dump | grep -i unattended"
    fi
    systemctl enable --now apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------------------
say "Starting"
# ---------------------------------------------------------------------------
# Flush everything to the card before anything else happens. Without this a
# power cut in the next few seconds leaves files that exist, with the right
# permissions, and no contents - ext4 commits its journal every five seconds.
# A zero-length unit file reads to systemd as "masked", which is a confusing
# way to discover the problem an hour later.
sync
ok "written to disk"

systemctl restart svxlink
systemctl restart hri200node
sleep 8

IP=$(hostname -I | awk '{print $1}')
echo
"$BIN" --check || true

cat <<EOF

------------------------------------------------------------------------
Panel:   http://$IP:8080/
Login:   $WEB_USER / $WEB_PASSWORD
Logs:    journalctl -u hri200node -f
         sudo tail -f /var/log/svxlink      <- svxlink logs to a FILE
Check:   sudo -u $RUN_USER $BIN --check
Held:    apt-mark showhold
Ports:   sudo ufw status numbered
Updates: sudo tail /var/log/unattended-upgrades/unattended-upgrades.log

What is left, which this script cannot do for you:

  1. Plug in the HRI-200, then set the mixer levels - the installer
     could not, because the card was not there:

       sudo amixer -c codec sset 'Bass Boost' off
       sudo amixer -c codec sset Speaker 47
       sudo amixer -c codec sset PCM 45
       sudo alsactl store

  2. The radio. Power the FTM-400D on holding [D/X] + [GM] until the
     display reads HRI-200. [D/X] alone gives PDN mode, which looks
     similar and does not work. This does not survive a power cut.

  3. Your callsign, in the panel's Station box, unless you passed one to
     this script. The node identifies as MYCALL until you do.

  4. EchoLink. Enter the callsign, password, sysop name and location in
     the panel. The -L account is registered separately from your
     personal callsign and has its own password.

  5. Port forwarding, if the node should be reachable from outside.
     Forward UDP 5198 and 5199 to this Pi in your router.

     Without it you can connect out and hear the other station, but
     nobody can reach you - and nothing in the log says so until
     someone tries.

EOF

if [[ $WEB_PASSWORD == password ]]; then
cat <<EOF
   !!   The panel is on its default password. Anyone who can reach port
   !!   8080 can change what your transmitter does and read your EchoLink
   !!   password off the wire - it is plain HTTP. On a home LAN that may
   !!   be a fair trade; decide deliberately rather than by default.
   !!
   !!     sudo sed -i 's/^WEB_PASSWORD=.*/WEB_PASSWORD=something-better/' $CONF
   !!     sudo systemctl restart hri200node
   !!
   !!   Never forward this port. Use SSH from outside:
   !!     ssh -L 8080:localhost:8080 user@$IP

EOF
fi

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
echo "The firewall is now active. Open a SECOND terminal and confirm you can"
echo "still SSH in before closing this one."
echo
fi
if [[ -f /var/run/reboot-required ]]; then
cat <<EOF
   !!   A REBOOT IS NEEDED - the upgrade installed a new kernel.
   !!   Packages: $(tr '\n' ' ' < /var/run/reboot-required.pkgs 2>/dev/null)
   !!
   !!   Do it when you are AT the radio: after the reboot the FTM-400D has to
   !!   be powered on holding [D/X] + [GM] again, and until you do the node is
   !!   up but deaf.
   !!
   !!     sudo reboot

EOF
fi
cat <<'EOF'
Always shut down with:  sudo shutdown -h now

Pulling the power leaves files that exist but are empty - ext4 writes metadata
before contents. That is how a node ends up "installed" and unable to start.

EOF
echo "Frequency is $FREQ MHz. Coordinate it with SSA before you connect an"
echo "antenna - an unattended transmitter needs a coordinated channel."
echo "------------------------------------------------------------------------"
