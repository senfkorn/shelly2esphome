#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Simulierter Shelly Gen2 zum Testen von shelly2esphome ohne echtes Gerät.

Verhalten wie ein Plus 2PM mit 1.7.5: Shelly.Update lädt das Zip per HTTP, 1.3.3 bootet
"uncommitted" und springt ohne OTA.Commit zurück, komprimierte Zips werden abgelehnt,
nach dem ESPHome-Flash ist der API-Port offen und die Shelly-RPC weg.

Als Programm:   python3 dev/mockshelly.py [--port 18080] [--api-port 6053] [--scenario normal]
Als Modul:      m = MockShelly(port=0, api_port=..., scenario="normal"); m.start(); ...; m.stop()

Szenarien: normal, auth, gen1, unsupported, already133, update_error, no_fetch,
           ignore_commit, reject_esphome, esphome_silent
"""

import argparse
import http.server
import io
import json
import socket
import threading
import time
import urllib.parse
import urllib.request
import zipfile

SCENARIOS = ("normal", "auth", "gen1", "unsupported", "already133", "update_error", "no_fetch",
             "ignore_commit", "reject_esphome", "esphome_silent")


class MockShelly:
    def __init__(self, port=18080, api_port=6053, scenario="normal", host="127.0.0.1",
                 rollback_delay=25, boot_delay=3, esphome_delay=5, quiet=False):
        assert scenario in SCENARIOS, scenario
        self.host, self.api_port, self.scenario = host, api_port, scenario
        self.rollback_delay, self.boot_delay, self.esphome_delay = rollback_delay, boot_delay, esphome_delay
        self.quiet = quiet
        self.calls = []        # alle RPC-Aufrufe (Methode, Parameter)
        self.events = []       # Zustandswechsel, für Tests
        self.ver = "1.3.3" if scenario == "already133" else "1.7.5"
        self.online = True
        self.pending = None
        self.boot_time = time.time()
        self.api_sock = None
        self.timers = []
        mock = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if not mock.online:
                    self.connection.close()
                    return
                u = urllib.parse.urlparse(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                method = u.path.split("/")[-1]
                mock.calls.append((method, q))
                code, r = mock.handle(method, q)
                b = json.dumps(r).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.httpd = http.server.ThreadingHTTPServer((host, port), Handler)
        self.port = self.httpd.server_address[1]
        self.addr = f"{host}:{self.port}"

    # ------------------------------------------------------------ Hilfen
    def log(self, msg):
        self.events.append(msg)
        if not self.quiet:
            print(f"[mock] {msg}", flush=True)

    def later(self, sec, fn):
        t = threading.Timer(sec, fn)
        t.daemon = True
        self.timers.append(t)
        t.start()

    def boot(self, ver, uncommitted=False):
        self.ver, self.online, self.boot_time = ver, True, time.time()
        self.log(f"bootet {ver}{' (uncommitted)' if uncommitted else ''}")
        if uncommitted:
            self.pending = ver

            def check():
                if self.pending == ver:
                    self.log("kein Commit -> Rollback auf 1.7.5")
                    self.pending = None
                    self.boot("1.7.5")
            self.later(self.rollback_delay, check)

    # ------------------------------------------------------------ RPC
    def handle(self, method, q):
        sc = self.scenario
        if method == "Shelly.GetDeviceInfo":
            info = {"name": "Mock", "id": "shellyplus2pm-mock", "gen": 2, "app": "Plus2PM",
                    "ver": self.ver, "auth_en": sc == "auth"}
            if sc == "gen1":
                info["gen"] = 1
            if sc == "unsupported":
                info["app"] = "Mini1PMG3"
            return 200, info
        if method == "Sys.GetStatus":
            return 200, {"uptime": int(time.time() - self.boot_time)}
        if method == "Shelly.Update":
            if sc == "update_error":
                return 500, {"code": -114, "message": "Update in progress"}
            if sc != "no_fetch":
                threading.Thread(target=self.do_update, args=(q["url"],), daemon=True).start()
            return 200, None
        if method == "OTA.Commit":
            self.log(f"OTA.Commit (pending={self.pending})")
            if sc != "ignore_commit":
                self.pending = None
            return 200, None
        if method == "Shelly.Reboot":
            self.online = False
            self.later(self.boot_delay, lambda: self.boot(self.ver))
            return 200, None
        return 404, {"code": 404, "message": f"No handler for {method}"}

    def do_update(self, url):
        try:
            data = urllib.request.urlopen(url, timeout=30).read()
            z = zipfile.ZipFile(io.BytesIO(data))
            m = json.loads(z.read("manifest.json"))
        except Exception as e:
            self.log(f"Download/Zip fehlgeschlagen: {e}")
            return
        if any(i.compress_type != 0 for i in z.infolist()):
            self.log("komprimiertes Zip -> abgelehnt")
            return
        self.log(f"Update geladen: {m['build_id']} ({len(data)} B)")
        if "esphome" in m["build_id"]:
            if self.scenario == "reject_esphome":
                self.log("ESPHome-Paket abgelehnt, bleibe auf " + self.ver)
                return
            self.online = False
            if self.scenario != "esphome_silent":
                self.later(self.esphome_delay, self.start_esphome)
        else:
            self.online = False
            self.later(self.boot_delay, lambda: self.boot(m["version"], uncommitted=True))

    def start_esphome(self):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.api_port))
        s.listen(5)
        self.api_sock = s
        self.log(f"ESPHome läuft, Port {self.api_port} offen")

    # ------------------------------------------------------------ Start/Stopp
    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        for t in self.timers:
            t.cancel()
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.api_sock:
            self.api_sock.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Simulierter Shelly Plus 2PM")
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--api-port", type=int, default=6053)
    ap.add_argument("--scenario", choices=SCENARIOS, default="normal")
    a = ap.parse_args()
    m = MockShelly(a.port, a.api_port, a.scenario).start()
    print(f"[mock] {a.scenario} auf {m.addr}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        m.stop()
