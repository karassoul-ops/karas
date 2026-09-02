"""IPA 재서명 백엔드.

기본은 zsign(https://github.com/zhlynn/zsign). macOS·Linux·Windows에서 동일하게 동작한다.
대안으로 rcodesign(apple-codesign)을 지원한다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import Config, tools_dir


class SignError(Exception):
    pass


@dataclass
class SignRequest:
    ipa: Path
    output: Path
    cert_p12: Path
    cert_password: Optional[str]
    profile: Path
    bundle_id: Optional[str] = None
    bundle_name: Optional[str] = None
    bundle_version: Optional[str] = None
    entitlements: Optional[Path] = None
    remove_extensions: bool = False
    remove_watch_app: bool = False
    enable_file_sharing: bool = False
    dylibs: tuple[Path, ...] = ()
    zip_level: int = 3


class Signer:
    name = "base"

    def __init__(self, executable: Path):
        self.executable = Path(executable)

    def version(self) -> str:
        return "?"

    def sign(self, req: SignRequest, log=print) -> Path:  # pragma: no cover - abstract
        raise NotImplementedError

    def _run(self, cmd: list[str], log=print) -> subprocess.CompletedProcess:
        log("$ " + " ".join(_quote(c) for c in cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if proc.stdout.strip():
            log(proc.stdout.rstrip())
        if proc.returncode != 0:
            raise SignError(f"{self.name} 실행 실패(코드 {proc.returncode}):\n{proc.stderr.strip() or proc.stdout.strip()}")
        return proc


def _quote(s: str) -> str:
    return f'"{s}"' if " " in s else s


class ZsignSigner(Signer):
    name = "zsign"

    def version(self) -> str:
        try:
            out = subprocess.run([str(self.executable), "-v"], capture_output=True, text=True, timeout=10)
            return (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr).strip() else "?"
        except Exception:  # noqa: BLE001
            return "?"

    def build_command(self, req: SignRequest) -> list[str]:
        cmd = [str(self.executable), "-k", str(req.cert_p12), "-m", str(req.profile)]
        if req.cert_password:
            cmd += ["-p", req.cert_password]
        if req.bundle_id:
            cmd += ["-b", req.bundle_id]
        if req.bundle_name:
            cmd += ["-n", req.bundle_name]
        if req.bundle_version:
            cmd += ["-r", req.bundle_version]
        if req.entitlements:
            cmd += ["-e", str(req.entitlements)]
        if req.remove_extensions:
            cmd.append("-E")
        if req.remove_watch_app:
            cmd.append("-W")
        if req.enable_file_sharing:
            cmd.append("-S")
        for d in req.dylibs:
            cmd += ["-l", str(d)]
        cmd += ["-z", str(req.zip_level), "-o", str(req.output), str(req.ipa)]
        return cmd

    def sign(self, req: SignRequest, log=print) -> Path:
        req.output.parent.mkdir(parents=True, exist_ok=True)
        self._run(self.build_command(req), log)
        if not req.output.exists():
            raise SignError("zsign 이 결과 IPA를 만들지 않았습니다.")
        return req.output


class RcodesignSigner(Signer):
    """rcodesign 은 .app 폴더 단위로 서명하므로 IPA를 풀었다가 다시 묶는다."""

    name = "rcodesign"

    def version(self) -> str:
        try:
            out = subprocess.run([str(self.executable), "--version"], capture_output=True, text=True, timeout=10)
            return out.stdout.strip() or "?"
        except Exception:  # noqa: BLE001
            return "?"

    def sign(self, req: SignRequest, log=print) -> Path:
        if req.bundle_id or req.bundle_name or req.bundle_version or req.dylibs:
            raise SignError("rcodesign 백엔드는 번들 ID/이름 변경, dylib 주입을 지원하지 않습니다. zsign 을 설치하세요.")
        with tempfile.TemporaryDirectory(prefix="sideload_") as td:
            work = Path(td)
            with zipfile.ZipFile(req.ipa) as zf:
                zf.extractall(work)
            apps = list((work / "Payload").glob("*.app"))
            if not apps:
                raise SignError("Payload/*.app 이 없습니다.")
            app = apps[0]
            shutil.copy(req.profile, app / "embedded.mobileprovision")
            cmd = [str(self.executable), "sign", "--p12-file", str(req.cert_p12),
                   "--provisioning-profile-path", str(req.profile)]
            if req.cert_password is not None:
                cmd += ["--p12-password", req.cert_password]
            if req.entitlements:
                cmd += ["--entitlements-xml-path", str(req.entitlements)]
            cmd.append(str(app))
            self._run(cmd, log)
            req.output.parent.mkdir(parents=True, exist_ok=True)
            _zip_dir(work, req.output, req.zip_level)
        return req.output


def _zip_dir(root: Path, out: Path, level: int) -> None:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=max(0, min(9, level))) as zf:
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root).as_posix()
            if p.is_dir():
                continue
            zf.write(p, rel)


# ---------- 탐색 ----------

def _exe_name(base: str) -> str:
    return base + (".exe" if sys.platform.startswith("win") else "")


def candidate_paths(config: Optional[Config] = None) -> list[Path]:
    cands: list[Path] = []
    if config and config.signer_path:
        cands.append(Path(config.signer_path).expanduser())
    env = os.environ.get("ZSIGN")
    if env:
        cands.append(Path(env))
    for base in ("zsign", "rcodesign"):
        cands.append(tools_dir() / _exe_name(base))
        which = shutil.which(base)
        if which:
            cands.append(Path(which))
    return cands


def make_signer(path: Path) -> Signer:
    if "rcodesign" in path.name.lower():
        return RcodesignSigner(path)
    return ZsignSigner(path)


def find_signer(config: Optional[Config] = None) -> Optional[Signer]:
    for p in candidate_paths(config):
        if p.exists() and p.is_file():
            return make_signer(p)
    return None


def require_signer(config: Optional[Config] = None) -> Signer:
    s = find_signer(config)
    if not s:
        raise SignError(
            "서명 도구(zsign)를 찾지 못했습니다.\n"
            "  - `python sideload.py tools install` 로 설치를 시도하거나\n"
            "  - https://github.com/zhlynn/zsign 에서 내려받아 PATH 또는 "
            f"{tools_dir()} 에 두거나\n"
            "  - `python sideload.py setup` 에서 서명 도구 경로를 지정하세요."
        )
    return s
