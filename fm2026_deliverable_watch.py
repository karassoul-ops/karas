"""
FM2026 산출물 신규·갱신 감지 및 정밀 분석 모듈
================================================
체크 주기마다 FM2026 프로젝트의 모든 첨부파일을 점검하여:
  - 신규 등록 산출물 → 정밀 분석 + 검토 의견 댓글 등록
  - 갱신(재등록) 산출물 → 변경 내용 파악 + 개선 의견 댓글 등록

동작 원리:
  1. 전체 이슈의 첨부파일 목록 조회
  2. 상태파일과 비교하여 신규 및 갱신 첨부파일 추출
  3. 산출물 유형 분류 → 팜맵 사업 특화 심층 분석
  4. 검토 의견(개선·수정·변경 사항) 댓글 등록
  5. 상태파일 갱신

사용법:
  python fm2026_deliverable_watch.py                 # dry-run (미리보기)
  python fm2026_deliverable_watch.py --review        # 근거+미리보기
  python fm2026_deliverable_watch.py --post-comments # 실제 댓글 등록
  python fm2026_deliverable_watch.py --reset         # 상태파일 초기화 (전체 재분석)
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

# ─── 산출물 유형 분류 규칙 ───────────────────────────────────────────────────
DELIVERABLE_RULES = [
    ("면담계획서",    "요구사항/면담",  ["면담 대상·일정 발주처 협의 완료 여부", "면담 항목 ↔ RFP 요구사항 매핑"]),
    ("면담결과서",    "요구사항/면담",  ["면담 결과 → 요구사항정의서 연계 여부", "증빙화면·회의록 첨부 여부"]),
    ("요구사항정의서", "요구사항",      ["기능/비기능 요구사항 분리 여부", "RFP 요구사항 추적표(RTM) 연계"]),
    ("요구사항추적",  "요구사항",      ["요구사항 ID 체계 일관성", "설계·테스트 단계 추적 가능 여부"]),
    ("화면설계",     "설계",          ["화면 ID 표준 준수", "농정원 UI/UX 가이드 반영 여부"]),
    ("프로그램설계",  "설계",          ["모듈 분할 적정성", "인터페이스 정의 명확성"]),
    ("DB설계",       "설계",          ["테이블·컬럼 표준 준수", "팜맵 공간데이터 스키마 정합성"]),
    ("ERD",          "설계",          ["엔티티 관계 정규화", "공간컬럼(geometry) 정의"]),
    ("테스트",       "테스트",         ["테스트케이스 커버리지", "단위/통합 테스트 구분"]),
    ("WBS",          "관리/계획",      ["일정 ↔ Jira Due Date 동기화", "마일스톤 누락 여부"]),
    ("과업대비표",    "관리/계획",      ["제안서 과업 대비 누락 항목", "변경 과업 반영 여부"]),
    ("사업수행계획서", "관리/계획",     ["추진 일정·조직·산출물 최신화", "팜맵 추진 방향 일치 여부"]),
    ("산출물 정의서", "관리/계획",     ["전체 산출물 목록 완전성", "단계별 산출물 매핑"]),
    ("운영계획",     "운영",           ["운영 조직·절차 정의", "장애대응·백업 계획 포함"]),
    ("품질",         "품질",           ["품질지표 수치 목표 명시", "검토·승인 절차 명시"]),
    ("회의록",       "공통",           ["의사결정 사항 명시", "액션아이템·담당자·기한 기재"]),
    ("보고서",       "공통",           ["보고 목적·결론 명확성", "근거 데이터 첨부"]),
]

# ─── 팜맵 사업 특화 심층 분석 규칙 ──────────────────────────────────────────
FARMMAP_DEEP_RULES: dict[str, list[tuple[str, str, str]]] = {
    "요구사항/면담": [
        ("F01", "면담 대상 기관·담당자 명시",
         "RFP 제4조: 착수 후 2주 이내 이해관계자 면담 계획 수립 필요",
         "면담 대상(농림축산식품부·농정원·지자체 등) 및 담당자 목록 추가"),
        ("F02", "면담 항목 ↔ RFP 요구사항 ID 매핑",
         "기술협상서 §2.1: 각 면담 항목은 RFP 기능 요구사항 ID(FR-NNN)와 연계 필요",
         "면담 항목 옆에 대응 RFP 요구사항 ID 컬럼 추가"),
        ("F03", "면담 일정 ↔ WBS 정합성",
         "사업수행계획서: 면담 일정이 WBS 분석 단계 일정 이내여야 함",
         "WBS 기준 면담 예정일·실시일·장소를 문서에 명시"),
        ("F04", "면담결과서 → 요구사항정의서 연계 명시",
         "산출물 정의서: 면담계획서→면담결과서→요구사항정의서 순서 추적 필수",
         "본 계획서 기반으로 생성될 하위 산출물 참조 추가"),
    ],
    "요구사항": [
        ("R01", "기능/비기능 요구사항 분리",
         "ISO/IEC 25010 및 RFP 품질 기준: 기능·성능·보안·가용성 요구사항 별도 분류",
         "요구사항 유형 컬럼(기능/비기능) 추가 및 각 항목 분류 기재"),
        ("R02", "요구사항 ID 체계 일관성",
         "기술협상서 §3.2: FR-NNN(기능), NFR-NNN(비기능) 형식 ID 표준 사용",
         "전체 ID를 표준 형식으로 통일 및 누락 ID 부여"),
        ("R03", "팜맵 갱신 관련 요구사항 포함",
         "RFP 핵심 과업: 연간 팜맵 갱신(필지 경계·지목 변경·신규 농경지) 요구사항 필수",
         "팜맵 갱신 주기·방법·정확도 기준(위치 오차 ≤1m) 관련 요구사항 추가"),
        ("R04", "활용서비스 연계 요구사항 포함",
         "RFP 핵심 과업: 농정원 내·외부 시스템 연계 및 활용서비스 관련 요구사항 필수",
         "API 연계·데이터 제공 방식·UI 요구사항 보완"),
    ],
    "설계": [
        ("D01", "팜맵 공간데이터 스키마 반영",
         "팜맵 DB 표준: WGS84/TM 좌표계, Geometry 타입(Polygon/MultiPolygon) 명시 필요",
         "공간 컬럼(geom) 및 좌표계(SRID=4326 또는 5179) 정의 추가"),
        ("D02", "농정원 UI/UX 가이드라인 준수",
         "농정원 표준 UI 가이드: 색상 코드·폰트·버튼 스타일·반응형 레이아웃 기준 준수",
         "화면 컴포넌트 목록에 가이드라인 준수 여부 체크 컬럼 추가"),
        ("D03", "화면 ID 표준화 (SCR-[모듈]-NNN)",
         "기술협상서 §4.1: 화면 ID는 'SCR-[모듈코드]-NNN' 형식 사용 규정",
         "전체 화면 목록 ID를 표준 형식으로 통일"),
        ("D04", "API 명세 명확성",
         "RFP §5 연계 요건: 각 API 엔드포인트의 Request/Response 스펙 문서화 필요",
         "API 명세서(Swagger/별도 문서)와 설계서 간 연계 참조 추가"),
    ],
    "테스트": [
        ("T01", "단위/통합/시스템 테스트 단계 구분",
         "품질계획서: 테스트 단계별(단위→통합→시스템→인수) 구분 및 담당자 명시 필수",
         "테스트 케이스에 테스트 단계 컬럼 추가 및 각 케이스 분류"),
        ("T02", "팜맵 갱신 공간 정확도 검증",
         "RFP 품질 기준: 갱신 결과물 기하 정확도(위치 오차 ≤1m) 검증 테스트 필수",
         "공간 정확도 검증 테스트 케이스 추가(샘플 필지 비교·검증)"),
        ("T03", "요구사항 추적 커버리지 기준",
         "품질계획서: 기능 요구사항 대비 테스트 커버리지 80% 이상 목표",
         "RTM과 테스트 케이스 매핑으로 커버리지 측정 방법 기재"),
    ],
    "관리/계획": [
        ("P01", "WBS ↔ Jira Due Date 동기화",
         "사업수행계획서: Jira 이슈 Due Date는 WBS 계획 일정과 반드시 일치",
         "WBS 마일스톤 기준으로 Jira 전체 이슈 Due Date 일괄 검토·갱신"),
        ("P02", "마일스톤(착수·중간·최종 보고) 누락 여부",
         "기술협상서 §1.2: 착수보고·중간보고·최종보고 마일스톤 명시 필수",
         "보고 일정 마일스톤이 WBS에 표시되어 있는지 확인 후 미표시 시 추가"),
        ("P03", "과업 변경사항 현행화",
         "계약 변경 이력: 과업 변경 시 과업대비표·WBS·산출물 정의서 동시 현행화 필수",
         "최근 변경 과업 내용 반영 여부 검토 후 미반영 항목 즉시 업데이트"),
        ("P04", "팜맵 2026 추진 방향 일치",
         "농정원 사업 방향: 팜맵 갱신 정확도 향상·활용서비스 확대·연계 기관 증가",
         "본 계획서 추진 방향이 최신 농정원 방침과 일치하는지 검토"),
    ],
    "운영": [
        ("O01", "운영 조직 및 역할 정의",
         "기술협상서 §6: 운영 조직도(개발사·농정원·지자체) 및 역할·책임 명시 필수",
         "운영 조직도와 각 역할별 담당 업무 상세화"),
        ("O02", "장애 대응 절차 및 RTO 명시",
         "RFP 운영 요건: 서비스 장애 시 복구 시간 목표(RTO 4h 이내) 및 복구 절차 명시",
         "장애 유형별(인프라·앱·DB) 대응 절차 및 에스컬레이션 경로 추가"),
        ("O03", "백업·복구 계획",
         "팜맵 데이터 중요도: 공간 DB 일 1회 이상 백업, 복구 시험 반기 1회 이상",
         "백업 주기·보관 기간·복구 절차·테스트 일정 명시"),
    ],
    "품질": [
        ("Q01", "품질 지표 수치 목표 명시",
         "기술협상서 §5: 결함 밀도(≤0.5건/FP), 테스트 커버리지(≥80%) 등 수치 목표 필수",
         "품질 목표 수치 및 측정 방법 명시"),
        ("Q02", "검토·승인 절차 및 담당자 명시",
         "품질계획서 표준: 산출물별 검토자·승인자·검토 방법·기한 정의 필요",
         "산출물 검토·승인 매트릭스(산출물명·검토자·승인자·기한) 추가"),
    ],
    "공통": [
        ("C01", "의사결정 사항 기재",
         "PM 관리 기준: 회의 결정 사항은 결론·담당자·기한 명시로 추적 가능해야 함",
         "의사결정 목록에 '결론', '담당자', '완료 기한' 컬럼 추가"),
        ("C02", "액션아이템 담당자·기한 기재",
         "사업수행계획서: 모든 액션아이템은 담당자 및 완료 기한 필수",
         "액션아이템 목록 점검 후 미기재 항목 보완"),
    ],
    "일반 산출물": [
        ("G01", "문서 목적·범위 명확성",
         "일반 문서 작성 기준: 목적·적용 범위·용어 정의를 문서 전두에 명시",
         "목적(Purpose)·범위(Scope)·용어(Glossary) 섹션 추가"),
        ("G02", "관련 산출물 상호 참조",
         "산출물 정의서: 각 산출물은 선행·후속 산출물과의 연관성 명시 필요",
         "관련 문서 목록 추가 및 참조 관계(선행·후속·병행) 기재"),
    ],
}


# ─── 상태 관리 ───────────────────────────────────────────────────────────────

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


# ─── 산출물 분류 ─────────────────────────────────────────────────────────────

def classify_deliverable(filename: str) -> tuple[str, list[str]]:
    """파일명 → (산출물유형, 분석관점 목록)"""
    name = filename.replace(" ", "")
    for keyword, dtype, perspectives in DELIVERABLE_RULES:
        if keyword.replace(" ", "") in name:
            return dtype, perspectives
    return "일반 산출물", ["문서 목적·범위 명확성", "관련 산출물과의 정합성"]


# ─── 통합 정밀 분석 댓글 생성 ────────────────────────────────────────────────

def build_analysis_comment(
    issue: dict,
    att: dict,
    is_change: bool = False,
    prev_date: str = "",
) -> core.CommentAction:
    """
    신규/갱신 산출물 1건에 대한 통합 정밀 분석 댓글 생성.

    - 메타데이터 기준 객관적 결함 (PDF 미등록, 버전 표기 없음) 우선 제시
    - 산출물 유형별 팜맵 특화 심층 점검 체크리스트 (근거/기준 포함)
    - 공통 품질 기준 체크리스트
    - 조치 요청
    """
    key      = issue["key"]
    summary  = issue["fields"].get("summary", "")
    fname    = att["filename"]
    ext_raw  = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    ext      = f".{ext_raw}" if ext_raw else ""
    author   = att.get("author", "")
    created  = att.get("created", "")[:10]
    size_kb  = att.get("size", 0) // 1024
    status   = issue["fields"].get("status", {}).get("name", "")
    duedate  = issue["fields"].get("duedate") or "미등록"

    dtype, perspectives = classify_deliverable(fname)
    context_label = "갱신 산출물" if is_change else "신규 산출물"
    check_time = core.TS()

    # ── 객관적 메타데이터 검사 ─────────────────────────────────────────────
    all_atts  = issue["fields"].get("attachment") or []
    pdf_stems = {
        (a.get("filename", "").rsplit(".", 1)[0] if "." in a.get("filename", "") else a.get("filename", ""))
        for a in all_atts if a.get("filename", "").lower().endswith(".pdf")
    }
    my_stem     = fname.rsplit(".", 1)[0] if "." in fname else fname
    has_pdf     = (ext == ".pdf") or (my_stem in pdf_stems)
    has_version = bool(
        re.search(r'[_\-\s]?v\d+[\.\d]*', fname, re.IGNORECASE)
        or re.search(r'_\d+(\.\d+)+', fname)
    )

    critical: list[tuple[str, str, str]] = []
    if not has_pdf:
        critical.append((
            "원본 + PDF 동시 등록 필요",
            "PMO 관리 방침: 원본(hwp/docx/xlsx 등) + PDF 동시 등록 필수 — "
            "검토자가 전용 뷰어 없이 열람 가능해야 함",
            f"파일명 `{my_stem}.pdf`를 원본과 함께 이슈에 첨부",
        ))
    if not has_version:
        critical.append((
            "버전 표기 누락",
            "문서 관리 기준: 모든 산출물은 버전(v1.0 등)과 작성일 필수 기재 — "
            "이력 추적 및 최신본 식별 필요",
            f"파일명에 버전 추가 (예: `{my_stem}_v1.0.{ext_raw}`)",
        ))

    # ── 유형별 심층 체크리스트 ─────────────────────────────────────────────
    type_rules = FARMMAP_DEEP_RULES.get(dtype, FARMMAP_DEEP_RULES["일반 산출물"])

    # ── 갱신 산출물 변경 이력 섹션 ─────────────────────────────────────────
    change_section = ""
    if is_change and prev_date:
        change_section = f"""
### 🔄 갱신 이력
| 항목 | 내용 |
|------|------|
| 이전 등록일 | {prev_date} |
| 갱신 등록일 | {created} |
| 변경 여부 | 파일명 동일, 재등록(버전 교체 추정) |

> ⚠️ 갱신 산출물은 이전 버전 대비 **무엇이 변경되었는지** 댓글로 간략히 기재해 주세요.

---
"""

    # ── 우선 개선 항목 섹션 ────────────────────────────────────────────────
    if critical:
        critical_md = "### 🚨 즉시 조치 필요 항목\n\n"
        critical_md += "| 항목 | 근거/기준 | 권고 조치 |\n|------|----------|----------|\n"
        for item, rat, sug in critical:
            critical_md += f"| 🔴 **{item}** | {rat} | {sug} |\n"
        critical_md += "\n---\n"
        priority = "높음" if len(critical) >= 2 else "보통"
        header_emoji = "🔧"
        header_text  = f"즉시 조치 필요 {len(critical)}건 + 심층 검토 의견"
    else:
        critical_md = ""
        priority = "낮음"
        header_emoji = "✅"
        header_text  = "심층 검토 의견 (즉시 조치 불필요)"

    # ── 유형별 체크리스트 MD ──────────────────────────────────────────────
    checklist_md = "| ID | 점검 항목 | 근거/기준 | 권고 조치 |\n|----|----------|----------|----------|\n"
    for cid, item, rat, sug in type_rules:
        checklist_md += f"| {cid} | {item} | {rat} | {sug} |\n"
    checklist_md += "| COM | 팜맵 2026 추진 방향 반영 | 사업 개요: 팜맵 갱신 정확도 향상·활용서비스 확대가 핵심 목표 | 본 산출물이 팜맵 갱신·활용서비스 목표와 연계되어 있는지 확인 |\n"

    # ── 관점별 검토 체크리스트 ─────────────────────────────────────────────
    perspectives_md = "\n".join(f"- [ ] {p}" for p in perspectives)

    marker = f"PMO 정밀분석 | {context_label} | {fname}"

    body = f"""## {header_emoji} [PMO 정밀분석 | {context_label}] {header_text}

**이슈**: [{key}]({core.issue_url(key)}) — {summary}
**산출물**: `{fname}` ({size_kb} KB) | **등록자**: {author} | **등록일**: {created}
**유형**: {dtype} | **이슈 상태**: {status} | **납기**: {duedate}
**PDF 동시등록**: {'✅ 확인됨' if has_pdf else '❌ 미등록'} | **버전 표기**: {'✅ 있음' if has_version else '⚠️ 없음'}

---
{change_section}{critical_md}
### 📋 [{dtype}] 유형별 심층 점검 체크리스트

팜맵 사업 기준(RFP·기술협상서·사업수행계획서)에 따라 아래 항목을 검토·보완해 주세요.

{checklist_md}

### 🔍 내용 검토 관점

{perspectives_md}

### ✅ 등록 기준 최종 확인

- [ ] 문서 내 버전(v1.0 등) 및 작성일 기재
- [ ] 원본 파일 + PDF 동시 등록
- [ ] 검토자/승인자 서명란 포함 (해당 시)
- [ ] WBS·과업대비표·산출물 정의서와 정합성 확인
- [ ] 상위 요구사항 → 본 산출물 → 하위 단계로 추적 가능한지 확인

> 보완 완료 후 본 댓글에 **'조치 완료'** 답글을 남겨 주세요.
> 미반영 사항은 사유를 함께 기재해 주시기 바랍니다.

*자동 생성: FM2026 PMO 정밀분석 | {check_time}*"""

    return core.CommentAction(
        issue_key=key,
        summary=summary,
        action_type="deliverable_deep_analysis",
        title=f"[{context_label}] 정밀분석: {fname}",
        rationale=(
            f"[{key}] '{fname}' — {context_label} 감지 (등록자: {author}, {created}). "
            f"유형: {dtype}. PDF: {'있음' if has_pdf else '없음'}, 버전: {'있음' if has_version else '없음'}. "
            f"즉시 조치 {len(critical)}건."
        ),
        comment_body=body,
        priority=priority,
        marker=marker,
    )


# ─── 메인 감시 루프 ──────────────────────────────────────────────────────────

def watch_deliverables(
    dry_run: bool,
    review_mode: bool = False,
    reset: bool = False,
    target_key: str | None = None,
) -> dict:
    if reset and os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
        log.info("상태파일 초기화 — 전체 산출물 재분석")

    state = load_seen()
    seen_ids: set[str]      = set(state.get("attachment_ids", []))
    # attachment_versions: {"FM2026-X:filename": {"id": "...", "date": "YYYY-MM-DD"}}
    att_versions: dict      = state.get("attachment_versions", {})

    if not core.JIRA_EMAIL or not core.JIRA_API_TOKEN:
        log.warning("JIRA 인증정보 미설정 → 시뮬레이션 모드")
        issues = _sim_issues()
    else:
        core.verify_jira_auth()
        issues = core.fetch_all_issues()

    new_actions: list[core.CommentAction] = []
    new_ids: list[str]     = []
    new_versions: dict     = {}
    summary_log: list[dict] = []

    for issue in issues:
        if target_key and issue["key"] != target_key:
            continue
        ikey = issue["key"]
        readable, unreadable = core.analyze_attachments(issue)

        for att in readable + unreadable:
            aid      = str(att["id"])
            fname    = att["filename"]
            att_date = att.get("created", "")[:10]  # "YYYY-MM-DD"
            ver_key  = f"{ikey}:{fname}"

            new_ids.append(aid)
            new_versions[ver_key] = {"id": aid, "date": att_date}

            is_new    = aid not in seen_ids
            prev_info = att_versions.get(ver_key)

            # 갱신 감지: 같은 파일명인데 이전과 다른 ID + 더 최근 날짜
            is_changed = (
                not is_new
                and prev_info is not None
                and prev_info.get("id") != aid
                and att_date > prev_info.get("date", "")
            )
            # 신규: 한 번도 본 적 없는 ID
            if is_new and prev_info is None:
                is_new = True
                is_changed = False
            elif is_new and prev_info is not None:
                # 같은 파일명의 새 버전 등록
                is_changed = True
                is_new     = False

            if not is_new and not is_changed:
                continue  # 변동 없음

            action = build_analysis_comment(
                issue, att,
                is_change=is_changed,
                prev_date=prev_info.get("date", "") if prev_info else "",
            )
            # 중복 댓글 방지: 같은 파일·같은 날짜로 이미 댓글이 있으면 건너뜀
            today = datetime.now(KST).strftime("%Y-%m-%d")
            marker_with_date = f"{action.marker} | {today}"
            if not core.already_commented(issue, action.marker):
                new_actions.append(action)
                summary_log.append({
                    "issue": ikey,
                    "file": fname,
                    "type": classify_deliverable(fname)[0],
                    "event": "갱신" if is_changed else "신규",
                })
            else:
                log.info("중복 건너뜀: %s %s (오늘 이미 댓글 있음)", ikey, fname)

    # ── 결과 처리 ─────────────────────────────────────────────────────────
    if review_mode:
        core.print_review_report(new_actions)
    else:
        for act in new_actions:
            core.execute_comment(act, dry_run)

    # ── 상태 갱신 ─────────────────────────────────────────────────────────
    if not dry_run and not review_mode:
        state["attachment_ids"]   = sorted(set(state.get("attachment_ids", [])) | set(new_ids))
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
    """시뮬레이션용 이슈 (신규·갱신 산출물 포함)"""
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
    print("  FM2026 산출물 신규·갱신 감시 결과")
    print(f"  점검 시각: {datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')}")
    print("=" * 70)
    nd = result["new_deliverables"]
    if not nd:
        print("  ✓ 신규·갱신 산출물 없음 (변동사항 없음)")
    else:
        print(f"  감지 산출물 {len(nd)}건:")
        for item in nd:
            print(f"    [{item['issue']}] [{item['event']}] {item['file']}  (유형: {item['type']})")
        print(f"\n  생성된 분석 댓글: {len(result['comments'])}건")
    print("=" * 70 + "\n")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    parser = argparse.ArgumentParser(description="FM2026 산출물 신규·갱신 감시 및 정밀 분석")
    parser.add_argument("--review",        action="store_true", help="근거+미리보기 출력")
    parser.add_argument("--post-comments", action="store_true", help="실제 댓글 등록")
    parser.add_argument("--reset",         action="store_true", help="상태파일 초기화 (전체 재분석)")
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
    )
    if not args.review:
        print_watch_summary(result)


if __name__ == "__main__":
    main()
