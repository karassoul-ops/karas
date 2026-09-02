""".p12 서명 인증서 검사·생성 (cryptography 사용)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from .profile import ProfileInfo


class CertError(Exception):
    pass


@dataclass
class CertInfo:
    path: Path
    subject: str
    common_name: str
    organizational_unit: str            # 보통 팀 ID
    issuer: str
    serial: str
    not_before: datetime
    not_after: datetime
    sha1: str
    der: bytes

    @property
    def days_left(self) -> float:
        return (self.not_after - datetime.now(timezone.utc)).total_seconds() / 86400

    @property
    def is_expired(self) -> bool:
        return self.days_left <= 0

    def summary(self) -> list[tuple[str, str]]:
        return [
            ("파일", str(self.path)),
            ("주체(CN)", self.common_name),
            ("팀(OU)", self.organizational_unit),
            ("발급자", self.issuer),
            ("유효 시작", self.not_before.astimezone().strftime("%Y-%m-%d")),
            ("만료", f"{self.not_after.astimezone().strftime('%Y-%m-%d')} (남은 일수 {self.days_left:.1f})"),
            ("SHA-1", self.sha1),
        ]


def _name_attr(name: x509.Name, oid) -> str:
    try:
        attrs = name.get_attributes_for_oid(oid)
    except Exception:  # noqa: BLE001
        return ""
    return str(attrs[0].value) if attrs else ""


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_p12(path: Path | str, password: Optional[str]) -> tuple[object, x509.Certificate]:
    path = Path(path)
    if not path.exists():
        raise CertError(f"인증서 파일이 없습니다: {path}")
    pw = password.encode() if password else None
    try:
        key, cert, _extra = pkcs12.load_key_and_certificates(path.read_bytes(), pw)
    except ValueError as e:
        raise CertError(f".p12 를 열지 못했습니다(비밀번호 오류 또는 손상): {e}") from e
    if cert is None:
        raise CertError(".p12 안에 인증서가 없습니다.")
    if key is None:
        raise CertError(".p12 안에 개인키가 없습니다. 키체인/키 관리자에서 '개인키 포함'으로 다시 내보내세요.")
    return key, cert


def inspect_cert(path: Path | str, password: Optional[str]) -> CertInfo:
    _key, cert = load_p12(path, password)
    return cert_info_from_x509(cert, Path(path))


def cert_info_from_x509(cert: x509.Certificate, path: Path) -> CertInfo:
    nb = getattr(cert, "not_valid_before_utc", None) or _utc(cert.not_valid_before)
    na = getattr(cert, "not_valid_after_utc", None) or _utc(cert.not_valid_after)
    return CertInfo(
        path=path,
        subject=cert.subject.rfc4514_string(),
        common_name=_name_attr(cert.subject, NameOID.COMMON_NAME),
        organizational_unit=_name_attr(cert.subject, NameOID.ORGANIZATIONAL_UNIT_NAME),
        issuer=_name_attr(cert.issuer, NameOID.COMMON_NAME) or cert.issuer.rfc4514_string(),
        serial=format(cert.serial_number, "X"),
        not_before=nb,
        not_after=na,
        sha1=cert.fingerprint(hashes.SHA1()).hex().upper(),
        der=cert.public_bytes(serialization.Encoding.DER),
    )


def cert_in_profile(cert: CertInfo, profile: ProfileInfo) -> bool:
    """인증서가 프로파일의 DeveloperCertificates 목록에 들어 있는지."""
    return any(d == cert.der for d in profile.certificates_der)


# ---------- 생성(ASC 자동 발급용) ----------

def generate_key_and_csr(common_name: str, email: Optional[str] = None, country: str = "KR") -> tuple[rsa.RSAPrivateKey, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    attrs = [
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        x509.NameAttribute(NameOID.COUNTRY_NAME, country),
    ]
    if email:
        attrs.insert(0, x509.NameAttribute(NameOID.EMAIL_ADDRESS, email))
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name(attrs)).sign(key, hashes.SHA256())
    return key, csr.public_bytes(serialization.Encoding.PEM).decode()


def write_p12(path: Path, key, cert_der: bytes, password: Optional[str], friendly_name: str = "ios_sideload") -> Path:
    cert = x509.load_der_x509_certificate(cert_der)
    enc = serialization.BestAvailableEncryption(password.encode()) if password else serialization.NoEncryption()
    data = pkcs12.serialize_key_and_certificates(friendly_name.encode(), key, cert, None, enc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path
