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
# Runs Heimdall as you, not root. Uses sudo only to install the service and, with --address,
# to add that address and send its port 80 to Heimdall each time the service starts.
set -eu

DIR=$(cd "$(dirname "$0")" && pwd)
UNIT=/etc/systemd/system/heimdall.service
USER_NAME=$(id -un)
PORT=$(sed -n 's/^ *port *= *\([0-9]*\).*/\1/p' "$DIR/heimdall.toml" 2>/dev/null | head -1)
PORT=${PORT:-8765}
ADDRESS=""
API_KEY=""
KEY_FILE=/etc/heimdall/anthropic_key   # root-only at rest; systemd hands it to the service (LoadCredential), never via the environment

while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall)
      sudo systemctl disable --now heimdall 2>/dev/null || true
      sudo rm -f "$UNIT" && sudo systemctl daemon-reload
      echo "Heimdall service removed. Your logs and settings in $DIR are untouched."
      exit 0 ;;
    --address) ADDRESS=${2:?"--address needs an address, e.g. 192.168.1.10"}; shift ;;
    --api-key) API_KEY=1 ;;
    --forget-api-key)
      sudo rm -f "$KEY_FILE"
      sudo rmdir /etc/heimdall 2>/dev/null || true
      echo "Stored key deleted. Also revoke it at platform.claude.com (API keys) if you won't use it again."
      echo "Re-running the install so Heimdall stops using it." ;;
    *) sed -n '2,12p' "$0"; exit 1 ;;
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

if [ -n "$API_KEY" ]; then
  # Claude reports need the anthropic package: kept in a private environment here, not installed system-wide
  if [ ! -x "$DIR/.venv/bin/pip" ]; then   # no pip means a half-made environment from a failed try: start again
    rm -rf "$DIR/.venv"
    VENV_PKG=python$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')-venv
    python3 -m venv --system-site-packages "$DIR/.venv" >/dev/null 2>&1 || {
      rm -rf "$DIR/.venv"
      echo "Couldn't make a Python environment. Install $VENV_PKG (sudo apt install $VENV_PKG), then run this again."; exit 1; }
  fi
  # pinned: the package runs with the key, so it changes only when Heimdall is updated to a checked version
  "$DIR/.venv/bin/pip" install --quiet "anthropic==2.3.0" || { echo "Couldn't install the anthropic package."; exit 1; }
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
PY="/usr/bin/env python3"
[ -x "$DIR/.venv/bin/pip" ] && PY="$DIR/.venv/bin/python"
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
  # "+" runs these as root even though Heimdall itself runs as you
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
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=$PY $DIR/server.py
$EXTRA_KEY
# show messages in journalctl straight away, not when a buffer fills
Environment=PYTHONUNBUFFERED=1
$EXTRA
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
echo "Heimdall is running and will start at boot. Open: http://heimdall.local$([ -n "$ADDRESS" ] || echo ":$PORT")"
echo "Logs: journalctl -u heimdall -f    Stop: sudo systemctl stop heimdall    Remove: ./install.sh --uninstall"
