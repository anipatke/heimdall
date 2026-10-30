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
Set `ROUTER=192.168.x.1` if your router address is different, and `PORT=...` to change the port.

Once a minute it also sweeps the network and lists every device that answers,
how quickly each responds, and when devices join or leave. It cannot see what
other devices are doing online - only that they're there and how responsive
they are.

## Run it on a home server as heimdall.local (Ubuntu)

```sh
git clone https://github.com/anipatke/heimdall.git && cd heimdall
sudo apt install avahi-utils             # lets other devices find heimdall.local
avahi-publish -a -R heimdall.local "$(hostname -I | awk '{print $1}')" &
sudo PORT=80 python3 server.py           # port 80 so the address needs no :8765
```

Then open http://heimdall.local from any device at home. Best results when
the server is plugged into the main router by cable.
