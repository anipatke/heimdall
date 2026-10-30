#!/usr/bin/env python3
"""Heimdall - home network lag watcher. Run: python3 server.py, then open http://localhost:8765"""
import json, os, random, re, socket, struct, subprocess, threading, time, csv
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen

PORT = int(os.environ.get("PORT", 8765))
ROUTER = os.environ.get("ROUTER", "192.168.86.1")
SUBNET = ROUTER.rsplit(".", 1)[0]  # assumes a /24 home network
SCAN_EVERY = 60
_s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
_s.connect((ROUTER, 9))  # no packet sent; just picks the address we'd reach the router from
MY_IP = _s.getsockname()[0]
_s.close()
# (pattern in the device's network name, friendly type) - first match wins
KINDS = [(r"nest-wifi-point|google-wifi", "Wifi point"), (r"google-home|nest-(mini|audio|hub)", "Smart speaker"),
         (r"sonos", "Sonos speaker"), (r"^esp_|tasmota|shelly", "Smart-home gadget"),
         (r"pixel|iphone|android|galaxy|oneplus|note\d|-s\d\d|-a\d\d", "Phone"), (r"ipad|tab", "Tablet"),
         (r"tizen|webos|roku|chromecast|tv", "TV"), (r"^brw|printer|epson|canon|^hp", "Printer"),
         (r"laundry|washer|dryer|fridge|dishwasher", "Appliance"), (r"macbook|laptop|desktop|pc", "Computer")]
TARGETS = {"router": ROUTER, "cloudflare": "1.1.1.1", "google": "8.8.8.8"}
DNS_NAMES = ["google.com", "netflix.com", "amazon.com", "bbc.co.uk", "github.com", "reddit.com", "spotify.com"]
KEEP = 3600  # seconds of history held in memory
HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "log.csv")

lock = threading.Lock()
samples = []          # {t, kind, value(ms or None)}
state = {"wifi": None, "speed": None, "speed_running": False, "last_scan": None}
devices = {}          # ip -> {name, mac, kind, first, last, online, rtt: [(t, avg, max)]}
events = []           # {t, ip, name, what: joined|left}


def record(kind, value):
    now = time.time()
    with lock:
        samples.append({"t": now, "k": kind, "v": value})
        while samples and samples[0]["t"] < now - KEEP:
            samples.pop(0)
    with open(LOG, "a", newline="") as f:
        csv.writer(f).writerow([time.strftime("%Y-%m-%d %H:%M:%S"), kind, "" if value is None else value])


def pinger(name, host):
    seq_re = re.compile(r"icmp_seq=(\d+).*time=([\d.]+)")
    miss_re = re.compile(r"no answer yet for icmp_seq=(\d+)")
    while True:
        p = subprocess.Popen(["ping", "-n", "-O", "-i", "1", "-W", "2", host],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for line in p.stdout:
            if m := seq_re.search(line):
                record(name, float(m.group(2)))
            elif miss_re.search(line):
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


def wifi_loop():
    while True:
        try:
            out = subprocess.run(["netsh.exe", "wlan", "show", "interfaces"], capture_output=True,
                                 text=True, timeout=10).stdout
            info = dict(re.findall(r"^\s+([A-Za-z][\w ()/-]*?)\s+: (.*?)\r?$", out, re.M))
            state["wifi"] = {k: info.get(k) for k in ("State", "SSID", "Signal", "Band", "Channel",
                                                      "Radio type", "Receive rate (Mbps)", "Transmit rate (Mbps)")}
            if info.get("Signal"):
                record("signal", float(info["Signal"].rstrip("%")))
        except Exception:
            state["wifi"] = None
        time.sleep(10)


def probe(ip):
    """Ping a device a few times; returns (avg_ms, max_ms) or None if it didn't answer."""
    out = subprocess.run(["ping", "-n", "-q", "-c", "4", "-i", "0.25", "-W", "1", ip],
                         capture_output=True, text=True).stdout
    m = re.search(r"= [\d.]+/([\d.]+)/([\d.]+)", out)
    return (float(m.group(1)), float(m.group(2))) if m else None


def kind_of(ip, name):
    if ip == ROUTER:
        return "Router"
    if ip == MY_IP:
        return "Heimdall (this machine)"
    for pat, kind in KINDS:
        if re.search(pat, name or "", re.I):
            return kind
    return "Unknown"


def scan_loop():
    while True:
        now = time.time()
        with ThreadPoolExecutor(64) as pool:
            ips = [f"{SUBNET}.{i}" for i in range(1, 255)]
            results = dict(zip(ips, pool.map(probe, ips)))
        macs = dict(re.findall(r"^(\S+) .*lladdr (\S+)", subprocess.run(["ip", "neigh"], capture_output=True, text=True).stdout, re.M))
        with lock:
            for ip, r in results.items():
                d = devices.get(ip)
                if r is None:
                    # one missed scan can be a sleepy phone; call it gone after two
                    if d and d["online"] and now - d["last"] > SCAN_EVERY * 1.5:
                        d["online"] = False
                        events.append({"t": now, "ip": ip, "name": d["name"], "what": "left"})
                    continue
                if not d:
                    try:
                        name = socket.gethostbyaddr(ip)[0].removesuffix(".lan")
                    except OSError:
                        name = ""
                    d = devices[ip] = {"ip": ip, "name": name, "kind": kind_of(ip, name), "first": now, "rtt": [], "online": False}
                if not d["online"]:
                    d["online"] = True
                    if state["last_scan"]:  # don't flood the feed on the very first scan
                        events.append({"t": now, "ip": ip, "name": d["name"], "what": "joined"})
                d["last"] = now
                d["mac"] = macs.get(ip, d.get("mac"))
                d["rtt"] = (d["rtt"] + [(now, *r)])[-60:]
            del events[:-200]
            state["last_scan"] = now
        time.sleep(max(0, SCAN_EVERY - (time.time() - now)))


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
        if self.path.startswith("/data"):
            with lock:
                data = list(samples)
                devs = [dict(d) for d in devices.values()]
                evs = list(events)
            self.send(json.dumps({"samples": data, "targets": TARGETS, "devices": devs, "events": evs,
                                  "host": socket.gethostname(), **state}).encode(), "application/json")
        elif self.path == "/speedtest":
            if not state["speed_running"]:
                threading.Thread(target=speed_test, daemon=True).start()
            self.send(b"{}", "application/json")
        else:
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self.send(f.read(), "text/html; charset=utf-8")


if __name__ == "__main__":
    for n, h in TARGETS.items():
        threading.Thread(target=pinger, args=(n, h), daemon=True).start()
    threading.Thread(target=dns_loop, daemon=True).start()
    threading.Thread(target=wifi_loop, daemon=True).start()
    threading.Thread(target=scan_loop, daemon=True).start()
    print(f"Dashboard: http://localhost:{PORT}  (log: {LOG})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
