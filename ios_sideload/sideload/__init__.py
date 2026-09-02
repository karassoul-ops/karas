"""
ios_sideload — 아이폰에 App Store를 거치지 않은 앱(IPA)을 서명·설치·갱신하는 도구.

구성:
  config    설정 파일(~/.ios_sideload/config.json) 관리
  ipa       IPA 파일 검사(번들 ID, 이름, 버전)
  profile   .mobileprovision 프로비저닝 프로파일 파싱
  cert      .p12 서명 인증서 검사·생성
  signer    zsign / rcodesign 백엔드로 IPA 재서명
  device    pymobiledevice3로 아이폰 연결·설치·삭제
  asc       App Store Connect API로 인증서·기기·프로파일 자동 발급
  ota       Safari에서 설치하는 OTA(itms-services) 페이지 생성
  registry  설치한 앱 기록과 만료 전 자동 갱신
  tools     zsign 바이너리 설치 도우미
  doctor    실행 환경 점검
"""

__version__ = "1.0.0"
