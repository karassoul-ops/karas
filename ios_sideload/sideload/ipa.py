"""IPA 파일 검사."""

from __future__ import annotations

import plistlib
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_APP_INFO_RE = re.compile(r"^Payload/([^/]+\.app)/Info\.plist$")


class IpaError(Exception):
    pass


@dataclass
class IpaInfo:
    path: Path
    app_dir: str                      # Payload/Foo.app
    bundle_id: str
    name: str
    version: str                      # CFBundleShortVersionString
    build: str                        # CFBundleVersion
    min_os: Optional[str]
    executable: Optional[str]
    has_extensions: bool = False
    has_watch_app: bool = False
    info: dict[str, Any] = field(default_factory=dict, repr=False)

    def summary(self) -> list[tuple[str, str]]:
        return [
            ("파일", str(self.path)),
            ("앱 이름", self.name),
            ("번들 ID", self.bundle_id),
            ("버전", f"{self.version} ({self.build})"),
            ("최소 iOS", self.min_os or "?"),
            ("실행 파일", self.executable or "?"),
            ("앱 확장(Extension)", "있음" if self.has_extensions else "없음"),
            ("워치 앱", "있음" if self.has_watch_app else "없음"),
        ]


def find_app_info_entry(names: list[str]) -> Optional[str]:
    """zip 항목 목록에서 최상위 앱의 Info.plist 경로를 찾는다."""
    for n in names:
        if _APP_INFO_RE.match(n):
            return n
    return None


def inspect_ipa(path: Path | str) -> IpaInfo:
    path = Path(path)
    if not path.exists():
        raise IpaError(f"IPA 파일이 없습니다: {path}")
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise IpaError(f"IPA(zip) 형식이 아닙니다: {path} ({e})") from e
    with zf:
        names = zf.namelist()
        entry = find_app_info_entry(names)
        if not entry:
            raise IpaError("Payload/*.app/Info.plist 를 찾지 못했습니다. 올바른 IPA가 아닙니다.")
        try:
            info = plistlib.loads(zf.read(entry))
        except Exception as e:  # noqa: BLE001
            raise IpaError(f"Info.plist 를 읽지 못했습니다: {e}") from e
        app_dir = entry.rsplit("/", 1)[0]
        has_ext = any(n.startswith(app_dir + "/PlugIns/") or n.startswith(app_dir + "/Extensions/") for n in names)
        has_watch = any(n.startswith(app_dir + "/Watch/") for n in names)

    bundle_id = info.get("CFBundleIdentifier")
    if not bundle_id:
        raise IpaError("Info.plist 에 CFBundleIdentifier 가 없습니다.")
    return IpaInfo(
        path=path,
        app_dir=app_dir,
        bundle_id=bundle_id,
        name=info.get("CFBundleDisplayName") or info.get("CFBundleName") or app_dir.split("/")[-1][:-4],
        version=str(info.get("CFBundleShortVersionString", "?")),
        build=str(info.get("CFBundleVersion", "?")),
        min_os=info.get("MinimumOSVersion"),
        executable=info.get("CFBundleExecutable"),
        has_extensions=has_ext,
        has_watch_app=has_watch,
        info=info,
    )
