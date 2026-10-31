# heimdall

A small, always-on dashboard for your home network. Open it from any phone or
computer at home and it tells you, in plain English, whether the router, the
wifi points, your devices and the internet are healthy, and which one to blame
when things lag.

- **Verdict**: one line each for the router, wifi points, devices and internet
- **Wifi mesh**: every Nest / Google Wifi unit, with response time, uptime,
  restarts and how it joins the mesh
- **Devices**: everything switched on, grouped by type, with its maker, how
  quickly it answers and whether it's struggling. Phones and gadgets that nap
  aren't flagged for being slow
- **Live lines**: response time of the router and the internet over the last hour
- **Comings and goings**: devices joining, leaving and restarting

No installs beyond Python, no accounts, nothing leaves your network except a
ping to a couple of public servers.

## Quick start

```sh
git clone https://github.com/anipatke/heimdall.git && cd heimdall
python3 server.py
```

Then open **http://heimdall.local:8765** from any device at home (or the
address it prints). It works out your router and network by itself.

### Keep it running

```sh
./install.sh
```

This installs a service that starts at boot and restarts if it stops. Heimdall
runs as you, not root; sudo is only used to install the service and, if ufw is
on, to let devices at home (and nothing outside) reach the dashboard.

**Want plain `http://heimdall.local`, without `:8765`?** If nothing else on the
machine uses port 80, set `port = 80` in `heimdall.toml` and run `./install.sh`. If something already
does, give Heimdall its own address instead. Pick one that's free and outside
the range your router hands out:

```sh
./install.sh --address 192.168.1.10
```

Remove it all with `./install.sh --uninstall`.

## Settings

Everything is optional. Copy `heimdall.example.toml` to `heimdall.toml` and
change only what you need:

| Setting | Default | What it does |
|---|---|---|
| `port` | 8765 | Port the dashboard listens on |
| `host` | all addresses | Listen on one address only |
| `mdns_name` | `heimdall.local` | Name other devices find it by; `""` turns it off |
| `mdns_ip` | this machine | Address that name points at |
| `router` | detected | Your router's address |
| `network` | detected | Address range to look for devices in (up to 1024 addresses) |
| `device_every` | 60 | Seconds between checks of known devices |
| `sweep_every` | 600 | Seconds between sweeps for new devices |
| `[checks.internet]` | Cloudflare, Google | Servers that stand in for "the internet" |
| `[[kinds]]` | | Your own rules for naming device types |

Environment variables `PORT`, `HOST`, `ROUTER`, `MDNS_NAME` and `MDNS_IP`
override the file.

## What it does to your network

Heimdall is meant to stay on, so it's deliberately light: about 7 small pings a
second in total, a few kbit/s.

| Check | How often | Traffic |
|---|---|---|
| Router, wifi points, internet | every second | 1 ping each |
| Website lookups | every 5 s | 1 lookup |
| Known devices | every minute | 3 pings each, 8 at a time |
| Sweep for new devices | every 10 min | 1 ping per unused address |
| Wifi mesh status | every minute | 1 small request per Nest / Google unit |
| Dashboard page | every 2 s (5 s on phones) | only the new readings |

The speed test downloads 25 MB, so it briefly uses your whole connection. It
only runs when someone presses the button.

## What it can and can't see

It sees which devices are switched on, when they come and go, who made them and
how quickly they answer. It can't see what they do online or how much data each
uses; your router keeps that to itself. Readings are logged to `log.csv` and
`devices.csv` on the machine running Heimdall, and never sent anywhere.

## Requirements and limits

- Linux with Python 3.11+, `ping` and `ip`. `iw` adds wifi signal details;
  avahi and `python3-dbus` (standard on Ubuntu desktop) give it its `.local` name
- Best on a machine plugged into the router by cable. On wifi, every reading
  includes Heimdall's own wifi hop
- The wifi mesh panel needs Google / Nest Wifi; with other routers it shows
  ping results only
- Devices with randomised "private" wifi addresses (most phones) can't be traced
  to a maker

## License

MIT
