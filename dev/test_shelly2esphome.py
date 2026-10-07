#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Tests für shelly2esphome, ohne echtes Gerät (nur Standardbibliothek).

    python3 dev/test_shelly2esphome.py            alle Tests
    python3 dev/test_shelly2esphome.py -k Flow    nur die Ablauf-Tests gegen den Mock

Alle Netzwerkzugriffe gehen ausschließlich an 127.0.0.1 (Mock). Ein Schutz in setUpModule
blockiert jeden RPC an andere Adressen, damit nie versehentlich ein echter Shelly geflasht wird.
"""

import hashlib
import io
import json
import os
import re
import socket
import struct
import builtins
import contextlib
import sys
import tempfile
import unittest
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import shelly2esphome as s  # noqa: E402
from mockshelly import MockShelly  # noqa: E402

SCRIPT = os.path.join(ROOT, "shelly2esphome.py")
# Echte Shelly-Firmware liegt nicht im Repo. Die Tests nutzen synthetische Zips (siehe
# make_shelly_zip) und prüfen echte Dateien nur, wenn firmware/*.zip vorhanden ist.
ORIG_SHA256 = dict(s.SHELLY_FW_SHA256)
REAL_FW_DIR = os.path.join(ROOT, "firmware")
HAVE_REAL_SHELLY_FW = all(os.path.exists(os.path.join(REAL_FW_DIR, f"{m}-{s.SHELLY_FW_VERSION}.zip"))
                          for m in ORIG_SHA256)


# ---------------------------------------------------------------- Test-Firmware

def make_image(project="testgeraet", version="2026.9.1", chip=0, multicore=False, size=200 * 1024,
               with_desc=True, hash_appended=True):
    """Erzeugt ein gültiges ESP32-App-Image (Header, ein Segment, Prüfsumme, SHA-256)."""
    desc = b""
    if with_desc:
        desc = (struct.pack("<I", 0xABCD5432) + b"\0" * 12 + version.encode().ljust(32, b"\0")
                + project.encode().ljust(32, b"\0") + b"\0" * 32 + b"v5.5.5".ljust(32, b"\0")).ljust(256, b"\0")
    body = desc + bytes((i * 7) & 0xFF for i in range(size))
    if multicore:
        body += s.MULTICORE_MARKER
    body += b"\0" * (-len(body) % 4)
    hdr = (bytes([0xE9, 1, 2, 2]) + struct.pack("<I", 0x400D0000) + bytes([0xEE, 0, 0, 0])
           + struct.pack("<H", chip) + bytes(9) + bytes([1 if hash_appended else 0]))
    img = hdr + struct.pack("<II", 0x3F400020, len(body)) + body
    cs = 0xEF
    for b in body:
        cs ^= b
    img += b"\0" * (15 - len(img) % 16) + bytes([cs])
    if hash_appended:
        img += hashlib.sha256(img).digest()
    return img


def make_shelly_zip(model):
    """Synthetische Shelly-Firmware 1.3.3 im Updateformat (gleiches Layout wie das Original)."""
    def pt_entry(label, ptype, sub, offset, size):
        return (b"\xaa\x50" + bytes([ptype, sub]) + struct.pack("<II", offset, size)
                + label.encode().ljust(16, b"\0") + b"\0" * 4)
    pt = (pt_entry("nvs", 1, 2, 0x9000, 0x4000) + pt_entry("otadata", 1, 0, 0xD000, 0x2000)
          + pt_entry("app_0", 0, 0x10, 0x10000, 0x180000) + pt_entry("app_1", 0, 0x11, 0x190000, 0x180000)
          + pt_entry("fs_0", 1, 0x82, 0x310000, 0x70000))
    pt = pt.ljust(0xC00, b"\xff").ljust(4096, b"\xff")
    fs_len = len(s.lzma.decompress(s.base64.b64decode("".join(s.TESTED_FS_IMAGE))))
    files = {"bootloader.bin": make_image(project="shelly-boot", size=20 * 1024),
             "partition-table.bin": pt, "otadata.bin": b"\xff" * 8192,
             "app.bin": make_image(project="shelly", size=300 * 1024), "fs.img": b"\xff" * fs_len}
    manifest = {"name": model, "version": s.SHELLY_FW_VERSION, "build_id": "20240625-122830/1.3.3-test",
                "parts": {"boot": {"type": "boot", "src": "bootloader.bin", "addr": 4096},
                          "pt": {"type": "pt", "src": "partition-table.bin", "addr": 32768},
                          "otadata": {"type": "otadata", "src": "otadata.bin", "ptn": "otadata"},
                          "app": {"type": "app", "src": "app.bin", "ptn": "app_0"},
                          "fs": {"type": "fs", "src": "fs.img", "ptn": "fs_0", "size": fs_len}}}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        z.writestr("manifest.json", json.dumps(manifest))
        for name, data in files.items():
            z.writestr(name, data)
    return out.getvalue()


def make_factory(app, bootloader):
    data = bytearray(b"\xff" * 0x10000)
    data[0x1000:0x1000 + len(bootloader)] = bootloader
    return bytes(data) + app


class Collector(s.UI):
    def __init__(self):
        self.lines, self.steps = [], []

    def log(self, msg="", kind=None):
        self.lines.append((msg, kind))

    def step(self, n, state):
        self.steps.append((n, state))

    def text(self):
        return "\n".join(m for m, _ in self.lines)


def free_port():
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


_real_rpc = s.rpc
_real_app_dir = s.app_dir
_fw_tmp = None


def setUpModule():
    global _fw_tmp
    s.set_lang("de")
    s.CONFIG_PATH = os.path.join(tempfile.gettempdir(), "s2e-test-config-unused.json")
    # Synthetische Shelly-Firmware statt der echten (die nicht im Repo liegt)
    _fw_tmp = tempfile.TemporaryDirectory()
    os.mkdir(os.path.join(_fw_tmp.name, "firmware"))
    for model in ORIG_SHA256:
        data = make_shelly_zip(model)
        with open(os.path.join(_fw_tmp.name, "firmware", f"{model}-{s.SHELLY_FW_VERSION}.zip"), "wb") as f:
            f.write(data)
        s.SHELLY_FW_SHA256[model] = hashlib.sha256(data).hexdigest()
    s.app_dir = lambda: _fw_tmp.name

    def guarded_rpc(ip, *a, **k):
        if not ip.startswith("127.0.0.1"):
            raise RuntimeError(f"Test-Schutz: RPC an {ip} blockiert")
        return _real_rpc(ip, *a, **k)
    s.rpc = guarded_rpc


def tearDownModule():
    s.rpc = _real_rpc
    s.app_dir = _real_app_dir
    s.SHELLY_FW_SHA256.update(ORIG_SHA256)
    _fw_tmp.cleanup()


class TmpFiles:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        p = os.path.join(self.tmp.name, name)
        with open(p, "wb") as f:
            f.write(data)
        return p


# ---------------------------------------------------------------- Firmware-Images

class ImageTest(TmpFiles, unittest.TestCase):
    def test_ota_format(self):
        fw = s.Firmware(self.write("a.bin", make_image()))
        self.assertEqual(fw.desc["project"], "testgeraet")
        self.assertEqual(fw.sources, ["fmt_ota", "fmt_default_bl"])
        self.assertFalse(fw.multicore)

    def test_factory_format(self):
        bl = s.base64.b64decode("".join(s.DEFAULT_BOOTLOADER_B64))
        app = make_image(project="fabrik")
        fw = s.Firmware(self.write("f.bin", make_factory(app, bl)))
        self.assertEqual(fw.sources, ["fmt_factory"])
        self.assertEqual(fw.app, app)
        self.assertEqual(fw.bootloader, bl)

    def test_trailing_garbage_is_cut(self):
        app = make_image()
        fw = s.Firmware(self.write("g.bin", app + b"\xff" * 1000))
        self.assertEqual(fw.app, app)

    def test_without_hash(self):
        s.Firmware(self.write("h.bin", make_image(hash_appended=False)))

    def test_multicore_detected(self):
        self.assertTrue(s.Firmware(self.write("m.bin", make_image(multicore=True))).multicore)

    def test_errors(self):
        img = make_image()
        cases = {
            "err_unknown_format": b"\x00" * 0x20000,
            "err_truncated": img[:len(img) // 2],
            "err_not_esp32": make_image(chip=5),
            "err_no_desc": make_image(with_desc=False),
        }
        flipped = bytearray(img)
        flipped[5000] ^= 0x01
        cases["err_checksum"] = bytes(flipped)
        bad_sha = bytearray(img)
        bad_sha[-1] ^= 0x01
        cases["err_checksum "] = bytes(bad_sha)
        for key, data in cases.items():
            with self.subTest(key=key):
                with self.assertRaises(s.Abort) as cm:
                    s.Firmware(self.write("x.bin", data))
                expected = s.T(key.strip(), chip=5) if "{" in s.TEXTS[key.strip()][0] else s.T(key.strip())
                self.assertEqual(str(cm.exception), expected)

    def test_bad_magic(self):
        with self.assertRaises(s.Abort) as cm:
            s.esp_image_info(b"\x00" * 100)
        self.assertEqual(str(cm.exception), s.T("err_magic"))

    def test_missing_file(self):
        with self.assertRaises(s.Abort) as cm:
            s.Firmware(os.path.join(self.tmp.name, "gibtsnicht.bin"))
        self.assertIn("gibtsnicht.bin", str(cm.exception))

    @unittest.skipUnless(os.environ.get("S2E_TEST_FIRMWARE"), "S2E_TEST_FIRMWARE nicht gesetzt")
    def test_real_firmwares(self):
        # Echte ESPHome-Builds prüfen: S2E_TEST_FIRMWARE=a.bin:b.bin (durch os.pathsep getrennt)
        for p in os.environ["S2E_TEST_FIRMWARE"].split(os.pathsep):
            with self.subTest(path=p):
                fw = s.Firmware(p)
                self.assertTrue(fw.desc["project"])


# ---------------------------------------------------------------- Paket

class PackageTest(TmpFiles, unittest.TestCase):
    def test_all_models(self):
        fw = s.Firmware(self.write("a.bin", make_image()))
        for model in s.SHELLY_FW_SHA256:
            with self.subTest(model=model):
                pkg = s.build_package(s.get_shelly_zip(model, Collector(), allow_download=False), fw)
                z = zipfile.ZipFile(io.BytesIO(pkg))
                self.assertTrue(all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist()))
                m = json.loads(z.read("manifest.json"))
                self.assertEqual(z.read(m["parts"]["app"]["src"]), fw.app)
                self.assertEqual(z.read(m["parts"]["boot"]["src"]), fw.bootloader)
                self.assertEqual(m["parts"]["boot"]["min_version"], "0.0.0")
                self.assertIn("esphome", m["build_id"])
                for p in m["parts"].values():
                    if "src" in p:
                        d = z.read(p["src"])
                        self.assertEqual(p["size"], len(d))
                        self.assertEqual(p["cs_sha256"], hashlib.sha256(d).hexdigest())
                        self.assertEqual(p["cs_sha1"], hashlib.sha1(d).hexdigest())

    def test_too_big(self):
        fw = s.Firmware(self.write("big.bin", make_image(size=1600 * 1024)))
        with self.assertRaises(s.Abort) as cm:
            s.build_package(s.get_shelly_zip("Plus2PM", Collector(), allow_download=False), fw)
        self.assertIn("1572864", str(cm.exception))

    def test_wrong_hash_rejected(self):
        p = self.write("other.zip", make_shelly_zip("Plus2PM") + b"x")
        with self.assertRaises(s.Abort) as cm:
            s.get_shelly_zip("Plus2PM", Collector(), path=p)
        self.assertEqual(str(cm.exception), s.T("err_shelly_fw_corrupt", source=p))

    @unittest.skipUnless(HAVE_REAL_SHELLY_FW, "echte firmware/*.zip nicht vorhanden")
    def test_real_shelly_firmware(self):
        fw = s.Firmware(self.write("a.bin", make_image()))
        for model, sha in ORIG_SHA256.items():
            with self.subTest(model=model):
                p = os.path.join(REAL_FW_DIR, f"{model}-{s.SHELLY_FW_VERSION}.zip")
                data = open(p, "rb").read()
                self.assertEqual(hashlib.sha256(data).hexdigest(), sha)
                z = zipfile.ZipFile(io.BytesIO(s.build_package(data, fw)))
                self.assertIn("fs.img", z.namelist())  # getestetes fs-Image wurde eingesetzt

    def test_corrupt_shelly_zip(self):
        p = self.write("x.zip", b"PK" + b"\0" * 100)
        with self.assertRaises(s.Abort):
            s.get_shelly_zip("Plus2PM", Collector(), path=p)


# ---------------------------------------------------------------- Texte

class TextTest(unittest.TestCase):
    ALLOWED = set("äöüÄÖÜß„“")

    def test_complete_and_consistent(self):
        for key, texts in s.TEXTS.items():
            with self.subTest(key=key):
                self.assertEqual(len(texts), len(s.LANGUAGES))
                fields = [sorted(re.findall(r"{(\w+)}", t)) for t in texts]
                self.assertEqual(fields[0], fields[1])

    def test_no_unicode_symbols(self):
        # Tk unter Linux stellt Symbole wie Pfeile oder Haken nicht überall dar
        for key, texts in s.TEXTS.items():
            for t in texts:
                bad = {c for c in t if ord(c) > 127 and c not in self.ALLOWED}
                self.assertFalse(bad, f"{key}: {bad}")

    def test_all_keys_used(self):
        src = open(SCRIPT, encoding="utf-8").read()
        for key in s.TEXTS:
            if key.startswith(("st_", "status_", "step", "intro", "fmt_")):
                continue  # werden zusammengesetzt
            self.assertRegex(src, rf"[\"']{key}[\"']", key)

    def test_detect_lang(self):
        old = dict(os.environ)
        try:
            s.CONFIG_PATH, saved = os.path.join(tempfile.gettempdir(), "s2e-test-none.json"), s.CONFIG_PATH
            for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
                os.environ.pop(var, None)
            os.environ["LANG"] = "de_DE.UTF-8"
            if sys.platform != "win32":
                self.assertEqual(s.detect_lang(), "de")
                os.environ["LANG"] = "en_US.UTF-8"
                self.assertEqual(s.detect_lang(), "en")
        finally:
            s.CONFIG_PATH = saved
            os.environ.clear()
            os.environ.update(old)


# ---------------------------------------------------------------- Dateiserver

class FileServerTest(unittest.TestCase):
    def setUp(self):
        self.ui = Collector()
        self.srv = s.FileServer(0, self.ui)
        self.data = bytes(range(256)) * 40
        self.srv.files["a.zip"] = self.data
        self.url = f"http://127.0.0.1:{self.srv.port}/a.zip"

    def tearDown(self):
        self.srv.close()

    def test_get_head_range(self):
        r = urllib.request.urlopen(urllib.request.Request(self.url, method="HEAD"))
        self.assertEqual(int(r.headers["Content-Length"]), len(self.data))
        self.assertEqual(r.read(), b"")
        self.assertNotIn("a.zip", self.srv.requested)
        r = urllib.request.urlopen(urllib.request.Request(self.url, headers={"Range": "bytes=100-199"}))
        self.assertEqual(r.status, 206)
        self.assertEqual(r.read(), self.data[100:200])
        r = urllib.request.urlopen(urllib.request.Request(self.url, headers={"Range": "bytes=-10"}))
        self.assertEqual(r.read(), self.data[-10:])
        self.assertEqual(urllib.request.urlopen(self.url).read(), self.data)
        self.assertIn("a.zip", self.srv.completed)

    def test_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.url + "x")
        self.assertEqual(cm.exception.code, 404)


# ---------------------------------------------------------------- Ablauf gegen den Mock

class FlowTest(TmpFiles, unittest.TestCase):
    """Kompletter Ablauf mit verkürzten Wartezeiten."""

    TIMES = dict(UPDATE_TIMEOUT=6, DOWNLOAD_START_TIMEOUT=1.5, REBOOT_SETTLE=0.3, REBOOT_TIMEOUT=6,
                 STABLE_UPTIME=2, POLL=0.1)

    def setUp(self):
        super().setUp()
        self.saved = {k: getattr(s, k) for k in list(self.TIMES) + ["ESPHOME_API_PORT"]}
        for k, v in self.TIMES.items():
            setattr(s, k, v)
        s.ESPHOME_API_PORT = free_port()
        self.fw = s.Firmware(self.write("fw.bin", make_image(project="flowtest")))
        self.mock = None

    def tearDown(self):
        if self.mock:
            self.mock.stop()
        for k, v in self.saved.items():
            setattr(s, k, v)
        super().tearDown()

    def start(self, scenario):
        self.mock = MockShelly(port=0, api_port=s.ESPHOME_API_PORT, scenario=scenario, rollback_delay=1.0,
                               boot_delay=0.3, esphome_delay=0.3, quiet=True).start()
        return self.mock.addr

    def flash(self, scenario):
        ip = self.start(scenario)
        ui = Collector()
        info, shelly_zip, pkg = s.prepare(ip, self.fw, ui)
        s.run_flash(ip, info, shelly_zip, pkg, ui)
        return ui

    def assert_aborts(self, scenario, key, before_flash=False, **kw):
        ui = Collector()
        with self.assertRaises(s.Abort) as cm:
            ip = self.start(scenario)
            info, shelly_zip, pkg = s.prepare(ip, self.fw, ui)
            s.run_flash(ip, info, shelly_zip, pkg, ui)
        msg = str(cm.exception)
        expected = s.T(key, **kw)
        self.assertEqual(msg, expected)
        if before_flash:
            self.assertNotIn("Shelly.Update", [c[0] for c in self.mock.calls])
        return ui, msg

    def test_normal(self):
        ui = self.flash("normal")
        # "4 run" erscheint nur, wenn der Shelly vor dem Öffnen des API-Ports kurz offline war
        steps = [st for st in ui.steps if st != (4, "run")]
        self.assertEqual(steps, [(1, "run"), (1, "ok"), (2, "run"), (2, "ok"), (3, "run"), (3, "ok"), (4, "ok")])
        ev = self.mock.events
        self.assertTrue(any("OTA.Commit (pending=1.3.3)" in e for e in ev), ev)
        self.assertTrue(any("ESPHome läuft" in e for e in ev), ev)
        self.assertFalse(any("Rollback" in e for e in ev), ev)
        self.assertIn(s.T("log_done", ip=self.mock.addr, port=s.ESPHOME_API_PORT), ui.text())

    def test_already_133(self):
        ui = self.flash("already133")
        self.assertIn((2, "skip"), ui.steps)
        urls = [c[1].get("url", "") for c in self.mock.calls if c[0] == "Shelly.Update"]
        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0].endswith("/esphome.zip"))

    def test_auth(self):
        self.assert_aborts("auth", "err_auth", before_flash=True)

    def test_gen1(self):
        self.assert_aborts("gen1", "err_gen", before_flash=True, gen=1)

    def test_unsupported(self):
        self.assert_aborts("unsupported", "err_model", before_flash=True, model="Mini1PMG3",
                           supported=", ".join(s.SHELLY_FW_SHA256))

    def test_update_error_message(self):
        self.assert_aborts("update_error", "err_rpc", method="Shelly.Update",
                           msg="Update in progress (code -114)")

    def test_no_fetch_firewall_hint(self):
        self.assert_aborts("no_fetch", "err_no_download")

    def test_rollback_detected(self):
        self.assert_aborts("ignore_commit", "err_rolled_back", ver="1.7.5")

    def test_esphome_rejected(self):
        self.assert_aborts("reject_esphome", "err_rejected", ver="1.3.3")

    def test_esphome_silent(self):
        self.assert_aborts("esphome_silent", "err_silent")

    def test_unreachable(self):
        ip = f"127.0.0.1:{free_port()}"
        with self.assertRaises(s.Abort) as cm:
            s.check_shelly(ip)
        self.assertEqual(str(cm.exception), s.T("err_unreachable", ip=ip))

    def test_already_esphome(self):
        api = socket.socket()
        api.bind(("127.0.0.1", s.ESPHOME_API_PORT))
        api.listen(1)
        try:
            ip = f"127.0.0.1:{free_port()}"
            with self.assertRaises(s.Abort) as cm:
                s.check_shelly(ip)
            self.assertEqual(str(cm.exception), s.T("err_already_esphome", ip=ip))
        finally:
            api.close()


# ---------------------------------------------------------------- Kommandozeile

class CliTest(TmpFiles, unittest.TestCase):
    def run_cli(self, *args, inp=None):
        """Startet die Kommandozeile im selben Prozess (damit die Test-Firmware greift)."""
        out = io.StringIO()
        answers = iter((inp or "").splitlines())

        def fake_input(prompt=""):
            out.write(prompt)
            try:
                return next(answers)
            except StopIteration:
                raise EOFError
        saved = sys.argv, builtins.input, s._lang["cur"], os.getcwd()
        sys.argv, builtins.input = ["shelly2esphome.py"] + list(args), fake_input
        os.chdir(self.tmp.name)
        code = 0
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                s.run()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        finally:
            sys.argv, builtins.input, s._lang["cur"] = saved[:3]
            os.chdir(saved[3])
        return code, out.getvalue()

    def test_help_both_languages(self):
        _, de = self.run_cli("--lang", "de", "--help")
        _, en = self.run_cli("--lang=en", "--help")
        self.assertIn("ESPHome per OTA auf Shelly Gen2 flashen", de)
        self.assertIn("Flash ESPHome over the air onto Shelly Gen2", en)

    def test_missing_firmware_no_traceback(self):
        code, out = self.run_cli("--lang", "en", "127.0.0.1:1", "missing.bin")
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", out)
        self.assertIn("ERROR: Cannot read file missing.bin", out)

    def test_multicore_needs_flag_with_yes(self):
        mock = MockShelly(port=0, api_port=free_port(), quiet=True).start()
        try:
            fw = self.write("mc.bin", make_image(multicore=True))
            code, out = self.run_cli("--lang", "en", mock.addr, fw, "-y")
            self.assertEqual(code, 1)
            self.assertIn("--allow-multicore", out)
            self.assertNotIn("Shelly.Update", [c[0] for c in mock.calls])
        finally:
            mock.stop()

    def test_confirm_no_aborts(self):
        mock = MockShelly(port=0, api_port=free_port(), quiet=True).start()
        try:
            fw = self.write("ok.bin", make_image())
            code, out = self.run_cli("--lang", "de", mock.addr, fw, inp="nein\n")
            self.assertEqual(code, 1)
            self.assertIn(s.T("log_aborted_nothing"), out)
            self.assertNotIn("Shelly.Update", [c[0] for c in mock.calls])
        finally:
            mock.stop()

    def test_build_only(self):
        mock = MockShelly(port=0, api_port=free_port(), quiet=True).start()
        try:
            fw = self.write("b.bin", make_image(project="nurbauen"))
            code, out = self.run_cli("--lang", "en", mock.addr, fw, "--build-only")
            self.assertEqual(code, 0, out)
            self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "esphome-nurbauen-Plus2PM.zip")))
            self.assertNotIn("Shelly.Update", [c[0] for c in mock.calls])
        finally:
            mock.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
