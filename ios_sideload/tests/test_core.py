import json
import os
import plistlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helpers import cert_der, make_fake_zsign, make_ipa, make_profile, make_self_signed

from sideload import ipa as ipa_mod
from sideload.cert import CertError, cert_in_profile, inspect_cert, write_p12
from sideload.config import Config
from sideload.ota import build_manifest, itms_link, write_ota_site
from sideload.profile import ProfileError, parse_profile, parse_profile_bytes
from sideload.registry import AppRecord, Registry
from sideload.signer import RcodesignSigner, SignRequest, ZsignSigner, find_signer, make_signer


class IpaTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_inspect(self):
        p = make_ipa(self.dir / "a.ipa", with_extension=True)
        info = ipa_mod.inspect_ipa(p)
        self.assertEqual(info.bundle_id, "com.example.demo")
        self.assertEqual(info.name, "Demo")
        self.assertEqual(info.version, "1.2.3")
        self.assertEqual(info.build, "45")
        self.assertEqual(info.app_dir, "Payload/Demo.app")
        self.assertTrue(info.has_extensions)
        self.assertFalse(info.has_watch_app)

    def test_not_zip(self):
        p = self.dir / "x.ipa"
        p.write_bytes(b"nope")
        with self.assertRaises(ipa_mod.IpaError):
            ipa_mod.inspect_ipa(p)

    def test_missing(self):
        with self.assertRaises(ipa_mod.IpaError):
            ipa_mod.inspect_ipa(self.dir / "none.ipa")

    def test_find_entry_ignores_nested(self):
        names = ["Payload/A.app/PlugIns/B.appex/Info.plist", "Payload/A.app/Info.plist"]
        self.assertEqual(ipa_mod.find_app_info_entry(names), "Payload/A.app/Info.plist")


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_parse_development(self):
        p = make_profile(self.dir / "d.mobileprovision", devices=["00008030-000A1B2C3D4E5F60"], days=7)
        prof = parse_profile(p)
        self.assertEqual(prof.team_id, "ABCDE12345")
        self.assertEqual(prof.bundle_id_pattern, "com.example.demo")
        self.assertEqual(prof.profile_type, "development")
        self.assertTrue(prof.matches_bundle_id("com.example.demo"))
        self.assertFalse(prof.matches_bundle_id("com.example.other"))
        self.assertTrue(prof.covers_device("00008030000A1B2C3D4E5F60"))
        self.assertFalse(prof.covers_device("deadbeef"))
        self.assertTrue(6 < prof.days_left <= 7)
        self.assertFalse(prof.is_expired)

    def test_wildcard_and_types(self):
        p = make_profile(self.dir / "w.mobileprovision", app_id="ABCDE12345.*", devices=["x"], get_task_allow=False)
        prof = parse_profile(p)
        self.assertEqual(prof.bundle_id_pattern, "*")
        self.assertTrue(prof.matches_bundle_id("anything.at.all"))
        self.assertEqual(prof.profile_type, "adhoc")
        p2 = make_profile(self.dir / "e.mobileprovision", app_id="ABCDE12345.com.co.*", all_devices=True, get_task_allow=False)
        prof2 = parse_profile(p2)
        self.assertEqual(prof2.profile_type, "enterprise")
        self.assertTrue(prof2.matches_bundle_id("com.co.app"))
        self.assertFalse(prof2.matches_bundle_id("com.other.app"))
        self.assertTrue(prof2.covers_device("whatever"))

    def test_expired(self):
        p = make_profile(self.dir / "x.mobileprovision", days=-1)
        self.assertTrue(parse_profile(p).is_expired)

    def test_garbage(self):
        with self.assertRaises(ProfileError):
            parse_profile_bytes(b"\x00\x01garbage")


class CertTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)
        self.key, self.cert = make_self_signed()

    def tearDown(self):
        self.td.cleanup()

    def test_p12_roundtrip_and_profile_match(self):
        p12 = write_p12(self.dir / "c.p12", self.key, cert_der(self.cert), "pw")
        info = inspect_cert(p12, "pw")
        self.assertEqual(info.organizational_unit, "ABCDE12345")
        self.assertIn("Apple Development", info.common_name)
        self.assertGreater(info.days_left, 300)
        prof = parse_profile(make_profile(self.dir / "p.mobileprovision", certs_der=[cert_der(self.cert)]))
        self.assertTrue(cert_in_profile(info, prof))
        other = parse_profile(make_profile(self.dir / "q.mobileprovision", certs_der=[b"other"]))
        self.assertFalse(cert_in_profile(info, other))

    def test_wrong_password(self):
        p12 = write_p12(self.dir / "c.p12", self.key, cert_der(self.cert), "pw")
        with self.assertRaises(CertError):
            inspect_cert(p12, "wrong")

    def test_no_password(self):
        p12 = write_p12(self.dir / "c.p12", self.key, cert_der(self.cert), None)
        self.assertEqual(inspect_cert(p12, None).organizational_unit, "ABCDE12345")


class SignerTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)
        self.zsign = make_fake_zsign(self.dir / "zsign")

    def tearDown(self):
        self.td.cleanup()

    def test_build_command(self):
        s = ZsignSigner(self.zsign)
        req = SignRequest(ipa=Path("in.ipa"), output=Path("out.ipa"), cert_p12=Path("c.p12"), cert_password="pw",
                          profile=Path("p.mobileprovision"), bundle_id="com.new.id", bundle_name="New",
                          remove_extensions=True, dylibs=(Path("a.dylib"),))
        cmd = s.build_command(req)
        self.assertEqual(cmd[0], str(self.zsign))
        for flag, val in (("-k", "c.p12"), ("-m", "p.mobileprovision"), ("-p", "pw"), ("-b", "com.new.id"),
                          ("-n", "New"), ("-l", "a.dylib"), ("-o", "out.ipa")):
            self.assertEqual(cmd[cmd.index(flag) + 1], val)
        self.assertIn("-E", cmd)
        self.assertEqual(cmd[-1], "in.ipa")

    def test_sign_runs(self):
        ipa = make_ipa(self.dir / "in.ipa")
        out = self.dir / "sub" / "out.ipa"
        s = ZsignSigner(self.zsign)
        logs = []
        s.sign(SignRequest(ipa=ipa, output=out, cert_p12=Path("c.p12"), cert_password=None,
                           profile=Path("p.mobileprovision")), log=logs.append)
        self.assertTrue(out.exists())
        args = json.loads((str(out) + ".args.json" and Path(str(out) + ".args.json")).read_text())
        self.assertNotIn("-p", args)
        self.assertEqual(s.version(), "zsign (v9.9.9-test)")
        self.assertTrue(any("signed ok" in l for l in logs))

    def test_find_signer_config_and_env(self):
        cfg = Config(signer_path=str(self.zsign))
        self.assertIsInstance(find_signer(cfg), ZsignSigner)
        os.environ["ZSIGN"] = str(self.zsign)
        try:
            self.assertEqual(find_signer(Config()).executable, self.zsign)
        finally:
            del os.environ["ZSIGN"]
        self.assertIsInstance(make_signer(Path("/x/rcodesign")), RcodesignSigner)


class OtaTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_manifest(self):
        m = plistlib.loads(build_manifest("com.x.y", "1.0", "X", "https://h/app.ipa", "https://h/icon.png"))
        item = m["items"][0]
        self.assertEqual(item["metadata"]["bundle-identifier"], "com.x.y")
        self.assertEqual(item["assets"][0]["url"], "https://h/app.ipa")
        self.assertEqual(len(item["assets"]), 3)
        self.assertTrue(itms_link("https://h/m.plist").startswith("itms-services://?action=download-manifest&url=https%3A%2F%2F"))

    def test_site(self):
        ipa = make_ipa(self.dir / "a.ipa")
        res = write_ota_site(ipa, self.dir / "site", "https://example.com/app")
        self.assertTrue((self.dir / "site" / "app.ipa").exists())
        self.assertTrue((self.dir / "site" / "manifest.plist").exists())
        html = (self.dir / "site" / "index.html").read_text(encoding="utf-8")
        self.assertIn("itms-services://", html)
        self.assertEqual(res["manifest_url"], "https://example.com/app/manifest.plist")


class RegistryTests(unittest.TestCase):
    def test_due_and_persist(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "r.json"
            reg = Registry(path)
            now = datetime.now(timezone.utc)

            def rec(bid, days):
                return AppRecord(bundle_id=bid, name=bid, source_ipa="s", signed_ipa="o", udid="U",
                                 signed_at=now.isoformat(), profile_path="p",
                                 profile_expires=(now + timedelta(days=days)).isoformat(),
                                 cert_path="c", cert_expires=(now + timedelta(days=300)).isoformat())
            reg.upsert(rec("a", 1))
            reg.upsert(rec("b", 30))
            reg.upsert(rec("a", 1.5))  # 같은 키 덮어쓰기
            reg2 = Registry(path)
            self.assertEqual(len(reg2.all()), 2)
            self.assertEqual([r.bundle_id for r in reg2.due(2)], ["a"])
            self.assertTrue(reg2.remove("U", "a"))
            self.assertFalse(reg2.remove("U", "zzz"))
            self.assertEqual(len(Registry(path).all()), 1)


class ConfigTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cfg.json"
            c = Config(cert_path="/a.p12", cert_password="x", asc_key_id="K", asc_issuer_id="I", asc_key_path="/k.p8")
            c.save(p)
            c2 = Config.load(p)
            self.assertEqual(c2.cert_path, "/a.p12")
            self.assertTrue(c2.has_asc())
            self.assertFalse(c2.has_signing_material())
            self.assertEqual(Config.load(Path(td) / "missing.json").refresh_before_days, 2)


if __name__ == "__main__":
    unittest.main()
