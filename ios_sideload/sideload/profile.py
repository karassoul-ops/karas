""".mobileprovision(프로비저닝 프로파일) 파싱.

프로파일은 CMS(PKCS#7)로 서명된 plist다. 서명 검증 없이 안에 든 plist만 꺼내 읽는다.
"""

from __future__ import annotations

import plistlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class ProfileError(Exception):
    pass


@dataclass
class ProfileInfo:
    path: Path
    name: str
    uuid: str
    team_id: str
    team_name: str
    app_id_name: str
    application_identifier: str          # "TEAMID.com.example.app" 또는 "TEAMID.*"
    creation: datetime
    expiration: datetime
    devices: list[str] = field(default_factory=list)
    provisions_all_devices: bool = False
    get_task_allow: bool = False
    certificates_der: list[bytes] = field(default_factory=list, repr=False)
    entitlements: dict[str, Any] = field(default_factory=dict, repr=False)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # ----- 파생 정보 -----
    @property
    def bundle_id_pattern(self) -> str:
        """TEAMID. 접두어를 뗀 번들 ID 패턴(와일드카드 포함 가능)."""
        prefix = self.team_id + "."
        if self.application_identifier.startswith(prefix):
            return self.application_identifier[len(prefix):]
        # 팀 ID가 아닌 다른 prefix(App ID prefix)를 쓰는 경우 첫 마디를 뗀다
        return self.application_identifier.split(".", 1)[1] if "." in self.application_identifier else "*"

    @property
    def profile_type(self) -> str:
        if self.provisions_all_devices:
            return "enterprise"
        if self.get_task_allow:
            return "development"
        if self.devices:
            return "adhoc"
        return "appstore"

    @property
    def days_left(self) -> float:
        return (self.expiration - datetime.now(timezone.utc)).total_seconds() / 86400

    @property
    def is_expired(self) -> bool:
        return self.days_left <= 0

    def matches_bundle_id(self, bundle_id: str) -> bool:
        pat = self.bundle_id_pattern
        if pat == "*":
            return True
        if pat.endswith(".*"):
            return bundle_id.startswith(pat[:-1]) or bundle_id == pat[:-2]
        return bundle_id == pat

    def covers_device(self, udid: str) -> bool:
        if self.provisions_all_devices or self.profile_type == "appstore":
            return True
        u = udid.replace("-", "").lower()
        return any(d.replace("-", "").lower() == u for d in self.devices)

    def summary(self) -> list[tuple[str, str]]:
        return [
            ("파일", str(self.path)),
            ("이름", self.name),
            ("유형", self.profile_type),
            ("팀", f"{self.team_name} ({self.team_id})"),
            ("App ID", self.application_identifier),
            ("생성", self.creation.astimezone().strftime("%Y-%m-%d %H:%M")),
            ("만료", f"{self.expiration.astimezone().strftime('%Y-%m-%d %H:%M')} (남은 일수 {self.days_left:.1f})"),
            ("등록 기기 수", "전체 기기" if self.provisions_all_devices else str(len(self.devices))),
            ("get-task-allow", "예" if self.get_task_allow else "아니오"),
            ("인증서 수", str(len(self.certificates_der))),
        ]


def extract_plist_bytes(data: bytes) -> bytes:
    """CMS 바이너리 안에서 plist XML 구간을 찾아 돌려준다."""
    start = data.find(b"<?xml")
    if start < 0:
        start = data.find(b"<plist")
    end = data.rfind(b"</plist>")
    if start < 0 or end < 0:
        # 혹시 바이너리 plist가 들어있는 경우
        bstart = data.find(b"bplist00")
        if bstart >= 0:
            return data[bstart:]
        raise ProfileError("프로파일 안에서 plist 를 찾지 못했습니다. .mobileprovision 파일이 맞는지 확인하세요.")
    return data[start:end + len(b"</plist>")]


def _as_utc(dt: Any) -> datetime:
    if isinstance(dt, datetime):
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    raise ProfileError(f"날짜 형식이 올바르지 않습니다: {dt!r}")


def parse_profile_bytes(data: bytes, path: Path | str = "<memory>") -> ProfileInfo:
    plist = plistlib.loads(extract_plist_bytes(data))
    ent = plist.get("Entitlements", {}) or {}
    teams = plist.get("TeamIdentifier") or []
    team_id = teams[0] if teams else ent.get("com.apple.developer.team-identifier", "")
    app_ident = ent.get("application-identifier", "")
    return ProfileInfo(
        path=Path(path),
        name=plist.get("Name", ""),
        uuid=plist.get("UUID", ""),
        team_id=team_id,
        team_name=plist.get("TeamName", ""),
        app_id_name=plist.get("AppIDName", ""),
        application_identifier=app_ident,
        creation=_as_utc(plist.get("CreationDate")),
        expiration=_as_utc(plist.get("ExpirationDate")),
        devices=list(plist.get("ProvisionedDevices", []) or []),
        provisions_all_devices=bool(plist.get("ProvisionsAllDevices", False)),
        get_task_allow=bool(ent.get("get-task-allow", False)),
        certificates_der=[bytes(c) for c in plist.get("DeveloperCertificates", []) or []],
        entitlements=dict(ent),
        raw=plist,
    )


def parse_profile(path: Path | str) -> ProfileInfo:
    path = Path(path)
    if not path.exists():
        raise ProfileError(f"프로파일 파일이 없습니다: {path}")
    return parse_profile_bytes(path.read_bytes(), path)
