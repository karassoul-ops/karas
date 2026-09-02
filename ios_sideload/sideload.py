#!/usr/bin/env python3
"""
ios_sideload — 아이폰 비(非) App Store 앱 설치 도구
====================================================

IPA 파일을 내 서명 인증서로 다시 서명하고, USB 로 연결한 아이폰에 설치하며,
서명 만료 전에 자동으로 다시 서명·설치해 "항상 쓸 수 있게" 유지한다.

빠른 시작:
  pip install -r requirements.txt
  python sideload.py setup            # 인증서·프로파일 또는 App Store Connect API 키 등록
  python sideload.py doctor           # 환경 점검
  python sideload.py deploy 앱.ipa    # 서명 + 설치 + 갱신 목록 등록
  python sideload.py refresh          # 만료 임박 앱 재서명·재설치
  python sideload.py watch            # 상주하며 자동 갱신

인자 없이 실행하면 한국어 메뉴가 뜬다.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sideload import __version__  # noqa: E402
from sideload.config import Config, app_home, signed_dir  # noqa: E402
from sideload.ipa import IpaError, IpaInfo, inspect_ipa  # noqa: E402
from sideload.profile import ProfileError, ProfileInfo, parse_profile  # noqa: E402
from sideload.cert import CertError, CertInfo, cert_in_profile, inspect_cert  # noqa: E402
from sideload.signer import SignError, SignRequest, require_signer  # noqa: E402
from sideload.registry import AppRecord, Registry, iso_now  # noqa: E402


class UserError(Exception):
    """사용자에게 그대로 보여줄 오류."""


# ---------------------------------------------------------------- 출력 도우미

def kv(rows: list[tuple[str, str]], indent: int = 2) -> None:
    w = max((len(k) for k, _ in rows), default=0)
    for k, v in rows:
        print(" " * indent + f"{k.ljust(w)} : {v}")


def note(msg: str) -> None:
    print(f"• {msg}")


def warn(msg: str) -> None:
    print(f"⚠ {msg}")


def ask(prompt: str, default: Optional[str] = None, secret: bool = False) -> Optional[str]:
    suffix = f" [{default}]" if default else ""
    try:
        v = (getpass.getpass if secret else input)(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise UserError("입력이 취소되었습니다.")
    return v or default


def ask_path(prompt: str, default: Optional[str] = None, must_exist: bool = True) -> Optional[str]:
    while True:
        v = ask(prompt, default)
        if not v:
            return None
        p = Path(v.strip().strip('"').strip("'")).expanduser()
        if must_exist and not p.exists():
            print(f"  파일이 없습니다: {p}")
            continue
        return str(p)


def yes(prompt: str, default: bool = True) -> bool:
    v = ask(prompt + (" (Y/n)" if default else " (y/N)"))
    if not v:
        return default
    return v.lower().startswith("y")


# ---------------------------------------------------------------- 서명 자료 결정

class SigningMaterial:
    def __init__(self, cert_path: Path, password: Optional[str], profile_path: Path):
        self.cert_path = cert_path
        self.password = password
        self.profile_path = profile_path
        self.profile: ProfileInfo = parse_profile(profile_path)
        self.cert: CertInfo = inspect_cert(cert_path, password)

    @property
    def fixed_bundle_id(self) -> Optional[str]:
        """프로파일이 단일 App ID 전용이면 그 번들 ID, 와일드카드면 None."""
        pat = self.profile.bundle_id_pattern
        return None if "*" in pat else pat

    def validate(self, bundle_id: str, udid: Optional[str], allow_rewrite: bool = False) -> list[str]:
        problems = []
        if self.profile.is_expired:
            problems.append(f"프로파일이 만료되었습니다 ({self.profile.expiration:%Y-%m-%d}).")
        if self.cert.is_expired:
            problems.append(f"인증서가 만료되었습니다 ({self.cert.not_after:%Y-%m-%d}).")
        if not cert_in_profile(self.cert, self.profile):
            problems.append("이 인증서는 프로파일에 포함된 인증서가 아닙니다. (같은 계정에서 발급한 짝을 쓰세요)")
        if not self.profile.matches_bundle_id(bundle_id) and not (allow_rewrite and self.fixed_bundle_id):
            problems.append(f"프로파일 App ID '{self.profile.application_identifier}' 가 번들 ID '{bundle_id}' 와 맞지 않습니다.")
        if udid and not self.profile.covers_device(udid):
            problems.append(f"프로파일에 이 기기(UDID {udid})가 등록되어 있지 않습니다.")
        return problems


def _asc_provision(cfg: Config, bundle_id: str, app_name: str, device, log=print) -> SigningMaterial:
    from sideload.asc import AscClient, AscError, provision
    client = AscClient.from_config(cfg)
    try:
        res = provision(client, bundle_id, device.udid, device.name, cfg.asc_profile_type, app_name,
                        p12_password=cfg.cert_password, log=log)
    except AscError as e:
        msg = str(e)
        if "not available" in msg.lower() or "already" in msg.lower() or "ENTITY_ERROR.ATTRIBUTE.INVALID" in msg:
            raise UserError(
                f"{msg}\n  이 번들 ID 는 다른 팀이 이미 쓰고 있을 수 있습니다. "
                f"`--bundle-id com.내이름.{bundle_id.rsplit('.', 1)[-1]}` 처럼 새 번들 ID 를 지정해 다시 시도하세요.") from e
        raise UserError(msg) from e
    return SigningMaterial(res.p12_path, cfg.cert_password, res.profile_path)


def resolve_material(cfg: Config, args, bundle_id: str, app_name: str, device, log=print) -> SigningMaterial:
    cert = getattr(args, "cert", None) or cfg.cert_path
    profile = getattr(args, "profile", None) or cfg.profile_path
    password = getattr(args, "password", None) if getattr(args, "password", None) is not None else cfg.cert_password
    explicit = bool(getattr(args, "cert", None) or getattr(args, "profile", None))

    if cert and profile:
        m = SigningMaterial(Path(cert).expanduser(), password, Path(profile).expanduser())
        # 단일 App ID 프로파일이고 사용자가 번들 ID 를 고정하지 않았으며 ASC 자동 발급도 없으면,
        # 프로파일의 App ID 로 번들 ID 를 바꿔 서명하는 것을 허용한다 (무료 계정의 흔한 경우).
        allow_rewrite = not getattr(args, "bundle_id", None) and not cfg.has_asc()
        problems = m.validate(bundle_id, device.udid if device else None, allow_rewrite=allow_rewrite)
        if not problems:
            return m
        if explicit or not cfg.has_asc():
            raise UserError("서명 자료 문제:\n  - " + "\n  - ".join(problems))
        log("저장된 프로파일을 쓸 수 없어 App Store Connect 에서 새로 발급합니다:")
        for p in problems:
            log("  - " + p)
    if cfg.has_asc():
        if device is None:
            raise UserError("ASC 자동 발급에는 기기 UDID 가 필요합니다. 아이폰을 연결하세요.")
        return _asc_provision(cfg, bundle_id, app_name, device, log)
    raise UserError(
        "서명 자료가 없습니다. `python sideload.py setup` 으로\n"
        "  (A) 인증서(.p12) + 프로비저닝 프로파일(.mobileprovision) 을 등록하거나\n"
        "  (B) 유료 개발자 계정의 App Store Connect API 키를 등록하세요.")


def effective_bundle_id(args, material: SigningMaterial, bundle_id: str, log=print) -> str:
    """프로파일이 단일 App ID 인데 IPA 번들 ID 와 다르면 그 App ID 로 바꿔 서명한다."""
    fixed = material.fixed_bundle_id
    if not getattr(args, "bundle_id", None) and fixed and fixed != bundle_id:
        log(f"프로파일 App ID 에 맞춰 번들 ID 를 {fixed} 로 바꿔 서명합니다.")
        return fixed
    return bundle_id


# ---------------------------------------------------------------- 핵심 동작

def do_sign(cfg: Config, args, info: IpaInfo, material: SigningMaterial, bundle_id: str,
            output: Optional[Path] = None, log=print) -> Path:
    signer = require_signer(cfg)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", bundle_id)
    output = output or (signed_dir() / f"{safe}-{info.version}-{stamp}.ipa")
    req = SignRequest(
        ipa=info.path, output=Path(output), cert_p12=material.cert_path, cert_password=material.password,
        profile=material.profile_path,
        bundle_id=bundle_id if bundle_id != info.bundle_id else None,
        bundle_name=getattr(args, "name", None),
        entitlements=Path(args.entitlements) if getattr(args, "entitlements", None) else None,
        remove_extensions=bool(getattr(args, "no_extensions", False)),
        remove_watch_app=bool(getattr(args, "no_watch", False)),
        enable_file_sharing=bool(getattr(args, "file_sharing", False)),
        dylibs=tuple(Path(d) for d in (getattr(args, "dylib", None) or [])),
    )
    log(f"서명 도구: {signer.name} ({signer.executable})")
    signer.sign(req, log)
    log(f"서명 완료: {output}")
    return Path(output)


def _progress_printer():
    last = {"p": -1}

    def cb(p: int):
        if p != last["p"] and (p % 10 == 0 or p >= 95):
            print(f"  설치 진행 {p}%")
            last["p"] = p
    return cb


def do_install(ipa: Path, udid: Optional[str], log=print) -> None:
    from sideload.device import install_ipa
    log(f"설치 중: {ipa.name}")
    install_ipa(ipa, udid, progress=_progress_printer(), upgrade=True)
    log("설치 완료")


def do_deploy(cfg: Config, args, ipa_path: Path, udid: Optional[str], log=print) -> AppRecord:
    from sideload.device import pick_device
    info = inspect_ipa(ipa_path)
    log(f"앱: {info.name} {info.version} ({info.bundle_id})")
    device = pick_device(udid)
    log(f"기기: {device.label()}")
    bundle_id = getattr(args, "bundle_id", None) or info.bundle_id
    material = resolve_material(cfg, args, bundle_id, info.name, device, log)
    bundle_id = effective_bundle_id(args, material, bundle_id, log)
    kv(material.profile.summary()[1:4] + material.cert.summary()[5:6])
    signed = do_sign(cfg, args, info, material, bundle_id, log=log)
    do_install(signed, device.udid, log)

    rec = AppRecord(
        bundle_id=bundle_id, name=info.name, source_ipa=str(info.path.resolve()), signed_ipa=str(signed),
        udid=device.udid, signed_at=iso_now(), profile_path=str(material.profile_path),
        profile_expires=material.profile.expiration.isoformat(), cert_path=str(material.cert_path),
        cert_expires=material.cert.not_after.isoformat(), original_bundle_id=info.bundle_id,
    )
    Registry().upsert(rec)
    if material.profile.profile_type == "development" and device.developer_mode is False:
        warn("이 기기는 개발자 모드가 꺼져 있어 앱이 실행되지 않습니다. `python sideload.py dev-mode enable` 을 실행하세요.")
    log(f"유효 기간: 약 {rec.days_left:.1f}일 남음 (프로파일 {material.profile.expiration:%Y-%m-%d}, 인증서 {material.cert.not_after:%Y-%m-%d})")
    return rec


def do_refresh(cfg: Config, args, force: bool = False, log=print) -> int:
    reg = Registry()
    records = reg.all() if force else reg.due(cfg.refresh_before_days)
    if not records:
        log("갱신할 앱이 없습니다." if not reg.all() else
            f"만료 {cfg.refresh_before_days}일 이내인 앱이 없습니다. (`refresh --all` 로 전부 갱신 가능)")
        return 0
    from sideload.device import DeviceError, list_devices
    connected = {d.udid.replace("-", "").lower() for d in list_devices(with_details=False)}
    done = 0
    for rec in records:
        log(f"\n== {rec.name} ({rec.bundle_id}) — {rec.days_left:.1f}일 남음")
        if rec.udid.replace("-", "").lower() not in connected:
            log("  기기가 연결되어 있지 않아 건너뜁니다.")
            continue
        src = Path(rec.source_ipa)
        if not src.exists():
            log(f"  원본 IPA 가 없어 건너뜁니다: {src}")
            continue
        ns = argparse.Namespace(bundle_id=rec.bundle_id, cert=None, profile=None, password=None,
                                name=None, entitlements=None, no_extensions=False, no_watch=False,
                                file_sharing=False, dylib=None)
        try:
            new = do_deploy(cfg, ns, src, rec.udid, log)
            if new.days_left <= rec.days_left + 0.01:
                warn("새 프로파일이 없어 유효 기간이 늘지 않았습니다. Xcode/개발자 사이트에서 새 프로파일을 받아 `setup` 으로 등록하세요.")
            done += 1
        except (UserError, DeviceError, SignError, CertError, ProfileError, IpaError) as e:
            warn(f"갱신 실패: {e}")
    return done


# ---------------------------------------------------------------- 명령

def cmd_setup(cfg: Config, args) -> None:
    print("== 설정 ==  (Enter 를 누르면 현재 값 유지)")
    print("서명 방식을 고르세요:")
    print("  1) 인증서(.p12) + 프로비저닝 프로파일(.mobileprovision) 직접 등록")
    print("     - 무료 Apple ID: Xcode 에서 만든 7일짜리 / 유료 계정: 개발자 사이트에서 1년짜리")
    print("  2) App Store Connect API 키(.p8) 등록 — 유료 계정, 인증서·프로파일 자동 발급(1년)")
    print("  3) 둘 다")
    mode = ask("선택", "3" if (cfg.has_asc() and cfg.has_signing_material()) else ("2" if cfg.has_asc() else "1"))
    if mode in ("1", "3"):
        cfg.cert_path = ask_path("서명 인증서 .p12 경로", cfg.cert_path) or cfg.cert_path
        pw = ask(".p12 비밀번호 (없으면 Enter)", None, secret=True)
        if pw is not None and pw != "":
            cfg.cert_password = pw
        elif cfg.cert_password is None:
            cfg.cert_password = None
        cfg.profile_path = ask_path("프로비저닝 프로파일 .mobileprovision 경로", cfg.profile_path) or cfg.profile_path
    if mode in ("2", "3"):
        cfg.asc_key_id = ask("ASC Key ID (예: 2X9R4HXF34)", cfg.asc_key_id)
        cfg.asc_issuer_id = ask("ASC Issuer ID (UUID)", cfg.asc_issuer_id)
        cfg.asc_key_path = ask_path("AuthKey_XXXX.p8 경로", cfg.asc_key_path) or cfg.asc_key_path
        t = ask("프로파일 유형 (1=개발용 IOS_APP_DEVELOPMENT, 2=애드혹 IOS_APP_ADHOC)",
                "1" if cfg.asc_profile_type == "IOS_APP_DEVELOPMENT" else "2")
        cfg.asc_profile_type = "IOS_APP_ADHOC" if t == "2" else "IOS_APP_DEVELOPMENT"
        if not cfg.cert_password:
            pw = ask("자동 발급할 .p12 에 걸 비밀번호 (없으면 Enter)", None, secret=True)
            cfg.cert_password = pw or None
    sp = ask_path("서명 도구(zsign) 경로 — 자동 탐색이면 Enter", cfg.signer_path)
    if sp:
        cfg.signer_path = sp
    d = ask("만료 며칠 전에 자동 갱신할지", str(cfg.refresh_before_days))
    try:
        cfg.refresh_before_days = max(0, int(d))
    except ValueError:
        pass
    path = cfg.save()
    print(f"\n저장됨: {path}")
    kv(cfg.describe())


def cmd_doctor(cfg: Config, args) -> None:
    from sideload.doctor import format_checks, run_checks
    print(f"ios_sideload {__version__} — 환경 점검 (홈: {app_home()})")
    print(format_checks(run_checks(cfg, probe_device=not args.no_device)))


def cmd_devices(cfg: Config, args) -> None:
    from sideload.device import list_devices
    devs = list_devices()
    if not devs:
        print("연결된 아이폰이 없습니다. USB 연결 → 잠금 해제 → '이 컴퓨터를 신뢰' 를 확인하세요.")
        return
    for d in devs:
        dm = "" if d.developer_mode is None else (" 개발자모드=ON" if d.developer_mode else " 개발자모드=OFF")
        print(f"  {d.label()}{dm}")


def cmd_apps(cfg: Config, args) -> None:
    from sideload.device import list_apps
    rows = list_apps(args.udid or cfg.default_udid, user_only=not args.all)
    for r in rows:
        print(f"  {r['bundle_id']:<45} {r['name']:<24} {r['version']:<10} {r['signer']}")
    print(f"  총 {len(rows)}개")


def cmd_inspect(cfg: Config, args) -> None:
    kv(inspect_ipa(args.ipa).summary())


def cmd_profile_info(cfg: Config, args) -> None:
    p = parse_profile(args.profile or cfg.profile_path or "")
    kv(p.summary())
    if p.devices and args.devices:
        print("  등록 기기:")
        for d in p.devices:
            print("    " + d)


def cmd_cert_info(cfg: Config, args) -> None:
    pw = args.password if args.password is not None else cfg.cert_password
    c = inspect_cert(args.cert or cfg.cert_path or "", pw)
    kv(c.summary())
    prof_path = args.profile or cfg.profile_path
    if prof_path:
        p = parse_profile(prof_path)
        print(f"  프로파일 포함 여부: {'예' if cert_in_profile(c, p) else '아니오'}")


def cmd_sign(cfg: Config, args) -> None:
    info = inspect_ipa(args.ipa)
    bundle_id = args.bundle_id or info.bundle_id
    device = None
    if cfg.has_asc() and not (args.cert or cfg.cert_path):
        from sideload.device import pick_device
        device = pick_device(args.udid or cfg.default_udid)
    material = resolve_material(cfg, args, bundle_id, info.name, device)
    bundle_id = effective_bundle_id(args, material, bundle_id)
    out = Path(args.output) if args.output else None
    do_sign(cfg, args, info, material, bundle_id, out)


def cmd_install(cfg: Config, args) -> None:
    from sideload.device import pick_device
    d = pick_device(args.udid or cfg.default_udid)
    print(f"기기: {d.label()}")
    do_install(Path(args.ipa), d.udid)


def cmd_deploy(cfg: Config, args) -> None:
    do_deploy(cfg, args, Path(args.ipa), args.udid or cfg.default_udid)


def cmd_uninstall(cfg: Config, args) -> None:
    from sideload.device import pick_device, uninstall_app
    d = pick_device(args.udid or cfg.default_udid)
    uninstall_app(args.bundle_id, d.udid)
    Registry().remove(d.udid, args.bundle_id)
    print(f"삭제 완료: {args.bundle_id}")


def cmd_list(cfg: Config, args) -> None:
    recs = Registry().all()
    if not recs:
        print("등록된 앱이 없습니다. `deploy` 로 설치하면 자동 등록됩니다.")
        return
    for r in recs:
        flag = "갱신 필요" if r.needs_refresh(cfg.refresh_before_days) else ""
        print(f"  {r.name:<20} {r.bundle_id:<40} {r.days_left:6.1f}일 남음  {r.udid[:8]}…  {flag}")


def cmd_refresh(cfg: Config, args) -> None:
    n = do_refresh(cfg, args, force=args.all)
    print(f"\n갱신 완료: {n}개")


def cmd_watch(cfg: Config, args) -> None:
    interval = max(60, int(args.interval_minutes * 60))
    print(f"자동 갱신 감시 시작 — {args.interval_minutes}분마다 확인, 만료 {cfg.refresh_before_days}일 전 갱신 (Ctrl+C 로 종료)")
    while True:
        try:
            print(f"\n[{datetime.now():%Y-%m-%d %H:%M}] 점검")
            do_refresh(cfg, args, force=False)
        except Exception as e:  # noqa: BLE001
            warn(f"점검 중 오류: {e}")
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("종료")
            return


def cmd_ota(cfg: Config, args) -> None:
    from sideload.ota import serve, write_ota_site
    res = write_ota_site(args.ipa, args.out, args.url, title=args.title, icon_path=Path(args.icon) if args.icon else None)
    print("OTA 페이지 생성:")
    kv([("폴더", res["dir"]), ("설치 페이지", res["index_url"]), ("manifest", res["manifest_url"]), ("itms 링크", res["itms_link"])])
    print("\n주의: iOS 는 https 로만 OTA 설치를 허용합니다. 이 폴더를 HTTPS 호스팅(또는 cloudflared/ngrok 터널)에 올리세요.")
    if args.serve:
        serve(res["dir"], args.port, args.tls_cert, args.tls_key)


def cmd_tools_install(cfg: Config, args) -> None:
    from sideload.tools import install_zsign
    p = install_zsign()
    if p:
        cfg.signer_path = str(p)
        cfg.save()


def cmd_dev_mode(cfg: Config, args) -> None:
    from sideload.device import developer_mode_status, enable_developer_mode
    udid = args.udid or cfg.default_udid
    if args.action == "status":
        st = developer_mode_status(udid)
        print("개발자 모드: " + ("켜짐" if st else "꺼짐" if st is False else "확인 불가(iOS 15 이하는 필요 없음)"))
    else:
        print("개발자 모드를 켭니다. 아이폰이 재시동되며, 재시동 후 화면에서 '켜기' 를 눌러야 합니다.")
        enable_developer_mode(udid)
        print("요청 완료. 아이폰에서 안내를 따르세요.")


def cmd_asc(cfg: Config, args) -> None:
    from sideload.asc import AscClient, AscError, provision
    try:
        client = AscClient.from_config(cfg)
        if args.asc_cmd == "devices":
            for d in client.list_devices():
                a = d["attributes"]
                print(f"  {a.get('name'):<24} {a.get('udid')}  {a.get('status')}  [{d['id']}]")
        elif args.asc_cmd == "certs":
            for c in client.get_all("/certificates"):
                a = c["attributes"]
                print(f"  {a.get('certificateType'):<20} {a.get('name'):<30} 만료 {str(a.get('expirationDate'))[:10]}  [{c['id']}]")
        elif args.asc_cmd == "revoke-cert":
            client.revoke_certificate(args.cert_id)
            print("폐기 완료")
        elif args.asc_cmd == "provision":
            from sideload.device import pick_device
            d = pick_device(args.udid or cfg.default_udid)
            res = provision(client, args.bundle_id, d.udid, d.name, cfg.asc_profile_type,
                            p12_password=cfg.cert_password)
            cfg.cert_path, cfg.profile_path = str(res.p12_path), str(res.profile_path)
            cfg.save()
            print("발급 완료 — 설정에 인증서·프로파일 경로를 저장했습니다.")
            kv(parse_profile(res.profile_path).summary())
    except AscError as e:
        raise UserError(str(e)) from e


# ---------------------------------------------------------------- 대화형 메뉴

def interactive(cfg: Config) -> None:
    print(f"ios_sideload {__version__} — 아이폰 앱 설치 도구")
    while True:
        print("""
  1) 앱 설치 (서명 + 설치 + 갱신 등록)
  2) 만료 임박 앱 갱신
  3) 설치 기록 보기
  4) 연결된 아이폰 보기
  5) 환경 점검(doctor)
  6) 설정(setup)
  7) 서명 도구(zsign) 설치
  8) 개발자 모드 켜기
  9) 상주 자동 갱신(watch)
  0) 종료""")
        c = ask("선택")
        try:
            if c == "1":
                ipa = ask_path("IPA 파일 경로")
                if ipa:
                    ns = argparse.Namespace(bundle_id=None, cert=None, profile=None, password=None, name=None,
                                            entitlements=None, no_extensions=False, no_watch=False,
                                            file_sharing=False, dylib=None)
                    do_deploy(cfg, ns, Path(ipa), cfg.default_udid)
            elif c == "2":
                do_refresh(cfg, argparse.Namespace(), force=yes("만료와 관계없이 전부 갱신할까요?", False))
            elif c == "3":
                cmd_list(cfg, None)
            elif c == "4":
                cmd_devices(cfg, None)
            elif c == "5":
                cmd_doctor(cfg, argparse.Namespace(no_device=False))
            elif c == "6":
                cmd_setup(cfg, None)
            elif c == "7":
                cmd_tools_install(cfg, None)
            elif c == "8":
                cmd_dev_mode(cfg, argparse.Namespace(action="enable", udid=None))
            elif c == "9":
                cmd_watch(cfg, argparse.Namespace(interval_minutes=360))
            elif c in ("0", "q", None):
                return
        except (UserError, IpaError, ProfileError, CertError, SignError) as e:
            print(f"\n오류: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"\n오류: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- 인자 파서

def _add_sign_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--cert", help=".p12 인증서 (설정값 대신)")
    p.add_argument("--password", help=".p12 비밀번호")
    p.add_argument("--profile", help=".mobileprovision (설정값 대신)")
    p.add_argument("--bundle-id", help="서명하면서 바꿀 번들 ID")
    p.add_argument("--name", help="서명하면서 바꿀 앱 표시 이름")
    p.add_argument("--entitlements", help="바꿔 넣을 entitlements plist")
    p.add_argument("--no-extensions", action="store_true", help="앱 확장(PlugIns) 제거 (프로파일 문제 회피)")
    p.add_argument("--no-watch", action="store_true", help="워치 앱 제거")
    p.add_argument("--file-sharing", action="store_true", help="파일 앱에서 문서 공유 허용")
    p.add_argument("--dylib", action="append", help="주입할 dylib (여러 번 지정 가능)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sideload.py", description="아이폰 비(非) App Store 앱 서명·설치·갱신 도구")
    p.add_argument("--version", action="version", version=f"ios_sideload {__version__}")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("setup", help="서명 자료·API 키 설정")
    d = sub.add_parser("doctor", help="환경 점검")
    d.add_argument("--no-device", action="store_true", help="기기 연결 확인 생략")
    sub.add_parser("devices", help="연결된 아이폰 목록")
    a = sub.add_parser("apps", help="아이폰에 설치된 앱 목록")
    a.add_argument("--udid")
    a.add_argument("--all", action="store_true", help="시스템 앱 포함")
    i = sub.add_parser("inspect", help="IPA 정보")
    i.add_argument("ipa")
    pi = sub.add_parser("profile-info", help="프로비저닝 프로파일 정보")
    pi.add_argument("profile", nargs="?")
    pi.add_argument("--devices", action="store_true", help="등록 기기 UDID 표시")
    ci = sub.add_parser("cert-info", help="서명 인증서 정보")
    ci.add_argument("cert", nargs="?")
    ci.add_argument("--password")
    ci.add_argument("--profile")

    s = sub.add_parser("sign", help="IPA 재서명만")
    s.add_argument("ipa")
    s.add_argument("-o", "--output")
    s.add_argument("--udid")
    _add_sign_opts(s)

    ins = sub.add_parser("install", help="(이미 서명된) IPA 설치만")
    ins.add_argument("ipa")
    ins.add_argument("--udid")

    dp = sub.add_parser("deploy", help="서명 + 설치 + 갱신 목록 등록")
    dp.add_argument("ipa")
    dp.add_argument("--udid")
    _add_sign_opts(dp)

    un = sub.add_parser("uninstall", help="앱 삭제")
    un.add_argument("bundle_id")
    un.add_argument("--udid")

    sub.add_parser("list", help="갱신 관리 중인 앱 목록")
    rf = sub.add_parser("refresh", help="만료 임박 앱 재서명·재설치")
    rf.add_argument("--all", action="store_true", help="만료와 관계없이 전부")
    w = sub.add_parser("watch", help="상주하며 주기적으로 refresh")
    w.add_argument("--interval-minutes", type=float, default=360)

    o = sub.add_parser("ota", help="Safari 로 설치하는 OTA 페이지 생성")
    o.add_argument("ipa")
    o.add_argument("--url", required=True, help="이 폴더가 공개될 https 주소 (예: https://example.com/app/)")
    o.add_argument("--out", default="ota_site")
    o.add_argument("--title")
    o.add_argument("--icon")
    o.add_argument("--serve", action="store_true", help="로컬 서버 실행")
    o.add_argument("--port", type=int, default=8080)
    o.add_argument("--tls-cert")
    o.add_argument("--tls-key")

    t = sub.add_parser("tools", help="보조 도구")
    ts = t.add_subparsers(dest="tools_cmd")
    ts.add_parser("install", help="zsign 내려받기")

    dm = sub.add_parser("dev-mode", help="개발자 모드 상태/켜기")
    dm.add_argument("action", choices=["status", "enable"])
    dm.add_argument("--udid")

    asc = sub.add_parser("asc", help="App Store Connect API (유료 계정)")
    ass = asc.add_subparsers(dest="asc_cmd")
    ass.add_parser("devices", help="등록 기기 목록")
    ass.add_parser("certs", help="인증서 목록")
    rc = ass.add_parser("revoke-cert", help="인증서 폐기")
    rc.add_argument("cert_id")
    pv = ass.add_parser("provision", help="번들 ID 로 인증서·프로파일 발급")
    pv.add_argument("bundle_id")
    pv.add_argument("--udid")
    return p


HANDLERS = {
    "setup": cmd_setup, "doctor": cmd_doctor, "devices": cmd_devices, "apps": cmd_apps, "inspect": cmd_inspect,
    "profile-info": cmd_profile_info, "cert-info": cmd_cert_info, "sign": cmd_sign, "install": cmd_install,
    "deploy": cmd_deploy, "uninstall": cmd_uninstall, "list": cmd_list, "refresh": cmd_refresh,
    "watch": cmd_watch, "ota": cmd_ota, "dev-mode": cmd_dev_mode, "asc": cmd_asc,
}


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = Config.load()
    try:
        if not args.cmd:
            interactive(cfg)
            return 0
        if args.cmd == "tools":
            if args.tools_cmd == "install":
                cmd_tools_install(cfg, args)
            else:
                parser.parse_args(["tools", "-h"])
            return 0
        if args.cmd == "asc" and not args.asc_cmd:
            parser.parse_args(["asc", "-h"])
            return 0
        HANDLERS[args.cmd](cfg, args)
        return 0
    except (UserError, IpaError, ProfileError, CertError, SignError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        from sideload.device import DeviceError
        if isinstance(e, DeviceError):
            print(f"기기 오류: {e}", file=sys.stderr)
            return 2
        if os.environ.get("IOS_SIDELOAD_DEBUG"):
            raise
        print(f"오류: {type(e).__name__}: {e}  (자세히 보려면 IOS_SIDELOAD_DEBUG=1)", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
