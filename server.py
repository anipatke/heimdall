#!/usr/bin/env python3
"""Heimdall - home network health watcher. Run: python3 server.py, then open http://localhost:8765"""
import json, os, random, re, socket, struct, subprocess, threading, time, csv
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen

PORT = int(os.environ.get("PORT", 8765))
HOST = os.environ.get("HOST", "0.0.0.0")
MDNS_NAME = os.environ.get("MDNS_NAME")  # e.g. heimdall.local - announced to the home network by name
ROUTER = os.environ.get("ROUTER", "192.168.86.1")
SUBNET = ROUTER.rsplit(".", 1)[0]  # assumes a /24 home network
# Kept gentle on purpose: a busy scan clogs Heimdall's own link and makes every reading look worse.
CHECK_EVERY = 60     # re-check devices we already know about (3 pings each, a few at a time)
SWEEP_EVERY = 600    # look for new devices across the whole address range (1 ping each)
_s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
_s.connect((ROUTER, 9))  # no packet sent; just picks the address we'd reach the router from
MY_IP = _s.getsockname()[0]
_s.close()
# every address this machine answers to (it may have a second one just for heimdall.local)
MY_IPS = set(re.findall(r"inet (\S+)/", subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True).stdout)) | {MY_IP}
# (pattern in the device's network name, friendly type) - first match wins
KINDS = [(r"nest-wifi-point|google-wifi", "Wifi point"), (r"google-home|nest-(mini|audio|hub)", "Smart speaker"),
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

TARGETS = {"router": ROUTER, "cloudflare": "1.1.1.1", "google": "8.8.8.8"}
DNS_NAMES = ["google.com", "netflix.com", "amazon.com", "bbc.co.uk", "github.com", "reddit.com", "spotify.com"]
KEEP = 3600  # seconds of history held in memory
HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "log.csv")
DEVICE_LOG = os.path.join(HERE, "devices.csv")
SAVED = os.path.join(HERE, "state.json")  # device list kept across restarts

lock = threading.Lock()
series = {}           # kind -> deque of (t, ms or None)
state = {"link": None, "speed": None, "speed_running": False, "last_scan": None, "last_sweep": None}
devices = {}          # ip -> {ip, name, mac, kind, first, last, online, rtt: [(t, avg, max, lost)]}
events = []           # {t, ip, name, what: joined|left}
points = set()        # wifi points getting nonstop checks
mesh = {}             # ip -> what the router / wifi point says about itself (its local status page)


def record(kind, value):
    now = time.time()
    with lock:
        q = series.setdefault(kind, deque())
        q.append((now, value))
        while q and q[0][0] < now - KEEP:
            q.popleft()
    with open(LOG, "a", newline="") as f:
        csv.writer(f).writerow([time.strftime("%Y-%m-%d %H:%M:%S"), kind, "" if value is None else value])


def pinger(name, host):
    seq_re = re.compile(r"icmp_seq=(\d+).*time=([\d.]+)")
    miss_re = re.compile(r"no answer yet for icmp_seq=(\d+)")
    while True:
        p = subprocess.Popen(["ping", "-n", "-O", "-i", "1", "-W", "2", host],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        late = {}  # seq -> when ping said "no answer yet"; a slow reply can still turn up
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
        time.sleep(5)


def read_link():
    """How Heimdall itself is connected: cable or wifi, and how good that wifi is."""
    route = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True).stdout
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
    out = subprocess.run(["iw", "dev", dev, "link"], capture_output=True, text=True, timeout=10).stdout
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
                         capture_output=True, text=True).stdout
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
        st = subprocess.run(["ip", "neigh", "show", ip], capture_output=True, text=True).stdout.split()
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
    out = subprocess.run(["ip", "neigh"], capture_output=True, text=True).stdout
    return {ip: mac for ip, mac, st in re.findall(r"^(\S+) .*lladdr (\S+).* (\S+)\s*$", out, re.M)
            if ip.startswith(SUBNET + ".") and ip not in MY_IPS and (not confirmed or st == "REACHABLE")}


def scan_loop():
    while True:
        now = time.time()
        macs = neighbours()
        sweep = not state["last_sweep"] or now - state["last_sweep"] >= SWEEP_EVERY
        known = {ip for ip, d in devices.items() if d["online"] or now - d["last"] < 3600} | set(macs)
        known -= MY_IPS
        rest = [f"{SUBNET}.{i}" for i in range(1, 255) if f"{SUBNET}.{i}" not in known | MY_IPS] if sweep else []
        with ThreadPoolExecutor(8) as pool:
            results = dict(zip(known, pool.map(lambda ip: probe(ip, 3), known)))
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
                        d["rtt"] = (d["rtt"] + [(now, None, None, 3)])[-30:]
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
                    vendor = vendor_of(macs.get(ip))
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
        with open(DEVICE_LOG, "a", newline="") as f:
            w = csv.writer(f)
            for d in rows:
                _, avg, mx, lost = d["rtt"][-1]
                w.writerow([stamp, d["ip"], d["name"], d["kind"], "" if avg is None else avg, "" if mx is None else mx, lost])
        time.sleep(max(0, CHECK_EVERY - (time.time() - now)))


def mdns_loop(name, ip):
    """Announce name.local -> ip through avahi, for as long as Heimdall runs. No root needed."""
    import dbus
    group = None
    while True:
        try:
            if group is None or group.GetState() not in (1, 2):  # registering / established
                bus = dbus.SystemBus()
                server = dbus.Interface(bus.get_object("org.freedesktop.Avahi", "/"), "org.freedesktop.Avahi.Server")
                group = dbus.Interface(bus.get_object("org.freedesktop.Avahi", server.EntryGroupNew()), "org.freedesktop.Avahi.EntryGroup")
                # 16 = no reverse record: avahi already names this machine's own addresses, and would call it a collision
                group.AddAddress(-1, 0, dbus.UInt32(16), name, ip)
                group.Commit()
        except Exception as e:  # avahi restarting or missing: try again shortly
            print(f"Couldn't announce {name}: {e}")
            group = None
        time.sleep(30)


def add_point(ip):
    if ip not in points:
        points.add(ip)
        threading.Thread(target=pinger, args=(f"point:{ip}", ip), daemon=True).start()


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
    try:
        with urlopen(f"http://{ip}/api/v1/status", timeout=3) as r:
            role = json.load(r).get("system", {}).get("groupRole")
    except Exception:
        role = None
    d = devices[ip]
    if role:
        with lock:
            d["kind"], d["identified"] = "Wifi point", True
        add_point(ip)
        return
    if d["name"]:
        d["identified"] = True
    elif name := mdns_name(ip):
        d["identified"] = True
        name = re.sub(r"^Android_\w+$", "Android device", name)
        with lock:
            d["name"] = name
            if d["kind"] == "Unknown":
                d["kind"] = "Phone" if name == "Android device" else kind_of(ip, name, d.get("vendor"))


def mesh_loop():
    """Once a minute, read the status page the Nest/Google router and each wifi point serve on the home network."""
    while True:
        for ip in [ROUTER, *sorted(points)]:
            try:
                with urlopen(f"http://{ip}/api/v1/status", timeout=5) as r:
                    st = json.load(r)
            except Exception:
                continue  # not a Google/Nest unit, or busy; the pings still say if it's alive
            sysinfo, sw, wan = st.get("system", {}), st.get("software", {}), st.get("wan", {})
            now, up = time.time(), sysinfo.get("uptime")
            old = mesh.get(ip)
            with lock:
                if old and up is not None and old.get("uptime") is not None and up < old["uptime"]:
                    name = "Router" if ip == ROUTER else f"Wifi point .{ip.rsplit('.', 1)[1]}"
                    events.append({"t": now, "ip": ip, "name": name, "what": "restarted"})
                mesh[ip] = {"ip": ip, "at": now, "uptime": up, "role": sysinfo.get("groupRole"), "model": sysinfo.get("modelId"),
                            "wired": sysinfo.get("lan0Link"), "led": sysinfo.get("ledAnimation"),
                            "version": sw.get("softwareVersion"), "update": sw.get("updateRequired") or sw.get("updateStatus") not in (None, "idle"),
                            "online": wan.get("online") if sysinfo.get("groupRole") == "root" else None,
                            "upstream": wan.get("gatewayIpAddress") if sysinfo.get("groupRole") == "root" else None}
        time.sleep(60)


def speed_test():
    state["speed_running"] = True
    try:
        t0 = time.perf_counter()
        with urlopen("https://speed.cloudflare.com/__down?bytes=25000000", timeout=30) as r:
            n = len(r.read())
        state["speed"] = {"mbps": round(n * 8 / (time.perf_counter() - t0) / 1e6, 1), "at": time.time()}
        record("download_mbps", state["speed"]["mbps"])
    except Exception as e:
        state["speed"] = {"error": str(e), "at": time.time()}
    finally:
        state["speed_running"] = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/data":
            # The page asks only for what's new since its last update, so an open tab stays cheap.
            q = parse_qs(url.query)
            since, seen_scan = float(q.get("since", ["0"])[0]), float(q.get("scan", ["0"])[0])
            with lock:
                fresh = {k: [[round(t, 2), v] for t, v in q if t > since] for k, q in series.items()}
                # the device list only changes once a minute, so skip it if the page already has this one
                devs = None if seen_scan == state["last_scan"] else [{**d, "rtt": d["rtt"][-10:]} for d in devices.values()]
                evs = events[-30:]
            self.send(json.dumps({"now": time.time(), "series": fresh, "targets": TARGETS, "points": sorted(points), "mesh": list(mesh.values()),
                                  "devices": devs, "events": evs, "host": socket.gethostname(), **state},
                                 separators=(",", ":")).encode(), "application/json")
        elif url.path == "/speedtest":
            if not state["speed_running"]:
                threading.Thread(target=speed_test, daemon=True).start()
            self.send(b"{}", "application/json")
        else:
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self.send(f.read(), "text/html; charset=utf-8")


if __name__ == "__main__":
    try:
        with open(SAVED) as f:
            saved = json.load(f)
        devices.update(saved["devices"])
        events.extend(saved["events"])
        for d in devices.values():
            d.pop("tries", None)  # give identification another go after a restart
            if d["kind"] == "Wifi point":
                add_point(d["ip"])
    except (OSError, ValueError, KeyError):
        pass
    for n, h in TARGETS.items():
        threading.Thread(target=pinger, args=(n, h), daemon=True).start()
    threading.Thread(target=dns_loop, daemon=True).start()
    threading.Thread(target=link_loop, daemon=True).start()
    threading.Thread(target=scan_loop, daemon=True).start()
    threading.Thread(target=mesh_loop, daemon=True).start()
    if MDNS_NAME:
        threading.Thread(target=mdns_loop, args=(MDNS_NAME, os.environ.get("MDNS_IP", MY_IP)), daemon=True).start()
    print(f"Dashboard: http://localhost:{PORT}  (log: {LOG})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
