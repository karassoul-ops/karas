"""zsign 실행 파일 설치 도우미.

GitHub 릴리스에서 현재 OS/CPU 에 맞는 zsign 을 내려받아 ~/.ios_sideload/tools 에 둔다.
릴리스 자산 이름은 버전마다 달라질 수 있어, 이름에 들어간 OS·CPU 키워드로 고른다.
실패하면 소스 빌드 방법을 안내한다.
"""

from __future__ import annotations

import io
import os
import platform
import stat
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Optional

from .config import tools_dir

ZSIGN_REPO = "zhlynn/zsign"
RELEASES_API = f"https://api.github.com/repos/{ZSIGN_REPO}/releases/latest"


def platform_keywords() -> tuple[list[str], list[str]]:
    """(OS 키워드, CPU 키워드)"""
    sysname = sys.platform
    machine = platform.machine().lower()
    if sysname.startswith("win"):
        os_kw = ["windows", "win"]
    elif sysname == "darwin":
        os_kw = ["macos", "darwin", "mac", "osx"]
    else:
        os_kw = ["linux", "ubuntu"]
    if machine in ("arm64", "aarch64"):
        cpu_kw = ["arm64", "aarch64"]
    else:
        cpu_kw = ["x86_64", "x64", "amd64"]
    return os_kw, cpu_kw


def choose_asset(assets: list[dict], os_kw: list[str], cpu_kw: list[str]) -> Optional[dict]:
    def score(a: dict) -> int:
        n = a.get("name", "").lower()
        s = 0
        if any(k in n for k in os_kw):
            s += 10
        if any(k in n for k in cpu_kw):
            s += 5
        if "universal" in n and s >= 10:
            s += 3
        if n.endswith((".zip", ".tar.gz", ".tgz")) or "." not in n.rsplit("/", 1)[-1]:
            s += 1
        return s

    ranked = sorted(assets, key=score, reverse=True)
    if not ranked or score(ranked[0]) < 10:
        return None
    return ranked[0]


def _extract_zsign(data: bytes, name: str, dest: Path) -> Optional[Path]:
    exe = "zsign.exe" if sys.platform.startswith("win") else "zsign"
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / exe
    lname = name.lower()
    if lname.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for m in zf.namelist():
                if Path(m).name.lower() in ("zsign", "zsign.exe"):
                    target.write_bytes(zf.read(m))
                    break
            else:
                return None
    elif lname.endswith((".tar.gz", ".tgz")):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            for m in tf.getmembers():
                if Path(m.name).name.lower() in ("zsign", "zsign.exe") and m.isfile():
                    f = tf.extractfile(m)
                    if f:
                        target.write_bytes(f.read())
                        break
            else:
                return None
    else:
        target.write_bytes(data)
    if not sys.platform.startswith("win"):
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def install_zsign(dest: Optional[Path] = None, log=print) -> Optional[Path]:
    import requests

    dest = dest or tools_dir()
    os_kw, cpu_kw = platform_keywords()
    log(f"zsign 최신 릴리스 조회 ({RELEASES_API})")
    try:
        r = requests.get(RELEASES_API, timeout=30, headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        rel = r.json()
    except Exception as e:  # noqa: BLE001
        log(f"릴리스 정보를 가져오지 못했습니다: {e}")
        log(manual_instructions())
        return None
    asset = choose_asset(rel.get("assets", []), os_kw, cpu_kw)
    if not asset:
        log("이 플랫폼에 맞는 릴리스 파일을 찾지 못했습니다. 릴리스 목록: " +
            ", ".join(a.get("name", "") for a in rel.get("assets", [])))
        log(manual_instructions())
        return None
    log(f"내려받기: {asset['name']} ({rel.get('tag_name')})")
    data = requests.get(asset["browser_download_url"], timeout=300).content
    path = _extract_zsign(data, asset["name"], dest)
    if not path:
        log("압축 파일 안에서 zsign 실행 파일을 찾지 못했습니다.")
        log(manual_instructions())
        return None
    log(f"설치 완료: {path}")
    return path


def manual_instructions() -> str:
    d = tools_dir()
    if sys.platform.startswith("win"):
        return (
            "\n수동 설치:\n"
            "  1) https://github.com/zhlynn/zsign/releases 에서 Windows 용 zsign.exe 를 내려받아\n"
            f"  2) {d}\\zsign.exe 로 저장하거나 PATH 에 두세요.\n"
        )
    if sys.platform == "darwin":
        return (
            "\n수동 설치(macOS):\n"
            "  brew install zsign     # Homebrew 에 있는 경우\n"
            "  또는 소스 빌드:\n"
            "  git clone https://github.com/zhlynn/zsign.git && cd zsign/build/macos && make\n"
            f"  cp zsign {d}/zsign\n"
        )
    return (
        "\n수동 설치(Linux):\n"
        "  sudo apt install -y git g++ make libssl-dev\n"
        "  git clone https://github.com/zhlynn/zsign.git && cd zsign/build/linux && make\n"
        f"  mkdir -p {d} && cp zsign {d}/zsign\n"
    )


def which_env_path() -> str:
    return os.environ.get("PATH", "")
