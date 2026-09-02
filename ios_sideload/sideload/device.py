"""아이폰 연결·설치 (pymobiledevice3).

USB(usbmuxd) 또는 같은 Wi-Fi 의 기기에 연결한다.
  - macOS: 별도 준비 없음
  - Windows: iTunes(Apple Mobile Device Support) 설치 필요
  - Linux: `sudo apt install usbmuxd` 후 usbmuxd 가 떠 있어야 함
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


class DeviceError(Exception):
    pass


@dataclass
class DeviceInfo:
    udid: str
    name: str
    ios_version: str
    product_type: str
    connection: str
    developer_mode: Optional[bool] = None

    def label(self) -> str:
        return f"{self.name} (iOS {self.ios_version}, {self.product_type}, {self.connection}) UDID={self.udid}"


def _import_pmd3():
    try:
        from pymobiledevice3 import usbmux  # noqa: F401
        from pymobiledevice3.lockdown import create_using_usbmux  # noqa: F401
        from pymobiledevice3.services.installation_proxy import InstallationProxyService  # noqa: F401
    except ImportError as e:  # pragma: no cover
        raise DeviceError(
            "pymobiledevice3 가 설치되어 있지 않습니다. `pip install -r requirements.txt` 를 실행하세요."
        ) from e
    from pymobiledevice3 import usbmux
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.installation_proxy import InstallationProxyService
    return usbmux, create_using_usbmux, InstallationProxyService


def _translate(e: Exception) -> DeviceError:
    name = type(e).__name__
    msg = str(e)
    hints = {
        "NoDeviceConnectedError": "아이폰이 연결되어 있지 않습니다. USB 케이블을 연결하고 잠금을 해제하세요.",
        "DeviceNotFoundError": "지정한 UDID 의 기기를 찾지 못했습니다. `devices` 명령으로 확인하세요.",
        "NotTrustedError": "아이폰에서 '이 컴퓨터를 신뢰하시겠습니까?' 를 '신뢰'로 누른 뒤 다시 시도하세요.",
        "PairingError": "페어링에 실패했습니다. 아이폰 잠금을 풀고 '신뢰'를 누르세요.",
        "NotPairedError": "페어링이 되어 있지 않습니다. 아이폰 잠금을 풀고 '신뢰'를 누르세요.",
        "PasswordRequiredError": "아이폰 잠금을 해제한 뒤 다시 시도하세요.",
        "MuxException": "usbmuxd 에 연결할 수 없습니다. (Windows: iTunes 설치 / Linux: usbmuxd 실행 / macOS: 케이블 확인)",
        "ConnectionFailedToUsbmuxdError": "usbmuxd 에 연결할 수 없습니다. (Windows: iTunes 설치 / Linux: sudo apt install usbmuxd 후 재연결 / macOS: 케이블 확인)",
        "ConnectionFailedError": "usbmuxd 에 연결할 수 없습니다. (Windows: iTunes 설치 / Linux: usbmuxd 실행)",
        "AppInstallError": "앱 설치 실패: " + msg,
    }
    hint = hints.get(name)
    if hint:
        return DeviceError(f"{hint}\n  (원인: {name}: {msg})")
    return DeviceError(f"{name}: {msg}")


def _run(coro):
    try:
        return asyncio.run(coro)
    except DeviceError:
        raise
    except Exception as e:  # noqa: BLE001
        raise _translate(e) from e


# ---------- 조회 ----------

async def _list_devices_async(with_details: bool = True) -> list[DeviceInfo]:
    usbmux, create_using_usbmux, _ = _import_pmd3()
    muxes = await usbmux.list_devices()
    out: list[DeviceInfo] = []
    seen: set[str] = set()
    for m in muxes:
        if m.serial in seen:
            continue
        seen.add(m.serial)
        info = DeviceInfo(udid=m.serial, name="?", ios_version="?", product_type="?", connection=m.connection_type)
        if with_details:
            try:
                ld = await create_using_usbmux(serial=m.serial, connection_type=m.connection_type, autopair=True)
                try:
                    v = ld.all_values
                    info.name = v.get("DeviceName", "?")
                    info.ios_version = v.get("ProductVersion", "?")
                    info.product_type = v.get("ProductType", "?")
                    info.udid = v.get("UniqueDeviceID", m.serial)
                    try:
                        info.developer_mode = await ld.get_developer_mode_status()
                    except Exception:  # noqa: BLE001
                        info.developer_mode = None
                finally:
                    await ld.close()
            except Exception as e:  # noqa: BLE001
                info.name = f"(정보 조회 실패: {type(e).__name__})"
        out.append(info)
    return out


def list_devices(with_details: bool = True) -> list[DeviceInfo]:
    return _run(_list_devices_async(with_details))


def pick_device(udid: Optional[str] = None) -> DeviceInfo:
    devs = list_devices()
    if not devs:
        raise DeviceError("연결된 아이폰이 없습니다. USB 케이블 연결·잠금 해제·'신뢰' 확인 후 다시 시도하세요.")
    if udid:
        for d in devs:
            if d.udid.replace("-", "").lower() == udid.replace("-", "").lower():
                return d
        raise DeviceError(f"UDID {udid} 기기를 찾지 못했습니다. 연결된 기기: " + ", ".join(d.udid for d in devs))
    usb = [d for d in devs if d.connection == "USB"]
    return (usb or devs)[0]


# ---------- 설치·삭제·목록 ----------

async def _with_lockdown(udid: Optional[str]):
    _, create_using_usbmux, _ = _import_pmd3()
    return await create_using_usbmux(serial=udid, autopair=True)


async def _install_async(ipa: Path, udid: Optional[str], progress: Optional[Callable[[int], None]], upgrade: bool) -> None:
    _, _, InstallationProxyService = _import_pmd3()
    ld = await _with_lockdown(udid)
    try:
        async with InstallationProxyService(lockdown=ld) as ip:
            handler = (lambda pct, *_a: progress(int(pct))) if progress else None
            if upgrade:
                await ip.upgrade(str(ipa), handler=handler)
            else:
                await ip.install_from_local(Path(ipa), handler=handler)
    finally:
        await ld.close()


def install_ipa(ipa: Path | str, udid: Optional[str] = None, progress: Optional[Callable[[int], None]] = None,
                upgrade: bool = True) -> None:
    """IPA 를 기기에 설치한다. upgrade=True 면 같은 번들 ID 앱의 데이터를 유지한 채 덮어쓴다."""
    ipa = Path(ipa)
    if not ipa.exists():
        raise DeviceError(f"IPA 파일이 없습니다: {ipa}")
    _run(_install_async(ipa, udid, progress, upgrade))


async def _uninstall_async(bundle_id: str, udid: Optional[str]) -> None:
    _, _, InstallationProxyService = _import_pmd3()
    ld = await _with_lockdown(udid)
    try:
        async with InstallationProxyService(lockdown=ld) as ip:
            await ip.uninstall(bundle_id)
    finally:
        await ld.close()


def uninstall_app(bundle_id: str, udid: Optional[str] = None) -> None:
    _run(_uninstall_async(bundle_id, udid))


async def _apps_async(udid: Optional[str], app_type: str) -> dict[str, dict[str, Any]]:
    _, _, InstallationProxyService = _import_pmd3()
    ld = await _with_lockdown(udid)
    try:
        async with InstallationProxyService(lockdown=ld) as ip:
            return await ip.get_apps(application_type=app_type)
    finally:
        await ld.close()


def list_apps(udid: Optional[str] = None, user_only: bool = True) -> list[dict[str, Any]]:
    apps = _run(_apps_async(udid, "User" if user_only else "Any"))
    rows = []
    for bid, a in sorted(apps.items()):
        rows.append({
            "bundle_id": bid,
            "name": a.get("CFBundleDisplayName") or a.get("CFBundleName") or "?",
            "version": a.get("CFBundleShortVersionString", "?"),
            "signer": a.get("SignerIdentity", ""),
            "type": a.get("ApplicationType", ""),
        })
    return rows


# ---------- 개발자 모드 ----------

async def _dev_mode_async(udid: Optional[str], enable: bool) -> Optional[bool]:
    ld = await _with_lockdown(udid)
    try:
        if not enable:
            return await ld.get_developer_mode_status()
        from pymobiledevice3.services.amfi import AmfiService
        await AmfiService(ld).enable_developer_mode()
        return True
    finally:
        await ld.close()


def developer_mode_status(udid: Optional[str] = None) -> Optional[bool]:
    return _run(_dev_mode_async(udid, enable=False))


def enable_developer_mode(udid: Optional[str] = None) -> None:
    """iOS 16+ 에서 개발용 서명 앱을 실행하려면 개발자 모드가 필요하다. 기기가 재시동될 수 있다."""
    _run(_dev_mode_async(udid, enable=True))
