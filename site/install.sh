#!/bin/sh
# Install Shoal (one hub per device) on Linux, Raspberry Pi, macOS, or Android (Termux).
#
# Your first hub: run it from your clone of the repository. That folder becomes the hub (its code
# is what `git pull` updates) and this device gets the key that signs updates for your mesh:
#   sh site/install.sh
# Every other device: any online hub serves this script with its own address already inserted, and
# the new hub downloads the code from it and joins the mesh:
#   curl -fsSL __HUB__/install.sh | sh
# On Termux without MagicDNS:
#   curl -fsSL --resolve __HUB_HOST__:443:__HUB_IP__ __HUB__/install.sh | sh
# Remove it again (keeps its data unless --purge):
#   curl -fsSL __HUB__/install.sh | sh -s -- --uninstall [--purge]
# Optional: HUB_PRIORITY=100 (an always-on device that should coordinate the mesh);
#           SHOAL_NO_SERVICE=1 (containers, tests: install and enroll, but do not start it at boot).
set -e
HUB="__HUB__"
HUB_IP="__HUB_IP__"
HOST="__HUB_HOST__"
DATA="${SHOAL_HOME:-$HOME/.hub-core}"
APP="$DATA/app"
FIRST=""
case "$HUB" in __HUB*)  # not served by a hub: the first hub, run from a clone
  FIRST=1
  APP="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)"
  [ -f "$APP/core/__main__.py" ] || { echo "[shoal] run this from your clone: sh site/install.sh"; exit 1; }
  HUB=""; HUB_IP=""; HOST="" ;;
esac
PORT=8790
UNIT=shoal
OLD_UNIT=tailnet-hub                 # the name used before the rename; replaced on reinstall
LABEL=dev.shoal.hub
OLD_LABEL=net.tailnet.hub
say() { printf '\033[32m[shoal]\033[0m %s\n' "$*"; }
# Termux (and some containers) have no MagicDNS: curl resolves the hub's name to its tailnet IP by hand
RES=""
case "$HUB" in https://*) RPORT=443 ;; *) RPORT=80 ;; esac
HP=${HUB#*://}; HP=${HP%%/*}
case "$HP" in *:*) RPORT=${HP##*:} ;; esac
[ -n "$HUB_IP" ] && [ -n "$HOST" ] && RES="--resolve $HOST:$RPORT:$HUB_IP"

if [ -n "$PREFIX" ] && echo "$PREFIX" | grep -q com.termux; then OS=termux
elif [ "$(uname)" = Darwin ]; then OS=macos
else OS=linux; fi
SUDO=""; [ "$(id -u)" != 0 ] && command -v sudo >/dev/null 2>&1 && SUDO=sudo
# without a terminal (automation, containers) sudo must fail at once instead of waiting for a password
[ -n "$SUDO" ] && ! [ -t 1 ] && SUDO="sudo -n"

# Stop and remove whatever starts the hub at boot (current and pre-rename names).
stop_service() {
  case $OS in
  termux)
    rm -f ~/.termux/boot/hub.sh
    pkill -f '[c]ore --home '"$DATA" 2>/dev/null || true
    ;;
  macos)
    for l in $LABEL $OLD_LABEL; do
      P=~/Library/LaunchAgents/$l.plist
      [ -f "$P" ] && { launchctl unload "$P" 2>/dev/null || true; rm -f "$P"; }
    done
    ;;
  linux)
    if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
      removed=""
      for u in $UNIT $OLD_UNIT; do
        if [ -f /etc/systemd/system/$u.service ]; then
          $SUDO systemctl disable --now $u >/dev/null 2>&1 || true
          $SUDO rm -f /etc/systemd/system/$u.service
          removed=1
        fi
      done
      [ -n "$removed" ] && { $SUDO systemctl daemon-reload || true; }
    fi
    pkill -f '[c]ore --home '"$DATA" 2>/dev/null || true
    ;;
  esac
}

if [ "$1" = "--uninstall" ]; then
  say "removing Shoal from this device"
  stop_service
  if command -v tailscale >/dev/null 2>&1 && [ $OS != termux ]; then
    tailscale serve --https=443 --set-path=/hub off >/dev/null 2>&1 || true
  fi
  [ -n "$FIRST" ] || rm -rf "$APP"  # a clone is yours: never deleted
  rm -rf "$DATA/venv"
  if [ "$2" = "--purge" ]; then
    rm -rf "$DATA"
    say "done: service and data removed"
  else
    say "done: service removed; its identity and settings stay in $DATA (add --purge to delete them)"
  fi
  [ -n "$FIRST" ] && say "your clone in $APP is untouched"
  say "other hubs will show it offline; remove it from the mesh in Control > Network"
  exit 0
fi

say "1/6 packages ($OS)"
if [ $OS = termux ]; then
  yes | pkg install -y python termux-api curl >/dev/null 2>&1 || pkg install -y python termux-api curl
  # Native Ed25519 (prebuilt package; pip would compile Rust). Optional: the hub has a
  # pure-Python fallback, just hundreds of times slower.
  yes | pkg install -y python-cryptography >/dev/null 2>&1 ||
    say "without python-cryptography: signatures use the slow pure-Python fallback"
  PY=python
else
  command -v python3 >/dev/null 2>&1 || { [ $OS = linux ] && $SUDO apt-get install -y python3 python3-venv; }
  PY=python3
  # Web Push (needed to coordinate the mesh): isolated venv, without touching the system Python
  # (Debian/Ubuntu ship venv without pip until python3-venv is installed: check for pip itself)
  venv_ok() { rm -rf "$DATA/venv"; $PY -m venv "$DATA/venv" >/dev/null 2>&1 && [ -x "$DATA/venv/bin/pip" ]; }
  if venv_ok || { [ $OS = linux ] && $SUDO apt-get install -y python3-venv >/dev/null 2>&1 && venv_ok; }; then
    # cryptography also gives native Ed25519 (signatures in well under 1 ms instead of 0.2-0.4 s)
    "$DATA/venv/bin/pip" install -q --disable-pip-version-check pywebpush cryptography && PY="$DATA/venv/bin/python" ||
      say "without pywebpush: the hub works, but it will not send notifications or coordinate the mesh"
  else
    rm -rf "$DATA/venv"
    say "no Python venv (install python3-venv): the hub works, but it will not send notifications or coordinate the mesh"
  fi
fi

mkdir -p "$DATA"
if [ -n "$FIRST" ]; then
  say "2/6 your first hub runs from $APP"
else
  say "2/6 downloading the hub from $HUB"
  mkdir -p "$APP"
  curl -fsSL $RES "$HUB/dist/hub.zip" -o "$DATA/hub.zip"
  $PY -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$DATA/hub.zip" "$APP"
  rm -f "$DATA/hub.zip"
fi

say "3/6 publishing /hub on the tailnet"
BIND=0.0.0.0
if command -v tailscale >/dev/null 2>&1 && [ $OS != termux ]; then
  # tailscale serve provides HTTPS and visitor identity; it needs root or --operator
  if tailscale serve --bg --https=443 --set-path=/hub http://127.0.0.1:$PORT >/dev/null 2>&1 ||
     { $SUDO tailscale set --operator="$(id -un)" && tailscale serve --bg --https=443 --set-path=/hub http://127.0.0.1:$PORT >/dev/null; }; then
    BIND=127.0.0.1
  else
    say "tailscale serve unavailable: the hub stays at http://<device>:$PORT/hub/ (tailnet only)"
  fi
fi

if [ -n "$FIRST" ]; then
  say "4/6 the key that signs updates for your mesh"
  PUB=$(cd "$APP" && $PY -m core --home "$DATA" --signing-key)
  say "    public key $PUB (back up $DATA/release_key.json: without it you cannot publish updates)"
else
  say "4/6 joining the mesh"
  ( cd "$APP" && HUB_IP="$HUB_IP" HUB_PRIORITY="${HUB_PRIORITY:-}" $PY -m core --home "$DATA" --bind $BIND --enroll "$HUB" )
fi

CMD="$PY -m core --home $DATA --port $PORT --bind $BIND --auto-deps"
if [ -n "$SHOAL_NO_SERVICE" ]; then
  say "5/6 automatic startup skipped (SHOAL_NO_SERVICE): start it with  cd $APP && $CMD"
  say "6/6 done"
  exit 0
fi

say "5/6 automatic startup"
stop_service  # one supervisor per hub: replaces an older install, including the pre-rename service
case $OS in
termux)
  mkdir -p ~/.termux/boot
  cat > ~/.termux/boot/hub.sh <<EOS
#!/data/data/com.termux/files/usr/bin/sh
# Shoal supervisor: if the hub exits (for example code 3 after an update), start it again
termux-wake-lock
pkill -f '[c]ore --home $DATA' 2>/dev/null
cd $APP
while true; do $CMD >> $DATA/core.log 2>&1; sleep 3; done
EOS
  chmod +x ~/.termux/boot/hub.sh
  nohup sh ~/.termux/boot/hub.sh >/dev/null 2>&1 &
  [ -d ~/storage/shared ] || { say "Android will ask for file access: tap Allow."; termux-setup-storage || true; }
  ;;
macos)
  P=~/Library/LaunchAgents/$LABEL.plist
  mkdir -p ~/Library/LaunchAgents
  cat > $P <<EOS
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/sh</string><string>-c</string><string>cd $APP &amp;&amp; exec $CMD</string></array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$DATA/core.log</string>
</dict></plist>
EOS
  launchctl load $P
  ;;
linux)
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    $SUDO tee /etc/systemd/system/$UNIT.service >/dev/null <<EOS
[Unit]
Description=Shoal hub
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
User=$(id -un)
WorkingDirectory=$APP
ExecStart=$CMD
Restart=always
RestartSec=3
# The hub is a control plane: keep it bounded and behind the device's real work.
MemoryMax=300M
TasksMax=128
CPUWeight=50
Nice=5

[Install]
WantedBy=multi-user.target
EOS
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable --now $UNIT
    $SUDO systemctl restart $UNIT
  else
    say "without systemd: start it with  cd $APP && $CMD"
    ( cd "$APP" && nohup $CMD >> "$DATA/core.log" 2>&1 & )
  fi
  ;;
esac

if [ -n "$FIRST" ]; then
  say "6/6 done. Open https://<this device>.<your tailnet>.ts.net/hub/ (or http://<this device>:$PORT/hub/),"
  say "    then add your other devices from Control > Network. To update: git pull here."
else
  say "6/6 done: it appears in the Mesh menu of every hub within a minute and updates itself."
fi
