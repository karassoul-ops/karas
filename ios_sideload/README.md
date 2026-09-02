# ios_sideload — 아이폰에 App Store 밖 앱을 설치하고 계속 쓰게 해 주는 도구

IPA 파일을 **내 서명 인증서로 다시 서명**하고, USB로 연결한 아이폰에 **설치**하며,
서명 만료 전에 **자동으로 다시 서명·설치**해 앱이 끊기지 않게 유지합니다.
macOS · Windows · Linux 에서 같은 명령으로 동작하고, 탈옥은 필요 없습니다.

```
python sideload.py            # 한국어 메뉴
python sideload.py deploy 앱.ipa   # 서명 + 설치 + 자동 갱신 등록
```

---

## 1. 먼저 알아야 할 것 — "항상 쓸 수 있게" 하려면

iOS 는 Apple 이 발급한 인증서로 서명된 앱만 실행합니다. 이 도구는 그 인증서를 **본인 Apple 계정**에서 받아 서명합니다.
어떤 계정을 쓰느냐에 따라 유효 기간이 다릅니다.

| 방식 | 비용 | 앱 유효 기간 | 기기 수 | 이 도구에서 |
|---|---|---|---|---|
| **A. 무료 Apple ID** (Xcode 로 인증서·프로파일 생성) | 무료 | **7일** | 3대 | 7일마다 새 프로파일을 받아 `refresh` |
| **B. Apple Developer Program** (유료, 연 $99) | 유료 | **1년** | 100대 | API 키만 등록하면 발급·서명·갱신 **전부 자동** |

"항상 이용" 이 목표라면 **B 방식**을 권합니다. 유료 계정의 App Store Connect API 키를 한 번 등록하면
번들 ID 등록 → 기기 등록 → 인증서 발급 → 프로파일 발급 → 서명 → 설치까지 한 명령으로 끝나고,
1년마다 `refresh` 한 번이면 됩니다. 무료 계정은 Apple 정책상 7일 제한을 이 도구로도 풀 수 없습니다.

또 iOS 16 이상에서 개발용 서명 앱을 실행하려면 아이폰의 **개발자 모드**가 켜져 있어야 합니다
(`python sideload.py dev-mode enable` 또는 설정 → 개인정보 보호 및 보안 → 개발자 모드).

> 본인 소유 기기에 본인 계정으로 서명해 설치하는 것은 Apple 이 개발자에게 공식 허용하는 절차입니다.
> 유료 앱 무단 배포·DRM 우회 등 저작권을 침해하는 사용은 하지 마세요.

---

## 2. 설치

```bash
cd ios_sideload
python -m pip install -r requirements.txt
python sideload.py tools install      # 서명 도구 zsign 내려받기 (실패하면 아래 수동 설치)
python sideload.py doctor             # 환경 점검
```

OS 별 준비:

- **macOS**: 추가 준비 없음. (zsign 은 `brew install zsign` 또는 소스 빌드)
- **Windows**: iTunes(Apple Mobile Device Support) 설치 필요. zsign.exe 를 `%USERPROFILE%\.ios_sideload\tools\` 에 두면 됩니다.
- **Linux**: `sudo apt install usbmuxd libssl-dev` 후 아이폰 연결. zsign 은 소스 빌드:
  ```bash
  git clone https://github.com/zhlynn/zsign.git && cd zsign/build/linux && make
  mkdir -p ~/.ios_sideload/tools && cp zsign ~/.ios_sideload/tools/
  ```

아이폰은 USB 로 연결하고 잠금을 푼 뒤 "이 컴퓨터를 신뢰하시겠습니까?" 에서 **신뢰**를 누르세요.

---

## 3. 설정 (한 번만)

```bash
python sideload.py setup
```

### B. 유료 계정 — App Store Connect API 키 (권장)

1. App Store Connect → 사용자 및 액세스 → **통합(Integrations)** → App Store Connect API → 키 생성
   (액세스 권한: **Admin** 또는 **App Manager** 이상)
2. `.p8` 파일 다운로드, **Key ID** 와 **Issuer ID** 를 메모
3. `setup` 에서 2번을 골라 세 값을 입력

이후 `deploy` 를 실행하면 필요한 인증서(.p12)와 프로파일이 `~/.ios_sideload/credentials/` 에 자동 저장됩니다.

### A. 무료 Apple ID — Xcode 로 직접 만들기 (Mac 필요)

1. Xcode → Settings → Accounts 에 Apple ID 추가
2. 빈 iOS 앱 프로젝트를 만들고 **Bundle Identifier** 를 설치할 앱 것으로 지정, Team 선택,
   "Automatically manage signing" 체크 → 아이폰을 연결해 한 번 실행(기기 등록·프로파일 생성)
3. 프로파일: `~/Library/MobileDevice/Provisioning Profiles/*.mobileprovision`
   (또는 `~/Library/Developer/Xcode/UserData/Provisioning Profiles/`)
4. 인증서: 키체인 접근 → 나의 인증서 → "Apple Development: …" 를 **개인 키 포함**해 `.p12` 로 내보내기
5. `setup` 에서 1번을 골라 두 파일 경로와 비밀번호 입력

7일이 지나면 2~3 단계를 다시 해서 새 프로파일을 받고 `setup` 으로 경로를 바꾼 뒤 `refresh --all` 을 실행합니다.

---

## 4. 사용

```bash
python sideload.py devices                 # 연결된 아이폰
python sideload.py inspect 앱.ipa          # 번들 ID·버전 확인
python sideload.py deploy 앱.ipa           # 서명 + 설치 + 갱신 목록 등록
python sideload.py list                    # 관리 중인 앱과 남은 일수
python sideload.py refresh                 # 만료 임박(기본 2일 전) 앱만 재서명·재설치
python sideload.py refresh --all           # 전부
python sideload.py watch                   # 6시간마다 refresh 를 반복하는 상주 모드
python sideload.py uninstall com.xxx.yyy   # 삭제
python sideload.py apps                    # 아이폰에 설치된 앱 목록
```

자주 쓰는 옵션 (`deploy` / `sign`):

| 옵션 | 설명 |
|---|---|
| `--bundle-id com.내이름.앱` | 번들 ID 변경. App Store 정식 앱과 나란히 설치하거나, 다른 팀이 선점한 번들 ID 를 피할 때 |
| `--name 이름` | 홈 화면 표시 이름 변경 |
| `--no-extensions` | 앱 확장(PlugIns) 제거 — 확장 때문에 서명·설치가 실패할 때 |
| `--no-watch` | 워치 앱 제거 |
| `--file-sharing` | 파일 앱에서 문서 폴더 접근 허용 |
| `--dylib x.dylib` | dylib 주입 |
| `--cert/--profile/--password` | 이번만 다른 서명 자료 사용 |

프로파일이 특정 App ID 하나만 허용하는데 IPA 번들 ID 가 다르면, 자동으로 프로파일의 App ID 로 바꿔 서명합니다.

### 자동 갱신을 항상 돌리기

`deploy` 로 설치한 앱은 `~/.ios_sideload/registry.json` 에 기록되고, `refresh` 가 남은 기간을 보고 갱신합니다.
같은 번들 ID 로 덮어쓰기(Upgrade) 설치하므로 **앱 데이터는 유지**됩니다. 아이폰이 연결된 상태여야 합니다.

- 간단히: `python sideload.py watch` 를 켜 두기
- macOS/Linux cron: `0 */6 * * * cd /경로/ios_sideload && python sideload.py refresh >> ~/.ios_sideload/refresh.log 2>&1`
- Windows 작업 스케줄러: 프로그램 `python`, 인수 `sideload.py refresh`, 시작 위치 `ios_sideload` 폴더, 6시간 반복

### OTA(무선) 설치 페이지

컴퓨터에 연결하지 않고 Safari 에서 설치하고 싶을 때:

```bash
python sideload.py sign 앱.ipa -o signed.ipa
python sideload.py ota signed.ipa --url https://내도메인/app/ --out ota_site
```

`ota_site/` 폴더(index.html, manifest.plist, app.ipa)를 **HTTPS** 로 호스팅하고 아이폰 Safari 로 index.html 을 여세요.
iOS 는 http 로는 OTA 설치를 허용하지 않습니다. 로컬 테스트는 `--serve --tls-cert cert.pem --tls-key key.pem`.

---

## 5. 문제 해결

| 증상 | 조치 |
|---|---|
| `연결된 아이폰이 없습니다` | 케이블·잠금 해제·"신뢰" 확인. Windows 는 iTunes, Linux 는 `usbmuxd` 필요 |
| `서명 도구(zsign)를 찾지 못했습니다` | `tools install` 또는 §2 수동 설치, `setup` 에서 경로 지정 |
| 설치는 됐는데 실행하면 "신뢰할 수 없는 개발자" / 바로 종료 | iOS 16+: `dev-mode enable` 로 개발자 모드 켜기. 설정 → 일반 → VPN 및 기기 관리에서 개발자 앱 신뢰 |
| `프로파일에 이 기기가 등록되어 있지 않습니다` | 무료 계정: Xcode 에 이 아이폰을 연결해 프로파일 재생성. 유료: ASC 키가 있으면 자동 등록됨 |
| `이 번들 ID 는 다른 팀이 이미 쓰고 있을 수 있습니다` | `--bundle-id com.내이름.앱이름` 으로 새 번들 ID 지정 |
| `AppInstallError ... ApplicationVerificationFailed` | 인증서·프로파일 짝이 안 맞거나 만료. `cert-info`, `profile-info` 로 확인 후 재발급 |
| 앱 확장 때문에 실패 | `--no-extensions` |
| 인증서 발급 한도 초과 | `asc certs` 로 확인, `asc revoke-cert <ID>` 로 안 쓰는 인증서 폐기 |

자세한 오류 스택은 `IOS_SIDELOAD_DEBUG=1` 로 볼 수 있습니다. 상태 폴더 위치는 `IOS_SIDELOAD_HOME` 으로 바꿀 수 있습니다.

---

## 6. 개발·테스트

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

테스트는 실제 기기와 서명 도구 없이 가짜 zsign·가짜 App Store Connect API 로 전체 흐름을 검증합니다.

구성: `sideload/config.py`(설정) · `ipa.py`(IPA 검사) · `profile.py`(프로파일 파싱) · `cert.py`(.p12) ·
`signer.py`(zsign/rcodesign) · `device.py`(pymobiledevice3) · `asc.py`(App Store Connect API) ·
`registry.py`(갱신 기록) · `ota.py` · `tools.py` · `doctor.py`, 진입점 `sideload.py`.
