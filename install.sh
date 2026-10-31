#!/bin/sh
# Install Heimdall as a service that starts at boot and restarts if it stops.
#
#   ./install.sh                         http://heimdall.local:8765
#   ./install.sh --address 192.168.1.10  http://heimdall.local, on its own address (for when port 80 is taken)
#   ./install.sh --api-key               also let Claude write reports (asks for an Anthropic API key);
#                                        options combine, and a re-run keeps an earlier --address
#   ./install.sh --forget-api-key        stop using Claude and delete the stored key
#   ./install.sh --uninstall
#
# Heimdall runs as its own user, heimdall, not as you or root. Its code is copied to /opt/heimdall
# (read-only to it: run this again after a git pull to update) and it keeps its logs and settings in
# /var/lib/heimdall. sudo is used to set that up, and with --address to add that address and send its
# port 80 to Heimdall each time the service starts.
set -eu

DIR=$(cd "$(dirname "$0")" && pwd)
UNIT=/etc/systemd/system/heimdall.service
APP=/opt/heimdall          # code: owned by root, so the service can't change what it runs
DATA=/var/lib/heimdall     # logs, state, trends and heimdall.toml: owned by the heimdall user
ME=$(id -un)
ADDRESS=""
API_KEY=""
KEY_FILE=/etc/heimdall/anthropic_key   # root-only at rest; systemd hands it to the service (LoadCredential), never via the environment

while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall)
      sudo systemctl disable --now heimdall 2>/dev/null || true
      sudo rm -f "$UNIT" && sudo systemctl daemon-reload
      echo "Heimdall service removed. Its logs and settings are kept in $DATA, the code in $APP;"
      echo "delete them with: sudo rm -rf $DATA $APP && sudo userdel heimdall"
      exit 0 ;;
    --address) ADDRESS=${2:?"--address needs an address, e.g. 192.168.1.10"}; shift ;;
    --api-key) API_KEY=1 ;;
    --forget-api-key)
      sudo rm -f "$KEY_FILE"
      sudo rmdir /etc/heimdall 2>/dev/null || true
      echo "Stored key deleted. Also revoke it at platform.claude.com (API keys) if you won't use it again."
      echo "Re-running the install so Heimdall stops using it." ;;
    *) sed -n '2,14p' "$0"; exit 1 ;;
  esac
  shift
done
# a re-run (say, to add a key) keeps the address an earlier --address set up, rather than quietly dropping it
if [ -z "$ADDRESS" ] && [ -f "$UNIT" ]; then
  ADDRESS=$(sed -n 's/^Environment=MDNS_IP=//p' "$UNIT" | head -1)
  [ -n "$ADDRESS" ] && echo "Keeping heimdall.local on its own address, $ADDRESS, as set up before."
fi

python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "Heimdall needs Python 3.11 or newer."; exit 1; }
command -v ip >/dev/null && command -v ping >/dev/null || { echo "Heimdall needs the ip and ping commands (Linux)."; exit 1; }
python3 -c 'import dbus' 2>/dev/null || echo "Note: without python3-dbus, heimdall.local won't be announced (sudo apt install python3-dbus)."
# Heimdall pings as an ordinary user: fine where the system lets everyone ping (Ubuntu, Debian, Raspberry Pi OS do)
case "$(sysctl -n net.ipv4.ping_group_range 2>/dev/null | tr -s ' \t' ' ')" in
  "1 0"|"") echo "Note: this system doesn't let ordinary users ping, which Heimdall needs (sysctl net.ipv4.ping_group_range)." ;;
esac

# its own user: can't log in, owns nothing but its data. You're added to its group, to read the logs.
if ! id heimdall >/dev/null 2>&1; then
  sudo useradd --system --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin heimdall
  echo "Created the heimdall user."
fi
id -nG "$ME" | grep -qw heimdall || { sudo usermod -aG heimdall "$ME"; echo "Added you to the heimdall group (log in again to read $DATA)."; }

# the code, copied where only root can change it
sudo install -d -m 755 -o root -g root "$APP"
sudo install -m 644 -o root -g root "$DIR/server.py" "$DIR/index.html" "$APP/"

# data: moved over from the repo the first time (earlier versions kept it next to server.py)
sudo install -d -m 750 -o heimdall -g heimdall "$DATA"
if ! sudo test -e "$DATA/state.json" && ! sudo test -e "$DATA/log.csv"; then
  MOVED=""
  for f in log.csv log.csv.old devices.csv devices.csv.old state.json trends.json heimdall.toml heimdall.toml.bak; do
    [ -f "$DIR/$f" ] || continue
    sudo systemctl stop heimdall 2>/dev/null || true   # nothing may write to it while it moves
    sudo install -m 640 -o heimdall -g heimdall "$DIR/$f" "$DATA/$f" && rm -f "$DIR/$f"
    MOVED="$MOVED $f"
  done
  [ -n "$MOVED" ] && echo "Moved to $DATA:$MOVED"
fi
PORT=$(sudo sed -n 's/^ *port *= *\([0-9]*\).*/\1/p' "$DATA/heimdall.toml" 2>/dev/null | head -1)
PORT=${PORT:-8765}

if [ -n "$API_KEY" ] || sudo test -f "$KEY_FILE"; then
  # Claude reports need the anthropic package: in a private environment next to the code, owned by root
  if ! sudo test -x "$APP/.venv/bin/pip"; then   # no pip means a half-made environment from a failed try: start again
    sudo rm -rf "$APP/.venv"
    VENV_PKG=python$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')-venv
    sudo python3 -m venv --system-site-packages "$APP/.venv" >/dev/null 2>&1 || {
      sudo rm -rf "$APP/.venv"
      echo "Couldn't make a Python environment. Install $VENV_PKG (sudo apt install $VENV_PKG), then run this again."; exit 1; }
  fi
  # pinned: the package runs with the key, so it changes only when Heimdall is updated to a checked version
  sudo "$APP/.venv/bin/pip" install --quiet "anthropic==2.3.0" || { echo "Couldn't install the anthropic package."; exit 1; }
fi
if [ -n "$API_KEY" ]; then
  # typed without showing on screen, never on the command line, so it stays out of shell history and ps
  printf "Anthropic API key (from platform.claude.com, won't show as you type): "
  trap "stty echo" EXIT INT; stty -echo; read -r KEY; stty echo; echo
  # letters, digits, - and _ only: anything else (a stray control character from the paste) would end up in error messages
  if ! printf %s "$KEY" | grep -Eqx 'sk-ant-[A-Za-z0-9_-]+'; then
    unset KEY; echo "That doesn't look like an Anthropic API key (sk-ant- then letters, digits, - and _). Nothing was saved."; exit 1
  fi
  sudo install -d -m 700 -o root -g root /etc/heimdall
  printf %s "$KEY" | sudo sh -c "umask 077; cat > $KEY_FILE"
  unset KEY
  echo "Key stored in $KEY_FILE (readable by root only)."
fi
PY="/usr/bin/python3"
sudo test -x "$APP/.venv/bin/pip" && PY="$APP/.venv/bin/python"
if sudo test -f "$KEY_FILE"; then
  EXTRA_KEY="LoadCredential=anthropic_key:$KEY_FILE"
else
  EXTRA_KEY=""
fi

DEV=$(ip -4 route show default | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1)
EXTRA=""
if [ -n "$ADDRESS" ]; then
  PREFIX=$(ip -4 -o addr show dev "$DEV" | sed -n 's/.*inet [0-9.]*\/\([0-9]*\).*/\1/p' | head -1)
  if ping -c 1 -W 1 "$ADDRESS" >/dev/null 2>&1 && ! ip -4 -o addr | grep -q "inet $ADDRESS/"; then
    echo "$ADDRESS is already in use by another device. Pick a free one, outside your router's handout range."; exit 1
  fi
  NAT="-d $ADDRESS -p tcp --dport 80 -j REDIRECT --to-ports $PORT"
  # "+" runs these as root even though Heimdall itself runs as the heimdall user
  EXTRA="Environment=MDNS_IP=$ADDRESS
ExecStartPre=+/bin/sh -c 'ip addr replace $ADDRESS/$PREFIX dev $DEV; for c in PREROUTING OUTPUT; do iptables -t nat -C \$c $NAT 2>/dev/null || iptables -t nat -A \$c $NAT; done'
ExecStopPost=+/bin/sh -c 'for c in PREROUTING OUTPUT; do iptables -t nat -D \$c $NAT 2>/dev/null; done; ip addr del $ADDRESS/$PREFIX dev $DEV 2>/dev/null; true'"
fi

# ports below 1024 normally need root; grant just that one ability
if [ "$PORT" -lt 1024 ]; then
  EXTRA="$EXTRA
AmbientCapabilities=CAP_NET_BIND_SERVICE"
fi

sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=Heimdall home network health dashboard
Wants=network-online.target
After=network-online.target avahi-daemon.service

[Service]
User=heimdall
Group=heimdall
StateDirectory=heimdall
StateDirectoryMode=0750
WorkingDirectory=$DATA
Environment=HEIMDALL_DATA=$DATA
ExecStart=$PY $APP/server.py
$EXTRA_KEY
# show messages in journalctl straight away, not when a buffer fills
Environment=PYTHONUNBUFFERED=1
$EXTRA
# Heimdall only needs to ping, read the network and write its own data, so it gets nothing else:
# no home folders, a read-only system, no way to gain privileges. Files it writes are readable by its
# group (you) but not by everyone: heimdall.toml holds the alert address, the secret part for ntfy.
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
NoNewPrivileges=yes
RestrictSUIDSGID=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
LockPersonality=yes
UMask=0027
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# let devices at home through the firewall, if ufw is on (nothing from outside your network)
if sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  NETWORK=$(ip -4 route show dev "$DEV" scope link | awk 'NR==1{print $1}')
  sudo ufw allow from "$NETWORK" to any port "$PORT" proto tcp comment Heimdall >/dev/null
fi

sudo systemctl daemon-reload
# restart, not just start: on a re-run the old settings would otherwise keep running
sudo systemctl enable heimdall
sudo systemctl restart heimdall
sleep 2
systemctl --no-pager --lines=0 status heimdall | head -3
echo
echo "Heimdall is running as the heimdall user and will start at boot. Open: http://heimdall.local$([ -n "$ADDRESS" ] || echo ":$PORT")"
echo "Settings and logs: $DATA    Code: $APP (run ./install.sh again after a git pull to update)"
echo "Logs: journalctl -u heimdall -f    Stop: sudo systemctl stop heimdall    Remove: ./install.sh --uninstall"
