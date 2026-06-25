"""
FM2026 산출물 신규·갱신 감지 및 주요산출물 기준 정밀 분석
============================================================
체크 주기마다 FM2026 프로젝트 이슈를 점검하여:
  1. 신규·갱신 산출물 감지 → 개별 파일 정밀 분석
  2. 세부사업별 등록 산출물 ↔ 주요산출물(제안요청서·제안서·기술협상서·WBS·
     과업대비표·산출물 정의서·업체별 업무분장) 기준 갭 분석
  3. 미등록·불완전 산출물에 대한 수정·개선·보완 요구 댓글 자동 등록

사용법:
  python fm2026_deliverable_watch.py                 # dry-run (미리보기)
  python fm2026_deliverable_watch.py --review        # 근거+미리보기
  python fm2026_deliverable_watch.py --post-comments # 실제 댓글 등록
  python fm2026_deliverable_watch.py --reset         # 상태파일 초기화
  python fm2026_deliverable_watch.py --gap-all       # 모든 이슈 갭 분석 (산출물 변동 없어도)
"""

from __future__ import annotations

import os
import re
import sys
import json
import argparse
import logging
from datetime import datetime, timezone, timedelta

import fm2026_pmo_auto as core

KST = core.KST
log = logging.getLogger("deliverable_watch")

STATE_FILE = ".fm2026_seen_attachments.json"

# 본인(감독관) Jira 계정 — 이 계정이 올린 첨부파일은 분석 대상에서 제외
# 업체(수행사)가 등록한 산출물만 분석함
PMO_ACCOUNT_IDS = {
    "712020:da41563e-fbd1-4031-81a7-86551f26eabc",  # Soul Karas (karassoul@gmail.com)
}
PMO_DISPLAY_NAMES = {"Soul Karas"}

# ═══════════════════════════════════════════════════════════════════════════════
# 1. 주요 산출물(기준 문서) 정의
# ═══════════════════════════════════════════════════════════════════════════════

KEY_REFERENCE_DOCS = {
    "제안요청서": {
        "desc": "발주처가 제시한 사업 요구사항·범위·조건 — 모든 산출물의 최상위 기준",
        "keywords": ["제안요청서", "rfp"],
    },
    "제안서": {
        "desc": "수행사가 제안한 사업 수행 방안·기술·비용 — 과업 범위의 근거",
        "keywords": ["제안서"],
    },
    "기술협상서": {
        "desc": "계약 후 확정된 기술 사항·범위·품질 기준 — 계약상 의무 사항",
        "keywords": ["기술협상서", "기술협상"],
    },
    "사업수행계획서": {
        "desc": "전체 사업 추진 계획·일정·조직·품질 계획 — 사업 운영의 기준 문서",
        "keywords": ["사업수행계획서", "수행계획서"],
    },
    "WBS": {
        "desc": "전체 사업 일정 및 작업 분류 체계 — Jira Due Date와 동기화 필수",
        "keywords": ["wbs", "작업분류"],
    },
    "과업대비표": {
        "desc": "제안서 과업 항목 대비 수행 현황 추적 — 누락 과업 없는지 상시 점검",
        "keywords": ["과업대비표", "과업 대비"],
    },
    "산출물 정의서": {
        "desc": "단계별 산출물 목록·책임자·납기 정의 — 본 이슈 산출물의 적정성 기준",
        "keywords": ["산출물 정의서", "산출물정의서"],
    },
    "업체별 업무분장": {
        "desc": "참여 업체별 역할·책임·담당 과업 분장 — 등록 주체 적정성 검증",
        "keywords": ["업무분장", "업체별 업무", "역할분담", "업무 분장"],
    },
}

# ═══════════════════════════════════════════════════════════════════════════════
# 2. 산출물 유형 분류 규칙 (파일명 키워드 → 유형)
# ═══════════════════════════════════════════════════════════════════════════════

DELIVERABLE_RULES = [
    ("면담계획서",    "요구사항/면담",  ["면담 대상·일정 발주처 협의 완료 여부", "면담 항목 ↔ RFP 요구사항 매핑"]),
    ("면담결과서",    "요구사항/면담",  ["면담 결과 → 요구사항정의서 연계 여부", "증빙화면·회의록 첨부 여부"]),
    ("요구사항정의서", "요구사항",      ["기능/비기능 요구사항 분리 여부", "RFP 요구사항 추적표(RTM) 연계"]),
    ("요구사항추적",  "요구사항",      ["요구사항 ID 체계 일관성", "설계·테스트 단계 추적 가능 여부"]),
    ("화면설계",     "설계",          ["화면 ID 표준(SCR-모듈-NNN) 준수", "농정원 UI/UX 가이드 반영"]),
    ("화면목록",     "설계",          ["화면 ID 부여 여부", "메뉴 구조 반영"]),
    ("프로그램설계",  "설계",          ["모듈 분할 적정성", "인터페이스 정의 명확성"]),
    ("DB설계",       "설계",          ["테이블·컬럼 표준 준수", "팜맵 공간데이터 스키마 정합성"]),
    ("ERD",          "설계",          ["엔티티 관계 정규화", "공간컬럼(geometry) 정의"]),
    ("인터페이스설계", "설계",         ["API 엔드포인트 명세", "Request/Response 스펙 명확성"]),
    ("테스트",       "테스트",         ["테스트케이스 커버리지", "단위/통합/시스템 테스트 구분"]),
    ("검수",         "테스트",         ["검수 기준 명시", "결함 목록 및 조치 결과"]),
    ("이행계획",     "이행",           ["이행 일정 및 절차 명확성", "데이터 이관 방법"]),
    ("이행결과",     "이행",           ["이행 완료 확인 방법", "운영 환경 검증"]),
    ("운영매뉴얼",   "이행",           ["운영 절차 상세화", "장애 대응 절차 포함"]),
    ("사용자매뉴얼", "이행",           ["사용자 관점 설명", "화면 캡처 포함 여부"]),
    ("WBS",          "관리/계획",      ["일정 ↔ Jira Due Date 동기화", "마일스톤 누락 여부"]),
    ("과업대비표",    "관리/계획",      ["제안서 과업 대비 누락 항목", "변경 과업 반영 여부"]),
    ("사업수행계획서", "관리/계획",     ["추진 일정·조직·산출물 최신화", "팜맵 추진 방향 일치"]),
    ("산출물 정의서", "관리/계획",     ["전체 산출물 목록 완전성", "단계별 산출물 매핑"]),
    ("업무분장",     "관리/계획",      ["업체별 역할·책임 명확성", "담당 과업 누락 여부"]),
    ("품질계획",     "품질",           ["품질 지표 수치 목표 명시", "검토·승인 절차 정의"]),
    ("품질검사",     "품질",           ["검사 기준 명확성", "결함 유형·건수 기록"]),
    ("운영계획",     "운영",           ["운영 조직·절차 정의", "장애대응·백업 계획"]),
    ("현황분석",     "분석",           ["As-Is 현황 정확성", "To-Be 개선 방향 연계"]),
    ("회의록",       "공통",           ["의사결정 사항 명시", "액션아이템·담당자·기한"]),
    ("보고서",       "공통",           ["보고 목적·결론 명확성", "근거 데이터 첨부"]),
    ("주간보고",     "공통",           ["주간 실적·계획 비교", "리스크 현황 포함"]),
]


def classify_deliverable(filename: str) -> tuple[str, list[str]]:
    name = filename.replace(" ", "").lower()
    for keyword, dtype, perspectives in DELIVERABLE_RULES:
        if keyword.replace(" ", "").lower() in name:
            return dtype, perspectives
    return "일반 산출물", ["문서 목적·범위 명확성", "관련 산출물과의 정합성"]


# ═══════════════════════════════════════════════════════════════════════════════
# 3. 세부사업 유형 × 단계별 필수 산출물 매트릭스
#    (주요산출물 제안요청서·제안서·기술협상서·WBS·과업대비표·산출물 정의서·업무분장 기준)
# ═══════════════════════════════════════════════════════════════════════════════

# (필수산출물명, RFP/기술협상서 근거, 중요도: "높음"/"보통"/"낮음")
REQUIRED_MATRIX: dict[str, dict[str, list[tuple[str, str, str]]]] = {
    "갱신": {
        "분석": [
            ("요구사항정의서",    "제안요청서 §3·기술협상서 §2: 갱신 대상 필지·지목 변경 요구사항 필수 정의", "높음"),
            ("현황분석서",       "사업수행계획서: 기존 팜맵 데이터 품질·갱신 범위 현황 분석 선행 필요", "보통"),
            ("면담계획서",       "제안요청서 §4: 지자체·농정원 이해관계자 면담을 통한 갱신 요건 도출", "보통"),
        ],
        "설계": [
            ("DB설계서",         "팜맵 DB 표준: WGS84/TM 좌표계·Geometry 타입 명시 필수", "높음"),
            ("ERD",              "팜맵 DB 표준: 공간 테이블 엔티티 관계 정의 (Polygon/MultiPolygon)", "높음"),
            ("갱신작업계획서",   "과업대비표: 갱신 순서·방법·품질검수 기준 상세 계획", "높음"),
        ],
        "구현": [
            ("갱신결과물",       "기술협상서 §3: 실제 갱신된 팜맵 데이터(레이어·파일) 납품", "높음"),
            ("품질검사결과서",   "제안요청서 품질 기준: 위치 오차 ≤1m 검증 결과 첨부 필수", "높음"),
        ],
        "테스트": [
            ("공간정확도검증결과서", "제안요청서 품질 기준: 갱신 데이터 기하 정확도 검증 보고", "높음"),
            ("검수결과서",       "기술협상서 §5: 발주처(농정원) 검수 결과 및 승인 서명 필수", "높음"),
        ],
        "이행": [
            ("이행계획서",       "산출물 정의서: DB 적재·서비스 반영 이행 절차 계획서 납품", "높음"),
            ("이행결과서",       "산출물 정의서: 이행 완료 후 결과 보고서 납품", "높음"),
        ],
    },
    "시스템개선": {
        "분석": [
            ("요구사항정의서",   "제안요청서 §3·기술협상서 §2.1: FR-NNN/NFR-NNN 체계로 기능/비기능 분리 정의", "높음"),
            ("면담계획서",       "기술협상서 §2.1: 착수 후 2주 이내 이해관계자 면담 계획 수립", "보통"),
            ("면담결과서",       "산출물 정의서: 면담 결과 → 요구사항정의서 연계 추적 필수", "보통"),
            ("현황분석서",       "제안서: As-Is 시스템 현황 분석 후 To-Be 목표 도출", "보통"),
            ("화면목록",         "기술협상서 §4.1: 개발 대상 화면 목록 및 메뉴 구조 정의", "보통"),
        ],
        "설계": [
            ("화면설계서",       "기술협상서 §4.1: SCR-[모듈]-NNN 체계 화면 ID + 농정원 UI/UX 가이드 준수", "높음"),
            ("프로그램설계서",   "기술협상서 §4: 모듈·클래스 설계 및 인터페이스 정의", "높음"),
            ("DB설계서",         "기술협상서 §4.3: 테이블 설계·인덱스·제약조건·팜맵 공간스키마", "높음"),
            ("ERD",              "기술협상서 §4.3: 엔티티 관계 다이어그램 (공간 컬럼 포함)", "높음"),
            ("인터페이스설계서", "제안요청서 §5: 외부 시스템 연계 API 엔드포인트·Request/Response 명세", "보통"),
        ],
        "구현": [
            ("단위테스트결과서", "품질계획서: 모듈별 단위 테스트 수행 및 결함 목록 제출", "보통"),
            ("소스코드목록",     "과업대비표: 개발 완료 프로그램 목록 (모듈명·파일명·담당자)", "낮음"),
        ],
        "테스트": [
            ("통합테스트결과서", "품질계획서: 모듈 간 연계 기능 통합 테스트 결과 납품", "높음"),
            ("시스템테스트결과서", "기술협상서 §5: 전체 시스템 기능·성능 테스트 결과", "높음"),
            ("사용자인수테스트결과서", "기술협상서 §5: 발주처 인수 테스트 및 최종 승인", "높음"),
        ],
        "이행": [
            ("이행계획서",       "산출물 정의서: 운영 환경 이행·데이터 이관 계획서", "높음"),
            ("이행결과서",       "산출물 정의서: 이행 완료 결과 보고서", "높음"),
            ("운영자매뉴얼",     "제안요청서: 시스템 운영 가이드 (장애 대응·배치 운영 포함)", "보통"),
            ("사용자매뉴얼",     "제안요청서: 사용자 조작 가이드 (화면 캡처 포함)", "보통"),
        ],
    },
    "활용서비스": {
        "분석": [
            ("요구사항정의서",   "제안요청서: 활용서비스 기능·연계·UI 요구사항 정의", "높음"),
            ("현황분석서",       "사업수행계획서: 현재 서비스 현황 및 개선 방향 분석", "보통"),
        ],
        "설계": [
            ("서비스설계서",     "기술협상서: 서비스 구성·API·데이터 흐름 설계", "높음"),
            ("화면설계서",       "기술협상서: 서비스 화면 설계 (농정원 UI 가이드 준수)", "보통"),
        ],
        "구현": [
            ("구현결과서",       "과업대비표: 서비스 구현 완료 결과 및 기능 목록", "보통"),
        ],
        "테스트": [
            ("테스트결과서",     "품질계획서: 서비스 기능·성능·사용성 테스트 결과", "높음"),
        ],
        "이행": [
            ("운영계획서",       "산출물 정의서: 서비스 운영·유지관리 계획", "높음"),
        ],
    },
    "운영": {
        "공통": [
            ("운영계획서",       "기술협상서 §6: 운영 조직도·역할·절차 정의 (개발사·농정원·지자체)", "높음"),
            ("장애대응절차서",   "제안요청서 운영 요건: 장애 유형별 대응 및 RTO 4h 이내 복구 절차", "높음"),
            ("운영결과보고서",   "과업대비표: 운영 결과 정기 보고 (월간/분기)", "보통"),
            ("백업복구계획서",   "제안요청서: 공간 DB 일 1회 이상 백업, 반기 1회 복구 시험", "보통"),
        ],
    },
    "사업관리": {
        "공통": [
            ("사업수행계획서",   "제안서: 전체 사업 추진 계획·일정·조직 — 착수 후 즉시 제출 필수", "높음"),
            ("WBS",              "기술협상서 §1.2: 전체 사업 작업 분류·상세 일정 — Jira Due Date와 동기화", "높음"),
            ("과업대비표",       "제안서: 제안 과업 항목 대비 수행 현황 — 변경 시 즉시 현행화", "높음"),
            ("산출물 정의서",    "기술협상서: 단계별 산출물 목록·책임자·납기 — 전 이슈 기준 적용", "높음"),
            ("업무분장표",       "제안서: 참여 업체별 역할·책임·담당 과업 분장 — 갈등 방지", "높음"),
            ("품질계획서",       "기술협상서 §5: 품질 목표(결함 밀도·커버리지)·검토 절차 정의", "보통"),
            ("주간보고서",       "사업수행계획서: 주간 실적·계획 비교, 리스크 현황 정기 보고", "보통"),
            ("회의록",           "PM 관리 기준: 주요 회의 결과·의사결정·액션아이템 기록", "보통"),
        ],
    },
}

# 전체 이슈 공통 필수 산출물 (모든 세부사업에 적용)
COMMON_REQUIRED: list[tuple[str, str, str]] = [
    ("WBS",           "기술협상서 §1.2: 전체 이슈의 Due Date는 WBS 계획 일정과 반드시 일치", "높음"),
    ("과업대비표",    "제안서: 본 이슈 과업이 과업대비표에 반영되어 있어야 함", "높음"),
    ("산출물 정의서", "기술협상서: 본 이슈의 산출물이 산출물 정의서에 정의되어 있어야 함", "높음"),
]


# ═══════════════════════════════════════════════════════════════════════════════
# 4. 세부사업 유형 및 단계 분류
# ═══════════════════════════════════════════════════════════════════════════════

def classify_sub_project(summary: str) -> tuple[str, str]:
    """이슈 제목 → (세부사업 유형, 단계)"""
    s = summary.replace(" ", "")

    # 유형 분류
    if "갱신" in s and "시스템개선" not in s and "활용" not in s:
        proj_type = "갱신"
    elif "시스템개선" in s or "시스템 개선" in summary:
        proj_type = "시스템개선"
    elif "활용서비스" in s or "활용 서비스" in summary:
        proj_type = "활용서비스"
    elif "운영" in s and "사업관리" not in s:
        proj_type = "운영"
    elif any(k in s for k in ["사업관리", "사업수행", "PMO", "관리"]):
        proj_type = "사업관리"
    else:
        proj_type = "사업관리"  # 분류 불명 → 사업관리로 처리

    # 단계 분류
    phase_map = {
        "분석": ["1.분석", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "1.8", "1.9",
                 "분석", "요구사항"],
        "설계": ["2.설계", "2.1", "2.2", "2.3", "2.4", "2.5",
                 "설계"],
        "구현": ["3.구현", "3.개발", "3.1", "3.2", "3.3",
                 "구현", "개발"],
        "테스트": ["4.테스트", "4.1", "4.2",
                   "테스트", "검수"],
        "이행": ["5.이행", "5.1", "5.2",
                 "이행", "전환", "배포"],
    }
    phase = "공통"
    for ph, patterns in phase_map.items():
        for p in patterns:
            if p.replace(" ", "") in s:
                phase = ph
                break
        if phase != "공통":
            break

    return proj_type, phase


# ═══════════════════════════════════════════════════════════════════════════════
# 5. 갭 분석 (등록된 산출물 ↔ 필수 산출물 비교)
# ═══════════════════════════════════════════════════════════════════════════════

def _fname_matches(filename: str, keyword: str) -> bool:
    """파일명에 키워드(공백 제거·소문자 비교)가 포함되는지"""
    return keyword.replace(" ", "").lower() in filename.replace(" ", "").lower()


def analyze_gap(
    issue: dict,
    proj_type: str,
    phase: str,
) -> list[dict]:
    """
    필수 산출물 대비 실제 등록 산출물 갭 분석.
    반환: [{"name": 산출물명, "basis": 근거, "importance": 중요도,
            "status": "등록됨"/"미등록"/"파일명 불분명", "matched_file": 파일명 or ""}]
    """
    all_atts  = issue["fields"].get("attachment") or []
    filenames = [a.get("filename", "") for a in all_atts]

    # 필수 산출물 목록 결정 (단계별 + 공통)
    matrix  = REQUIRED_MATRIX.get(proj_type, REQUIRED_MATRIX["사업관리"])
    phase_req: list[tuple[str, str, str]] = matrix.get(phase, []) + matrix.get("공통", [])

    # 세부사업 사업관리 이슈가 아닐 경우 COMMON_REQUIRED 추가
    if proj_type != "사업관리":
        phase_req = list(phase_req)  # copy
        # (중복 방지: 이미 같은 이름이 있으면 추가 안 함)
        existing_names = {r[0] for r in phase_req}
        for r in COMMON_REQUIRED:
            if r[0] not in existing_names:
                phase_req.append(r)

    gap: list[dict] = []
    for name, basis, importance in phase_req:
        matched = next((f for f in filenames if _fname_matches(f, name)), None)
        if matched:
            status = "등록됨"
        elif any(_fname_matches(f, name[:3]) for f in filenames):
            # 부분 일치 (파일명 약어 등)
            matched = next(f for f in filenames if _fname_matches(f, name[:3]))
            status = "파일명 불분명"
        else:
            status = "미등록"
        gap.append({
            "name": name,
            "basis": basis,
            "importance": importance,
            "status": status,
            "matched_file": matched or "",
        })

    return gap


# ═══════════════════════════════════════════════════════════════════════════════
# 6. 개별 파일 분석 (파일 메타데이터 기준)
# ═══════════════════════════════════════════════════════════════════════════════

FARMMAP_DEEP_RULES: dict[str, list[tuple[str, str, str]]] = {
    "요구사항/면담": [
        ("F01", "면담 대상 기관·담당자 명시",
         "기술협상서 §2.1: 이해관계자 면담 계획 수립 필수",
         "면담 대상(농림축산식품부·농정원·지자체) 및 담당자 목록 추가"),
        ("F02", "면담 항목 ↔ RFP 요구사항 ID 매핑",
         "기술협상서 §2.1: 면담 항목은 FR-NNN 요구사항 ID와 연계",
         "면담 항목 옆에 대응 RFP 요구사항 ID 컬럼 추가"),
        ("F03", "면담 일정 ↔ WBS 정합성",
         "사업수행계획서: 면담 일정이 WBS 분석 단계 일정 이내",
         "WBS 기준 면담 예정일·실시일·장소 명시"),
        ("F04", "면담결과서 → 요구사항정의서 연계",
         "산출물 정의서: 면담계획서→면담결과서→요구사항정의서 추적 필수",
         "하위 산출물(이슈 링크 또는 파일명) 참조 추가"),
    ],
    "요구사항": [
        ("R01", "기능/비기능 요구사항 분리",
         "ISO/IEC 25010·RFP 품질 기준: 기능·성능·보안·가용성 별도 분류",
         "요구사항 유형 컬럼(기능/비기능) 추가 및 분류 기재"),
        ("R02", "요구사항 ID 체계 일관성",
         "기술협상서 §3.2: FR-NNN(기능), NFR-NNN(비기능) 표준 ID",
         "전체 ID 형식 통일 및 누락 ID 부여"),
        ("R03", "팜맵 갱신 관련 요구사항 포함",
         "제안요청서 핵심 과업: 연간 팜맵 갱신 요구사항 필수",
         "팜맵 갱신 주기·방법·정확도 기준(오차 ≤1m) 추가"),
        ("R04", "활용서비스 연계 요구사항 포함",
         "제안요청서: 농정원 내·외부 시스템 연계 요구사항 필수",
         "API 연계·데이터 제공 방식·UI 요구사항 보완"),
    ],
    "설계": [
        ("D01", "팜맵 공간데이터 스키마 반영",
         "팜맵 DB 표준: WGS84/TM 좌표계·Geometry 타입 명시 필수",
         "공간 컬럼(geom) 및 좌표계(SRID=4326 또는 5179) 정의 추가"),
        ("D02", "농정원 UI/UX 가이드라인 준수",
         "농정원 표준 UI 가이드: 색상·폰트·버튼·반응형 레이아웃 기준",
         "컴포넌트 목록에 가이드라인 준수 여부 체크 컬럼 추가"),
        ("D03", "화면 ID 표준화 (SCR-[모듈]-NNN)",
         "기술협상서 §4.1: 화면 ID 표준 형식 규정",
         "전체 화면 목록 ID 형식 통일"),
        ("D04", "API 명세 명확성",
         "제안요청서 §5: 각 API의 Request/Response 스펙 문서화",
         "API 명세서(Swagger/별도)와 설계서 간 참조 추가"),
    ],
    "테스트": [
        ("T01", "단위/통합/시스템 테스트 단계 구분",
         "품질계획서: 테스트 단계별 구분 및 담당자 명시",
         "테스트 케이스에 테스트 단계 컬럼 추가"),
        ("T02", "팜맵 갱신 공간 정확도 검증",
         "제안요청서 품질 기준: 기하 정확도(오차 ≤1m) 검증 필수",
         "공간 정확도 검증 테스트 케이스 추가"),
        ("T03", "요구사항 추적 커버리지 기준",
         "품질계획서: 기능 요구사항 대비 테스트 커버리지 ≥80%",
         "RTM과 테스트 케이스 매핑으로 커버리지 측정 방법 기재"),
    ],
    "이행": [
        ("M01", "이행 단계별 롤백 계획",
         "기술협상서: 이행 실패 시 이전 상태 복구 절차 필수",
         "이행 단계별 체크포인트 및 롤백 조건·방법 추가"),
        ("M02", "데이터 이관 정합성 검증",
         "사업수행계획서: 이관 전·후 레코드 수·체크섬 비교 검증",
         "이관 결과 검증 방법 및 허용 오차 기준 명시"),
    ],
    "관리/계획": [
        ("P01", "WBS ↔ Jira Due Date 동기화",
         "사업수행계획서: Jira 이슈 Due Date는 WBS 일정과 반드시 일치",
         "WBS 기준으로 Jira 전체 이슈 Due Date 검토·갱신"),
        ("P02", "마일스톤(착수·중간·최종 보고) 누락",
         "기술협상서 §1.2: 착수보고·중간보고·최종보고 마일스톤 명시",
         "보고 일정 마일스톤을 WBS에 표시"),
        ("P03", "과업 변경사항 현행화",
         "계약 변경 이력: 변경 시 과업대비표·WBS·산출물 정의서 동시 현행화",
         "최근 변경 과업 내용 반영 여부 검토"),
        ("P04", "팜맵 2026 추진 방향 일치",
         "농정원 사업 방향: 팜맵 갱신 정확도 향상·활용서비스 확대",
         "본 계획서 추진 방향이 최신 농정원 방침과 일치하는지 검토"),
    ],
    "운영": [
        ("O01", "운영 조직 및 역할 정의",
         "기술협상서 §6: 운영 조직도 및 역할·책임 명시",
         "운영 조직도와 역할별 담당 업무 상세화"),
        ("O02", "장애 대응 절차 및 RTO 명시",
         "제안요청서 운영 요건: 장애 시 RTO 4h 이내 및 복구 절차",
         "장애 유형별 대응 절차 및 에스컬레이션 경로 추가"),
        ("O03", "백업·복구 계획",
         "팜맵 데이터 중요도: 공간 DB 일 1회 백업, 복구 시험 반기 1회",
         "백업 주기·보관 기간·복구 절차·테스트 일정 명시"),
    ],
    "분석": [
        ("A01", "As-Is 현황 분석 완전성",
         "제안서: 현재 시스템·데이터·프로세스 현황 상세 분석",
         "현황 분석 항목이 누락 없이 작성되어 있는지 확인"),
        ("A02", "To-Be 개선 방향 연계",
         "사업수행계획서: 현황 분석 결과가 개선 목표와 연계",
         "현황 문제점 → 개선 목표 → 요구사항 연결 흐름 명시"),
    ],
    "공통": [
        ("C01", "의사결정 사항 기재",
         "PM 관리 기준: 결론·담당자·기한 명시로 추적 가능해야 함",
         "의사결정 목록에 '결론', '담당자', '완료 기한' 컬럼 추가"),
        ("C02", "액션아이템 담당자·기한 기재",
         "사업수행계획서: 모든 액션아이템은 담당자 및 완료 기한 필수",
         "액션아이템 목록 점검 후 미기재 항목 보완"),
    ],
    "일반 산출물": [
        ("G01", "문서 목적·범위 명확성",
         "일반 문서 기준: 목적·적용 범위·용어 정의를 전두에 명시",
         "목적(Purpose)·범위(Scope)·용어(Glossary) 섹션 추가"),
        ("G02", "관련 산출물 상호 참조",
         "산출물 정의서: 선행·후속 산출물과의 연관성 명시",
         "관련 문서 목록 추가 및 참조 관계 기재"),
    ],
}


def _meta_check(issue: dict, att: dict) -> tuple[bool, bool, list[tuple[str, str, str]]]:
    """
    메타데이터 기준 객관적 결함 검사.
    반환: (has_pdf, has_version, critical_list)
    """
    fname    = att["filename"]
    ext_raw  = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    ext      = f".{ext_raw}" if ext_raw else ""
    my_stem  = fname.rsplit(".", 1)[0] if "." in fname else fname

    all_atts  = issue["fields"].get("attachment") or []
    pdf_stems = {
        (a.get("filename", "").rsplit(".", 1)[0] if "." in a.get("filename", "") else a.get("filename", ""))
        for a in all_atts if a.get("filename", "").lower().endswith(".pdf")
    }
    has_pdf     = (ext == ".pdf") or (my_stem in pdf_stems)
    has_version = bool(
        re.search(r'[_\-\s]?v\d+[\.\d]*', fname, re.IGNORECASE)
        or re.search(r'_\d+(\.\d+)+', fname)
    )

    critical: list[tuple[str, str, str]] = []
    if not has_pdf:
        critical.append((
            "원본 + PDF 동시 등록 필요",
            "PMO 관리 방침: 원본(hwp/docx/xlsx 등) + PDF 동시 등록 필수",
            f"`{my_stem}.pdf`를 원본과 함께 첨부",
        ))
    if not has_version:
        critical.append((
            "버전 표기 누락",
            "문서 관리 기준: 버전(v1.0 등)과 작성일 필수 기재",
            f"파일명에 버전 추가 → `{my_stem}_v1.0.{ext_raw}`",
        ))
    return has_pdf, has_version, critical


# ═══════════════════════════════════════════════════════════════════════════════
# 7. 통합 분석 댓글 생성
# ═══════════════════════════════════════════════════════════════════════════════

def build_comment(
    issue: dict,
    att: dict | None,
    proj_type: str,
    phase: str,
    gap: list[dict],
    is_change: bool = False,
    prev_date: str = "",
) -> core.CommentAction:
    """
    갭 분석 + 개별 파일 분석을 결합한 통합 정밀 분석 댓글 생성.
    att=None 이면 갭 분석 단독 댓글.
    """
    key     = issue["key"]
    summary = issue["fields"].get("summary", "")
    status  = issue["fields"].get("status", {}).get("name", "")
    duedate = issue["fields"].get("duedate") or "미등록"
    check_time = core.TS()

    # ── 갭 분석 섹션 ──────────────────────────────────────────────────────────
    missing   = [g for g in gap if g["status"] == "미등록"]
    unclear   = [g for g in gap if g["status"] == "파일명 불분명"]
    present   = [g for g in gap if g["status"] == "등록됨"]

    critical_missing_high = [g for g in missing if g["importance"] == "높음"]

    gap_rows = ""
    for g in gap:
        if g["status"] == "등록됨":
            icon = "✅"
            matched = f"`{g['matched_file']}`" if g["matched_file"] else ""
        elif g["status"] == "파일명 불분명":
            icon = "⚠️"
            matched = f"`{g['matched_file']}` ← 파일명 재확인 필요"
        else:
            icon = "🔴" if g["importance"] == "높음" else "🟡"
            matched = "**미등록**"
        imp_tag = {"높음": "🔴 높음", "보통": "🟡 보통", "낮음": "⚪ 낮음"}.get(g["importance"], g["importance"])
        gap_rows += f"| {icon} | {g['name']} | {imp_tag} | {matched} | {g['basis']} |\n"

    # ── 개별 파일 분석 섹션 ───────────────────────────────────────────────────
    file_section = ""
    file_critical: list[tuple[str, str, str]] = []
    if att:
        fname    = att["filename"]
        ext_raw  = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
        raw_a    = att.get("author", "")
        author   = raw_a.get("displayName", str(raw_a)) if isinstance(raw_a, dict) else str(raw_a)
        created  = att.get("created", "")[:10]
        size_kb  = att.get("size", 0) // 1024
        dtype, perspectives = classify_deliverable(fname)
        has_pdf, has_version, file_critical = _meta_check(issue, att)
        type_rules = FARMMAP_DEEP_RULES.get(dtype, FARMMAP_DEEP_RULES["일반 산출물"])
        context_label = "갱신 산출물" if is_change else "신규 산출물"

        change_note = ""
        if is_change and prev_date:
            change_note = f"\n> 🔄 **갱신**: 이전 등록일 {prev_date} → 현재 {created}. 변경 내용을 댓글로 기재해 주세요.\n"

        meta_row = (
            f"| 항목 | 내용 |\n|------|------|\n"
            f"| 파일명 | `{fname}` ({size_kb} KB) |\n"
            f"| 산출물 유형 | {dtype} |\n"
            f"| 등록자 | {author} |\n"
            f"| 등록일 | {created} |\n"
            f"| PDF 동시등록 | {'✅ 확인됨' if has_pdf else '❌ 미등록'} |\n"
            f"| 버전 표기 | {'✅ 있음' if has_version else '⚠️ 없음'} |\n"
        )

        critical_md = ""
        if file_critical:
            critical_md = "\n**🚨 즉시 조치 필요**\n\n| 항목 | 근거 | 권고 조치 |\n|------|------|----------|\n"
            for item, rat, sug in file_critical:
                critical_md += f"| 🔴 {item} | {rat} | {sug} |\n"

        checklist_md = "\n".join(f"- [ ] **{cid}** {item} — _{rat}_ → {sug}" for cid, item, rat, sug in type_rules)
        perspectives_md = "\n".join(f"- [ ] {p}" for p in perspectives)

        file_section = f"""
---

### 🆕 {context_label} 정밀 분석: `{fname}`
{change_note}
{meta_row}{critical_md}

**[{dtype}] 유형별 심층 점검 체크리스트**

{checklist_md}

**내용 검토 관점**

{perspectives_md}
"""

    # ── 우선순위 및 헤더 결정 ─────────────────────────────────────────────────
    total_issues = len(critical_missing_high) + len(file_critical)
    if total_issues >= 3 or len(critical_missing_high) >= 2:
        priority = "긴급"
        header_emoji = "🚨"
    elif total_issues >= 1:
        priority = "높음"
        header_emoji = "🔧"
    else:
        priority = "보통"
        header_emoji = "✅"

    # 주요산출물 기준 참조 줄
    ref_docs_line = " · ".join(KEY_REFERENCE_DOCS.keys())

    summary_line = (
        f"미등록 {len(missing)}건(높음 {len(critical_missing_high)}건) · "
        f"불분명 {len(unclear)}건 · 등록됨 {len(present)}건"
    )

    # 마커 (중복 방지)
    if att:
        marker = f"PMO 정밀분석 | {('갱신' if is_change else '신규')} | {att['filename']}"
    else:
        marker = f"PMO 갭분석 | {proj_type} | {phase}"

    body = f"""{header_emoji} **[PMO 정밀분석 | {proj_type} · {phase}단계] 세부사업 산출물 적정성 검토**

**이슈**: [{key}]({core.issue_url(key)}) — {summary}
**이슈 상태**: {status} | **납기**: {duedate}
**세부사업 유형**: {proj_type} | **단계**: {phase}
**점검 기준**: {ref_docs_line}

---

### 📊 세부사업별 산출물 갭 분석 ({summary_line})

| 상태 | 필수 산출물 | 중요도 | 등록 파일 | 근거/기준 |
|------|------------|--------|----------|----------|
{gap_rows}

**점검 결과 요약**

- 🔴 **즉시 등록 필요 (중요도 높음)**: {len(critical_missing_high)}건
  {chr(10).join(f'  - `{g["name"]}` — {g["basis"].split(":")[0]}' for g in critical_missing_high) if critical_missing_high else "  없음"}
- 🟡 **등록 필요 (중요도 보통 이하)**: {len([g for g in missing if g["importance"] != "높음"])}건
- ⚠️ **파일명 재확인 필요**: {len(unclear)}건
  {chr(10).join(f'  - `{g["matched_file"]}` → `{g["name"]}` 에 해당하는지 확인' for g in unclear) if unclear else "  없음"}
{file_section}
---

### ✅ 공통 등록 기준 최종 확인

- [ ] 모든 산출물: 원본 파일 + PDF 동시 등록
- [ ] 파일명에 버전(v1.0 등) 및 작성일 포함
- [ ] WBS · 과업대비표 · 산출물 정의서와 정합성 확인
- [ ] 상위 요구사항 → 본 산출물 → 하위 단계 추적 가능 여부

> 보완 완료 후 본 댓글에 **'조치 완료'** 답글과 수정본을 등록해 주세요.
> 미반영 사항은 반드시 사유를 함께 기재해 주시기 바랍니다.

*자동 생성: FM2026 PMO 정밀분석 | {check_time}*"""

    return core.CommentAction(
        issue_key=key,
        summary=summary,
        action_type="deliverable_gap_analysis",
        title=f"[{proj_type}·{phase}] 산출물 갭분석: 미등록 {len(missing)}건",
        rationale=(
            f"[{key}] {proj_type}·{phase} — "
            f"필수 산출물 {len(gap)}건 중 미등록 {len(missing)}건(높음 {len(critical_missing_high)}건), "
            f"불분명 {len(unclear)}건. "
            + (f"신규파일: {att['filename']}." if att else "")
        ),
        comment_body=body,
        priority=priority,
        marker=marker,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 8. 상태 관리
# ═══════════════════════════════════════════════════════════════════════════════

def load_seen() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("상태파일 로드 실패(%s) → 초기화", e)
    return {"attachment_ids": [], "attachment_versions": {}, "last_run": ""}


def save_seen(state: dict) -> None:
    state["last_run"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════════════
# 9. 메인 감시 루프
# ═══════════════════════════════════════════════════════════════════════════════

def watch_deliverables(
    dry_run: bool,
    review_mode: bool = False,
    reset: bool = False,
    target_key: str | None = None,
    gap_all: bool = False,
) -> dict:
    if reset and os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
        log.info("상태파일 초기화 — 전체 산출물 재분석")

    state       = load_seen()
    seen_ids    = set(state.get("attachment_ids", []))
    att_versions: dict = state.get("attachment_versions", {})

    if not core.JIRA_EMAIL or not core.JIRA_API_TOKEN:
        log.warning("JIRA 인증정보 미설정 → 시뮬레이션 모드")
        issues = _sim_issues()
    else:
        core.verify_jira_auth()
        issues = core.fetch_all_issues()

    new_actions: list[core.CommentAction] = []
    new_ids: list[str]    = []
    new_versions: dict    = {}
    summary_log: list[dict] = []
    today = datetime.now(KST).strftime("%Y-%m-%d")

    for issue in issues:
        if target_key and issue["key"] != target_key:
            continue

        ikey    = issue["key"]
        isummary = issue["fields"].get("summary", "")
        proj_type, phase = classify_sub_project(isummary)
        readable, unreadable = core.analyze_attachments(issue)
        all_atts_for_issue   = readable + unreadable

        # ── 신규·갱신 산출물 감지 ─────────────────────────────────────────────
        changed_atts: list[tuple[dict, bool, str]] = []  # (att, is_change, prev_date)

        for att in all_atts_for_issue:
            aid      = str(att["id"])
            fname    = att["filename"]
            att_date = att.get("created", "")[:10]
            ver_key  = f"{ikey}:{fname}"

            new_ids.append(aid)
            new_versions[ver_key] = {"id": aid, "date": att_date}

            # 업체(수행사)가 등록한 산출물만 분석 — 본인 계정 업로드는 건너뜀
            # Jira API: att["author"]가 dict {"accountId": ..., "displayName": ...} 형태
            raw_author = att.get("author", "")
            if isinstance(raw_author, dict):
                att_author_id  = raw_author.get("accountId", "")
                att_author     = raw_author.get("displayName", "")
            else:
                att_author_id  = ""
                att_author     = str(raw_author)
            is_pmo_upload = (
                att_author in PMO_DISPLAY_NAMES
                or att_author_id in PMO_ACCOUNT_IDS
            )
            if is_pmo_upload:
                log.debug("PMO 계정 업로드 건너뜀: %s %s (등록자: %s)", ikey, fname, att_author)
                continue

            prev_info = att_versions.get(ver_key)
            is_new    = aid not in seen_ids
            is_changed = (
                prev_info is not None
                and prev_info.get("id") != aid
                and att_date > prev_info.get("date", "")
            )

            if is_new and prev_info is not None:
                is_changed = True
                is_new     = False

            if is_new or is_changed:
                changed_atts.append((att, is_changed, prev_info.get("date", "") if prev_info else ""))

        # ── 갭 분석 (신규·갱신 발생 시 or gap_all 옵션 시) ───────────────────
        should_analyze = bool(changed_atts) or gap_all
        if not should_analyze:
            continue

        gap = analyze_gap(issue, proj_type, phase)

        # 첨부파일이 변동된 경우: 파일별 + 갭 분석 통합 댓글
        if changed_atts:
            for att, is_change, prev_date in changed_atts:
                action = build_comment(
                    issue, att, proj_type, phase, gap,
                    is_change=is_change, prev_date=prev_date,
                )
                if not core.already_commented(issue, action.marker):
                    new_actions.append(action)
                    summary_log.append({
                        "issue": ikey,
                        "file": att["filename"],
                        "type": classify_deliverable(att["filename"])[0],
                        "event": "갱신" if is_change else "신규",
                        "gap_missing": len([g for g in gap if g["status"] == "미등록"]),
                    })
                else:
                    log.info("중복 건너뜀: %s %s", ikey, att["filename"])
        elif gap_all:
            # gap_all 모드: 첨부 변동 없어도 갭 분석 댓글 등록
            action = build_comment(issue, None, proj_type, phase, gap)
            gap_marker_today = f"{action.marker} | {today}"
            if not core.already_commented(issue, action.marker):
                new_actions.append(action)
                summary_log.append({
                    "issue": ikey,
                    "file": "",
                    "type": proj_type,
                    "event": "갭분석",
                    "gap_missing": len([g for g in gap if g["status"] == "미등록"]),
                })

    # ── 결과 처리 ─────────────────────────────────────────────────────────────
    if review_mode:
        core.print_review_report(new_actions)
    else:
        for act in new_actions:
            core.execute_comment(act, dry_run)

    # ── 상태 갱신 ─────────────────────────────────────────────────────────────
    if not dry_run and not review_mode:
        state["attachment_ids"]      = sorted(set(state.get("attachment_ids", [])) | set(new_ids))
        state["attachment_versions"] = {**att_versions, **new_versions}
        save_seen(state)
        log.info("상태파일 갱신: 총 %d개 산출물 추적 중", len(state["attachment_ids"]))
    else:
        log.info("(dry-run/review 모드 — 상태파일 미갱신, 감지 %d건)", len(summary_log))

    return {
        "new_deliverables": summary_log,
        "comments": [{"issue": a.issue_key, "title": a.title} for a in new_actions],
    }


def _sim_issues() -> list[dict]:
    return [
        {
            "key": "FM2026-153",
            "fields": {
                "summary": "[시스템개선] 1.분석 - 1.9.요구사항 이해 (면담을 통한 요구사항 도출)",
                "status": {"name": "진행 중"},
                "duedate": "2026-07-10",
                "attachment": [
                    {"id": "24289", "filename": "(SYS01_04)면담계획서.hwp", "size": 384512,
                     "created": "2026-06-23T21:32:37+0900", "author": "윤병석",
                     "content": "x", "mimeType": "application/x-ole-storage", "ext": ".hwp"},
                    {"id": "24290", "filename": "(SYS01_04)면담계획서.pdf", "size": 182956,
                     "created": "2026-06-23T21:32:37+0900", "author": "윤병석",
                     "content": "x", "mimeType": "application/pdf", "ext": ".pdf"},
                ],
                "comment": {"comments": []},
                "description": None,
                "updated": "2026-06-23",
            },
        },
        {
            "key": "FM2026-160",
            "fields": {
                "summary": "[시스템개선] 2.설계 - 2.2.화면 설계",
                "status": {"name": "진행 중"},
                "duedate": "2026-08-07",
                "attachment": [
                    {"id": "30001", "filename": "화면설계서_v1.0.pptx", "size": 1048576,
                     "created": "2026-06-24T10:00:00+0900", "author": "윤병석",
                     "content": "x", "mimeType": "application/vnd.ms-powerpoint", "ext": ".pptx"},
                ],
                "comment": {"comments": []},
                "description": None,
                "updated": "2026-06-24",
            },
        },
    ]


def print_watch_summary(result: dict) -> None:
    print("\n" + "=" * 70)
    print("  FM2026 산출물 신규·갱신 감시 + 갭분석 결과")
    print(f"  점검 시각: {datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')}")
    print("=" * 70)
    nd = result["new_deliverables"]
    if not nd:
        print("  ✓ 신규·갱신 산출물 없음 (변동사항 없음)")
    else:
        print(f"  감지 {len(nd)}건:")
        for item in nd:
            gap_info = f"  미등록 {item.get('gap_missing', 0)}건" if item.get("gap_missing") else ""
            print(f"    [{item['issue']}] [{item['event']}] {item['file'] or item['type']}{gap_info}")
        print(f"\n  생성된 분석 댓글: {len(result['comments'])}건")
    print("=" * 70 + "\n")


# ═══════════════════════════════════════════════════════════════════════════════
# 10. CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    parser = argparse.ArgumentParser(description="FM2026 산출물 신규·갱신 감시 및 주요산출물 기준 정밀 분석")
    parser.add_argument("--review",        action="store_true", help="근거+미리보기 출력")
    parser.add_argument("--post-comments", action="store_true", help="실제 댓글 등록")
    parser.add_argument("--reset",         action="store_true", help="상태파일 초기화 (전체 재분석)")
    parser.add_argument("--gap-all",       action="store_true", help="모든 이슈 갭분석 (산출물 변동 없어도)")
    parser.add_argument("--issue",         metavar="KEY",       help="특정 이슈만")
    args = parser.parse_args()

    dry_run = not args.post_comments

    if args.post_comments:
        log.info("=== LIVE 모드 — 신규·갱신 산출물 분석 댓글 실제 등록 ===")
    elif args.review:
        log.info("=== REVIEW 모드 — 근거 및 미리보기 ===")
    else:
        log.info("=== DRY-RUN 모드 — 미리보기 (상태파일 미갱신) ===")

    result = watch_deliverables(
        dry_run=dry_run,
        review_mode=args.review,
        reset=args.reset,
        target_key=args.issue,
        gap_all=args.gap_all,
    )
    if not args.review:
        print_watch_summary(result)


if __name__ == "__main__":
    main()
