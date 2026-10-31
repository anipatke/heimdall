#!/usr/bin/env python3
"""Heimdall - home network health watcher. Run: python3 server.py, then open http://localhost:8765"""
import shutil, sys
if sys.version_info < (3, 11):
    raise SystemExit("Heimdall needs Python 3.11 or newer.")
if not (shutil.which("ip") and shutil.which("ping")):
    raise SystemExit("Heimdall needs the ip and ping commands (Linux).")
import ipaddress, json, os, random, re, socket, struct, subprocess, threading, time, tomllib, csv
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote
from urllib.request import urlopen, Request, build_opener, ProxyHandler, HTTPRedirectHandler

HERE = os.path.dirname(os.path.abspath(__file__))
C_LOCALE = {**os.environ, "LC_ALL": "C"}  # ping and ip output is parsed, so keep it in English whatever the system language


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *a):
        return None


# For reading status pages of devices at home: no proxy, and no following a device's redirect somewhere else.
LOCAL = build_opener(ProxyHandler({}), _NoRedirect)
OUT = build_opener(_NoRedirect)  # alerts: a redirect could point back into this machine, past the checks below


CONFIG = os.environ.get("HEIMDALL_CONFIG", os.path.join(HERE, "heimdall.toml"))

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
    ("logs", "max_mb"): (100, 1, 2000),
}
ALL_ALERTS = ["internet", "wifi_points", "restarts", "new_devices"]


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
try:
    with open("/usr/share/ieee-data/oui.txt", encoding="utf-8", errors="ignore") as f:
        VENDORS = {m.group(1).replace("-", ":").lower(): m.group(2).strip() for line in f if (m := re.match(r"^([0-9A-F]{2}-[0-9A-F]{2}-[0-9A-F]{2})\s+\(hex\)\s+(.+)", line))}
except OSError:
    pass


def vendor_of(mac):
    if not mac or int(mac[:2], 16) & 2:
        return None  # randomised "private" address, as phones use: no maker to find
    return VENDORS.get(mac[:8].lower())

# what "the internet" means: a couple of big, always-up servers
TARGETS = {"router": ROUTER, **CFG.get("internet", {"cloudflare": "1.1.1.1", "google": "8.8.8.8"})}
DNS_NAMES = CFG.get("dns_names", ["google.com", "netflix.com", "amazon.com", "bbc.co.uk", "github.com", "reddit.com", "spotify.com"])
KEEP = 3600  # seconds of history held in memory
LOG = os.path.join(HERE, "log.csv")
DEVICE_LOG = os.path.join(HERE, "devices.csv")
SAVED = os.path.join(HERE, "state.json")  # device list kept across restarts
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


def alert_url_problem(url):
    """Why an alert address can't be used, or None. Alerts go to services out there or elsewhere at home,
    never back into this machine, so the settings page can't be used to poke at what runs here."""
    m = re.fullmatch(r"(https?)://([^/:?#\s]+|\[[0-9a-fA-F:]+\])(:\d+)?([/?#]\S*)?", url) if len(url) <= 500 else None
    if not m:
        return "The alert address should be a web address starting with http:// or https://"
    try:
        addrs = {ipaddress.ip_address(a[4][0].split("%")[0]) for a in socket.getaddrinfo(m.group(2).strip("[]"), None)}
    except (OSError, ValueError):
        return "Couldn't find that alert address"
    if any(a.is_loopback or a.is_link_local or a.is_unspecified or a.is_multicast or str(a) in MY_IPS for a in addrs):
        return "Alerts can't be sent to this machine itself"


def send_alert(text, url=None):
    """Send one alert to [alerts] url. With {text} in the url it's a GET with the message filled in (WhatsApp or
    Signal via CallMeBot); otherwise the message is POSTed as the body (ntfy and most webhooks). Returns an error or None."""
    text = f"Heimdall: {text}"
    url = url or ALERTS["url"]
    if problem := alert_url_problem(url):
        return problem
    try:
        req = url.replace("{text}", quote(text, safe="")) if "{text}" in url else Request(url, data=text.encode())
        with OUT.open(req, timeout=20) as r:
            r.read(65536)
    except Exception as e:
        return f"Couldn't send alert: {e}"


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
    qid = random.randint(0, 65535)
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0" + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        t0 = time.perf_counter()
        s.sendto(q, (server, 53))
        s.recvfrom(512)
        return round((time.perf_counter() - t0) * 1000, 1)
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
                            events.append({"t": now, "ip": ip, "name": d["name"], "what": "left"})
                    continue
                if not d:
                    try:
                        name = socket.gethostbyaddr(ip)[0].removesuffix(".lan")
                    except OSError:
                        name = ""
                    name = NAMES.get(ip, name)
                    vendor = vendor_of(macs.get(ip))
                    if "new_devices" in ALERT_ON and (state["last_scan"] or restored) and macs.get(ip) not in {x.get("mac") for x in devices.values()}:
                        alert(f"New device joined: {name or 'no name'} ({vendor or 'unknown maker'}) at {ip}")
                    d = devices[ip] = {"ip": ip, "name": name, "vendor": vendor, "kind": kind_of(ip, name, vendor), "first": now, "rtt": [], "online": False}
                if not d["online"]:
                    d["online"] = True
                    if state["last_scan"]:  # don't flood the feed on the very first scan
                        events.append({"t": now, "ip": ip, "name": d["name"], "what": "joined"})
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
        with open(SAVED + ".tmp", "w") as f:
            f.write(saved)
        os.replace(SAVED + ".tmp", SAVED)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        out = []
        for d in rows:
            _, avg, mx, lost = d["rtt"][-1]
            out.append([stamp, d["ip"], d["name"], d["kind"], "" if avg is None else avg, "" if mx is None else mx, lost])
        append_log(DEVICE_LOG, out)
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
        with LOCAL.open(f"http://{ip}/api/v1/status", timeout=timeout) as r:
            st = json.loads(r.read(65536))
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
            up = up if type(up) in (int, float) else None
            old = mesh.get(ip)
            with lock:
                if old and up is not None and old.get("uptime") is not None and up < old["uptime"]:
                    name = NAMES.get(ip) or ("Router" if ip == ROUTER else f"Wifi point .{ip.rsplit('.', 1)[1]}")
                    events.append({"t": now, "ip": ip, "name": name, "what": "restarted"})
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
    if not all(isinstance(new.get(s, {}), dict) for s in ("checks", "thresholds", "alerts", "logs", "names")):
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
        host = (self.headers.get("Host") or "").lower()
        host = host[1:host.find("]")] if host.startswith("[") else host.rsplit(":", 1)[0]
        if host in HOSTS:
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
        elif url.path == "/settings":
            self.send(json.dumps({
                "values": {s: {k: conf(s, k) for s2, k in EDITABLE if s2 == s} for s, _ in EDITABLE},
                "defaults": {s: {k: d for (s2, k), (d, _, _) in EDITABLE.items() if s2 == s} for s, _ in EDITABLE},
                "limits": {f"{s}.{k}": [lo, hi] for (s, k), (_, lo, hi) in EDITABLE.items()},
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
    for n, h in TARGETS.items():
        forever(pinger, n, h)
    for job in (dns_loop, link_loop, scan_loop, mesh_loop, alert_loop):
        forever(job)
    if MDNS_NAME:
        forever(mdns_loop, MDNS_NAME, MDNS_IP)
    where = f"http://{MDNS_NAME}" + ("" if PORT == 80 else f":{PORT}") if MDNS_NAME else f"http://{MY_IP}:{PORT}"
    print(f"Heimdall: {where}  (router {ROUTER}, network {NET}, log {LOG})")
    HOSTS = allowed_hosts()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
