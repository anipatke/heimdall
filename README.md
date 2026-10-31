# heimdall

A small live dashboard that watches your home network for lag.

Every second it times replies from your router and from two internet servers
(Cloudflare and Google), checks how fast website names are looked up, and
reads this computer's wifi signal. Everything is also written to `log.csv` so
you can look back at when lag happened.

## Run it

```sh
python3 server.py
```

Then open http://localhost:8765. No installs needed (Python 3 standard library only).
Set `ROUTER=192.168.x.1` if your router address is different, `PORT=...` to change the port,
and `HOST=...` to listen on one address only (default: all of them).

Once a minute it also sweeps the network and lists every device that answers,
how quickly each responds, and when devices join or leave. It cannot see what
other devices are doing online - only that they're there and how responsive
they are.

## Run it on a home server as heimdall.local (Ubuntu)

Heimdall can announce its own name through avahi (already running on Ubuntu desktops),
so no extra installs and no root are needed for the name itself:

```sh
MDNS_NAME=heimdall.local python3 server.py    # then open http://heimdall.local:8765
```

To drop the `:8765` when something else already owns port 80, give Heimdall its own
address and send port 80 on that address to Heimdall (needs sudo, lost on reboot):

```sh
sudo ip addr add 192.168.86.10/24 dev wlp4s0     # pick a free address outside the router's handout range
sudo iptables -t nat -A PREROUTING -d 192.168.86.10 -p tcp --dport 80 -j REDIRECT --to-ports 8765
sudo iptables -t nat -A OUTPUT     -d 192.168.86.10 -p tcp --dport 80 -j REDIRECT --to-ports 8765
MDNS_NAME=heimdall.local MDNS_IP=192.168.86.10 python3 server.py
```

Then open http://heimdall.local from any device at home. Readings are sharpest when
the server is plugged into the main router by cable; on wifi every number includes
its own wifi hop.

Heimdall is built to stay on: it checks known devices once a minute, sweeps for new
ones every 10 minutes, and an open page only fetches what's new - a few pings a second
and a couple of kbit/s in total.
