"""CLI 통합 테스트 — 기기 연결과 zsign 을 가짜로 바꿔 deploy/refresh 흐름을 검증한다."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import cert_der, make_fake_zsign, make_ipa, make_profile, make_self_signed

import sideload as pkg  # noqa: F401  (경로 확인)
from sideload.cert import write_p12
from sideload.config import Config
from sideload.device import DeviceInfo
from sideload.registry import Registry

import importlib.util

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sideload_cli", ROOT / "sideload.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

UDID = "00008030-000A1B2C3D4E5F60"
DEVICE = DeviceInfo(udid=UDID, name="Test iPhone", ios_version="17.5", product_type="iPhone14,2", connection="USB",
                    developer_mode=True)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)
        self.home = self.dir / "home"
        os.environ["IOS_SIDELOAD_HOME"] = str(self.home)
        key, cert = make_self_signed()
        self.p12 = write_p12(self.dir / "c.p12", key, cert_der(cert), "pw")
        self.profile = make_profile(self.dir / "p.mobileprovision", devices=[UDID], certs_der=[cert_der(cert)], days=7)
        self.zsign = make_fake_zsign(self.dir / "zsign")
        self.ipa = make_ipa(self.dir / "demo.ipa")
        Config(cert_path=str(self.p12), cert_password="pw", profile_path=str(self.profile),
               signer_path=str(self.zsign)).save()
        self.installed = []

        patches = [
            mock.patch("sideload.device.pick_device", lambda udid=None: DEVICE),
            mock.patch("sideload.device.list_devices", lambda with_details=True: [DEVICE]),
            mock.patch("sideload.device.install_ipa", self._fake_install),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        os.environ.pop("IOS_SIDELOAD_HOME", None)
        self.td.cleanup()

    def _fake_install(self, ipa, udid=None, progress=None, upgrade=True):
        if progress:
            progress(50)
            progress(100)
        self.installed.append((Path(ipa), udid, upgrade))

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_inspect_and_info(self):
        code, out = self.run_cli("inspect", str(self.ipa))
        self.assertEqual(code, 0)
        self.assertIn("com.example.demo", out)
        code, out = self.run_cli("profile-info")
        self.assertEqual(code, 0)
        self.assertIn("development", out)
        code, out = self.run_cli("cert-info")
        self.assertEqual(code, 0)
        self.assertIn("프로파일 포함 여부: 예", out)

    def test_deploy_registers_and_refresh(self):
        code, out = self.run_cli("deploy", str(self.ipa))
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.installed), 1)
        signed, udid, upgrade = self.installed[0]
        self.assertTrue(signed.exists())
        self.assertEqual(udid, UDID)
        args = json.loads(Path(str(signed) + ".args.json").read_text())
        self.assertNotIn("-b", args)  # 번들 ID 가 프로파일과 같으므로 변경 없음
        recs = Registry().all()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].bundle_id, "com.example.demo")
        self.assertTrue(6 < recs[0].days_left <= 7)

        code, out = self.run_cli("list")
        self.assertIn("com.example.demo", out)

        # 2일 전 기준으로는 갱신 대상이 아님
        code, out = self.run_cli("refresh")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.installed), 1)
        # 강제 갱신 → 재서명·재설치, 새 프로파일이 없으므로 경고
        code, out = self.run_cli("refresh", "--all")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.installed), 2)
        self.assertIn("유효 기간이 늘지 않았습니다", out)

    def test_deploy_rewrites_bundle_id_to_profile_app_id(self):
        other = make_ipa(self.dir / "other.ipa", bundle_id="com.vendor.realapp", name="Real")
        code, out = self.run_cli("deploy", str(other))
        self.assertEqual(code, 0, out)
        args = json.loads(Path(str(self.installed[0][0]) + ".args.json").read_text())
        self.assertEqual(args[args.index("-b") + 1], "com.example.demo")
        self.assertIn("번들 ID 를 com.example.demo 로", out)

    def test_deploy_device_not_in_profile(self):
        prof = make_profile(self.dir / "p2.mobileprovision", devices=["OTHER"], certs_der=[b"x"])
        code, out = self.run_cli("deploy", str(self.ipa), "--profile", str(prof))
        self.assertEqual(code, 1)
        self.assertIn("등록되어 있지 않습니다", out)
        self.assertIn("포함된 인증서가 아닙니다", out)
        self.assertEqual(self.installed, [])

    def test_sign_only_with_explicit_options(self):
        out_ipa = self.dir / "signed.ipa"
        code, out = self.run_cli("sign", str(self.ipa), "-o", str(out_ipa), "--name", "이름변경", "--no-extensions")
        self.assertEqual(code, 0, out)
        args = json.loads(Path(str(out_ipa) + ".args.json").read_text())
        self.assertEqual(args[args.index("-n") + 1], "이름변경")
        self.assertIn("-E", args)

    def test_missing_material_message(self):
        Config().save()
        code, out = self.run_cli("deploy", str(self.ipa))
        self.assertEqual(code, 1)
        self.assertIn("setup", out)

    def test_doctor_no_device(self):
        code, out = self.run_cli("doctor", "--no-device")
        self.assertEqual(code, 0)
        self.assertIn("zsign", out)
        self.assertNotIn("연결된 아이폰", out)

    def test_uninstall_removes_record(self):
        self.run_cli("deploy", str(self.ipa))
        with mock.patch("sideload.device.uninstall_app", lambda bid, udid=None: None):
            code, out = self.run_cli("uninstall", "com.example.demo")
        self.assertEqual(code, 0, out)
        self.assertEqual(Registry().all(), [])

    def test_ota(self):
        code, out = self.run_cli("ota", str(self.ipa), "--url", "https://x.example/app", "--out", str(self.dir / "site"))
        self.assertEqual(code, 0, out)
        self.assertTrue((self.dir / "site" / "manifest.plist").exists())


if __name__ == "__main__":
    unittest.main()
