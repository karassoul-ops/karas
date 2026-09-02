"""실행 환경 점검."""

from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path
from typing import Callable

from .config import Config, app_home
from .signer import find_signer

Check = tuple[str, bool, str]  # (항목, 통과 여부, 설명)


def _module_ok(name: str) -> tuple[bool, str]:
    try:
        m = importlib.import_module(name)
        return True, getattr(m, "__version__", "설치됨")
    except Exception as e:  # noqa: BLE001
        return False, f"없음 ({type(e).__name__})"


def run_checks(cfg: Config, probe_device: bool = True, list_devices: Callable | None = None) -> list[Check]:
    checks: list[Check] = []
    checks.append(("Python", sys.version_info >= (3, 9), sys.version.split()[0] + " (3.9 이상 필요)"))
    for mod, label in (("pymobiledevice3", "pymobiledevice3 (기기 연결)"),
                       ("cryptography", "cryptography (인증서)"),
                       ("jwt", "PyJWT (App Store Connect API)"),
                       ("requests", "requests (다운로드/API)")):
        ok, msg = _module_ok(mod)
        checks.append((label, ok, msg))

    signer = find_signer(cfg)
    checks.append(("서명 도구(zsign/rcodesign)", signer is not None,
                   f"{signer.name} {signer.version()} — {signer.executable}" if signer else "없음 → `tools install` 실행"))

    if sys.platform.startswith("linux"):
        has_mux = shutil.which("usbmuxd") is not None or Path("/var/run/usbmuxd").exists()
        checks.append(("usbmuxd (Linux USB 연결)", has_mux, "실행 중/설치됨" if has_mux else "없음 → sudo apt install usbmuxd"))
    elif sys.platform.startswith("win"):
        amds = any(Path(p).exists() for p in (
            r"C:\Program Files\Common Files\Apple\Mobile Device Support",
            r"C:\Program Files (x86)\Common Files\Apple\Mobile Device Support"))
        checks.append(("Apple Mobile Device Support (iTunes)", amds, "설치됨" if amds else "iTunes 설치 필요"))

    checks.append(("설정 파일", app_home().exists(), str(app_home())))
    if cfg.has_asc():
        p8 = Path(cfg.asc_key_path).expanduser().exists()
        checks.append(("ASC API 키(.p8)", p8, cfg.asc_key_path))
    if cfg.cert_path:
        checks.append(("서명 인증서(.p12)", Path(cfg.cert_path).expanduser().exists(), cfg.cert_path))
    if cfg.profile_path:
        checks.append(("프로비저닝 프로파일", Path(cfg.profile_path).expanduser().exists(), cfg.profile_path))
    if not cfg.has_asc() and not cfg.has_signing_material():
        checks.append(("서명 자료", False, "인증서+프로파일 또는 ASC API 키 중 하나는 있어야 합니다 → `setup`"))

    if probe_device:
        try:
            fn = list_devices
            if fn is None:
                from .device import list_devices as fn
            devs = fn()
            checks.append(("연결된 아이폰", len(devs) > 0,
                           "; ".join(d.label() for d in devs) if devs else "없음 (USB 연결·잠금 해제·'신뢰' 확인)"))
            for d in devs:
                if d.developer_mode is False:
                    checks.append((f"개발자 모드 ({d.name})", False, "꺼짐 → `dev-mode enable` 또는 설정 > 개인정보 보호 및 보안 > 개발자 모드"))
        except Exception as e:  # noqa: BLE001
            checks.append(("연결된 아이폰", False, str(e).splitlines()[0]))
    return checks


def format_checks(checks: list[Check]) -> str:
    lines = []
    for label, ok, msg in checks:
        lines.append(f"  [{'OK' if ok else '!!'}] {label}: {msg}")
    fails = sum(1 for _, ok, _ in checks if not ok)
    lines.append("")
    lines.append("모든 항목 통과" if fails == 0 else f"조치 필요 항목 {fails}개")
    return "\n".join(lines)
