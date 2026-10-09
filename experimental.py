# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in Gen3/Gen4 testing. No Gen2 bootloader, downgrade or generic chip unlock."""
import hashlib
import io
import json
import re
import socket
import struct
import time
import zipfile
from pathlib import Path

PROFILES = {
    "PlugSG3": {"gen": 3, "chip": 5, "variant": "ESP32C3", "slot_size": 0x2a0000},
    "S2PMG4": {"gen": 4, "chip": 13, "variant": "ESP32C6", "slot_size": 0x300000},
}
RESULTS = ("boot", "reboot", "ota1", "ota2", "functions")
MAX_FILE = 8 * 1024 * 1024


class Session:
    """Whitelist-only report: never serialize RPC responses, errors or raw logs."""
    def __init__(self, core):
        self.core = core
        self.report = {"schema": 1, "tool": "shelly2esphome", "experimental": True,
                       "feature_revision": 1,
                       "device": {"profile": "unknown"}, "checks": {},
                       "outcome": "not_started", "tests": {k: "unknown" for k in RESULTS}}
        self.profile = None
        self.info = None

    def fail(self, code):
        self.report["outcome"] = "blocked"
        self.report["checks"][code] = "failed"
        raise self.core.Abort(code + ": see docs/EXPERIMENTAL.md / siehe docs/EXPERIMENTAL.md")

    def identify(self, ip):
        info = self.core.rpc(ip, "Shelly.GetDeviceInfo")
        if not isinstance(info, dict):
            self.fail("device_response")
        self.info = info
        gen = info.get("gen")
        if type(gen) is int and gen in (1, 2, 3, 4):
            self.report["device"]["generation"] = gen
        ver = info.get("ver", "")
        if isinstance(ver, str) and re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}(?:-[a-z]+\d{0,4})?", ver):
            self.report["device"]["stock_version"] = ver
        name = info.get("app")
        profile = PROFILES.get(name) if isinstance(name, str) else None
        if profile and gen == profile["gen"]:
            self.profile = profile
            self.report["device"]["profile"] = name
            self.report["checks"]["profile"] = "passed"
        else:
            self.report["checks"]["profile"] = "failed"
        self.report["checks"]["authentication"] = "failed" if info.get("auth_en") else "passed"
        self.report["outcome"] = "identified"
        return info

    def export(self, path, results=()):
        for result in results:
            key, sep, value = result.partition("=")
            if not sep or key not in RESULTS or value not in ("passed", "failed", "unknown"):
                raise self.core.Abort("--test-result: boot|reboot|ota1|ota2|functions=passed|failed|unknown")
            self.report["tests"][key] = value
        Path(path).write_text(json.dumps(self.report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def prepare(self, ip, factory_path, stock_path):
        info = self.identify(ip)
        if self.profile is None:
            self.fail("profile")
        if info.get("auth_en"):
            self.fail("authentication")
        self.check_transport(ip)
        if not stock_path or not factory_path:
            self.fail("factory_and_stock_required")
        factory = bounded_read(factory_path)
        stock = bounded_read(stock_path, MAX_FILE * 3)
        self.report["artifacts"] = {"factory_sha256": hashlib.sha256(factory).hexdigest(),
                                    "stock_sha256": hashlib.sha256(stock).hexdigest()}
        try:
            package, layout = build_package(self.core, info, self.profile, factory, stock)
        except (self.core.Abort, ValueError, KeyError, TypeError, struct.error, zipfile.BadZipFile, UnicodeError):
            self.fail("package_layout")
        self.report["layout"] = layout
        self.report["checks"]["package_layout"] = "passed"
        self.report["outcome"] = "preflight_passed"
        return package

    def check_transport(self, ip):
        config = self.core.rpc(ip, "Sys.GetConfig")
        if not isinstance(config, dict) or not isinstance(config.get("device"), dict):
            self.fail("security_state_unknown")
        if config["device"].get("enhanced_security", False):
            self.fail("https_required")
        self.report["checks"]["http_update_transport"] = "passed"

    def export_partitions(self, ip, stock_path, output):
        info = self.identify(ip)
        if self.profile is None or info.get("auth_en") or not stock_path:
            self.fail("profile_and_stock_required")
        stock = bounded_read(stock_path, MAX_FILE * 3)
        try:
            with zipfile.ZipFile(io.BytesIO(stock)) as z:
                if z.getinfo("manifest.json").file_size > 65536:
                    self.fail("stock_manifest")
                m = json.loads(z.read("manifest.json"))
                if m["name"] != info["app"] or m["version"] != info["ver"] or m["parts"]["pt"]["addr"] != 0x10000:
                    self.fail("stock_identity")
                part = m["parts"]["pt"]
                if z.getinfo(part["src"]).file_size != 4096:
                    self.fail("stock_partition_size")
                data = z.read(part["src"])
                for algorithm in ("sha1", "sha256"):
                    if algorithm in part and hashlib.new(algorithm, data).hexdigest() != part[algorithm]:
                        self.fail("stock_partition_checksum")
            layout = partitions(data)
        except (KeyError, ValueError, TypeError, zipfile.BadZipFile):
            self.fail("stock_layout")
        text = "# Name, Type, SubType, Offset, Size, Flags\n"
        text += "".join(f"{name},0x{kind:x},0x{sub:x},0x{off:x},0x{size:x},\n"
                        for name, kind, sub, off, size, flags in layout)
        Path(output).write_text(text, encoding="utf-8")
        self.report["outcome"] = "partition_csv_exported"

    def flash(self, ip, package, ui, port=0):
        # Check again immediately before writes; a package is bound to the stock version.
        original = (self.info["app"], self.info["gen"], self.info["ver"])
        current = self.identify(ip)
        if (current.get("app"), current.get("gen"), current.get("ver")) != original or current.get("auth_en"):
            self.fail("device_changed")
        self.check_transport(ip)
        if self.core.port_open(ip, self.core.ESPHOME_API_PORT):
            self.fail("api_already_open")
        slot = probe_slot(self.core, ip)
        self.report["target_slot"] = slot if slot in (0, 1) else "unknown"
        if slot != 0:
            self.fail("target_slot_zero_required")
        self.report["checks"]["target_slot"] = "passed"
        server = self.core.FileServer(port, ui)
        try:
            server.files["esphome.zip"] = package
            url = f"http://{self.core.local_ip_for(ip)}:{server.port}/esphome.zip"
            self.report["outcome"] = "update_started"
            self.core.flash_esphome(ip, url, ui, server)
            if "esphome.zip" not in server.completed:
                self.fail("download_incomplete")
            # TCP listener is only a hint, never proof of boot or future OTA success.
            self.report["outcome"] = "api_port_observed"
            ui.log("API-Port erreichbar / API port reachable. Reboot + OTA1 + OTA2 noch testen / still need testing.")
        finally:
            server.close()


def bounded_read(path, limit=MAX_FILE):
    with open(path, "rb") as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ValueError("file_too_large")
    return data


def partitions(data):
    """Validate full IDF table, including MD5 when present and non-overlapping ranges."""
    entries = []
    for offset in range(0, len(data), 32):
        e = data[offset:offset + 32]
        if e[:2] == b"\xeb\xeb":
            if len(e) != 32 or e[16:] != hashlib.md5(data[:offset]).digest():
                raise ValueError("partition_md5")
            break
        if e == b"\xff" * 32:
            break
        if len(e) != 32 or e[:2] != b"\xaa\x50":
            raise ValueError("partition_entry")
        kind, sub, address, size, label, flags = struct.unpack("<BBII16sI", e[2:])
        label = label.rstrip(b"\0").decode("ascii")
        if not size or address < 0x11000 or address + size > MAX_FILE or flags:
            raise ValueError("partition_range")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", label):
            raise ValueError("partition_label")
        if label in [p[0] for p in entries] or any(address < p[3] + p[4] and p[3] < address + size for p in entries):
            raise ValueError("partition_overlap")
        entries.append((label, kind, sub, address, size, flags))
    else:
        raise ValueError("partition_terminator")
    if not entries:
        raise ValueError("partition_empty")
    return entries


def build_package(core, info, profile, factory, stock):
    def require(condition):
        if not condition:
            raise ValueError("incompatible_package")
    with zipfile.ZipFile(io.BytesIO(stock)) as z:
        names = z.namelist()
        require(len(names) == len(set(names)) and len(names) <= 20)
        require(all(i.file_size <= MAX_FILE for i in z.infolist()))
        require(sum(i.file_size for i in z.infolist()) <= MAX_FILE * 3)
        m = json.loads(z.read("manifest.json"))
        require(m["name"] == info["app"] and m["version"] == info["ver"])
        expected_platform = "esp32c3" if profile["chip"] == 5 else "esp32c6"
        require(m.get("platform", expected_platform) == expected_platform)
        parts = m["parts"]
        require(set(parts) == {"boot", "pt", "otadata", "app", "fs"})
        require(parts["boot"]["addr"] == 0 and parts["pt"]["addr"] == 0x10000)
        require(parts["app"]["ptn"] == "app_0" and parts["fs"]["ptn"] == "fs_0")
        require(parts["otadata"]["ptn"] == "otadata")
        files = {}
        require(len({p["src"] for p in parts.values()}) == len(parts))
        for p in parts.values():
            require(p.get("encrypt", False) is False)
            data = z.read(p["src"])
            for algorithm in ("sha1", "sha256"):
                if algorithm in p:
                    require(hashlib.new(algorithm, data).hexdigest() == p[algorithm])
            if "size" in p:
                require(p["size"] == len(data))
            files[p["src"]] = data
        require(all(p.get("type") == name for name, p in parts.items()))
    pt = files[parts["pt"]["src"]]
    layout = partitions(pt)
    require(layout == partitions(factory[0x10000:0x11000]))
    table = {p[0]: p for p in layout}
    slot_size = profile["slot_size"]
    require(table["app_0"][1:5] == (0, 0x10, 0x20000, slot_size))
    require(table["app_1"][1] == 0 and table["app_1"][2] == 0x11 and table["app_1"][4] == slot_size)
    require(table["otadata"][1:5] == (1, 0, 0x11000, 0x2000))
    require(table["fs_0"][1:3] == (1, 0x82))
    for name in ("app", "otadata", "fs"):
        part = parts[name]
        entry = table[part["ptn"]]
        require("addr" not in part or part["addr"] == entry[3])
        require(len(files[part["src"]]) <= entry[4])
    bl_len, bl_chip, bl_desc = core.esp_image_info(factory)
    app_len, app_chip, app_desc = core.esp_image_info(factory, 0x20000)
    _, stock_chip, _ = core.esp_image_info(files[parts["boot"]["src"]])
    require(bl_chip == app_chip == stock_chip == profile["chip"])
    require(not bl_desc and bool(app_desc) and bl_len < 0x10000 and app_len <= slot_size)
    # Reject signed/extra payloads instead of stripping signature blocks silently.
    require(factory[23] == 1 and factory[0x20000 + 23] == 1)
    require(all(b == 255 for b in factory[bl_len:0x10000]))
    require(all(b == 255 for b in factory[0x20000 + app_len:]))
    # 8 MB flash size encoded in the high nibble. Compare mode/frequency to stock.
    stock_boot = files[parts["boot"]["src"]]
    require(factory[3] >> 4 == 3 and factory[2:4] == stock_boot[2:4])
    require(factory[0x20002:0x20004] == factory[2:4])
    # No higher minimum silicon revision than the matching official images.
    stock_app = files[parts["app"]["src"]]
    _, stock_app_chip, _ = core.esp_image_info(stock_app)
    require(stock_app_chip == profile["chip"])
    for ours, reference in ((factory, stock_boot), (factory[0x20000:], stock_app)):
        require(ours[14] <= reference[14])
        require(struct.unpack_from("<H", ours, 15)[0] <= struct.unpack_from("<H", reference, 15)[0])
        require(struct.unpack_from("<H", ours, 17)[0] >= struct.unpack_from("<H", reference, 17)[0])
    require(parts["boot"].get("min_version", "0.0.0") in ("0.0.0", "1.0.0", "1.0.1", "1.0.2", "1.0.3"))
    files[parts["boot"]["src"]] = factory[:bl_len]
    files[parts["app"]["src"]] = factory[0x20000:0x20000 + app_len]
    # Existing public conversion uses 1.0.9 to trigger stock bootloader replacement.
    # This is experimental; the report never asserts replacement or hardware success.
    parts["boot"]["min_version"] = "1.0.9"
    m["version"] = "99.0.0"
    m["build_id"] = "shelly2esphome-experimental"
    for p in parts.values():
        data = files[p["src"]]
        p.update(size=len(data), sha1=hashlib.sha1(data).hexdigest(), sha256=hashlib.sha256(data).hexdigest())
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        z.writestr("manifest.json", json.dumps(m))
        for name, data in files.items():
            z.writestr(name, data)
    return out.getvalue(), {"chip_id": app_chip, "boot_offset": 0, "partition_offset": 0x10000,
                            "app_offset": 0x20000, "app_bytes": app_len, "slot_bytes": slot_size}


def probe_slot(core, ip, timeout=6):
    """Temporary UDP logging, restored before flash; never persist raw log bytes."""
    config = core.rpc(ip, "Sys.GetConfig")
    old_addr = config["debug"]["udp"]["addr"]
    host = socket.gethostbyname(core.host_of(ip))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((core.local_ip_for(ip), 0))
        sock.settimeout(0.25)
        addr = f"{sock.getsockname()[0]}:{sock.getsockname()[1]}"
        slots = set()
        try:
            core.rpc(ip, "Sys.SetConfig", {"config": {"debug": {"udp": {"addr": addr}}}})
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                try:
                    data, source = sock.recvfrom(8192)
                except socket.timeout:
                    continue
                if source[0] != host:
                    continue
                slots.update(int(x) for x in re.findall(rb"Storing core dumps to app_([01])\b", data))
        finally:
            core.rpc(ip, "Sys.SetConfig", {"config": {"debug": {"udp": {"addr": old_addr}}}})
            restored = core.rpc(ip, "Sys.GetConfig")
            if restored["debug"]["udp"]["addr"] != old_addr:
                raise core.Abort("debug_restore_failed: no flash / kein Flash")
    return next(iter(slots)) if len(slots) == 1 else None


def run_cli(core, args):
    session = Session(core)
    try:
        if not args.ip:
            raise core.Abort("Experimental: IP erforderlich / IP required")
        if args.diagnose:
            session.identify(args.ip)
            print(json.dumps(session.report, indent=2))
            return
        if args.export_partitions:
            session.export_partitions(args.ip, args.shelly_zip, args.export_partitions)
            return
        package = session.prepare(args.ip, args.firmware, args.shelly_zip)
        if args.check:
            print("Experimental preflight OK; device unchanged / Geraet unveraendert.")
            return
        if args.build_only:
            path = "esphome-experimental-" + session.info["app"] + ".zip"
            Path(path).write_bytes(package)
            print(path)
            return
        print("EXPERIMENTAL: untested hardware. Serial recovery may be needed. / Hardware ungetestet, serielle Rettung ggf. erforderlich.")
        if not args.yes and input("FLASH eingeben / type FLASH: ").strip() != "FLASH":
            session.report["outcome"] = "cancelled"
            return
        session.flash(args.ip, package, core.UI(), args.port)
    except core.Abort:
        if session.report["outcome"] != "blocked":
            session.report["outcome"] = "error"
        raise
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as e:
        session.report["outcome"] = "blocked"
        raise core.Abort("Experimental input/network error / Eingabe-/Netzwerkfehler (" + type(e).__name__ + ")") from None
    finally:
        if args.report:
            try:
                session.export(args.report, args.test_result)
            except OSError as e:
                raise core.Abort("Cannot write diagnostic report / Diagnosebericht nicht schreibbar (" + type(e).__name__ + ")") from None


def run_gui(core, parent):
    """Separate opt-in window keeps Gen2 controls and semantics intact."""
    import queue
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    window = tk.Toplevel(parent)
    window.transient(parent)
    window.grab_set()
    window.title("Gen3 / Gen4 - Experimental")
    window.geometry("720x520")
    frame = ttk.Frame(window, padding=16)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="Experimental: PlugSG3 / S2PMG4. Hardware untested / ungetestet.\n"
              "Factory BIN + matching official stock ZIP required. Read docs/EXPERIMENTAL.md.", wraplength=680).pack(anchor="w")
    values = {}
    for key, label, file in (("ip", "IP", False), ("factory", "ESPHome factory BIN", True), ("stock", "Official stock ZIP", True)):
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=label, width=24).pack(side="left")
        var = tk.StringVar()
        values[key] = var
        ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True)
        if file:
            def browse(v=var):
                path = filedialog.askopenfilename(parent=window)
                if path:
                    v.set(path)
            ttk.Button(row, text="...", command=browse).pack(side="right")
    status = tk.StringVar(value="No automatic uploads / Keine automatischen Uploads.")
    session = Session(core)
    events = queue.Queue()
    busy = False
    buttons = []

    def task(mode):
        nonlocal session, busy
        if busy:
            return
        ip, factory, stock = [values[k].get().strip() for k in ("ip", "factory", "stock")]
        output = None
        if mode in ("package", "csv"):
            output = filedialog.asksaveasfilename(parent=window, defaultextension=".csv" if mode == "csv" else ".zip")
            if not output:
                return
        if mode == "flash" and not messagebox.askyesno("Experimental", "Ungetestet: serielle Rettung ggf. erforderlich.\nUntested: serial recovery may be required.\n\nFlash?", parent=window):
            return
        session = Session(core)
        for value in tests.values():
            value.set("unknown")
        busy = True
        for b in buttons:
            b.configure(state="disabled")
        status.set("Working / Arbeite...")
        def job():
            try:
                if mode == "diagnose":
                    session.identify(ip)
                elif mode == "csv":
                    session.export_partitions(ip, stock, output)
                else:
                    package = session.prepare(ip, factory, stock)
                    if mode == "package":
                        Path(output).write_bytes(package)
                    elif mode == "flash":
                        session.flash(ip, package, core.UI())
                events.put(session.report["outcome"])
            except Exception as e:
                if session.report["outcome"] != "blocked":
                    session.report["outcome"] = "error"
                # On-screen errors stay local. Report contains no exception message.
                events.put(str(e))
        threading.Thread(target=job, daemon=True).start()

    row = ttk.Frame(frame)
    row.pack(fill="x", pady=8)
    for mode, label in (("diagnose", "Diagnose"), ("csv", "Export CSV"), ("check", "Preflight"), ("package", "Save ZIP"), ("flash", "Flash (experimental)")):
        b = ttk.Button(row, text=label, command=lambda m=mode: task(m))
        b.pack(side="left", padx=2)
        buttons.append(b)
    tests = {}
    ttk.Label(frame, text="Tester results / Testergebnisse (manuell):").pack(anchor="w", pady=(12, 4))
    for key in RESULTS:
        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Label(row, text=key, width=24).pack(side="left")
        v = tk.StringVar(value="unknown")
        tests[key] = v
        ttk.Combobox(row, textvariable=v, values=("unknown", "passed", "failed"), state="readonly", width=12).pack(side="left")
    def export():
        if busy:
            return
        path = filedialog.asksaveasfilename(parent=window, initialfile="shelly-diagnostic.json", defaultextension=".json")
        if path:
            try:
                session.export(path, [f"{k}={v.get()}" for k, v in tests.items()])
                status.set("Report saved / Bericht gespeichert.")
            except OSError as e:
                messagebox.showerror("Report", str(e), parent=window)
    export_btn = ttk.Button(frame, text="Export diagnostic JSON / Diagnose exportieren", command=export)
    export_btn.pack(anchor="w", pady=10)
    buttons.append(export_btn)
    ttk.Label(frame, textvariable=status, wraplength=680).pack(anchor="w")
    def poll():
        nonlocal busy
        try:
            status.set(events.get_nowait())
            busy = False
            for b in buttons:
                b.configure(state="normal")
        except queue.Empty:
            pass
        window.after(100, poll)
    window.protocol("WM_DELETE_WINDOW", lambda: None if busy else window.destroy())
    poll()
