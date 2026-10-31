#!/bin/sh
# Install Heimdall as a service that starts at boot and restarts if it stops.
#
#   ./install.sh                         http://heimdall.local:8765
#   ./install.sh --address 192.168.1.10  http://heimdall.local, on its own address (for when port 80 is taken)
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

case "${1:-}" in
  --uninstall)
    sudo systemctl disable --now heimdall 2>/dev/null || true
    sudo rm -f "$UNIT" && sudo systemctl daemon-reload
    echo "Heimdall service removed. Your logs and settings in $DIR are untouched."
    exit 0 ;;
  --address) ADDRESS=${2:?"--address needs an address, e.g. 192.168.1.10"} ;;
  "") ;;
  *) sed -n '2,9p' "$0"; exit 1 ;;
esac

python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "Heimdall needs Python 3.11 or newer."; exit 1; }
command -v ip >/dev/null && command -v ping >/dev/null || { echo "Heimdall needs the ip and ping commands (Linux)."; exit 1; }
python3 -c 'import dbus' 2>/dev/null || echo "Note: without python3-dbus, heimdall.local won't be announced (sudo apt install python3-dbus)."

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
ExecStart=/usr/bin/env python3 $DIR/server.py
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
sudo systemctl enable --now heimdall
sleep 2
systemctl --no-pager --lines=0 status heimdall | head -3
echo
echo "Heimdall is running and will start at boot. Open: http://heimdall.local$([ -n "$ADDRESS" ] || echo ":$PORT")"
echo "Logs: journalctl -u heimdall -f    Stop: sudo systemctl stop heimdall    Remove: ./install.sh --uninstall"
