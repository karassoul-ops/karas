"""설정·경로 관리."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional


def app_home() -> Path:
    """도구가 상태를 보관하는 디렉터리. IOS_SIDELOAD_HOME 환경변수로 바꿀 수 있다."""
    return Path(os.environ.get("IOS_SIDELOAD_HOME", Path.home() / ".ios_sideload")).expanduser()


def config_file() -> Path:
    return app_home() / "config.json"


def tools_dir() -> Path:
    return app_home() / "tools"


def signed_dir() -> Path:
    return app_home() / "signed"


def credentials_dir() -> Path:
    return app_home() / "credentials"


def registry_file() -> Path:
    return app_home() / "registry.json"


@dataclass
class Config:
    # 서명 자료
    cert_path: Optional[str] = None          # .p12
    cert_password: Optional[str] = None
    profile_path: Optional[str] = None       # .mobileprovision
    signer_path: Optional[str] = None        # zsign / rcodesign 실행 파일 (비우면 자동 탐색)

    # App Store Connect API (유료 개발자 계정) — 자동 발급용
    asc_key_id: Optional[str] = None
    asc_issuer_id: Optional[str] = None
    asc_key_path: Optional[str] = None       # AuthKey_XXXX.p8
    asc_profile_type: str = "IOS_APP_DEVELOPMENT"  # 또는 IOS_APP_ADHOC

    # 기기·갱신
    default_udid: Optional[str] = None
    refresh_before_days: int = 2             # 프로파일 만료 며칠 전에 갱신할지
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        path = path or config_file()
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(**kwargs)

    def save(self, path: Optional[Path] = None) -> Path:
        path = path or config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(path, 0o600)  # 비밀번호가 들어가므로 본인만 읽게
        except OSError:
            pass
        return path

    # 편의 판정
    def has_signing_material(self) -> bool:
        return bool(self.cert_path and self.profile_path)

    def has_asc(self) -> bool:
        return bool(self.asc_key_id and self.asc_issuer_id and self.asc_key_path)

    def describe(self) -> list[tuple[str, str]]:
        def show(v: Optional[str]) -> str:
            return v if v else "(미설정)"

        return [
            ("서명 인증서(.p12)", show(self.cert_path)),
            ("인증서 비밀번호", "설정됨" if self.cert_password else "(없음)"),
            ("프로비저닝 프로파일", show(self.profile_path)),
            ("서명 도구 경로", show(self.signer_path) if self.signer_path else "(자동 탐색)"),
            ("ASC Key ID", show(self.asc_key_id)),
            ("ASC Issuer ID", show(self.asc_issuer_id)),
            ("ASC 키 파일(.p8)", show(self.asc_key_path)),
            ("ASC 프로파일 유형", self.asc_profile_type),
            ("기본 기기 UDID", show(self.default_udid)),
            ("만료 전 갱신 일수", str(self.refresh_before_days)),
        ]
