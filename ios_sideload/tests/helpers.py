"""테스트용 가짜 IPA / 프로파일 / 인증서 생성기."""

from __future__ import annotations

import os
import plistlib
import stat
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402


def make_ipa(path: Path, bundle_id: str = "com.example.demo", name: str = "Demo", version: str = "1.2.3",
             build: str = "45", with_extension: bool = False) -> Path:
    info = {
        "CFBundleIdentifier": bundle_id, "CFBundleName": name, "CFBundleDisplayName": name,
        "CFBundleShortVersionString": version, "CFBundleVersion": build, "MinimumOSVersion": "15.0",
        "CFBundleExecutable": name,
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"Payload/{name}.app/Info.plist", plistlib.dumps(info, fmt=plistlib.FMT_BINARY))
        zf.writestr(f"Payload/{name}.app/{name}", b"\xcf\xfa\xed\xfefake-macho")
        if with_extension:
            zf.writestr(f"Payload/{name}.app/PlugIns/Ext.appex/Info.plist", plistlib.dumps({"CFBundleIdentifier": bundle_id + ".ext"}))
    return path


def make_self_signed(team: str = "ABCDE12345", cn: str = "Apple Development: Test (XYZ)"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, team),
        x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
    ])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=365)).sign(key, hashes.SHA256()))
    return key, cert


def make_profile(path: Path, app_id: str = "ABCDE12345.com.example.demo", team: str = "ABCDE12345",
                 devices: list[str] | None = None, days: int = 7, certs_der: list[bytes] | None = None,
                 get_task_allow: bool = True, all_devices: bool = False) -> Path:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    plist = {
        "AppIDName": "Demo", "ApplicationIdentifierPrefix": [team], "CreationDate": now,
        "ExpirationDate": now + timedelta(days=days), "Name": "Demo Dev Profile", "TeamIdentifier": [team],
        "TeamName": "Test Team", "UUID": "11111111-2222-3333-4444-555555555555", "Version": 1,
        "Entitlements": {"application-identifier": app_id, "get-task-allow": get_task_allow,
                         "com.apple.developer.team-identifier": team},
        "DeveloperCertificates": [plistlib.Data(c) if hasattr(plistlib, "Data") else c for c in (certs_der or [])],
    }
    if all_devices:
        plist["ProvisionsAllDevices"] = True
    elif devices is not None:
        plist["ProvisionedDevices"] = devices
    body = plistlib.dumps(plist)
    path.write_bytes(b"\x30\x82\x1a\x2bJUNKCMSHEADER" + body + b"\x00\x00TRAILER")
    return path


def make_fake_zsign(path: Path) -> Path:
    """인자를 기록하고 입력 IPA 를 출력 경로로 복사하는 가짜 zsign."""
    script = (
        "#!/usr/bin/env python3\n"
        "import sys, shutil, json, os\n"
        "args = sys.argv[1:]\n"
        "if args == ['-v']:\n    print('zsign (v9.9.9-test)'); sys.exit(0)\n"
        "out = args[args.index('-o') + 1]\n"
        "src = args[-1]\n"
        "shutil.copy(src, out)\n"
        "open(out + '.args.json', 'w').write(json.dumps(args))\n"
        "print('signed ok')\n"
    )
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def cert_der(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.DER)
