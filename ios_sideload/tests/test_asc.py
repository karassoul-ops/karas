import base64
import json
import tempfile
import unittest
from pathlib import Path

from helpers import cert_der, make_self_signed

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from sideload.asc import AscClient, AscError, provision
from sideload.cert import inspect_cert
from sideload.profile import parse_profile
import helpers


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    """App Store Connect API 흉내. 호출 기록과 최소한의 상태를 가진다."""

    def __init__(self):
        self.calls = []
        self.devices = []
        self.bundle_ids = []
        self.certs = []
        self.profiles = []
        self.cert_limit = 99
        self.reject_bundle = False
        self.cert_der = b""
        self.seq = 0

    def next_id(self, prefix):
        self.seq += 1
        return f"{prefix}{self.seq}"

    def issue_cert(self, csr_pem: str) -> bytes:
        """CSR 의 공개키로 인증서를 발급한다 (Apple 과 같은 동작)."""
        from datetime import datetime, timedelta, timezone
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.x509.oid import NameOID
        csr = x509.load_pem_x509_csr(csr_pem.encode())
        ca_key, ca_cert = make_self_signed(cn="Apple Worldwide Developer Relations (fake)")
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Apple Development: ios_sideload"),
                             x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "ABCDE12345")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(ca_cert.subject)
                .public_key(csr.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now).not_valid_after(now + timedelta(days=365)).sign(ca_key, hashes.SHA256()))
        self.cert_der = cert_der(cert)
        return self.cert_der

    def request(self, method, url, headers=None, timeout=None, json=None, params=None):
        assert headers["Authorization"].startswith("Bearer ")
        path = url.split("/v1", 1)[1]
        self.calls.append((method, path, json, params))
        data = (json or {}).get("data", {})
        if method == "GET" and path == "/devices":
            return FakeResp(200, {"data": self.devices})
        if method == "POST" and path == "/devices":
            d = {"type": "devices", "id": self.next_id("dev"), "attributes": {**data["attributes"], "status": "ENABLED"}}
            self.devices.append(d)
            return FakeResp(201, {"data": d})
        if method == "GET" and path == "/bundleIds":
            return FakeResp(200, {"data": [b for b in self.bundle_ids if b["attributes"]["identifier"] == params["filter[identifier]"]]})
        if method == "POST" and path == "/bundleIds":
            if self.reject_bundle:
                return FakeResp(409, {"errors": [{"code": "ENTITY_ERROR.ATTRIBUTE.INVALID", "detail": "The identifier is not available"}]})
            b = {"type": "bundleIds", "id": self.next_id("bid"), "attributes": data["attributes"]}
            self.bundle_ids.append(b)
            return FakeResp(201, {"data": b})
        if method == "GET" and path == "/certificates":
            return FakeResp(200, {"data": self.certs})
        if method == "POST" and path == "/certificates":
            if len(self.certs) >= self.cert_limit:
                return FakeResp(409, {"errors": [{"code": "ENTITY_ERROR", "detail": "You already have a current certificate... maximum limit"}]})
            der = self.issue_cert(data["attributes"]["csrContent"])
            c = {"type": "certificates", "id": self.next_id("cert"), "attributes": {
                "certificateType": data["attributes"]["certificateType"], "name": "ios_sideload",
                "certificateContent": base64.b64encode(der).decode()}}
            self.certs.append(c)
            return FakeResp(201, {"data": c})
        if method == "DELETE" and path.startswith("/certificates/"):
            self.certs = [c for c in self.certs if c["id"] != path.rsplit("/", 1)[1]]
            return FakeResp(204, {})
        if method == "GET" and path == "/profiles":
            return FakeResp(200, {"data": [p for p in self.profiles if p["attributes"]["name"] == params["filter[name]"]]})
        if method == "DELETE" and path.startswith("/profiles/"):
            self.profiles = [p for p in self.profiles if p["id"] != path.rsplit("/", 1)[1]]
            return FakeResp(204, {})
        if method == "POST" and path == "/profiles":
            with tempfile.TemporaryDirectory() as td:
                content = helpers.make_profile(Path(td) / "p.mobileprovision", days=365, certs_der=[self.cert_der],
                                               devices=["UDID1"]).read_bytes()
            p = {"type": "profiles", "id": self.next_id("prof"), "attributes": {
                **data["attributes"], "profileContent": base64.b64encode(content).decode()},
                "relationships": data["relationships"]}
            self.profiles.append(p)
            return FakeResp(201, {"data": p})
        return FakeResp(404, {"errors": [{"code": "NOT_FOUND", "detail": path}]})


def ec_key_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


class AscTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.out = Path(self.td.name)
        self.session = FakeSession()
        self.client = AscClient("KEYID", "issuer-uuid", ec_key_pem(), session=self.session)

    def tearDown(self):
        self.td.cleanup()

    def test_token_is_cached(self):
        t1 = self.client.token()
        self.assertEqual(t1, self.client.token())
        self.assertEqual(len(t1.split(".")), 3)

    def test_provision_flow_and_reuse(self):
        logs = []
        res = provision(self.client, "com.example.demo", "UDID1", "My iPhone", "IOS_APP_DEVELOPMENT",
                        app_name="Demo", p12_password="pw", out_dir=self.out, log=logs.append)
        self.assertTrue(res.p12_path.exists())
        self.assertTrue(res.profile_path.exists())
        self.assertEqual(inspect_cert(res.p12_path, "pw").organizational_unit, "ABCDE12345")
        self.assertEqual(parse_profile(res.profile_path).profile_type, "development")
        self.assertEqual(len(self.session.certs), 1)
        self.assertEqual(len(self.session.devices), 1)
        posted_profile = [c for c in self.session.calls if c[0] == "POST" and c[1] == "/profiles"][0][2]["data"]
        self.assertEqual(posted_profile["relationships"]["devices"]["data"][0]["id"], self.session.devices[0]["id"])
        self.assertEqual(posted_profile["attributes"]["profileType"], "IOS_APP_DEVELOPMENT")

        # 두 번째 발급: 인증서·기기·번들 재사용, 프로파일은 지우고 새로
        res2 = provision(self.client, "com.example.demo", "UDID1", "My iPhone", "IOS_APP_DEVELOPMENT", out_dir=self.out,
                         p12_password="pw", log=logs.append)
        self.assertEqual(res2.certificate_id, res.certificate_id)
        self.assertEqual(len(self.session.certs), 1)
        self.assertEqual(len(self.session.devices), 1)
        self.assertEqual(len(self.session.bundle_ids), 1)
        self.assertEqual(len(self.session.profiles), 1)
        self.assertNotEqual(res2.profile_id, res.profile_id)

    def test_cert_revoked_triggers_reissue(self):
        res = provision(self.client, "com.example.demo", "UDID1", "iPhone", out_dir=self.out, log=lambda *_: None)
        self.session.certs = []  # 포털에서 폐기됨
        res2 = provision(self.client, "com.example.demo", "UDID1", "iPhone", out_dir=self.out, log=lambda *_: None)
        self.assertNotEqual(res.certificate_id, res2.certificate_id)

    def test_cert_limit_message(self):
        self.session.cert_limit = 0
        with self.assertRaises(AscError) as cm:
            provision(self.client, "com.example.demo", "UDID1", "iPhone", out_dir=self.out, log=lambda *_: None)
        self.assertIn("revoke-cert", str(cm.exception))

    def test_bundle_unavailable(self):
        self.session.reject_bundle = True
        with self.assertRaises(AscError) as cm:
            provision(self.client, "com.spotify.client", "UDID1", "iPhone", out_dir=self.out, log=lambda *_: None)
        self.assertIn("not available", str(cm.exception))

    def test_adhoc_uses_distribution_cert(self):
        provision(self.client, "com.example.demo", "UDID1", "iPhone", "IOS_APP_ADHOC", out_dir=self.out, log=lambda *_: None)
        self.assertEqual(self.session.certs[0]["attributes"]["certificateType"], "DISTRIBUTION")

    def test_bad_profile_type(self):
        with self.assertRaises(AscError):
            provision(self.client, "com.example.demo", "UDID1", "iPhone", "IOS_APP_STORE", out_dir=self.out, log=lambda *_: None)


if __name__ == "__main__":
    unittest.main()
