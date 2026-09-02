"""OTA(무선) 설치 페이지 생성.

Safari 에서 itms-services:// 링크를 열면 iOS 가 manifest.plist 를 읽어 IPA 를 설치한다.
주의: iOS 는 manifest 와 IPA 를 반드시 **정상 인증서의 HTTPS** 로 받아야 한다.
로컬 http 서버는 테스트용이며, 실제로는 HTTPS 호스팅(또는 cloudflared/ngrok 같은 터널)이 필요하다.
"""

from __future__ import annotations

import http.server
import plistlib
import shutil
import ssl
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from .ipa import inspect_ipa


def build_manifest(bundle_id: str, version: str, title: str, ipa_url: str,
                   icon_url: Optional[str] = None) -> bytes:
    assets = [{"kind": "software-package", "url": ipa_url}]
    if icon_url:
        assets.append({"kind": "display-image", "url": icon_url})
        assets.append({"kind": "full-size-image", "url": icon_url})
    manifest = {
        "items": [{
            "assets": assets,
            "metadata": {
                "bundle-identifier": bundle_id,
                "bundle-version": version,
                "kind": "software",
                "title": title,
            },
        }]
    }
    return plistlib.dumps(manifest)


def itms_link(manifest_url: str) -> str:
    return "itms-services://?action=download-manifest&url=" + quote(manifest_url, safe="")


def build_index_html(title: str, bundle_id: str, version: str, manifest_url: str) -> str:
    link = itms_link(manifest_url)
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} 설치</title>
<style>
body{{font-family:-apple-system,system-ui,sans-serif;margin:0;padding:32px 20px;background:#f5f5f7;color:#1d1d1f}}
.card{{max-width:420px;margin:0 auto;background:#fff;border-radius:16px;padding:28px;box-shadow:0 2px 12px rgba(0,0,0,.08)}}
h1{{font-size:22px;margin:0 0 6px}} p{{margin:6px 0;color:#6e6e73;font-size:14px}}
a.btn{{display:block;text-align:center;margin-top:20px;padding:14px;border-radius:12px;background:#0071e3;color:#fff;text-decoration:none;font-weight:600}}
ol{{font-size:13px;color:#6e6e73;padding-left:18px}}
</style></head><body><div class="card">
<h1>{title}</h1>
<p>{bundle_id}</p><p>버전 {version}</p>
<a class="btn" href="{link}">아이폰에 설치</a>
<ol>
<li>이 페이지를 아이폰 Safari 에서 여세요.</li>
<li>설치 버튼을 누르고 "설치"를 허용하세요.</li>
<li>홈 화면에 앱이 나타날 때까지 기다리세요.</li>
<li>실행이 막히면 설정 → 일반 → VPN 및 기기 관리에서 개발자 앱을 신뢰하세요.</li>
</ol></div></body></html>
"""


def write_ota_site(ipa: Path | str, out_dir: Path | str, base_url: str,
                   title: Optional[str] = None, icon_path: Optional[Path] = None) -> dict[str, str]:
    """out_dir 에 ipa, manifest.plist, index.html 을 만든다. base_url 은 out_dir 이 공개될 https URL."""
    ipa = Path(ipa)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    info = inspect_ipa(ipa)
    base = base_url.rstrip("/") + "/"
    ipa_name = "app.ipa"
    shutil.copy(ipa, out / ipa_name)
    icon_url = None
    if icon_path and Path(icon_path).exists():
        shutil.copy(icon_path, out / "icon.png")
        icon_url = base + "icon.png"
    title = title or info.name
    manifest_url = base + "manifest.plist"
    (out / "manifest.plist").write_bytes(build_manifest(info.bundle_id, info.version, title, base + ipa_name, icon_url))
    (out / "index.html").write_text(build_index_html(title, info.bundle_id, info.version, manifest_url), encoding="utf-8")
    return {
        "dir": str(out),
        "index_url": base + "index.html",
        "manifest_url": manifest_url,
        "itms_link": itms_link(manifest_url),
    }


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".plist": "application/xml", ".ipa": "application/octet-stream"}

    def log_message(self, fmt, *args):  # noqa: D401
        print("[http] " + fmt % args)


def serve(directory: Path | str, port: int = 8080, certfile: Optional[str] = None, keyfile: Optional[str] = None) -> None:
    """디렉터리를 정적으로 서비스한다. certfile/keyfile 을 주면 HTTPS."""
    directory = str(directory)

    def handler(*a, **kw):
        return _QuietHandler(*a, directory=directory, **kw)

    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
    scheme = "http"
    if certfile:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile, keyfile)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    print(f"{scheme}://0.0.0.0:{port}/ 에서 서비스 중 (Ctrl+C 로 종료)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
