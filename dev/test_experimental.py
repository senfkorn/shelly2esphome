# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic images and mocked RPC only; no physical device/network access."""
import hashlib
import io
import json
import os
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import shelly2esphome as s
import experimental as e
from test_shelly2esphome import make_image


def image(chip, desc=False):
    data = bytearray(make_image(chip=chip, with_desc=desc, size=1024))
    data[3] = 0x32  # 8 MB, 40 MHz
    data[-32:] = hashlib.sha256(data[:-32]).digest()
    return bytes(data)


def fixture(name="PlugSG3", bad_layout=False):
    p = e.PROFILES[name]
    size = p["slot_size"]
    app1 = 0x20000 + size + 0xe0000
    entries = [("otadata", 1, 0, 0x11000, 0x2000), ("nvs", 1, 2, 0x14000, 0xc000),
               ("app_0", 0, 0x10, 0x20000, size), ("fs_0", 1, 0x82, 0x20000 + size, 0xe0000),
               ("app_1", 0, 0x11, app1, size), ("fs_1", 1, 0x82, app1 + size, 0xe0000),
               ("scratch", 1, 0x80, 0x7e0000, 0x10000), ("shelly", 1, 0x88, 0x7f0000, 0x10000)]
    pt = b"".join(b"\xaa\x50" + struct.pack("<BBII16sI", kind, sub, off, length, label.encode(), 0)
                  for label, kind, sub, off, length in entries).ljust(4096, b"\xff")
    boot, app = image(p["chip"]), image(p["chip"], True)
    factory = bytearray(b"\xff" * 0x20000 + app)
    factory[:len(boot)] = boot
    factory[0x10000:0x11000] = pt
    if bad_layout:
        factory[0x10000 + 4] ^= 1
    parts = {"boot": {"type": "boot", "src": "boot.bin", "addr": 0, "min_version": "1.0.2"},
             "pt": {"type": "pt", "src": "pt.bin", "addr": 0x10000},
             "app": {"type": "app", "src": "app.bin", "ptn": "app_0"},
             "otadata": {"type": "otadata", "src": "ota.bin", "ptn": "otadata"},
             "fs": {"type": "fs", "src": "fs.bin", "ptn": "fs_0"}}
    files = {"boot.bin": boot, "pt.bin": pt, "app.bin": app, "ota.bin": b"\xff" * 8192, "fs.bin": b"\xff" * 1024}
    for part in parts.values():
        data = files[part["src"]]
        part.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    manifest = {"name": name, "version": "2.0.1", "parts": parts}
    return bytes(factory), archive(manifest, files), manifest, files


def archive(manifest, files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        z.writestr("manifest.json", json.dumps(manifest))
        for name, data in files.items():
            z.writestr(name, data)
    return out.getvalue()


class ExperimentalTest(unittest.TestCase):
    def test_both_profiles_and_output_checksums(self):
        for name, profile in e.PROFILES.items():
            factory, stock, _, _ = fixture(name)
            package, layout = e.build_package(s, {"app": name, "ver": "2.0.1"}, profile, factory, stock)
            self.assertEqual(layout["chip_id"], profile["chip"])
            with zipfile.ZipFile(io.BytesIO(package)) as z:
                m = json.loads(z.read("manifest.json"))
                self.assertEqual(m["parts"]["boot"]["min_version"], "1.0.9")
                for p in m["parts"].values():
                    data = z.read(p["src"])
                    self.assertEqual(p["sha256"], hashlib.sha256(data).hexdigest())
                    self.assertEqual(p["sha1"], hashlib.sha1(data).hexdigest())

    def test_reject_layout_chip_version_and_checksum(self):
        factory, stock, manifest, files = fixture()
        profile = e.PROFILES["PlugSG3"]
        cases = [(fixture(bad_layout=True)[0], stock, "2.0.1"),
                 (fixture("S2PMG4")[0], stock, "2.0.1"), (factory, stock, "2.0.0")]
        files["app.bin"] = b"corrupt"
        cases.append((factory, archive(manifest, files), "2.0.1"))
        for f, z, ver in cases:
            with self.assertRaises(ValueError):
                e.build_package(s, {"app": "PlugSG3", "ver": ver}, profile, f, z)

    def test_ota_only_and_gen2_bootloader_rejected(self):
        factory, stock, _, _ = fixture()
        for data in (factory[0x20000:], image(0, True)):
            with self.assertRaises((ValueError, s.Abort)):
                e.build_package(s, {"app": "PlugSG3", "ver": "2.0.1"}, e.PROFILES["PlugSG3"], data, stock)

    def test_partition_md5_and_overlap(self):
        _, _, _, files = fixture()
        raw = files["pt.bin"][:8 * 32]
        md5 = b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(raw).digest()
        self.assertEqual(len(e.partitions(raw + md5)), 8)
        with self.assertRaises(ValueError):
            e.partitions(raw + md5[:-1] + b"x")
        bad = bytearray(raw + b"\xff" * 32)
        bad[32 + 4:32 + 8] = struct.pack("<I", 0x11000)
        with self.assertRaises(ValueError):
            e.partitions(bad)

    def test_report_does_not_export_secrets(self):
        session = e.Session(s)
        data = {"app": "PlugSG3", "gen": 3, "ver": "2.0.1", "auth_en": False,
                "id": "SECRET_MAC", "name": "SECRET_NAME", "wifi": {"password": "SECRET_PASSWORD"}}
        with patch.object(s, "rpc", return_value=data) as rpc:
            session.identify("SECRET_IP")
            rpc.assert_called_once_with("SECRET_IP", "Shelly.GetDeviceInfo")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            session.export(path, ["reboot=passed", "ota1=failed"])
            text = path.read_text()
            self.assertNotIn("SECRET", text)
            self.assertEqual(json.loads(text)["tests"]["ota2"], "unknown")

    def test_unknown_profile_read_only_and_flash_blocked(self):
        session = e.Session(s)
        with patch.object(s, "rpc", return_value={"app": "OtherG4", "gen": 4, "ver": "2.0.1"}) as rpc:
            session.identify("127.0.0.1")
            self.assertEqual(session.report["device"]["profile"], "unknown")
            with self.assertRaises(s.Abort):
                session.prepare("127.0.0.1", "missing", "missing")
            self.assertTrue(all(call.args[1] == "Shelly.GetDeviceInfo" for call in rpc.call_args_list))

    def test_preflight_never_writes_rpc(self):
        factory, stock, _, _ = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            f, z = Path(tmp) / "factory.bin", Path(tmp) / "stock.zip"
            f.write_bytes(factory)
            z.write_bytes(stock)
            session = e.Session(s)
            with patch.object(s, "rpc", side_effect=[{"app": "PlugSG3", "gen": 3, "ver": "2.0.1"}, {"device": {"enhanced_security": False}}]) as rpc:
                session.prepare("127.0.0.1", f, z)
                self.assertEqual([call.args[1] for call in rpc.call_args_list], ["Shelly.GetDeviceInfo", "Sys.GetConfig"])
                self.assertEqual(session.report["outcome"], "preflight_passed")

    def test_ambiguous_or_slot1_blocks_before_update(self):
        for slot in (None, 1):
            session = e.Session(s)
            session.info = {"app": "PlugSG3", "gen": 3, "ver": "2.0.1"}
            with patch.object(s, "rpc", side_effect=[session.info, {"device": {"enhanced_security": False}}]) as rpc, patch.object(s, "port_open", return_value=False), patch.object(e, "probe_slot", return_value=slot):
                with self.assertRaises(s.Abort):
                    session.flash("127.0.0.1", b"zip", s.UI())
                self.assertNotIn("Shelly.Update", [call.args[1] for call in rpc.call_args_list])

    def test_probe_restores_even_on_error(self):
        config = {"debug": {"udp": {"addr": "previous:1234"}}}
        calls = []
        def rpc(ip, method, params=None):
            calls.append((method, params))
            if method == "Sys.GetConfig":
                return config
        with patch.object(s, "rpc", side_effect=rpc), patch.object(s, "local_ip_for", return_value="127.0.0.1"), patch.object(e.socket, "socket") as sock:
            udp = sock.return_value.__enter__.return_value
            udp.getsockname.return_value = ("127.0.0.1", 4567)
            udp.recvfrom.side_effect = OSError("private error")
            with self.assertRaises(OSError):
                e.probe_slot(s, "127.0.0.1")
        self.assertEqual(calls[-2], ("Sys.SetConfig", {"config": {"debug": {"udp": {"addr": "previous:1234"}}}}))

    def test_result_allowlist(self):
        with self.assertRaises(s.Abort):
            e.Session(s).export("unused.json", ["password=secret"])

    def test_security_blocks_without_write(self):
        for config in ({"device": {"enhanced_security": True}}, {}):
            session = e.Session(s)
            with patch.object(s, "rpc", return_value=config) as rpc:
                with self.assertRaises(s.Abort):
                    session.check_transport("127.0.0.1")
                self.assertEqual(rpc.call_args.args[1], "Sys.GetConfig")

    def test_partition_export_is_read_only(self):
        _, stock, _, _ = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            stock_path, csv_path = Path(tmp) / "stock.zip", Path(tmp) / "stock.csv"
            stock_path.write_bytes(stock)
            session = e.Session(s)
            with patch.object(s, "rpc", return_value={"app": "PlugSG3", "gen": 3, "ver": "2.0.1"}) as rpc:
                session.export_partitions("127.0.0.1", stock_path, csv_path)
                rpc.assert_called_once_with("127.0.0.1", "Shelly.GetDeviceInfo")
            self.assertIn("app_0,0x0,0x10,0x20000,0x2a0000,", csv_path.read_text())
            self.assertIn("fs_1,", csv_path.read_text())

    def test_probe_rejects_conflicts_and_wrong_source(self):
        config = {"debug": {"udp": {"addr": None}}}
        with patch.object(s, "rpc", return_value=config), patch.object(s, "local_ip_for", return_value="127.0.0.1"), patch.object(e.socket, "socket") as sock, patch.object(e.time, "monotonic", side_effect=[0, 0, 0, 0, 10]):
            udp = sock.return_value.__enter__.return_value
            udp.getsockname.return_value = ("127.0.0.1", 1234)
            udp.recvfrom.side_effect = [(b"Storing core dumps to app_0", ("127.0.0.2", 1234)),
                                        (b"Storing core dumps to app_0", ("127.0.0.1", 1234)),
                                        (b"Storing core dumps to app_1", ("127.0.0.1", 1234))]
            self.assertIsNone(e.probe_slot(s, "127.0.0.1"))

    def test_restore_failure_blocks_update(self):
        configs = [{"debug": {"udp": {"addr": None}}}, None, None,
                   {"debug": {"udp": {"addr": "wrong"}}}]
        with patch.object(s, "rpc", side_effect=configs) as rpc, patch.object(s, "local_ip_for", return_value="127.0.0.1"), patch.object(e.socket, "socket") as sock, patch.object(e.time, "monotonic", side_effect=[0, 10]):
            sock.return_value.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 1234)
            with self.assertRaisesRegex(s.Abort, "debug_restore_failed"):
                e.probe_slot(s, "127.0.0.1")
            self.assertNotIn("Shelly.Update", [call.args[1] for call in rpc.call_args_list])

    def test_success_is_only_api_observation_not_hardware_claim(self):
        session = e.Session(s)
        session.info = {"app": "PlugSG3", "gen": 3, "ver": "2.0.1"}
        with patch.object(s, "rpc", side_effect=[session.info, {"device": {}}]), patch.object(s, "port_open", return_value=False), patch.object(e, "probe_slot", return_value=0), patch.object(s, "FileServer") as server, patch.object(s, "flash_esphome"), patch.object(s, "local_ip_for", return_value="127.0.0.1"):
            server.return_value.completed = {"esphome.zip"}
            session.flash("127.0.0.1", b"package", s.UI())
            server.return_value.close.assert_called_once()
        self.assertEqual(session.report["outcome"], "api_port_observed")
        self.assertTrue(all(v == "unknown" for v in session.report["tests"].values()))

    def test_cli_failure_still_exports_private_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "diagnostic.json"
            args = SimpleNamespace(ip="127.0.0.1", diagnose=True, report=str(path), test_result=[])
            with patch.object(s, "rpc", side_effect=s.Abort("SECRET_RPC_ERROR")):
                with self.assertRaises(s.Abort):
                    e.run_cli(s, args)
            data = path.read_text()
            self.assertNotIn("SECRET", data)
            self.assertEqual(json.loads(data)["outcome"], "error")

    def test_bootloader_minimum_and_encrypt_rejected(self):
        factory, _, manifest, files = fixture()
        for key, value in (("min_version", "1.0.9"), ("encrypt", True)):
            manifest["parts"]["boot"][key] = value
            with self.assertRaises(ValueError):
                e.build_package(s, {"app": "PlugSG3", "ver": "2.0.1"}, e.PROFILES["PlugSG3"], factory, archive(manifest, files))
            manifest["parts"]["boot"].pop(key)


if __name__ == "__main__":
    unittest.main(verbosity=2)
