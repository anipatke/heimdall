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
Set `ROUTER=192.168.x.1` if your router address is different.
