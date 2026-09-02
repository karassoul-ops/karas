"""App Store Connect API 연동 — 유료 Apple Developer Program 계정용.

API 키(.p8)로 인증서 발급·기기 등록·번들 ID 생성·프로비저닝 프로파일 발급을 자동화한다.
개발용/애드혹 프로파일은 1년 유효하므로 7일마다 재서명할 필요가 없다.

API 키 만들기: App Store Connect → 사용자 및 액세스 → 통합(Integrations) → App Store Connect API
  → 키 생성 (권한: Admin 또는 App Manager 이상) → .p8 다운로드, Key ID·Issuer ID 기록
"""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .cert import generate_key_and_csr, write_p12
from .config import credentials_dir

API_BASE = "https://api.appstoreconnect.apple.com/v1"

CERT_TYPE_FOR_PROFILE = {
    "IOS_APP_DEVELOPMENT": "DEVELOPMENT",
    "IOS_APP_ADHOC": "DISTRIBUTION",
}
LEGACY_CERT_TYPES = {
    "DEVELOPMENT": ("DEVELOPMENT", "IOS_DEVELOPMENT"),
    "DISTRIBUTION": ("DISTRIBUTION", "IOS_DISTRIBUTION"),
}


class AscError(Exception):
    pass


@dataclass
class ProvisionResult:
    p12_path: Path
    profile_path: Path
    certificate_id: str
    profile_id: str
    bundle_id: str
    device_id: str


class AscClient:
    def __init__(self, key_id: str, issuer_id: str, private_key_pem: str, session=None):
        self.key_id = key_id
        self.issuer_id = issuer_id
        self.private_key_pem = private_key_pem
        self._token: Optional[str] = None
        self._token_exp: float = 0
        if session is None:
            import requests
            session = requests.Session()
        self.session = session

    @classmethod
    def from_config(cls, cfg) -> "AscClient":
        if not cfg.has_asc():
            raise AscError("App Store Connect API 키가 설정되지 않았습니다. `setup` 에서 Key ID / Issuer ID / .p8 경로를 입력하세요.")
        pem = Path(cfg.asc_key_path).expanduser().read_text(encoding="utf-8")
        return cls(cfg.asc_key_id, cfg.asc_issuer_id, pem)

    # ----- 인증 -----
    def token(self) -> str:
        now = time.time()
        if self._token and now < self._token_exp - 60:
            return self._token
        import jwt
        exp = int(now) + 20 * 60
        payload = {"iss": self.issuer_id, "iat": int(now), "exp": exp, "aud": "appstoreconnect-v1"}
        self._token = jwt.encode(payload, self.private_key_pem, algorithm="ES256", headers={"kid": self.key_id, "typ": "JWT"})
        self._token_exp = exp
        return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"}

    def _request(self, method: str, path: str, **kw) -> dict[str, Any]:
        url = path if path.startswith("http") else API_BASE + path
        resp = self.session.request(method, url, headers=self._headers(), timeout=60, **kw)
        if resp.status_code == 204:
            return {}
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text}
        if resp.status_code >= 400:
            errs = body.get("errors") or []
            detail = "; ".join(f"{e.get('code', '')}: {e.get('detail') or e.get('title', '')}" for e in errs) or str(body)
            raise AscError(f"ASC API {method} {path} 실패 ({resp.status_code}): {detail}")
        return body

    def get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return self._request("GET", path, params=params)

    def get_all(self, path: str, params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        params = dict(params or {})
        params.setdefault("limit", 200)
        body = self.get(path, params)
        items = list(body.get("data", []))
        nxt = (body.get("links") or {}).get("next")
        while nxt:
            body = self._request("GET", nxt)
            items.extend(body.get("data", []))
            nxt = (body.get("links") or {}).get("next")
        return items

    def post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, json={"data": data})

    def delete(self, path: str) -> None:
        self._request("DELETE", path)

    # ----- 기기 -----
    def list_devices(self) -> list[dict[str, Any]]:
        return self.get_all("/devices", {"filter[platform]": "IOS"})

    def find_device(self, udid: str) -> Optional[dict[str, Any]]:
        u = udid.replace("-", "").lower()
        for d in self.list_devices():
            if d["attributes"].get("udid", "").replace("-", "").lower() == u:
                return d
        return None

    def register_device(self, name: str, udid: str) -> dict[str, Any]:
        existing = self.find_device(udid)
        if existing:
            if existing["attributes"].get("status") == "DISABLED":
                return self._request("PATCH", f"/devices/{existing['id']}", json={"data": {
                    "type": "devices", "id": existing["id"], "attributes": {"status": "ENABLED"}}})["data"]
            return existing
        return self.post("/devices", {"type": "devices", "attributes": {"name": name, "udid": udid, "platform": "IOS"}})["data"]

    # ----- 번들 ID -----
    def find_bundle_id(self, identifier: str) -> Optional[dict[str, Any]]:
        for b in self.get_all("/bundleIds", {"filter[identifier]": identifier, "filter[platform]": "IOS"}):
            if b["attributes"].get("identifier") == identifier:
                return b
        return None

    def ensure_bundle_id(self, identifier: str, name: Optional[str] = None) -> dict[str, Any]:
        found = self.find_bundle_id(identifier)
        if found:
            return found
        safe_name = re.sub(r"[^A-Za-z0-9 ]", " ", name or identifier).strip() or "Sideload App"
        return self.post("/bundleIds", {"type": "bundleIds", "attributes": {
            "identifier": identifier, "name": safe_name[:64], "platform": "IOS"}})["data"]

    # ----- 인증서 -----
    def list_certificates(self, cert_kind: str) -> list[dict[str, Any]]:
        types = LEGACY_CERT_TYPES.get(cert_kind, (cert_kind,))
        certs = self.get_all("/certificates")
        return [c for c in certs if c["attributes"].get("certificateType") in types]

    def create_certificate(self, csr_pem: str, cert_kind: str) -> dict[str, Any]:
        api_type = LEGACY_CERT_TYPES.get(cert_kind, (cert_kind,))[0]
        try:
            return self.post("/certificates", {"type": "certificates", "attributes": {
                "certificateType": api_type, "csrContent": csr_pem}})["data"]
        except AscError as e:
            # 구형 계정은 IOS_DEVELOPMENT / IOS_DISTRIBUTION 만 받는 경우가 있다
            fallback = LEGACY_CERT_TYPES.get(cert_kind, (None, None))[1]
            if fallback and "certificateType" in str(e):
                return self.post("/certificates", {"type": "certificates", "attributes": {
                    "certificateType": fallback, "csrContent": csr_pem}})["data"]
            raise

    def revoke_certificate(self, cert_id: str) -> None:
        self.delete(f"/certificates/{cert_id}")

    # ----- 프로파일 -----
    def find_profiles(self, name: str) -> list[dict[str, Any]]:
        return self.get_all("/profiles", {"filter[name]": name})

    def delete_profile(self, profile_id: str) -> None:
        self.delete(f"/profiles/{profile_id}")

    def create_profile(self, name: str, profile_type: str, bundle_id_res: str,
                       cert_ids: list[str], device_ids: list[str]) -> dict[str, Any]:
        data = {
            "type": "profiles",
            "attributes": {"name": name, "profileType": profile_type},
            "relationships": {
                "bundleId": {"data": {"type": "bundleIds", "id": bundle_id_res}},
                "certificates": {"data": [{"type": "certificates", "id": c} for c in cert_ids]},
            },
        }
        if profile_type in ("IOS_APP_DEVELOPMENT", "IOS_APP_ADHOC"):
            data["relationships"]["devices"] = {"data": [{"type": "devices", "id": d} for d in device_ids]}
        return self.post("/profiles", data)["data"]


def profile_name_for(bundle_id: str, profile_type: str) -> str:
    kind = "dev" if profile_type == "IOS_APP_DEVELOPMENT" else "adhoc"
    return f"sideload {kind} {bundle_id}"[:120]


def provision(client: AscClient, bundle_id: str, udid: str, device_name: str,
              profile_type: str = "IOS_APP_DEVELOPMENT", app_name: Optional[str] = None,
              p12_password: Optional[str] = None, cert_email: Optional[str] = None,
              out_dir: Optional[Path] = None, log=print) -> ProvisionResult:
    """번들 ID 등록 → 기기 등록 → 인증서 발급(.p12 저장) → 프로파일 발급(.mobileprovision 저장)."""
    if profile_type not in CERT_TYPE_FOR_PROFILE:
        raise AscError(f"지원하지 않는 프로파일 유형: {profile_type} (IOS_APP_DEVELOPMENT / IOS_APP_ADHOC)")
    out_dir = out_dir or credentials_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    cert_kind = CERT_TYPE_FOR_PROFILE[profile_type]

    log(f"[1/4] 번들 ID 확인: {bundle_id}")
    bid = client.ensure_bundle_id(bundle_id, app_name)

    log(f"[2/4] 기기 등록: {device_name} ({udid})")
    dev = client.register_device(device_name[:50] or "iPhone", udid)

    # 인증서: 개인키를 우리가 갖고 있는 것만 쓸 수 있으므로, 저장된 .p12 가 없으면 새로 발급한다.
    p12_path = out_dir / f"sideload_{cert_kind.lower()}.p12"
    cert_id_file = out_dir / f"sideload_{cert_kind.lower()}.certid"
    cert_id: Optional[str] = None
    if p12_path.exists() and cert_id_file.exists():
        cert_id = cert_id_file.read_text().strip()
        live = {c["id"] for c in client.list_certificates(cert_kind)}
        if cert_id not in live:
            log("  저장된 인증서가 포털에 없어(폐기/만료) 새로 발급합니다.")
            cert_id = None
    if not cert_id:
        log(f"[3/4] {cert_kind} 인증서 발급")
        key, csr = generate_key_and_csr("ios_sideload", cert_email)
        try:
            cert = client.create_certificate(csr, cert_kind)
        except AscError as e:
            if "limit" in str(e).lower() or "maximum" in str(e).lower():
                raise AscError(
                    f"{e}\n  인증서 발급 한도에 걸렸습니다. `asc certs` 로 목록을 보고 "
                    "`asc revoke-cert <ID>` 로 안 쓰는 인증서를 폐기한 뒤 다시 시도하세요.") from e
            raise
        cert_id = cert["id"]
        der = base64.b64decode(cert["attributes"]["certificateContent"])
        write_p12(p12_path, key, der, p12_password)
        cert_id_file.write_text(cert_id)
        log(f"  저장: {p12_path}")
    else:
        log(f"[3/4] 저장된 인증서 재사용: {cert_id}")

    log(f"[4/4] 프로파일 발급 ({profile_type})")
    name = profile_name_for(bundle_id, profile_type)
    for old in client.find_profiles(name):
        client.delete_profile(old["id"])  # 기기 추가·인증서 교체가 반영되도록 새로 만든다
    prof = client.create_profile(name, profile_type, bid["id"], [cert_id], [dev["id"]])
    content = base64.b64decode(prof["attributes"]["profileContent"])
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", bundle_id)
    profile_path = out_dir / f"{safe}.{'dev' if profile_type == 'IOS_APP_DEVELOPMENT' else 'adhoc'}.mobileprovision"
    profile_path.write_bytes(content)
    log(f"  저장: {profile_path}")
    return ProvisionResult(p12_path, profile_path, cert_id, prof["id"], bid["id"], dev["id"])
