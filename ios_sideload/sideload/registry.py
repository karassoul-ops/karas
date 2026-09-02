"""설치한 앱 기록과 만료 전 갱신."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import registry_file


@dataclass
class AppRecord:
    bundle_id: str                    # 서명 후 실제 설치된 번들 ID
    name: str
    source_ipa: str                   # 원본 IPA 경로
    signed_ipa: str                   # 서명본 경로
    udid: str
    signed_at: str                    # ISO 8601 UTC
    profile_path: str
    profile_expires: str              # ISO 8601 UTC
    cert_path: str
    cert_expires: str
    original_bundle_id: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def profile_days_left(self) -> float:
        return (datetime.fromisoformat(self.profile_expires) - datetime.now(timezone.utc)).total_seconds() / 86400

    @property
    def cert_days_left(self) -> float:
        return (datetime.fromisoformat(self.cert_expires) - datetime.now(timezone.utc)).total_seconds() / 86400

    @property
    def days_left(self) -> float:
        return min(self.profile_days_left, self.cert_days_left)

    def needs_refresh(self, before_days: float) -> bool:
        return self.days_left <= before_days

    def key(self) -> str:
        return f"{self.udid}:{self.bundle_id}"


class Registry:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or registry_file()
        self.records: dict[str, AppRecord] = {}
        self.load()

    def load(self) -> None:
        self.records = {}
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        known = {f.name for f in fields(AppRecord)}
        for item in data.get("apps", []):
            rec = AppRecord(**{k: v for k, v in item.items() if k in known})
            self.records[rec.key()] = rec

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "apps": [asdict(r) for r in self.records.values()]}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def upsert(self, rec: AppRecord) -> None:
        self.records[rec.key()] = rec
        self.save()

    def remove(self, udid: str, bundle_id: str) -> bool:
        k = f"{udid}:{bundle_id}"
        if k in self.records:
            del self.records[k]
            self.save()
            return True
        return False

    def all(self) -> list[AppRecord]:
        return sorted(self.records.values(), key=lambda r: r.days_left)

    def due(self, before_days: float) -> list[AppRecord]:
        return [r for r in self.all() if r.needs_refresh(before_days)]


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()
