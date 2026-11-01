#!/usr/bin/env python3
"""Heimdall - home network health watcher. Run: python3 server.py, then open http://localhost:8765"""
import shutil, sys
if sys.version_info < (3, 11):
    raise SystemExit("Heimdall needs Python 3.11 or newer.")
if not (shutil.which("ip") and shutil.which("ping")):
    raise SystemExit("Heimdall needs the ip and ping commands (Linux).")
import http.client, ipaddress, json, math, os, random, re, signal, socket, ssl, struct, subprocess, threading, time, tomllib, csv
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, urlsplit, parse_qs, quote
from urllib.request import urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
# Logs, saved state and settings: next to server.py, or (installed as a service) /var/lib/heimdall, where only the
# heimdall user can write and the code itself stays read-only to it
DATA = os.environ.get("HEIMDALL_DATA") or HERE
# ping and ip output is parsed, so keep it in English whatever the system language. Anthropic settings (an API key,
# if run by hand) stay out of every child process's environment.
C_LOCALE = {**{k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}, "LC_ALL": "C"}


def http_fetch(host, port, path, *, addr=None, https=False, data=None, timeout=5, limit=65536):
    """One HTTP request to a device at home or an alert service; returns (status, first `limit` bytes of the body).
    - timeout covers the whole request, not each read, so something sending a byte every few seconds can't hold us up
    - addr: connect to this already-checked address while still using `host` as the name (and for the https
      certificate), so a name can't point somewhere else between being checked and being connected to
    - never follows redirects or uses a proxy, which could also take the request somewhere unchecked"""
    if https:
        conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    if addr:
        def connect():
            conn.sock = socket.create_connection((addr, port), timeout)
            if https:
                conn.sock = conn._context.wrap_socket(conn.sock, server_hostname=host)
        conn.connect = connect

    sock = []  # our own hold on the socket: http.client lets go of it once the headers are in

    def cut():
        for s in sock:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    deadline = threading.Timer(timeout, cut)
    deadline.start()
    try:
        conn.connect()
        sock.append(conn.sock)
        conn.request("GET" if data is None else "POST", path, body=data)
        r = conn.getresponse()
        return r.status, r.read(limit)
    finally:
        deadline.cancel()
        conn.close()


def finite(text):
    """For json.loads: numbers from other devices must be real numbers, not Infinity or NaN (which the page can't read)."""
    v = float(text)
    if not math.isfinite(v):
        raise ValueError("not a finite number")
    return v


def no_constants(name):
    raise ValueError(f"{name} isn't allowed")


CONFIG = os.environ.get("HEIMDALL_CONFIG", os.path.join(DATA, "heimdall.toml"))

# What the settings page can change: (section, key) -> (default, lowest, highest). A list default means
# [good, fair, poor] limits: under each number is that level. Port, address and router stay file-only,
# since a mistake there could lock you out of the page.
EDITABLE = {
    ("checks", "ping_every"): (1, 0.2, 60),  # router, internet and wifi points; ping refuses faster without root
    ("checks", "dns_every"): (5, 1, 3600),
    ("checks", "mesh_every"): (60, 10, 3600),
    ("checks", "device_every"): (60, 15, 3600),
    ("checks", "pings_per_check"): (3, 1, 10),
    ("checks", "sweep_every"): (600, 60, 86400),
    ("checks", "speedtest_mb"): (25, 1, 200),
    ("thresholds", "router"): ([10, 30, 100], 0, 60000),
    ("thresholds", "router_wifi"): ([30, 80, 200], 0, 60000),  # Heimdall on wifi: every reading includes its own hop
    ("thresholds", "wifi_point_extra"): ([20, 40, 100], 0, 60000),  # added to the router's limits
    ("thresholds", "internet"): ([30, 60, 150], 0, 60000),
    ("thresholds", "loss"): ([0, 2, 5], 0, 100),  # % lost; good means at or under the first
    ("thresholds", "lookups"): (150, 1, 60000),
    ("thresholds", "device"): ([60, 150], 1, 60000),  # "a little slow", "slow to answer"
    ("thresholds", "device_missed"): ([10, 34], 1, 100),  # % missed: "missing replies", "not answering"
    ("alerts", "internet_after"): (60, 10, 86400),
    ("alerts", "offline_after"): (120, 10, 86400),
    ("alerts", "summary_hour"): (8, 0, 23),  # local hour the daily summary (and Monday's weekly one) is sent
    ("logs", "max_mb"): (100, 1, 2000),
}
ALL_ALERTS = ["internet", "wifi_points", "restarts", "new_devices", "daily_summary", "weekly_summary"]


def load_config():
    """Settings come from heimdall.toml (all optional), then environment variables. Anything unset is detected."""
    try:
        with open(CONFIG, "rb") as f:
            cfg = tomllib.load(f)
    except FileNotFoundError:
        cfg = {}
    flat = {**cfg.get("server", {}), **cfg.get("network", {}), **cfg.get("checks", {})}
    for key in ("port", "host", "router", "mdns_name", "mdns_ip"):
        if os.environ.get(key.upper()) or (key == "mdns_name" and "MDNS_NAME" in os.environ):  # MDNS_NAME="" turns it off
            flat[key] = os.environ[key.upper()]
    return flat, cfg.get("kinds", []), cfg.get("names", {}), cfg


def conf(section, key):
    """An editable setting: from heimdall.toml, or its default."""
    return RAW.get(section, {}).get(key, EDITABLE[(section, key)][0])


def default_route():
    """(router address, network interface) from this machine's default route."""
    out = subprocess.run(["ip", "-4", "route", "show", "default"], capture_output=True, text=True, env=C_LOCALE).stdout
    m = re.search(r"via (\S+) dev (\S+)", out)
    if not m:
        raise SystemExit("Couldn't find your router: this machine has no default route. Set router in heimdall.toml.")
    return m.group(1), m.group(2)


CFG, EXTRA_KINDS, NAMES, RAW = load_config()  # NAMES: address -> your own name for it, from [names]
_router, _dev = default_route()
PORT = int(CFG.get("port", 8765))
HOST = CFG.get("host", "0.0.0.0")
ROUTER = CFG.get("router", _router)
MDNS_NAME = CFG.get("mdns_name", "heimdall.local") or None  # announced to the home network; "" turns it off
# Kept gentle on purpose: a busy scan clogs Heimdall's own link and makes every reading look worse.
CHECK_EVERY = int(conf("checks", "device_every"))   # re-check devices we already know about (a few pings each, a few at a time)
SWEEP_EVERY = int(conf("checks", "sweep_every"))     # look for new devices across the whole address range (1 ping each)
PINGS_PER_CHECK = int(conf("checks", "pings_per_check"))
PING_EVERY = max(0.2, float(conf("checks", "ping_every")))
DNS_EVERY = float(conf("checks", "dns_every"))
MESH_EVERY = float(conf("checks", "mesh_every"))
SPEEDTEST_MB = float(conf("checks", "speedtest_mb"))
# What counts as good / fair / poor / bad. The page reads these too, so the dashboard and alerts agree.
THRESHOLDS = {k: conf(s, k) for s, k in EDITABLE if s == "thresholds"}
_s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
_s.connect((ROUTER, 9))  # no packet sent; just picks the address we'd reach the router from
MY_IP = _s.getsockname()[0]
_s.close()
MDNS_IP = CFG.get("mdns_ip", MY_IP)
# The home network, from the address and prefix on the router-facing interface. Sweeps stay within
# 1024 addresses: a bigger network gets the /22 around this machine.
_m = re.search(rf"inet ({re.escape(MY_IP)}/\d+)", subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True, env=C_LOCALE).stdout)
NET = ipaddress.ip_interface(CFG.get("network") or (_m.group(1) if _m else f"{MY_IP}/24")).network
if NET.num_addresses > 1024:
    NET = ipaddress.ip_interface(f"{MY_IP}/22").network
# every address this machine answers to (it may have a second one just for heimdall.local)
MY_IPS = set(re.findall(r"inet (\S+)/", subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True, env=C_LOCALE).stdout)) | {MY_IP}
# the same, IPv6 too, for keeping alerts from being sent back into this machine
OWN = {ipaddress.ip_address(a.split("%")[0]) for a in re.findall(r"inet6? (\S+)/", subprocess.run(
    ["ip", "-o", "addr"], capture_output=True, text=True, env=C_LOCALE).stdout)} | {ipaddress.ip_address(MY_IP)}
# (pattern in the device's network name, friendly type) - first match wins; heimdall.toml's [[kinds]] go first
KINDS = [(k["match"], k["kind"]) for k in EXTRA_KINDS] + [(r"nest-wifi-point|google-wifi", "Wifi point"), (r"google-home|nest-(mini|audio|hub)", "Smart speaker"),
         (r"sonos", "Sonos speaker"), (r"^esp_|tasmota|shelly", "Smart-home gadget"),
         (r"pixel|iphone|android|galaxy|oneplus|note\d|-s\d\d|-a\d\d", "Phone"), (r"ipad|tab", "Tablet"),
         (r"tizen|webos|roku|chromecast|tv", "TV"), (r"^brw|printer|epson|canon|^hp", "Printer"),
         (r"laundry|washer|dryer|fridge|dishwasher|daikin", "Appliance"), (r"macbook|laptop|desktop|pc", "Computer")]
# when the name gives nothing away, the maker sometimes does
VENDOR_KINDS = [(r"nintendo|sony interactive|xbox", "Games console"), (r"daikin", "Appliance"), (r"sonos", "Sonos speaker"),
                (r"brother|epson|canon", "Printer"), (r"espressif", "Smart-home gadget"), (r"microsoft|dell|lenovo|hewlett|asustek|giga-byte", "Computer")]
VENDORS = {}  # first 3 bytes of a hardware address -> maker, from the IEEE list Ubuntu ships
# `update-ieee-data` writes a fresh copy to /var/lib; the one in /usr/share is only as new as the package
for path in ("/var/lib/ieee-data/oui.txt", "/usr/share/ieee-data/oui.txt"):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            VENDORS = {m.group(1).replace("-", ":").lower(): m.group(2).strip() for line in f if (m := re.match(r"^([0-9A-F]{2}-[0-9A-F]{2}-[0-9A-F]{2})\s+\(hex\)\s+(.+)", line))}
        break
    except OSError:
        pass


def vendor_of(mac):
    if not mac or int(mac[:2], 16) & 2:
        return None  # randomised "private" address, as phones use: no maker to find
    return VENDORS.get(mac[:8].lower())

# what "the internet" means: a couple of big, always-up servers
TARGETS = {"router": ROUTER, **CFG.get("internet", {"cloudflare": "1.1.1.1", "google": "8.8.8.8"})}
INET0 = next((k for k in TARGETS if k != "router"), None)  # "the internet" for charts and trends
DNS_NAMES = CFG.get("dns_names", ["google.com", "netflix.com", "amazon.com", "bbc.co.uk", "github.com", "reddit.com", "spotify.com"])
KEEP = 3600  # seconds of history held in memory
LOG = os.path.join(DATA, "log.csv")
DEVICE_LOG = os.path.join(DATA, "devices.csv")
SAVED = os.path.join(DATA, "state.json")  # device list kept across restarts
LOG_MAX = int(float(conf("logs", "max_mb")) * 1_000_000)  # a full log moves to .old, so each log takes at most twice this
ALERTS = RAW.get("alerts", {})  # url: where alerts go ({text} in it is filled in); on: which kinds of alert
ALERT_ON = set(ALERTS.get("on", ALL_ALERTS))
START = time.time()

lock = threading.Lock()
log_lock = threading.Lock()
series = {}           # kind -> deque of (t, ms or None)
state = {"link": None, "speed": None, "speed_running": False, "last_scan": None, "last_sweep": None}
devices = {}          # ip -> {ip, name, mac, kind, first, last, online, rtt: [(t, avg, max, lost)]}
events = []           # {t, ip, name, what: joined|left}
points = set()        # wifi points getting nonstop checks
last_ok = {}          # kind -> when it last answered
restored = False      # devices came from state.json, so anything new on the first scan really is new
mesh = {}             # ip -> what the router / wifi point says about itself (its local status page)


def append_log(path, rows):
    """Add rows to a CSV log. Heimdall runs for months, so a full log replaces the previous .old one."""
    with log_lock:
        try:
            if os.path.getsize(path) > LOG_MAX:
                os.replace(path, path + ".old")
        except OSError:
            pass
        with open(path, "a", newline="") as f:
            csv.writer(f).writerows(rows)


def record(kind, value):
    now = time.time()
    with lock:
        q = series.setdefault(kind, deque())
        q.append((now, value))
        while q and q[0][0] < now - KEEP:
            q.popleft()
        if value is not None:
            last_ok[kind] = now
    append_log(LOG, [[time.strftime("%Y-%m-%d %H:%M:%S"), kind, "" if value is None else value]])
    tally(now, kind, value)


def tail_rows(path, since):
    """CSV rows of a log written after `since`, oldest first, reading only the end of the file
    (and of the .old one when the current log is newer than that). Rows start with a local time stamp."""
    def stamp(line):
        try:
            return time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            return None
    out = []
    for p in (path, path + ".old"):
        try:
            with open(p, "rb") as f:
                end = start = f.seek(0, 2)
                lines = []
                while start > 0:  # step back a megabyte at a time until the first whole line is older than `since`
                    start = max(0, start - 1_000_000)
                    f.seek(start)
                    lines = f.read(end - start).decode("utf-8", "ignore").splitlines()[1 if start else 0:]  # drop a half line
                    if lines and (stamp(lines[0]) or since) < since:
                        break
        except OSError:
            continue
        out = [r for r in csv.reader(lines) if r and (stamp(r[0]) or 0) >= since] + out
        if lines and (stamp(lines[0]) or since) < since:
            break  # this log already reaches back far enough
    return out


def load_recent():
    """Refill the live charts with the last hour from the log, so a restart doesn't blank them."""
    for r in tail_rows(LOG, time.time() - KEEP):
        try:
            t, v = time.mktime(time.strptime(r[0], "%Y-%m-%d %H:%M:%S")), float(r[2]) if r[2] else None
        except (ValueError, IndexError):
            continue
        series.setdefault(r[1], deque()).append((t, v))  # last_ok is left alone: alerts start afresh


def add_event(e):
    """A device joined or left, or a unit restarted: for the live feed and for trends. Caller holds `lock`."""
    events.append(e)
    with trend_lock:
        trends["events"].append([e["t"], e["what"], e["ip"], e["name"]])


# ---- Trends: every reading rolled up by the hour and kept for two weeks, for the Day and Week views and the
# daily / weekly summaries. Small enough to keep in memory and save every few minutes, whatever the ping rate.
TRENDS = os.path.join(DATA, "trends.json")
TREND_KEEP = 15 * 86400  # two full weeks, so a week can be compared with the one before
SPELL_GAP = 60           # bad readings closer together than this are one problem spell
NAPPERS = {"Phone", "Tablet", "Smart-home gadget", "Smart speaker", "Appliance", "Printer", "Unknown"}  # as on the page
trend_lock = threading.Lock()
# hours: local hour start -> {"k": {kind: [n, lost, sum, min, max, {bin: count}]}, "on": devices on,
#        "d": {ip: [checks, ignored pings, pings sent, pings lost, ms sum, ms count]}}
# events: [t, what, ip, name]; spells: problem spells that ended; names: ip -> [name, kind]; sent: summary -> date
trends = {"hours": {}, "events": [], "spells": [], "names": {}, "sent": {}, "wifi": False}
spells = {}              # spells still going: "home" (the router) / "internet"


def hour_start(t):
    """Start of t's hour in local time (right for half-hour time zones too)."""
    lt = time.localtime(t)
    return int(t) - lt.tm_min * 60 - lt.tm_sec


def hbin(ms):
    """Response times go in bins 25% apart, so percentiles come out within a few percent from a few dozen numbers."""
    return 0 if ms <= 0.1 else min(80, int(math.log(ms / 0.1, 1.25)))


def hval(i):
    return 0.1 * 1.25 ** (i + 0.5)


def on_wifi():
    link = state["link"]
    return link["type"] == "wifi" if link else trends["wifi"]


def bad_limit(kind):
    """Over this a reading counts as a problem: the top of "poor" for that kind, as the dashboard judges it."""
    rt = THRESHOLDS["router_wifi" if on_wifi() else "router"][2]
    if kind == "router":
        return rt
    if kind.startswith("point:"):
        return rt + THRESHOLDS["wifi_point_extra"][2]
    if kind == "dns":
        return THRESHOLDS["lookups"]
    return THRESHOLDS["internet"][2]


def tally(t, kind, value):
    """Add one reading to its hour, and follow problem spells: the router going slow or silent (inside your home),
    and the internet doing so (your provider's, unless the router was struggling at the same time)."""
    with trend_lock:
        b = trends["hours"].setdefault(str(hour_start(t)), {"k": {}})
        s = b["k"].setdefault(kind, [0, 0, 0.0, None, None, {}])
        s[0] += 1
        if value is None:
            s[1] += 1
        else:
            s[2] += value
            s[3] = value if s[3] is None else min(s[3], value)
            s[4] = value if s[4] is None else max(s[4], value)
            if kind not in ("signal", "download_mbps"):
                i = str(hbin(value))
                s[5][i] = s[5].get(i, 0) + 1
        if kind not in ("router", INET0):
            return
        # internet spells that overlap trouble at the router are the home's doing; the report sorts that out. The router
        # is also held to the internet's own limit for this ("near"), so a router a bit slow over wifi still counts.
        follow("home" if kind == "router" else "internet", t, value is None or value > bad_limit(kind), value)
        if kind == "router":
            follow("near", t, value is None or value > bad_limit(INET0), value)


def follow(kind, t, bad, value):
    sp = spells.get(kind)
    if sp and t - sp["end"] > SPELL_GAP:
        end_spell(kind)
        sp = None
    if bad:
        if not sp:
            sp = spells[kind] = {"type": kind, "start": t, "end": t, "bad": 0, "lost": 0, "worst": 0}
        sp["end"] = t
        sp["bad"] += 1
        sp["lost"] += value is None
        sp["worst"] = max(sp["worst"], value or 0)


def end_spell(kind):
    sp = spells.pop(kind, None)
    if sp and (sp["bad"] >= 3 or kind == "near"):  # one or two slow replies are a blip, not a problem
        trends["spells"].append(sp)


def tally_scan(t, rows, online):
    """One device check: rows of (ip, name, kind, avg ms, lost pings; -1 = there, but ignores pings)."""
    with trend_lock:
        b = trends["hours"].setdefault(str(hour_start(t)), {"k": {}})
        b["on"] = max(b.get("on", 0), online)
        ds = b.setdefault("d", {})
        for ip, name, kind, avg, lost in rows:
            trends["names"][ip] = [name, kind]
            s = ds.setdefault(ip, [0, 0, 0, 0, 0.0, 0])
            s[0] += 1
            if lost < 0:
                s[1] += 1
            else:
                s[2] += PINGS_PER_CHECK
                s[3] += lost
            if avg is not None:
                s[4] += avg
                s[5] += 1


def save_trends():
    cut = time.time() - TREND_KEEP
    with trend_lock:
        trends["hours"] = {h: b for h, b in trends["hours"].items() if int(h) >= cut}
        trends["events"] = [e for e in trends["events"] if e[0] >= cut]
        trends["spells"] = [e for e in trends["spells"] if e["end"] >= cut]
        text = json.dumps(trends, separators=(",", ":"))
    with open(TRENDS + ".tmp", "w") as f:
        f.write(text)
    os.replace(TRENDS + ".tmp", TRENDS)


def load_trends(build=True):
    """Trends saved last time, or (the first time) built from the logs Heimdall already has."""
    try:
        with open(TRENDS) as f:
            saved = json.load(f)
        if isinstance(saved, dict) and isinstance(saved.get("hours"), dict):
            trends.update(saved)
            return True
    except (OSError, ValueError):
        pass
    if not build:
        return False
    print("Building trends from the logs so far (once only)…", flush=True)
    cache = {}

    def when(stamp):  # many readings share a timestamp; parsing each one is the slow part
        if stamp not in cache:
            if len(cache) > 1000:
                cache.clear()
            cache[stamp] = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
        return cache[stamp]

    def rows(path):
        for p in (path + ".old", path):
            try:
                with open(p, newline="") as f:
                    yield from csv.reader(f)
            except OSError:
                pass
    for r in rows(LOG):
        try:
            t, v = when(r[0]), float(r[2]) if r[2] else None
        except (ValueError, IndexError):
            continue
        if r[1] == "signal":
            trends["wifi"] = True
        tally(t, r[1], v)
    for kind in list(spells):
        end_spell(kind)
    scan, stamp = [], None

    def flush():
        if scan:
            tally_scan(when(stamp), scan, sum(1 for r in scan if r[3] is not None or r[4] < 0))
    for r in rows(DEVICE_LOG):
        try:
            row = (r[1], r[2], r[3], float(r[4]) if r[4] else None, int(float(r[6])))
            if r[0] != stamp:
                flush()
                scan, stamp = [], r[0]
            scan.append(row)
        except (ValueError, IndexError):
            continue
    flush()
    trends["events"] = [[e["t"], e["what"], e["ip"], e["name"]] for e in events]
    save_trends()
    return True


def merged(buckets, kind):
    """Several hours of one kind, summed and summarised: typical, slow (95%), worst, % lost, % slow or lost."""
    n = lost = 0
    total, lo, hi, hist = 0.0, None, None, {}
    for b in buckets:
        s = b["k"].get(kind)
        if not s:
            continue
        n, lost, total = n + s[0], lost + s[1], total + s[2]
        if s[3] is not None:
            lo, hi = (s[3], s[4]) if lo is None else (min(lo, s[3]), max(hi, s[4]))
        for i, c in s[5].items():
            hist[int(i)] = hist.get(int(i), 0) + c
    if not n:
        return None
    ok = n - lost
    out = {"n": n, "loss": round(100 * lost / n, 2), "max": hi, "min": lo}
    if kind in ("signal", "download_mbps"):
        return {**out, "mean": round(total / ok, 1) if ok else None}
    bins = sorted(hist.items())

    def pct(p):
        run = 0
        for i, c in bins:
            run += c
            if run >= p * ok:
                return round(min(max(hval(i), lo), hi), 1)
        return hi
    limit = hbin(bad_limit(kind))
    badn = lost + sum(c for i, c in bins if i > limit)
    return {**out, "med": pct(.5) if ok else None, "p95": pct(.95) if ok else None,
            "bad": round(100 * badn / n, 2), "badn": badn}


def dur(s):
    s = round(s)
    return f"{s} s" if s < 90 else f"{round(s / 60)} min" if s < 5400 else f"{s // 3600} h {round(s % 3600 / 60)} min"


def fmt_ms(v):
    return "–" if v is None else f"{v / 1000:.1f} s" if v >= 1000 else f"{v:.1f} ms" if v < 10 else f"{round(v)} ms"


def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def unit_name(ip):
    return "The router" if ip == ROUTER else point_label(ip)


def trend_report(days):
    """The last `days` days, hour by hour, against the same stretch before it, with a plain-English read."""
    now = time.time()
    start = hour_start(now) - (days * 24 - 1) * 3600
    before = start - days * 86400
    with trend_lock:
        H = {int(h): b for h, b in trends["hours"].items() if int(h) >= before}
        found = [{**e, "secs": e["end"] - e["start"] + PING_EVERY}
                 for e in trends["spells"] + list(spells.values()) if e["end"] >= start - SPELL_GAP and (e["bad"] >= 3 or e["type"] == "near")]
        evs = [e for e in trends["events"] if e[0] >= start]
        names = dict(trends["names"])
    cur = [b for h, b in H.items() if h >= start]
    prev = [b for h, b in H.items() if h < start]
    kinds = sorted({k for b in H.values() for k in b["k"]})
    hours, t = [], start
    while t <= now:
        b = H.get(t)
        hours.append({"t": t, "on": b.get("on") if b else None, "k": {k: merged([b], k) for k in b["k"]} if b else {}})
        t += 3600
    covered = lambda bs: sum(1 for b in bs if b["k"].get("router"))

    def devs(bs):
        out = {}
        for b in bs:
            for ip, v in b.get("d", {}).items():
                o = out.setdefault(ip, [0] * 6)
                for j in range(6):
                    o[j] += v[j]
        return out
    dc, dp = devs(cur), devs(prev)
    miss = lambda v: round(100 * v[3] / v[2], 1) if v and v[2] else None
    devices = [{"ip": ip, "name": names.get(ip, ["", "Unknown"])[0], "kind": names.get(ip, ["", "Unknown"])[1], "checks": v[0],
                "quiet": v[1], "missed": miss(v), "avg": round(v[4] / v[5], 1) if v[5] else None, "prev_missed": miss(dp.get(ip))}
               for ip, v in dc.items()]
    # devices leaving together, in the same check, usually means a wifi point or the router blinked
    drops, group = [], []
    for e in sorted((e for e in evs if e[1] == "left"), key=lambda e: e[0]) + [[math.inf]]:
        if group and e[0] - group[0][0] > 5:
            if len(group) >= 4:
                drops.append({"t": group[0][0], "names": [g[3] or g[2] for g in group]})
            group = []
        group.append(e)
    restarts = [{"t": e[0], "ip": e[2]} for e in evs if e[1] == "restarted"]
    # an internet spell during trouble at the router is the home network's: only those clear of it are the provider's
    home = [e for e in found if e["type"] != "internet"]
    found = [e for e in found if e["end"] >= start and (e["type"] == "home" or e["type"] == "internet" and not any(
        h["start"] - SPELL_GAP <= e["end"] and e["start"] <= h["end"] + SPELL_GAP for h in home))]
    found.sort(key=lambda e: e["start"])
    for e in found:  # what else happened around each spell
        e["notes"] = [f"{len(d['names'])} devices dropped off" for d in drops if e["start"] - 300 <= d["t"] <= e["end"] + 300] + \
                     [f"{unit_name(r['ip'])} restarted" for r in restarts if e["start"] - 300 <= r["t"] <= e["end"] + 300]
    rep = {"days": days, "start": start, "now": now, "hours": hours, "covered": covered(cur), "prev_covered": covered(prev),
           "cur": {k: merged(cur, k) for k in kinds}, "prev": {k: merged(prev, k) for k in kinds},
           "spells": found, "drops": drops, "restarts": restarts, "devices": devices,
           "joined": sum(e[1] == "joined" for e in evs), "left": sum(e[1] == "left" for e in evs),
           "points": sorted(k[6:] for k in kinds if k.startswith("point:")), "inet": INET0, "router": ROUTER,
           "names": NAMES, "thresholds": THRESHOLDS, "wifi": on_wifi(), "ping_every": PING_EVERY}
    rep["headline"], rep["findings"] = findings(rep)
    return rep


def findings(rep):
    """(headline, [{l: level, area, text}]): what went wrong, where and when, worst first."""
    days, cur, prev = rep["days"], rep["cur"], rep["prev"]
    label = "in the last 24 hours" if days == 1 else "in the last 7 days"
    when = lambda t: time.strftime("%H:%M" if days == 1 else "%a %H:%M", time.localtime(t))
    r, i = cur.get("router"), cur.get(rep["inet"])
    if not r:
        return "No readings yet for this stretch.", []
    out = []
    add = lambda l, area, text: out.append({"l": l, "area": area, "text": text})
    hours = days * 24
    if rep["covered"] < hours * 0.9:
        add("off", "coverage", f"Heimdall was running for {rep['covered']} of the last {hours} hours, so this is a partial picture.")

    def level(secs):  # share of the watched time spent in problem spells
        share = 100 * secs / max(1, rep["covered"] * 3600)
        return "warning" if share < 0.5 else "serious" if share < 2 else "critical"

    he = [e for e in rep["spells"] if e["type"] == "home"]
    ie = [e for e in rep["spells"] if e["type"] == "internet"]
    if i:
        if not ie:
            add("good", "internet", f"Your internet provider was solid: typically {fmt_ms(i['med'])}, and nothing went wrong beyond your router.")
        else:
            tot, worst = sum(e["secs"] for e in ie), max(ie, key=lambda e: e["secs"])
            add(level(tot), "internet", f"Your internet provider had {plural(len(ie), 'problem spell')} while your home network was fine, "
                f"{dur(tot)} in all. The longest was {dur(worst['secs'])} at {when(worst['start'])}.")
    if not he:
        add("good", "home", f"Your home network was steady: the router typically answered in {fmt_ms(r['med'])}.")
    else:
        tot, worst = sum(e["secs"] for e in he), max(he, key=lambda e: e["secs"])
        text = (f"Your home network stalled {plural(len(he), 'time')}, {dur(tot)} in all "
                f"({100 * tot / max(1, rep['covered'] * 3600):.1f}% of the time). The longest was {dur(worst['secs'])} at "
                f"{when(worst['start'])}, with replies taking up to {fmt_ms(max(e['worst'] for e in he))}.")
        # is it bunched at one time of day? (evenings usually mean a busy network)
        by_hour = [0.0] * 24
        for e in he:
            by_hour[time.localtime(e["start"]).tm_hour] += e["secs"]
        h0 = max(range(24), key=lambda h: by_hour[h] + by_hour[(h + 1) % 24] + by_hour[(h + 2) % 24])
        share = sum(by_hour[(h0 + k) % 24] for k in range(3)) / tot
        if share >= 0.5 and len(he) >= 3:
            text += f" Most of it ({share:.0%}) came between {h0:02d}:00 and {(h0 + 3) % 24:02d}:00"
            if days > 1:
                on = {time.strftime("%a", time.localtime(e["start"])) for e in he if (time.localtime(e["start"]).tm_hour - h0) % 24 < 3}
                text += f", on {plural(len(on), 'day')}"
            text += "."
        add(level(tot), "home", text)
    pts = [(ip, cur[f"point:{ip}"]) for ip in rep["points"] if cur.get(f"point:{ip}")]
    if pts:
        lossy = sorted(pts, key=lambda p: (p[1]["loss"], p[1]["bad"]), reverse=True)
        ip, w = lossy[0]
        rest = [s for p, s in pts if p != ip]
        loss_lim = THRESHOLDS["loss"]
        if w["loss"] > loss_lim[0] and w["loss"] >= loss_lim[1] or (rest and w["bad"] > 2 * max(s["bad"] for s in rest) + 1):
            hrs = [(h["t"], h["k"].get(f"point:{ip}")) for h in rep["hours"]]
            peak = max((x for x in hrs if x[1]), key=lambda x: x[1]["loss"])
            text = (f"{point_label(ip)} is your weakest point: it lost {w['loss']:.1f}% of replies"
                    + (f" (the others {max(s['loss'] for s in rest):.1f}% at most)" if rest else "")
                    + f", worst around {when(peak[0])} ({peak[1]['loss']:.0f}% that hour).")
            p = prev.get(f"point:{ip}")
            if p and rep["prev_covered"] >= 6:
                text += f" The {'day' if days == 1 else 'week'} before it lost {p['loss']:.1f}%."
            text += " If it's far from its nearest point, moving it closer or giving it a cable would steady the mesh."
            add("serious" if w["loss"] >= loss_lim[2] else "warning", "points", text)
        else:
            add("good", "points", f"All {plural(len(pts), 'wifi point')} kept up, losing {max(s['loss'] for _, s in pts):.1f}% of replies at most.")
        slow = max(pts, key=lambda p: p[1]["med"] or 0)
        others = sorted(s["med"] for p, s in pts if p != slow[0] and s["med"] is not None)
        if others and slow[1]["med"] is not None:
            typical = others[len(others) // 2]
            if slow[1]["med"] > 1.4 * typical and slow[1]["med"] - typical > 3:
                add("warning", "points", f"{point_label(slow[0])} is consistently the slowest point ({fmt_ms(slow[1]['med'])} typically, "
                    f"against {fmt_ms(typical)} for the rest), a sign its link to the rest of the mesh is weaker.")
    for d in rep["drops"]:
        names = ", ".join(d["names"][:4]) + ("…" if len(d["names"]) > 4 else "")
        add("serious", "home", f"{len(d['names'])} devices dropped off at once at {when(d['t'])} ({names}). "
            "That usually means a wifi point or the router briefly went down.")
    for x in rep["restarts"]:
        add("warning", "home", f"{unit_name(x['ip'])} restarted at {when(x['t'])}.")
    flaky = [d for d in rep["devices"] if d["kind"] not in NAPPERS and d["checks"] >= 10 and d["missed"] is not None
             and d["missed"] >= THRESHOLDS["device_missed"][0]]
    if flaky:
        add("warning", "devices", "Missing replies: " + ", ".join(f"{d['name'] or d['ip']} ({d['missed']:.0f}% of pings)" for d in flaky[:5]) + ".")
    pr, pi = prev.get("router"), prev.get(rep["inet"])
    if pr and rep["prev_covered"] >= 6:
        a, b = pr["bad"], r["bad"]
        trend = "about the same" if abs(a - b) < 0.3 or 0.8 <= (b + .01) / (a + .01) <= 1.25 else "better" if b < a else "worse"
        text = f"Against the {'day' if days == 1 else 'week'} before, home stalls were {trend} ({a:.1f}% → {b:.1f}% of the time)"
        if pi and i:
            text += f", and the internet typically {fmt_ms(pi['med'])} → {fmt_ms(i['med'])}"
        add("warning" if trend == "worse" else "good", "trend", text + ".")
    if (s := cur.get("signal")) and s.get("mean") is not None:
        if s["mean"] < -70:
            add("warning", "heimdall", f"Heimdall's own wifi was weak ({s['mean']:.0f} dBm on average), which makes every reading look "
                "worse than your network really is. Move it nearer a wifi point, or use a cable.")
        elif rep["wifi"]:
            add("off", "heimdall", f"Heimdall is on wifi ({s['mean']:.0f} dBm), so every reading includes its own hop. A cable would sharpen them.")
    if (sp := cur.get("download_mbps")) and sp.get("mean"):
        add("off", "internet", f"Speed tests: {sp['mean']:g} Mbps on average ({sp['min']:g} to {sp['max']:g}), from {plural(sp['n'], 'test')}.")
    rank = {"critical": 0, "serious": 1, "warning": 2, "good": 3, "off": 4}
    out.sort(key=lambda f: rank[f["l"]])
    trouble = lambda areas: any(f["l"] in ("warning", "serious", "critical") for f in out if f["area"] in areas)
    home, net = trouble({"home", "points"}), trouble({"internet"})
    headline = (f"Your home network and your internet provider both had trouble {label}." if home and net
                else f"Your internet was fine {label}; the trouble was inside your home." if home
                else f"Your home network was fine {label}; your internet provider was the weak spot." if net
                else f"All steady {label}.")
    return headline, out


def summary_text(rep):
    lines = [rep["headline"]] + [f"• {f['text']}" for f in rep["findings"] if f["area"] != "coverage" or len(rep["findings"]) < 3]
    link = f"http://{MDNS_NAME or MY_IP}" + ("" if PORT == 80 else f":{PORT}") + f"/#{'day' if rep['days'] == 1 else 'week'}"
    text = "\n".join(lines)
    return (text if len(text) < 1400 else text[:1400] + "…") + f"\nMore: {link}"


# ---- Optional: Claude writes the report. Off unless an Anthropic API key is found, since it sends a summary of your
# network (hourly figures, problem times, device names; no hardware addresses) to Anthropic.
AI = RAW.get("ai", {})
# Settings picked from a list: (default, choices). Cost per report is roughly model price x how hard Claude thinks.
CHOICES = {
    ("ai", "model"): ("claude-opus-5-5", ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]),
    ("ai", "effort"): ("high", ["low", "medium", "high", "xhigh", "max"]),
}


def choice(section, key):
    """A setting picked from a list: from heimdall.toml, or its default. A model typed into the file by hand that
    isn't in the list is used as-is; an effort that isn't one of the levels falls back to the default."""
    default, options = CHOICES[(section, key)]
    v = RAW.get(section, {}).get(key, default)
    return v if isinstance(v, str) and (v in options or key == "model") else default


AI_MODEL = choice("ai", "model")
AI_EFFORT = choice("ai", "effort")


def ai_key():
    """The key never lives in heimdall.toml (readable by anyone here, copied to .bak, rewritten by the settings page).
    As a service it comes from a systemd credential (./install.sh --api-key): stored root-only, and while Heimdall
    runs readable only by root and the user it runs as. Run by hand, from ANTHROPIC_API_KEY."""
    if AI.get("on") is False:
        return None
    try:
        with open(os.path.join(os.environ["CREDENTIALS_DIRECTORY"], "anthropic_key")) as f:
            key = f.read().strip()
    except (KeyError, OSError):
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key and not re.fullmatch(r"sk-ant-[A-Za-z0-9_-]+", key):
        # a stray character would make the HTTP library reject the header, and its error message contains the key
        print("The Anthropic API key isn't in the expected form (sk-ant-…), so Claude reports are off. Set it again "
              "with ./install.sh --api-key.", flush=True)
        return None
    return key or None


AI_KEY = ai_key()
ai_running = set()


def ai_entry(days):
    """Claude's latest report for a stretch, kept in trends.json so it survives restarts:
    {"at", "text", "model"} for the last report, plus "error" and "error_at" if the latest try failed."""
    return trends.setdefault("ai", {}).setdefault(str(days), {})


def ai_store(days, result):
    """Keep a new report, or note a failed try without losing the last good report."""
    with trend_lock:
        e = ai_entry(days)
        if result.get("text"):
            e.clear()
            e.update(result)
        else:
            e["error"], e["error_at"] = result.get("error"), result.get("at", time.time())


def ai_write(days):
    """Write one report for a stretch (no more than one at a time) and keep it."""
    if days in ai_running:
        return {}
    ai_running.add(days)
    try:
        result = ai_report(days)
    except Exception as e:
        print(f"Claude report crashed: {e!r}", flush=True)
        result = {"at": time.time(), "error": "Something went wrong; Heimdall's log has the details."}
    finally:
        ai_running.discard(days)
    ai_store(days, result)
    save_trends()
    return result


def ai_next(days):
    """When the next report for a stretch is due: the summary hour every day (last 24 hours), or on Mondays (week)."""
    hour, now = int(conf("alerts", "summary_hour")), time.time()
    lt = time.localtime(now)
    for k in range(9):
        d = time.localtime(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + k, 12, 0, 0, 0, 0, -1)))  # noon: clear of clock changes
        t = time.mktime((d.tm_year, d.tm_mon, d.tm_mday, hour, 0, 0, 0, 0, -1))
        if t > now and (days == 1 or d.tm_wday == 0):
            return t


AI_SYSTEM = """You are the analyst inside Heimdall, a small dashboard that watches a home network from one Linux machine.
You get a JSON summary of a stretch of readings and write a short report for the household: plain English, no jargon,
like a knowledgeable friend explaining what happened on their network and what to do about it.

What the data means:
- Heimdall pings the router, each wifi point ("point:<address>"), and internet servers every few seconds. Per hour you get
  med (typical ms), p95 (the slowest 5%), loss (% of pings unanswered) and bad (% of pings lost or slower than the
  "poor" limit). "dns" is website lookups through the router. "signal" is Heimdall's own wifi signal in dBm.
- spells: problem spells. type "home" = the router itself went slow or silent, so the trouble was inside the home
  (wifi, mesh, router). type "internet" = the internet went bad while the router was fine: the provider. Each has
  start/end (unix time), secs, worst ms, lost pings, and notes on what else happened then.
- drops: several devices leaving in the same check, which usually means a wifi point or the router blinked.
  restarts: a router or wifi point rebooted.
- devices: per device, missed = % of pings unanswered. Phones, tablets, gadgets, speakers, appliances, printers and
  unknown devices nap to save battery, so their misses and slow replies are normal and not worth mentioning.
- If "wifi" is true Heimdall is itself on wifi, so every reading includes its own wireless hop; a weak signal
  (below about -70 dBm) makes everything look worse than it is. Keep that in mind before blaming the network.
- "prev" is the same-length stretch before, for comparison; prev_covered says how many hours of it were watched.
- covered says how many hours Heimdall was actually running; missing hours mean no data, not a quiet network.
- names maps addresses to the household's own names for units; findings are Heimdall's own rule-based notes.
- Times: convert unix times to local times using the "tz" offset given and say them like "6:15 pm" or "Tuesday evening".

Write:
1. First, a summary of two or three sentences: the overall verdict and the single most useful thing to do. This is
   sent to their phone on its own, so it must stand alone.
2. Then short sections with "## " headings: what happened (the important patterns, with times), the likely causes
   (separate what the data shows from what you are inferring), and what to try, most useful first. Add a comparison
   with the stretch before if there is one worth mentioning.
Be specific and honest about uncertainty. Don't pad: if all was well, say so briefly. Under 400 words. Markdown,
but only headings, bullet lists and **bold**."""


def ai_input(rep):
    """The report, trimmed to what helps: per-hour figures for the network units, no hardware addresses."""
    keep = {"router", rep["inet"], "dns", "signal", *(f"point:{p}" for p in rep["points"])}
    slim = lambda s: s and {k: s[k] for k in ("n", "med", "p95", "loss", "bad", "max", "mean") if s.get(k) is not None}
    return json.dumps({
        "period": "last 24 hours" if rep["days"] == 1 else "last 7 days", "tz": time.strftime("%z"),
        "now": round(rep["now"]), "covered": rep["covered"], "prev_covered": rep["prev_covered"], "wifi": rep["wifi"],
        "router": rep["router"], "names": rep["names"], "ping_every_s": rep["ping_every"], "limits": rep["thresholds"],
        "cur": {k: slim(v) for k, v in rep["cur"].items() if k in keep}, "prev": {k: slim(v) for k, v in rep["prev"].items() if k in keep},
        "hours": [{"t": h["t"], "devices_on": h["on"], **{k: slim(v) for k, v in h["k"].items() if k in keep}} for h in rep["hours"] if h["k"]],
        "spells": [{k: round(e[k]) if isinstance(e[k], float) else e[k] for k in ("type", "start", "end", "secs", "worst", "lost", "notes")} for e in rep["spells"]],
        "drops": rep["drops"], "restarts": rep["restarts"], "joined": rep["joined"], "left": rep["left"],
        "devices": [d for d in rep["devices"] if d["checks"] >= 10], "findings": [f["text"] for f in rep["findings"]],
    }, separators=(",", ":"))


def ai_report(days):
    """Ask Claude to write the report; kept until the next one. Returns the stored result."""
    try:
        import anthropic
    except ImportError:
        return {"at": time.time(), "error": "Claude reports need the anthropic package; ./install.sh --api-key sets it up."}
    rep = trend_report(days)
    if not rep["covered"]:
        return {"at": time.time(), "error": "No readings yet for this stretch."}
    # base_url fixed, so an ANTHROPIC_BASE_URL left in the environment (say, for a proxy) can't send the key elsewhere
    client = anthropic.Anthropic(api_key=AI_KEY, base_url="https://api.anthropic.com", timeout=300)
    extra = {}
    if AI_MODEL in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-opus-5"):
        # if a safety check declines the request, the API retries it on a suitable model instead of failing
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    try:
        msg = client.beta.messages.create(
            model=AI_MODEL, max_tokens=16000, output_config={"effort": AI_EFFORT}, system=AI_SYSTEM,
            messages=[{"role": "user", "content": ai_input(rep)}], **extra)
    except anthropic.AuthenticationError:
        return {"at": time.time(), "error": "Anthropic didn't accept the API key. Set it again with ./install.sh --api-key."}
    except anthropic.RateLimitError:
        return {"at": time.time(), "error": "Anthropic is rate limiting this key; try again in a minute."}
    except anthropic.APIStatusError as e:
        print(f"Claude report failed: {e.status_code} {e.message}", flush=True)
        return {"at": time.time(), "error": f"Anthropic answered {e.status_code}; Heimdall's log has the details."}
    except anthropic.APIConnectionError:
        return {"at": time.time(), "error": "Couldn't reach Anthropic."}
    if msg.stop_reason == "refusal":
        return {"at": time.time(), "error": "Claude declined to write this report."}
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    return {"at": time.time(), "text": text, "model": msg.model} if text else {"at": time.time(), "error": "Claude sent back an empty report."}


def summary_loop():
    """Save trends every few minutes. At the set hour: Claude's reports, if a key is set (the last 24 hours every
    day, the week on Mondays; nobody can ask for more, since each one costs money), then the summaries to your phone."""
    if AI_KEY:  # first start with a key: one report per stretch now, rather than waiting for tomorrow morning
        for days in (1, 7):
            e = ai_entry(days)
            if not e.get("text") and time.time() - e.get("error_at", 0) > 86400:
                ai_write(days)
    n = 0
    while True:
        time.sleep(60)
        n += 1
        if n % 5 == 0:
            save_trends()
        lt = time.localtime()
        if lt.tm_hour != int(conf("alerts", "summary_hour")):
            continue
        today = time.strftime("%Y-%m-%d", lt)
        for kind, days, due in (("daily_summary", 1, True), ("weekly_summary", 7, lt.tm_wday == 0)):
            if not due or trends["sent"].get(kind) == today:
                continue
            with trend_lock:
                trends["sent"][kind] = today
            rep = trend_report(days)
            if not rep["covered"]:
                continue
            ai = ai_write(days) if AI_KEY else {}
            if kind not in ALERT_ON or not ALERTS.get("url"):
                continue
            text = summary_text(rep)
            if ai.get("text"):  # Claude's own summary paragraph, if it wrote one; Heimdall's findings if not
                link = text.rsplit("\n", 1)[-1]
                text = ai["text"].split("\n\n", 1)[0].strip() + "\n" + link
            alert(("Daily summary: " if days == 1 else "Weekly summary: ") + text)


def off_limits(a):
    """Addresses alerts may never go to: this machine, by any of its addresses or loopback, and link-local ones."""
    if a.version == 6 and a.ipv4_mapped:  # ::ffff:192.168.1.10 is 192.168.1.10
        a = a.ipv4_mapped
    return a.is_loopback or a.is_link_local or a.is_unspecified or a.is_multicast or a in OWN


def alert_target(url):
    """(problem, None), or (None, (https, host, port, path, address)) with the one address to connect to.
    Alerts go to services out there or elsewhere at home, never back into this machine, so the settings page
    can't be used to poke at what runs here. The address is checked once here and then used as-is."""
    if len(url) > 500 or not re.fullmatch(r"https?://\S+", url):
        return "The alert address should be a web address starting with http:// or https://", None
    try:
        u = urlsplit(url)
        port = u.port or (443 if u.scheme == "https" else 80)
        addrs = [ipaddress.ip_address(a[4][0].split("%")[0]) for a in socket.getaddrinfo(u.hostname, port, type=socket.SOCK_STREAM)]
    except (OSError, ValueError, TypeError):
        return "Couldn't find that alert address", None
    if not addrs or any(map(off_limits, addrs)):
        return "Alerts can't be sent to this machine itself", None
    return None, (u.scheme == "https", u.hostname, port, (u.path or "/") + (f"?{u.query}" if u.query else ""), str(addrs[0]))


def alert_url_problem(url):
    return alert_target(url)[0]


def send_alert(text, url=None):
    """Send one alert to [alerts] url. With {text} in the url it's a GET with the message filled in (WhatsApp or
    Signal via CallMeBot); otherwise the message is POSTed as the body (ntfy and most webhooks). Returns an error or None."""
    text = f"Heimdall: {text}"
    url = url or ALERTS["url"]
    get = "{text}" in url
    problem, target = alert_target(url.replace("{text}", quote(text, safe="")) if get else url)
    if problem:
        return problem
    https, host, port, path, addr = target
    try:
        status, _ = http_fetch(host, port, path, addr=addr, https=https, data=None if get else text.encode(), timeout=20)
    except Exception as e:
        return f"Couldn't send alert: {e}"
    if status >= 300:
        return f"Couldn't send alert: the service answered {status}"


def alert(text):
    print(f"Alert: {text}", flush=True)
    if ALERTS.get("url"):
        def send():
            if err := send_alert(text):
                print(err, flush=True)
        threading.Thread(target=send, daemon=True).start()


def point_label(ip):
    return f"{NAMES[ip]} wifi point" if ip in NAMES else f"Wifi point .{ip.rsplit('.', 1)[1]}"


def alert_loop():
    """Every 10 s: has the internet or a wifi point gone quiet, or come back? One alert each way, not one per minute."""
    gone = {}  # what -> when it went quiet, for things we've alerted about
    after = int(conf("alerts", "offline_after"))
    while True:
        time.sleep(10)
        now = time.time()
        quiet = lambda kind: now - last_ok.get(kind, START)
        checks = {}
        if "internet" in ALERT_ON:
            checks["The internet"] = (min(quiet(k) for k in TARGETS if k != "router"), int(conf("alerts", "internet_after")))
        if "wifi_points" in ALERT_ON:
            checks |= {point_label(ip): (quiet(f"point:{ip}"), after) for ip in point_list()}
        # if even the router is silent, it's Heimdall's own connection that's down, so don't blame anything else
        heimdall_ok = quiet("router") < 30
        for what, (silent, limit) in checks.items():
            if what not in gone and silent >= limit and heimdall_ok:
                gone[what] = now - silent
                alert(f"{what} is {'down' if what == 'The internet' else 'not answering'} (nothing for {round(silent / 60)} min)")
            elif what in gone and silent < 15:
                alert(f"{what} is back, after {max(1, round((now - gone.pop(what)) / 60))} min")


def forever(job, *args):
    """Run a background job in its own thread. If it ever crashes, say why and start it again,
    so one surprise doesn't quietly freeze part of the dashboard."""
    def run():
        while True:
            try:
                job(*args)
            except Exception as e:
                print(f"{job.__name__} crashed ({e!r}); restarting in 10 s", flush=True)
                time.sleep(10)
    threading.Thread(target=run, daemon=True).start()


def pinger(name, host):
    seq_re = re.compile(r"icmp_seq=(\d+).*time=([\d.]+)")
    miss_re = re.compile(r"no answer yet for icmp_seq=(\d+)")
    while True:
        p = subprocess.Popen(["ping", "-n", "-O", "-i", str(PING_EVERY), "-W", "2", host], env=C_LOCALE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        late = {}  # seq -> when ping said "no answer yet"; a slow reply can still turn up
        try:
            for line in p.stdout:
                if m := seq_re.search(line):
                    late.pop(m.group(1), None)
                    record(name, float(m.group(2)))
                elif m := miss_re.search(line):
                    late.setdefault(m.group(1), time.time())
                for seq, t in list(late.items()):
                    if time.time() - t > 3:  # still nothing: it really was lost
                        del late[seq]
                        record(name, None)
        finally:
            p.kill()  # don't leave a ping running with nobody reading it
        time.sleep(2)


def dns_query(server, name, timeout=2.0):
    """How long one website lookup through `server` takes, in ms, or None if it failed: no answer in time, or an
    answer saying the name couldn't be looked up (SERVFAIL, REFUSED...). Stray replies are ignored."""
    qid = random.randint(0, 65535)
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0" + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        t0 = time.perf_counter()
        s.sendto(q, (server, 53))
        while (left := t0 + timeout - time.perf_counter()) > 0:
            s.settimeout(left)
            data, peer = s.recvfrom(512)
            # an answer to our question, from the server we asked: same id, "this is a reply" bit, question echoed back
            if peer[0] != server or len(data) < len(q) or data[12:len(q)] != q[12:]:
                continue
            rid, flags = struct.unpack(">HH", data[:4])
            if rid != qid or not flags & 0x8000:
                continue
            return round((time.perf_counter() - t0) * 1000, 1) if flags & 0xF == 0 else None  # result code 0: it worked
        return None
    except OSError:
        return None
    finally:
        s.close()


def dns_loop():
    while True:
        record("dns", dns_query(ROUTER, random.choice(DNS_NAMES)))
        time.sleep(DNS_EVERY)


def read_link():
    """How Heimdall itself is connected: cable or wifi, and how good that wifi is."""
    route = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True, env=C_LOCALE).stdout
    m = re.search(r"\bdev (\S+)", route)
    if not m:
        return None
    dev = m.group(1)
    if not os.path.exists(f"/sys/class/net/{dev}/wireless"):
        try:
            speed = int(open(f"/sys/class/net/{dev}/speed").read())
        except (OSError, ValueError):
            speed = None
        return {"type": "cable", "dev": dev, "speed_mbps": speed if speed and speed > 0 else None}
    out = subprocess.run(["iw", "dev", dev, "link"], capture_output=True, text=True, env=C_LOCALE, timeout=10).stdout
    get = lambda pat: (m := re.search(pat, out)) and m.group(1)
    freq = get(r"freq: ([\d.]+)")
    return {"type": "wifi", "dev": dev, "ssid": get(r"SSID: (.+)"), "bssid": get(r"Connected to ([0-9a-f:]{17})"),
            "signal_dbm": int(get(r"signal: (-?\d+)") or 0) or None,
            "rx_mbps": float(get(r"rx bitrate: ([\d.]+)") or 0) or None,
            "tx_mbps": float(get(r"tx bitrate: ([\d.]+)") or 0) or None,
            "band": None if not freq else "5 GHz" if float(freq) > 4900 else "2.4 GHz"}


def link_loop():
    while True:
        try:
            state["link"] = read_link()
            if state["link"]:
                trends["wifi"] = state["link"]["type"] == "wifi"
            if state["link"] and state["link"].get("signal_dbm"):
                record("signal", state["link"]["signal_dbm"])
        except Exception:
            state["link"] = None
        time.sleep(10)


def probe(ip, count, arp=True):
    """Ping a device; returns (avg_ms, max_ms, lost) or None if it didn't answer at all."""
    out = subprocess.run(["ping", "-n", "-q", "-c", str(count), "-i", "1", "-W", "1", ip],
                         capture_output=True, text=True, env=C_LOCALE).stdout
    got = re.search(r"(\d+) received", out)
    m = re.search(r"= [\d.]+/([\d.]+)/([\d.]+)", out)
    if m and got:
        return float(m.group(1)), float(m.group(2)), count - int(got.group(1))
    return (None, None, -1) if arp and answers_arp(ip) else None  # -1 lost: here, but ignores pings


def answers_arp(ip, wait=7):
    """Some devices (Windows laptops, consoles, TVs) ignore pings but must still answer "who has this address?".
    The ping we just sent makes the kernel ask; watch for the answer for a few seconds."""
    start = time.time()
    while time.time() - start < wait:
        st = subprocess.run(["ip", "neigh", "show", ip], capture_output=True, text=True, env=C_LOCALE).stdout.split()
        if "REACHABLE" in st:
            return True
        if not st or "FAILED" in st:
            return False
        time.sleep(1)
    return False


def kind_of(ip, name, vendor=None):
    if ip == ROUTER:
        return "Router"
    if ip == MY_IP:
        return "Heimdall (this machine)"
    for pat, kind in KINDS:
        if re.search(pat, name or "", re.I):
            return kind
    for pat, kind in VENDOR_KINDS:
        if re.search(pat, vendor or "", re.I):
            return kind
    return "Unknown"


def neighbours(confirmed=False):
    """Addresses this machine has recently talked to, with their hardware addresses - free to read.
    confirmed=True keeps only ones that just answered the network's "who has this address?" call."""
    out = subprocess.run(["ip", "neigh"], capture_output=True, text=True, env=C_LOCALE).stdout
    return {ip: mac for ip, mac, st in re.findall(r"^(\S+) .*lladdr (\S+).* (\S+)\s*$", out, re.M)
            if ipaddress.ip_address(ip) in NET and ip not in MY_IPS and (not confirmed or st == "REACHABLE")}


def reverse_name(ip):
    try:
        return socket.gethostbyaddr(ip)[0].removesuffix(".lan")
    except OSError:
        return ""


def scan_loop():
    while True:
        now = time.time()
        macs = neighbours()
        sweep = not state["last_sweep"] or now - state["last_sweep"] >= SWEEP_EVERY
        known = {ip for ip, d in devices.items() if d["online"] or now - d["last"] < 3600} | set(macs)
        known -= MY_IPS
        rest = [str(a) for a in NET.hosts() if str(a) not in known | MY_IPS] if sweep else []
        with ThreadPoolExecutor(8) as pool:
            results = dict(zip(known, pool.map(lambda ip: probe(ip, PINGS_PER_CHECK), known)))
        with ThreadPoolExecutor(16) as pool:
            results.update({ip: r for ip, r in zip(rest, pool.map(lambda ip: probe(ip, 1, arp=False), rest)) if r})
        results[MY_IP] = (0.0, 0.0, 0)
        macs.update(neighbours())
        # names for devices seen for the first time, looked up before taking the lock: a slow lookup mustn't
        # hold up the dashboard
        new = [ip for ip, r in results.items() if r and ip not in devices]
        with ThreadPoolExecutor(8) as pool:
            reverse = dict(zip(new, pool.map(reverse_name, new)))
        rows = []
        with lock:
            for ip, r in results.items():
                d = devices.get(ip)
                if r is None:
                    if d and d["online"]:
                        d["rtt"] = (d["rtt"] + [(now, None, None, PINGS_PER_CHECK)])[-30:]
                        rows.append(d)
                        # one missed check can be a sleepy phone; call it gone after two
                        if now - d["last"] > CHECK_EVERY * 1.5:
                            d["online"] = False
                            add_event({"t": now, "ip": ip, "name": d["name"], "what": "left"})
                    continue
                if not d:
                    name = NAMES.get(ip, reverse.get(ip, ""))
                    vendor = vendor_of(macs.get(ip))
                    if "new_devices" in ALERT_ON and (state["last_scan"] or restored) and macs.get(ip) not in {x.get("mac") for x in devices.values()}:
                        alert(f"New device joined: {name or 'no name'} ({vendor or 'unknown maker'}) at {ip}")
                    d = devices[ip] = {"ip": ip, "name": name, "vendor": vendor, "kind": kind_of(ip, name, vendor), "first": now, "rtt": [], "online": False}
                if not d["online"]:
                    d["online"] = True
                    if state["last_scan"]:  # don't flood the feed on the very first scan
                        add_event({"t": now, "ip": ip, "name": d["name"], "what": "joined"})
                d["last"] = now
                d["mac"] = macs.get(ip, d.get("mac"))
                if not d.get("vendor") and (vendor := vendor_of(d["mac"])):
                    d["vendor"] = vendor
                    if d["kind"] == "Unknown":
                        d["kind"] = kind_of(ip, d["name"], vendor)
                d["rtt"] = (d["rtt"] + [(now, *r)])[-30:]
                rows.append(d)
                if d["kind"] == "Wifi point":
                    add_point(ip)
                elif not d.get("identified") and d.get("tries", 0) < 3 and ip not in (ROUTER, MY_IP):
                    d["tries"] = d.get("tries", 0) + 1  # a few tries per device: is it a mesh unit, does it announce a name?
                    threading.Thread(target=identify, args=(ip,), daemon=True).start()
            del events[:-200]
            state["last_scan"] = now
            if sweep:
                state["last_sweep"] = now
            saved = json.dumps({"devices": devices, "events": events})
            online = sum(1 for d in devices.values() if d["online"])
        with open(SAVED + ".tmp", "w") as f:
            f.write(saved)
        os.replace(SAVED + ".tmp", SAVED)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        out = []
        for d in rows:
            _, avg, mx, lost = d["rtt"][-1]
            out.append([stamp, d["ip"], d["name"], d["kind"], "" if avg is None else avg, "" if mx is None else mx, lost])
        append_log(DEVICE_LOG, out)
        tally_scan(now, [(r[1], r[2], r[3], r[4] if r[4] != "" else None, r[6]) for r in out], online)
        time.sleep(max(0, CHECK_EVERY - (time.time() - now)))


def mdns_loop(name, ip):
    """Announce name.local -> ip through avahi, for as long as Heimdall runs. No root needed."""
    try:
        import dbus
    except ImportError:
        print(f"Not announcing {name}: needs python3-dbus (sudo apt install python3-dbus)")
        return
    group = None
    while True:
        try:
            if not group or group.GetState() not in (1, 2):  # registering / established
                bus = dbus.SystemBus()
                server = dbus.Interface(bus.get_object("org.freedesktop.Avahi", "/"), "org.freedesktop.Avahi.Server")
                group = dbus.Interface(bus.get_object("org.freedesktop.Avahi", server.EntryGroupNew()), "org.freedesktop.Avahi.EntryGroup")
                # 16 = no reverse record: avahi already names this machine's own addresses, and would call it a collision
                group.AddAddress(-1, 0, dbus.UInt32(16), name, ip)
                group.Commit()
        except Exception as e:  # avahi restarting or missing: try again shortly
            if group is not False:
                print(f"Couldn't announce {name} (is avahi running?): {e}")
            group = False
        time.sleep(30)


points_lock = threading.Lock()


def add_point(ip):
    with points_lock:
        if ip in points:
            return
        points.add(ip)
    forever(pinger, f"point:{ip}", ip)


def point_list():
    with points_lock:
        return sorted(points)


def status_page(ip, timeout):
    """A Google/Nest unit's own status page, as a dict, or None. Read at most 64 KB, since any device at home
    could answer on that address, and something that never stops sending mustn't use up all our memory."""
    try:
        status, body = http_fetch(ip, 80, "/api/v1/status", timeout=timeout)
        st = json.loads(body, parse_float=finite, parse_constant=no_constants) if status == 200 else None
    except Exception:
        return None
    return st if isinstance(st, dict) else None


def mdns_name(ip):
    """The name a device announces about itself (Apple, Android, Daikin...), via avahi."""
    try:
        import dbus
        avahi = dbus.Interface(dbus.SystemBus().get_object("org.freedesktop.Avahi", "/"), "org.freedesktop.Avahi.Server")
        name = str(avahi.ResolveAddress(-1, 0, ip, dbus.UInt32(0))[4]).removesuffix(".local")
    except Exception:
        return None
    return None if re.fullmatch(r"[0-9a-f]{32}", name) else name  # long hex ids aren't names


def identify(ip):
    """For each new device: is it a Google/Nest mesh unit (they often have no name), and does it announce a name?"""
    sysinfo = (status_page(ip, 3) or {}).get("system")
    role = sysinfo.get("groupRole") if isinstance(sysinfo, dict) else None
    d = devices[ip]
    if isinstance(role, str) and role:
        with lock:
            d["kind"], d["identified"] = "Wifi point", True
        add_point(ip)
        return
    name = None if d["name"] else mdns_name(ip)
    with lock:
        if d["name"]:
            d["identified"] = True
        elif name:
            d["identified"] = True
            name = re.sub(r"^Android_\w+$", "Android device", name)
            d["name"] = name
            if d["kind"] == "Unknown":
                d["kind"] = "Phone" if name == "Android device" else kind_of(ip, name, d.get("vendor"))


def mesh_loop():
    """Once a minute, read the status page the Nest/Google router and each wifi point serve on the home network."""
    while True:
        for ip in [ROUTER, *point_list()]:
            st = status_page(ip, 5)
            if not st:
                continue  # not a Google/Nest unit, or busy; the pings still say if it's alive
            # anything that isn't the shape we expect is treated as missing, so one odd unit can't stop the rest
            sysinfo, sw, wan = (st.get(k) if isinstance(st.get(k), dict) else {} for k in ("system", "software", "wan"))
            now, up = time.time(), sysinfo.get("uptime")
            up = up if type(up) in (int, float) and math.isfinite(up) else None
            old = mesh.get(ip)
            with lock:
                if old and up is not None and old.get("uptime") is not None and up < old["uptime"]:
                    name = NAMES.get(ip) or ("Router" if ip == ROUTER else f"Wifi point .{ip.rsplit('.', 1)[1]}")
                    add_event({"t": now, "ip": ip, "name": name, "what": "restarted"})
                    if "restarts" in ALERT_ON:
                        alert(f"{'Router' if ip == ROUTER else point_label(ip)} restarted")
                mesh[ip] = {"ip": ip, "at": now, "uptime": up, "role": sysinfo.get("groupRole"), "model": sysinfo.get("modelId"),
                            "wired": sysinfo.get("lan0Link"), "led": sysinfo.get("ledAnimation"),
                            "version": sw.get("softwareVersion"), "update": sw.get("updateRequired") or sw.get("updateStatus") not in (None, "idle"),
                            "online": wan.get("online") if sysinfo.get("groupRole") == "root" else None,
                            "upstream": wan.get("gatewayIpAddress") if sysinfo.get("groupRole") == "root" else None}
        time.sleep(MESH_EVERY)


def speed_test():
    state["speed_running"] = True
    try:
        t0 = time.perf_counter()
        with urlopen(f"https://speed.cloudflare.com/__down?bytes={int(SPEEDTEST_MB * 1e6)}", timeout=30 + SPEEDTEST_MB) as r:
            n = 0
            while chunk := r.read(1 << 16):  # count it, don't keep it
                n += len(chunk)
        state["speed"] = {"mbps": round(n * 8 / (time.perf_counter() - t0) / 1e6, 1), "at": time.time()}
        record("download_mbps", state["speed"]["mbps"])
    except Exception as e:
        state["speed"] = {"error": str(e), "at": time.time()}
    finally:
        state["speed_running"] = False


def toml_text(cfg):
    """heimdall.toml from a dict. Enough TOML for Heimdall's own settings: tables of numbers, strings and lists."""
    def text(t):  # a TOML string: JSON's escapes, but real characters for emoji (TOML has no \ud83d-style pairs)
        return json.dumps(t, ensure_ascii=False).replace("\x7f", "\\u007f")

    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, str):
            return text(v)
        if isinstance(v, (int, float)):
            return json.dumps(v)
        return "[" + ", ".join(map(val, v)) + "]"

    def key(k):
        return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else text(k)
    out = ["# Heimdall settings, saved from the settings page. Editing by hand is fine too; see heimdall.example.toml."]
    for section, table in cfg.items():
        for t in (table if isinstance(table, list) else [table]):  # [[kinds]] is a list of tables
            flat = {k: v for k, v in t.items() if not isinstance(v, dict)}
            out += ["", f"[[{section}]]" if isinstance(table, list) else f"[{section}]"] + [f"{key(k)} = {val(v)}" for k, v in flat.items()]
            for sub, st in t.items():
                if isinstance(st, dict):
                    out += ["", f"[{section}.{key(sub)}]"] + [f"{key(k)} = {val(v)}" for k, v in st.items()]
    return "\n".join(out) + "\n"


def save_settings(new):
    """Check what the settings page sent and merge it into heimdall.toml. Returns a problem to show, or None.
    Settings left at their default are taken out of the file, so it only holds what you changed."""
    cfg = load_config()[3]  # fresh from disk, so hand edits since startup aren't lost
    if not all(isinstance(new.get(s, {}), dict) for s in ("checks", "thresholds", "alerts", "logs", "names", "ai")):
        return "That didn't look like settings."
    for (section, k), (default, lo, hi) in EDITABLE.items():
        if k not in new.get(section, {}):
            continue
        v = new[section][k]
        nums = v if isinstance(default, list) else [v]
        if not isinstance(nums, list) or len(nums) != (len(default) if isinstance(default, list) else 1) \
                or not all(type(x) in (int, float) and lo <= x <= hi for x in nums):
            return f"{section}.{k}: numbers between {lo} and {hi}, please"
        if nums != sorted(nums):
            return f"{section}.{k}: each number should be at least the one before"
        if v == default:
            cfg.get(section, {}).pop(k, None)
        else:
            cfg.setdefault(section, {})[k] = v
    for (section, k), (default, options) in CHOICES.items():
        if k not in new.get(section, {}):
            continue
        v = new[section][k]
        if v not in options and v != cfg.get(section, {}).get(k):  # what's in the file is always fine, even set by hand
            return f"{section}.{k}: one of {', '.join(options)}, please"
        if v == default:
            cfg.get(section, {}).pop(k, None)
        else:
            cfg.setdefault(section, {})[k] = v
    alerts = new.get("alerts", {})
    if "url" in alerts:
        u = str(alerts["url"]).strip()
        if u and (problem := alert_url_problem(u)):
            return problem
        cfg.setdefault("alerts", {})["url"] = u
        if not u:
            cfg["alerts"].pop("url")
    if "on" in alerts:
        if not isinstance(alerts["on"], list):
            return "That didn't look like settings."
        on = [a for a in ALL_ALERTS if a in alerts["on"]]
        cfg.setdefault("alerts", {})["on"] = on
        if on == ALL_ALERTS:
            cfg["alerts"].pop("on")
    for ip, name in new.get("names", {}).items():
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            return f"{ip} isn't an address"
        name = "".join(c for c in str(name) if c.isprintable()).strip()[:40]  # no control characters
        cfg.setdefault("names", {})[ip] = name
        if not name:
            cfg["names"].pop(ip)
    cfg = {s: t for s, t in cfg.items() if t}  # drop tables that ended up empty
    text = toml_text(cfg)
    try:
        same = tomllib.loads(text) == cfg
    except tomllib.TOMLDecodeError:
        same = False
    if not same:  # never write a file that wouldn't read back the same
        return "Couldn't write these settings safely, so nothing was changed."
    if os.path.exists(CONFIG):
        shutil.copyfile(CONFIG, CONFIG + ".bak")
    with open(CONFIG + ".tmp", "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(CONFIG + ".tmp", CONFIG)


def restart():
    """Start Heimdall afresh in this same process, so new settings apply everywhere at once."""
    print("Settings changed: restarting", flush=True)
    save_trends()
    os.execv(sys.executable, [sys.executable, *sys.argv])


def allowed_hosts():
    """Names and addresses the dashboard may be opened by. Anything else is refused, which stops a website from
    pointing its own name at this machine (DNS rebinding) to read or change Heimdall through your browser."""
    me = socket.gethostname().lower()
    extra = CFG.get("allowed_hosts", [])  # e.g. a name your router or a VPN gives this machine
    return {h.lower() for h in [MDNS_NAME or "", MDNS_IP, *MY_IPS, HOST, "localhost", "127.0.0.1", "::1",
                                me, f"{me}.local", f"{me}.lan", f"{me}.home", *extra] if h}


class Handler(BaseHTTPRequestHandler):
    timeout = 10  # a client that stops talking mid-request doesn't hold a thread forever

    def log_message(self, *a):
        pass

    def send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")  # no other page may frame this one and trick clicks on Save
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def host_ok(self):
        hosts = self.headers.get_all("Host") or []
        m = re.fullmatch(r"(\[[0-9a-f:.]+\]|[a-z0-9.-]+)(:\d{1,5})?", hosts[0].lower()) if len(hosts) == 1 else None
        if m and m.group(1).strip("[]") in HOSTS:
            return True
        self.send(f"Open Heimdall at http://{MDNS_NAME or MY_IP}:{PORT}/ (this address isn't one it answers to; "
                  "add it to allowed_hosts in heimdall.toml if it should be)".encode(), "text/plain; charset=utf-8", 421)
        return False

    def do_GET(self):
        if not self.host_ok():
            return
        url = urlparse(self.path)
        if url.path == "/data":
            # The page asks only for what's new since its last update, so an open tab stays cheap.
            q = parse_qs(url.query)
            since, seen_scan = float(q.get("since", ["0"])[0]), float(q.get("scan", ["0"])[0])
            with lock:
                fresh = {k: [[round(t, 2), v] for t, v in dq if t > since] for k, dq in series.items()}
                # the device list only changes once a minute, so skip it if the page already has this one
                devs = None if seen_scan == state["last_scan"] else [{**d, "rtt": d["rtt"][-10:]} for d in devices.values()]
                evs = events[-30:]
            self.send(json.dumps({"now": time.time(), "series": fresh, "targets": TARGETS, "names": NAMES, "thresholds": THRESHOLDS, "pings_per_check": PINGS_PER_CHECK, "points": point_list(), "mesh": list(mesh.values()),
                                  "devices": devs, "events": evs, "host": socket.gethostname(), **state},
                                 separators=(",", ":")).encode(), "application/json")
        elif url.path == "/trends":
            days = 7 if parse_qs(url.query).get("days") == ["7"] else 1
            rep = trend_report(days)
            with trend_lock:
                e = dict(ai_entry(days))
            rep["ai"] = {"on": bool(AI_KEY), "model": AI_MODEL, "effort": AI_EFFORT, "running": days in ai_running, "next_at": ai_next(days), **e}
            self.send(json.dumps(rep, separators=(",", ":")).encode(), "application/json")
        elif url.path == "/settings":
            self.send(json.dumps({
                "values": {s: {k: conf(s, k) for s2, k in EDITABLE if s2 == s} for s, _ in EDITABLE},
                "defaults": {s: {k: d for (s2, k), (d, _, _) in EDITABLE.items() if s2 == s} for s, _ in EDITABLE},
                "limits": {f"{s}.{k}": [lo, hi] for (s, k), (_, lo, hi) in EDITABLE.items()},
                "choices": {f"{s}.{k}": {"value": choice(s, k), "default": d, "options": o} for (s, k), (d, o) in CHOICES.items()},
                "ai_on": bool(AI_KEY),
                # where alerts go, but not the whole address: for ntfy that's the secret, for CallMeBot the key
                "alerts": {"saved": re.sub(r"^(https?://[^/?#]+).*", r"\1/…", ALERTS.get("url", "")), "on": sorted(ALERT_ON)},
                "all_alerts": ALL_ALERTS, "names": NAMES, "router": ROUTER, "points": point_list()}).encode(), "application/json")
        else:
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self.send(f.read(), "text/html; charset=utf-8")

    def do_POST(self):
        # Changes need this header, which other websites can't add to a request sent through your browser.
        if not self.host_ok():
            return
        if self.headers.get("X-Heimdall") != "1":
            return self.send(b'{"error": "Not allowed"}', "application/json", 403)
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = -1
        if not 0 <= size <= 100_000:
            return self.send(b'{"error": "Too big"}', "application/json", 413)
        try:
            body = json.loads(self.rfile.read(size) or "{}")
        except ValueError:
            body = None
        try:
            if not isinstance(body, dict):
                err = "That didn't look like settings."
            elif self.path == "/settings":
                err = save_settings(body)
                if not err:
                    threading.Timer(1, restart).start()  # after this reply has gone out
            elif self.path == "/test-alert":
                u = str(body.get("url") or ALERTS.get("url") or "")
                err = (alert_url_problem(u) if u else "Add an alert address first.") or send_alert("test alert from the settings page.", u)
                if err and not err.startswith(("The alert", "Alerts can't", "Couldn't find", "Add an")):
                    print(err, flush=True)  # the details stay in Heimdall's log, not in the reply
                    err = "Couldn't send it. Check the address; Heimdall's log has the details."
            elif self.path == "/speedtest":
                if not state["speed_running"]:
                    threading.Thread(target=speed_test, daemon=True).start()
                err = None
            else:
                err = "Unknown request"
        except Exception as e:  # a bad request mustn't take the page down with it
            print(f"Request to {self.path} failed: {e!r}", flush=True)
            err = "Something went wrong; Heimdall's log has the details."
        self.send(json.dumps({"error": err}).encode(), "application/json")


if __name__ == "__main__":
    if "--test-alert" in sys.argv:
        err = send_alert("test alert. If you can read this, alerts work.") if ALERTS.get("url") else "No url under [alerts] in heimdall.toml."
        if err:
            raise SystemExit(err)
        print("Sent. Check your phone.")
        raise SystemExit
    if "--report" in sys.argv:  # the day's or week's findings, in the terminal
        days = 7 if sys.argv[sys.argv.index("--report") + 1:][:1] == ["week"] else 1
        if not load_trends(build=False):
            raise SystemExit("No trends yet: start Heimdall first (it builds them from its logs).")
        if "--ai" in sys.argv:
            r = ai_report(days)
            raise SystemExit(r.get("error") or r["text"])
        rep = trend_report(days)
        print(rep["headline"])
        for f in rep["findings"]:
            print(f"- {f['text']}")
        raise SystemExit
    load_recent()  # before any checks start, so new readings land after the reloaded ones
    try:
        with open(SAVED) as f:
            saved = json.load(f)
        devices.update(saved["devices"])
        restored = bool(devices)
        events.extend(saved["events"])
        for d in devices.values():
            d["name"] = NAMES.get(d["ip"], d["name"])
            d.pop("tries", None)  # give identification another go after a restart
            if d["kind"] == "Wifi point":
                add_point(d["ip"])
            else:  # re-apply naming rules, so edits to heimdall.toml take effect
                d["kind"] = kind_of(d["ip"], d["name"], d.get("vendor"))
    except (OSError, ValueError, KeyError):
        pass
    load_trends()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # systemd stopping us: save trends on the way out
    for n, h in TARGETS.items():
        forever(pinger, n, h)
    for job in (dns_loop, link_loop, scan_loop, mesh_loop, alert_loop, summary_loop):
        forever(job)
    if MDNS_NAME:
        forever(mdns_loop, MDNS_NAME, MDNS_IP)
    where = f"http://{MDNS_NAME}" + ("" if PORT == 80 else f":{PORT}") if MDNS_NAME else f"http://{MY_IP}:{PORT}"
    print(f"Heimdall: {where}  (router {ROUTER}, network {NET}, log {LOG})")
    HOSTS = allowed_hosts()
    try:
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    finally:
        save_trends()
