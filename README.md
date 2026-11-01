# Heimdall

**Is it the wifi, the router, or the internet?** Heimdall is a small, always-on
dashboard for your home network that answers that in plain English.

![Heimdall dashboard: a verdict for the router, wifi points, devices and internet, above a card for each wifi point](docs/screenshot.png)

Run it on any Linux machine at home, open `http://heimdall.local:8765` from your
phone, and you get:

- **A verdict**: one line each for the router, wifi points, devices and the
  internet, saying which one is to blame when things lag
- **Wifi mesh**: every Google / Nest Wifi unit, with response time, uptime,
  restarts and how it joins the mesh
- **Devices**: everything switched on, grouped by type, with its maker and how
  quickly it answers. Phones and gadgets that nap to save battery aren't flagged
  as slow
- **Live lines**: response time of the router, wifi points and internet over the last hour
- **Comings and goings**: devices joining, leaving and restarting
- **A speed test**, on demand
- **Day and week reports**: what went wrong, where and when, compared with the day or week before
  (`#day` and `#week`), and optionally a written report from Claude
- **Alerts** on WhatsApp, Signal or ntfy when the internet drops, a wifi point
  stops answering or restarts, or a new device joins (optional)

It's one Python file and one HTML page. No packages to install, no accounts, no
cloud. Nothing leaves your network except pings and a few website lookups (DNS) to check
the internet is up.

## Quick start

You need Linux with Python 3.11 or newer (Ubuntu 24.04+, Debian 12+, Raspberry
Pi OS Bookworm). On Ubuntu or Debian, these extras give it device makers, wifi
details and the `heimdall.local` name:

```sh
sudo apt install ieee-data iw avahi-daemon python3-dbus
```

Then:

```sh
git clone https://github.com/anipatke/heimdall.git && cd heimdall
python3 server.py
```

Open **http://heimdall.local:8765** from any device at home, or the address it
prints. It finds your router and network by itself. The first scan for devices
takes about a minute.

If the page won't open from other devices but works on the machine itself, a
firewall is in the way. `./install.sh` below sorts that out for ufw.

### Keep it running

```sh
./install.sh
```

This installs a systemd service that starts at boot and restarts if it stops.
Heimdall runs as its own user, `heimdall`: not as you, and not as root. It's locked down to
what it needs (pinging and reading the network) and can't touch your home folder:

- **Code** is copied to `/opt/heimdall`, owned by root, so Heimdall can't change what it runs.
  After a `git pull`, run `./install.sh` again to update it.
- **Logs, settings and saved state** live in `/var/lib/heimdall`. The first install moves
  them there from the repo. You're added to the `heimdall` group, so you can read them
  (after logging in again) without sudo.

sudo is only used to set this up and, if ufw is on, to let devices at home (and nothing
outside) reach the dashboard.

Logs: `journalctl -u heimdall -f`. Remove the service: `./install.sh --uninstall` (it
says how to delete the data too).

**Want plain `http://heimdall.local`, without `:8765`?** If nothing else on the
machine uses port 80, set `port = 80` in `heimdall.toml` (`/var/lib/heimdall/heimdall.toml`
once installed) and run `./install.sh`.
If something already does, give Heimdall its own address. Pick one that's free
and outside the range your router hands out:

```sh
./install.sh --address 192.168.1.10
```

### Where to run it

Use a machine plugged into the router by cable: a spare laptop, a mini PC or a
Raspberry Pi. It works over wifi, but then every reading includes Heimdall's own
wifi hop, and the dashboard says so.

## Settings

Everything is optional. The easiest way to change things is **⚙ Settings** on
the dashboard: how often each check runs, what counts as slow, alerts, wifi point
names and log size. Saving writes `heimdall.toml` (keeping the previous one as
`heimdall.toml.bak`) and Heimdall restarts itself to apply it. Installed as a service, it
lives in `/var/lib/heimdall`; run by hand, next to `server.py`. There's no login,
so anyone on your home network can change these.

You can also edit `heimdall.toml` by hand; `heimdall.example.toml` lists every
setting. Port, address and router can only be changed there, since a mistake
could lock you out of the page:

| Setting | Default | What it does |
|---|---|---|
| `port` | 8765 | Port the dashboard listens on |
| `host` | all addresses | Listen on one address only |
| `mdns_name` | `heimdall.local` | Name other devices find it by; `""` turns it off |
| `mdns_ip` | this machine | Address that name points at |
| `allowed_hosts` | its own names | Extra names it may be opened by (see Privacy) |
| `router` | detected | Your router's address |
| `network` | detected | Address range to look for devices in (up to 1024 addresses) |
| `ping_every` | 1 | Seconds between pings to the router, internet and wifi points |
| `dns_every` | 5 | Seconds between website lookup timings |
| `mesh_every` | 60 | Seconds between reads of each wifi point's status |
| `device_every` | 60 | Seconds between checks of known devices |
| `pings_per_check` | 3 | Pings per device check |
| `sweep_every` | 600 | Seconds between sweeps for new devices |
| `speedtest_mb` | 25 | Speed test download size |
| `dns_names` | a few big sites | Names used to time website lookups |
| `[checks.internet]` | Cloudflare, Google | Servers that stand in for "the internet" |
| `[thresholds]` | see example | What counts as good, fair and poor |
| `[alerts]` | off | Where to send alerts, which ones, and after how long; see below |
| `[logs] max_mb` | 100 | Size each log can reach before it's rotated |
| `[names]` | | Your own names for devices and wifi points, by address |
| `[[kinds]]` | | Your own rules for naming device types |

Environment variables `PORT`, `HOST`, `ROUTER`, `MDNS_NAME` and `MDNS_IP`
override the file, and `HEIMDALL_CONFIG` points at a different settings file.

Devices are named from what they call themselves on the network. If yours show
up as "Unknown", add a `[[kinds]]` rule. For example, `match = "^living-room"`
with `kind = "TV"`.

## Alerts

Heimdall can message you when:

- the internet has been down for a minute (and when it's back, with how long it lasted)
- a wifi point hasn't answered for 2 minutes (and when it's back)
- the router or a wifi point restarts
- a device it has never seen before joins your network
- every morning, a summary of the last 24 hours; on Mondays, of the last week (8 am by default)

If Heimdall's own connection drops, it stays quiet rather than blaming everything else.

Add a `url` under `[alerts]` in `heimdall.toml`. Any `{text}` in it is replaced with the
message; without `{text}`, the message is sent as the body of a POST.

**WhatsApp or Signal** via [CallMeBot](https://www.callmebot.com), free for personal use:
follow their instructions to send their number a message from your phone, and they reply with
your key. Then:

```toml
[alerts]
url = "https://api.callmebot.com/whatsapp.php?phone=+61400000000&apikey=123456&text={text}"
# Signal: https://signal.callmebot.com/signal/send.php?phone=+61400000000&apikey=123456&text={text}
```

**ntfy**: install the [ntfy app](https://ntfy.sh), subscribe to a topic name nobody would
guess, then `url = "https://ntfy.sh/that-topic"`.

Or paste the address into **⚙ Settings → Alerts** and press **Send test**. You can
also pick which alerts you want there. From the command line: `python3 server.py --test-alert`, then restart Heimdall
(`sudo systemctl restart heimdall`). Alert text goes through whichever service you choose.

## Day and week reports

**Last 24 h** and **Last 7 days** at the top of the dashboard look back over what Heimdall has
seen. Every reading is rolled up by the hour as it comes in and kept for two weeks in
`trends.json`. The first time, Heimdall builds this from the logs it already has.

Each view gives:

- **Findings** in plain English: whether trouble was inside your home (the router itself went
  slow or silent) or your provider's (the internet dropped while the router was fine), how long
  it lasted and when, which wifi point is weakest, devices dropping off together, restarts, and
  how it compares with the day or week before
- **When things went wrong**: each hour's share of slow or lost replies; for a week, a grid of
  days by hours, which shows up problems that come back at the same time each day
- **Slowest replies each hour**, a table per wifi point, the worst moments, and always-on
  devices that miss replies

The same findings go out as the daily and weekly summaries if alerts are on, and print in a
terminal with `python3 server.py --report day` (or `week`). Installed as a service, point it
at the service's data: `HEIMDALL_DATA=/var/lib/heimdall python3 server.py --report day`.

### A written report from Claude (optional)

Claude can read the same figures and write you a short report: what happened, the likely
causes, and what to try. It writes one for the last 24 hours each morning, and one for the
week on Mondays, at the summary hour (8 am by default), plus one of each the first time
Heimdall starts with a key. They appear on the Day and Week views, and each one's opening
paragraph replaces the rule-based summary on your phone. There's no button to ask for more,
so the cost stays predictable: about 37 reports a month.

This is the one feature that sends data about your network out of your home. Each report
sends Anthropic that stretch's hourly figures, problem times, device names and addresses on
your network (not hardware addresses). It needs an
[Anthropic API key](https://platform.claude.com) and costs about 10¢ a report on the
default Claude Opus 5.5 at high effort (a few dollars a month). Sonnet 5.5 costs about
half that, Haiku 5.5 well under a cent, and lower effort costs less again: pick both in
**⚙ Settings → Claude reports**. Use a key from a workspace of its
own with a monthly spend limit, so the cost is capped whatever happens.

```sh
./install.sh --api-key
```

This asks for the key without showing it, so it never lands in your shell history. It stores
the key in `/etc/heimdall/anthropic_key`, readable by root only, and systemd hands it to the
Heimdall service as a credential (`LoadCredential`), not as an environment variable. While
Heimdall runs, only root and the `heimdall` user can read it: not you, and not other programs
you run. Still, make it a key just for Heimdall, from a workspace with a spending limit at
platform.claude.com. It also installs a pinned version of the `anthropic` package into
`/opt/heimdall/.venv`, owned by root, not system-wide. `./install.sh --forget-api-key` deletes the key and turns reports off;
revoke it at platform.claude.com too.

Nobody on your network ever sees the key, or can make Heimdall use it: the dashboard only
shows the reports and says whether they're on.

The key is never read from `heimdall.toml`, which isn't meant for secrets. To run by
hand with reports, set the key without it landing in your shell history, then start Heimdall:

```sh
read -rs ANTHROPIC_API_KEY && export ANTHROPIC_API_KEY   # paste the key, then Enter
python3 server.py      # needs the anthropic package: pip install anthropic==2.3.0 in a venv
```
The model and effort can also be set in `heimdall.toml`, as can turning reports off while keeping the key:

```toml
[ai]
model = "claude-sonnet-5-5"   # default claude-opus-5-5; claude-haiku-5-5 is cheapest
effort = "medium"             # low, medium, high (default), xhigh or max
# on = false
```

## What it does to your network

Heimdall is meant to stay on, so it's deliberately light: about 7 small pings a
second in total, a few kbit/s.

| Check | How often | Traffic |
|---|---|---|
| Router, wifi points, internet | every second | 1 ping each |
| Website lookups | every 5 s | 1 lookup |
| Known devices | every minute | 3 pings each, 8 at a time |
| Sweep for new devices | every 10 min | 1 ping per unused address |
| Wifi mesh status | every minute | 1 small request per Google / Nest unit |
| Dashboard page | every 2 s (5 s on phones) | only the new readings |

The speed test downloads 25 MB from Cloudflare, so it briefly uses your whole
connection. It only runs when someone presses the button.

## Privacy

Heimdall sees which devices are switched on, when they come and go, who made
them and how quickly they answer. It can't see what they do online or how much
data each uses; your router keeps that to itself.

Readings are kept on the machine running Heimdall, in `log.csv`, `devices.csv`,
`state.json` and `trends.json` (in `/var/lib/heimdall` when installed as a service, readable
by Heimdall and the `heimdall` group only; next to `server.py` when run by hand), and never
sent anywhere. If you turn on
alerts, only the alert messages leave your network. If you add an Anthropic API key, a
summary of each report's figures goes to Anthropic too (see
[A written report from Claude](#a-written-report-from-claude-optional)). Each log is
capped at 100 MB (`[logs] max_mb`). When it fills up it's renamed to `.old`, replacing the
previous one, so the logs never take more than about 400 MB.

The dashboard has no login. Anyone on your home network can open it, see your
device names and change settings, though not the saved alert address, which is
never shown. Don't expose it to the internet.

Heimdall only answers when it's opened by its own name or address
(`heimdall.local`, this machine's name and addresses, `localhost`). That stops
a website from pointing a name of its own at your network to reach it through
your browser. If you open it some other way, for example a name your router or
VPN gives this machine, add that name to `allowed_hosts`.

## Limits

- **Linux only.** It relies on the `ping` and `ip` commands, with Python 3.11+
  (`ping` from iputils, as on Ubuntu, Debian and Raspberry Pi OS)
- **Mesh details need Google / Nest Wifi.** With other routers you still get the
  verdict, devices and pings, just not the per-unit status panel
- **Phones are hard to identify.** Most use randomised "private" wifi addresses,
  which can't be traced to a maker; they're named from what they announce instead
- **IPv4 only**

## Troubleshooting

| Problem | Try |
|---|---|
| `heimdall.local` doesn't open | Use the address it prints. Check `python3-dbus` is installed and avahi is running (`systemctl status avahi-daemon`). Some older Android phones don't support `.local` names |
| Works on the machine, not from other devices | A firewall: run `./install.sh`, or allow the port yourself |
| "Couldn't find your router" | The machine has no default route. Set `router` in `heimdall.toml` |
| Devices show no maker | Install `ieee-data`. If it is only newer gadgets, the list is out of date: run `sudo update-ieee-data`, then restart Heimdall |
| Everything looks slow | Is Heimdall on wifi? Check the note under the verdict |

## How it works

Two files, standard library only (the optional Claude reports use the `anthropic` package):

- **`server.py`** runs a handful of background threads (pings, DNS timing, device
  scans, mesh status) and serves the page plus a `/data` endpoint the page polls
  for new readings. `tally` rolls every reading into hourly buckets, and `trend_report` /
  `findings` turn them into the `/trends` endpoint and the summaries
- **`index.html`** is the whole dashboard: plain HTML, CSS and JavaScript with
  hand-drawn SVG charts, no build step

Common things to change when forking:

- **Device types**: `KINDS` and `VENDOR_KINDS` near the top of `server.py` map
  network names and makers to types
- **Other mesh systems**: `mesh_loop` reads each Google / Nest unit's local
  `/api/v1/status` page. Another router's status page could be read there in the same way
- **What counts as slow**: `EDITABLE` in `server.py` holds every setting's default
  and allowed range. The page reads the thresholds from the server, so the
  dashboard and alerts always agree

## License

MIT
