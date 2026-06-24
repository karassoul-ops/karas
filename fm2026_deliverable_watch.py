"""
FM2026 산출물 신규 등록 감지 및 정밀 분석 모듈
================================================
1시간 단위로 FM2026 프로젝트를 점검하여 **새로 등록된 산출물(첨부파일)**을 감지하고,
해당 산출물을 정밀 분석하여 개선·수정·변경 사항을 Jira 댓글로 작성합니다.

동작 원리:
  1. 전체 이슈의 첨부파일 목록을 조회
  2. 상태파일(.fm2026_seen_attachments.json)과 비교하여 신규 등록분만 추출
  3. 신규 산출물의 파일명·유형·문서단계를 분석
  4. 산출물 유형별 정밀 분석 댓글 생성 (개선/수정/변경 사항 제시)
  5. 상태파일 갱신 (다음 실행 시 중복 분석 방지)

사용법:
  python fm2026_deliverable_watch.py                 # dry-run (미리보기)
  python fm2026_deliverable_watch.py --review        # 근거+미리보기
  python fm2026_deliverable_watch.py --post-comments # 실제 댓글 등록
  python fm2026_deliverable_watch.py --reset         # 상태파일 초기화 (전체 재분석)

스케줄러(1시간 단위)는 fm2026_pmo_scheduler.py --watch 사용.
"""

from __future__ import annotations

import os
import sys
import json
import argparse
import logging
from datetime import datetime, timezone, timedelta

# 기존 PMO 모듈 재사용 (API 헬퍼, 유틸리티)
import fm2026_pmo_auto as core

KST = core.KST
log = logging.getLogger("deliverable_watch")

STATE_FILE = ".fm2026_seen_attachments.json"

# ─── 산출물 유형 분류 규칙 ───────────────────────────────────────────────────
# (키워드, 산출물유형, 분석 관점) 순
DELIVERABLE_RULES = [
    ("면담계획서",   "요구사항/면담",   ["면담 대상·일정의 발주처 협의 완료 여부", "면담 항목이 RFP 요구사항과 매핑되는지"]),
    ("면담결과서",   "요구사항/면담",   ["면담 결과가 요구사항정의서로 연계되는지", "증빙화면·회의록 첨부 여부"]),
    ("요구사항정의서", "요구사항",       ["기능/비기능 요구사항 분리 여부", "RFP·제안서 요구사항 추적표(RTM) 연계"]),
    ("요구사항추적",  "요구사항",       ["요구사항 ID 체계 일관성", "설계·테스트 단계 추적 가능 여부"]),
    ("화면설계",     "설계",           ["화면 ID 표준 준수", "팜맵 UI/UX 가이드 반영 여부"]),
    ("프로그램설계",  "설계",           ["모듈 분할의 적정성", "인터페이스 정의 명확성"]),
    ("DB설계",      "설계",           ["테이블/컬럼 표준 준수", "팜맵 공간데이터 스키마 정합성"]),
    ("ERD",        "설계",           ["엔티티 관계 정규화", "공간컬럼(geometry) 정의"]),
    ("테스트",      "테스트",         ["테스트케이스 커버리지", "단위/통합 테스트 구분"]),
    ("WBS",        "관리/계획",       ["일정과 Jira Due Date 동기화 여부", "마일스톤 누락 여부"]),
    ("과업대비표",   "관리/계획",       ["제안서 과업 대비 누락 항목", "변경 과업 반영 여부"]),
    ("사업수행계획서", "관리/계획",      ["추진 일정·조직·산출물 최신화", "팜맵 추진 방향 일치 여부"]),
    ("산출물 정의서", "관리/계획",      ["전체 산출물 목록 완전성", "단계별 산출물 매핑"]),
    ("운영계획",     "운영",           ["운영 조직·절차 정의", "장애대응·백업 계획 포함"]),
    ("품질",        "품질",           ["품질지표 정의", "검토·승인 절차 명시"]),
    ("회의록",      "공통",           ["의사결정 사항 명시", "액션아이템·담당자·기한 기재"]),
    ("보고서",      "공통",           ["보고 목적·결론 명확성", "근거 데이터 첨부"]),
]


# ─── 상태 관리 ───────────────────────────────────────────────────────────────

def load_seen() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("상태파일 로드 실패(%s) → 초기화", e)
    return {"attachment_ids": [], "last_run": ""}


def save_seen(state: dict) -> None:
    state["last_run"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ─── 산출물 분석 ─────────────────────────────────────────────────────────────

def classify_deliverable(filename: str) -> tuple[str, list[str]]:
    """파일명 → (산출물유형, 분석관점 목록)"""
    name = filename.replace(" ", "")
    for keyword, dtype, perspectives in DELIVERABLE_RULES:
        if keyword.replace(" ", "") in name:
            return dtype, perspectives
    return "일반 산출물", ["문서 목적·범위 명확성", "관련 산출물과의 정합성"]


def analyze_new_deliverable(issue: dict, att: dict) -> core.CommentAction:
    """신규 산출물 1건에 대한 정밀 분석 댓글 생성"""
    key      = issue["key"]
    summary  = issue["fields"].get("summary", "")
    fname    = att["filename"]
    ext      = att["ext"]
    author   = att["author"]
    created  = att["created"]
    dtype, perspectives = classify_deliverable(fname)

    # 동일 이슈 내 PDF 짝 존재 여부
    all_atts = issue["fields"].get("attachment") or []
    stems = {
        (a.get("filename", "").rsplit(".", 1)[0] if "." in a.get("filename", "") else a.get("filename", ""))
        for a in all_atts
        if a.get("filename", "").lower().endswith(".pdf")
    }
    my_stem = fname.rsplit(".", 1)[0] if "." in fname else fname
    has_pdf_pair = (ext == ".pdf") or (my_stem in stems)

    pdf_note = (
        "원본+PDF 동시 등록 확인됨 ✓"
        if has_pdf_pair
        else "⚠️ PDF 미등록 — 원본과 함께 PDF도 등록 필요"
    )

    perspective_lines = "\n".join(f"  - [ ] {p}" for p in perspectives)

    rationale = (
        f"이슈 [{key}]에 신규 산출물 '{fname}' 등록 감지 (등록자: {author}, {created}).\n"
        f"산출물 유형: {dtype}. 정밀 분석 후 개선/수정/변경 사항 제시 필요.\n"
        f"PDF 동시등록: {'예' if has_pdf_pair else '아니오'}"
    )

    body = f"""## 🔍 [PMO 자동분석 | 산출물] 신규 등록 산출물 정밀 분석

**이슈**: [{key}]({core.issue_url(key)})
**제목**: {summary}
**신규 산출물**: `{fname}`  ({att['size']//1024}KB)
**산출물 유형**: {dtype}  |  **등록자**: {author}  |  **등록일**: {created}
**PDF 동시등록**: {pdf_note}

---

### 📋 정밀 분석 체크리스트 ({dtype})
아래 관점에서 산출물을 검토하고 보완해 주세요.
{perspective_lines}

### 🔧 개선·수정·변경 권고 (공통)
| 구분 | 점검 항목 |
|------|----------|
| **개선** | 팜맵 2026 추진 방향(갱신·활용서비스 운영·개선)과 일치하는지 |
| **수정** | RFP·제안서·기술협상서 요건과 불일치하는 내용은 없는지 |
| **변경** | 과업 변경사항이 본 산출물에 반영되었는지 (미반영 시 현행화) |
| **정합성** | WBS·과업대비표·산출물 정의서와 상호 모순 없는지 |
| **추적성** | 상위 요구사항 → 본 산출물 → 하위 단계로 추적 가능한지 |

### ✅ 등록 전 최종 확인
- [ ] 문서 버전(v1.0 등) 및 작성일자 기재
- [ ] 원본 파일 + PDF 동시 등록
- [ ] 검토자/승인자 서명란 포함 (해당 시)
- [ ] 관련 산출물 상호 참조 정확성

> 본 분석은 자동 생성된 검토 가이드입니다. 사업담당자가 내용 확인 후
> 실제 보완 여부를 판단하시기 바랍니다.

*자동 생성: FM2026 PMO 산출물 감시 | {core.TS()}*"""

    return core.CommentAction(
        issue_key=key, summary=summary,
        action_type="deliverable_analysis",
        title=f"신규 산출물 분석: {fname}",
        rationale=rationale,
        comment_body=body,
        priority="보통",
        marker=f"PMO 자동분석 | 산출물] 신규 등록 산출물 정밀 분석",
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
    seen_ids = set(state.get("attachment_ids", []))

    # Jira 조회 (인증 없으면 시뮬레이션)
    if not core.JIRA_EMAIL or not core.JIRA_API_TOKEN:
        log.warning("JIRA 인증정보 미설정 → 시뮬레이션 모드")
        issues = _sim_issues()
    else:
        issues = core.fetch_all_issues()

    new_actions: list[core.CommentAction] = []
    new_ids: list[str] = []
    summary_log: list[dict] = []

    for issue in issues:
        if target_key and issue["key"] != target_key:
            continue
        atts = issue["fields"].get("attachment") or []
        readable, unreadable = core.analyze_attachments(issue)
        for att in readable + unreadable:
            aid = str(att["id"])
            new_ids.append(aid)
            if aid in seen_ids:
                continue  # 기존 산출물 → 건너뜀
            # 신규 산출물 발견
            action = analyze_new_deliverable(issue, att)
            if not core.already_commented(issue, action.marker + f" {att['filename']}"):
                # 동일 파일에 대한 분석 댓글이 이미 없으면 추가
                new_actions.append(action)
            summary_log.append({
                "issue": issue["key"],
                "file": att["filename"],
                "type": classify_deliverable(att["filename"])[0],
            })

    # 결과 처리
    if review_mode:
        core.print_review_report(new_actions)
    else:
        for act in new_actions:
            core.execute_comment(act, dry_run)

    # 상태 갱신 (실제 등록 모드일 때만 seen 갱신, dry-run/review는 보존)
    if not dry_run and not review_mode:
        state["attachment_ids"] = sorted(set(state.get("attachment_ids", [])) | set(new_ids))
        save_seen(state)
        log.info("상태파일 갱신: 총 %d개 산출물 추적 중", len(state["attachment_ids"]))
    else:
        log.info("(dry-run/review 모드 — 상태파일 미갱신, 신규 %d건 감지)", len(summary_log))

    return {
        "new_deliverables": summary_log,
        "comments": [{"issue": a.issue_key, "title": a.title} for a in new_actions],
    }


def _sim_issues() -> list[dict]:
    """시뮬레이션용 신규 산출물 포함 이슈"""
    return [
        {
            "key": "FM2026-153",
            "fields": {
                "summary": "[시스템개선] 1.분석 - 1.9.요구사항 이해 (면담을 통한 요구사항 도출)",
                "status": {"name": "진행 중"},
                "attachment": [
                    {"id": "24289", "filename": "(SYS01_04)면담계획서.hwp", "size": 384512,
                     "created": "2026-06-23T21:32:37+0900", "author": {"displayName": "윤병석"},
                     "content": "x", "mimeType": "application/x-ole-storage"},
                    {"id": "24290", "filename": "(SYS01_04)면담계획서.pdf", "size": 182956,
                     "created": "2026-06-23T21:32:37+0900", "author": {"displayName": "윤병석"},
                     "content": "x", "mimeType": "application/pdf"},
                ],
                "comment": {"comments": []},
                "description": None,
                "updated": "2026-06-23",
                "duedate": "2026-07-10",
            },
        },
        {
            "key": "FM2026-160",
            "fields": {
                "summary": "[시스템개선] 2.설계 - 2.2.화면 설계",
                "status": {"name": "진행 중"},
                "attachment": [
                    {"id": "30001", "filename": "화면설계서_v1.0.pptx", "size": 1048576,
                     "created": "2026-06-24T10:00:00+0900", "author": {"displayName": "윤병석"},
                     "content": "x", "mimeType": "application/vnd.ms-powerpoint"},
                ],
                "comment": {"comments": []},
                "description": None,
                "updated": "2026-06-24",
                "duedate": "2026-08-07",
            },
        },
    ]


def print_watch_summary(result: dict) -> None:
    print("\n" + "=" * 70)
    print("  FM2026 산출물 신규 등록 감시 결과")
    print(f"  점검 시각: {datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')}")
    print("=" * 70)
    nd = result["new_deliverables"]
    if not nd:
        print("  ✓ 신규 등록된 산출물 없음 (변동사항 없음)")
    else:
        print(f"  신규 산출물 {len(nd)}건 감지:")
        for item in nd:
            print(f"    [{item['issue']}] {item['file']}  (유형: {item['type']})")
        print(f"\n  생성된 분석 댓글: {len(result['comments'])}건")
    print("=" * 70 + "\n")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    parser = argparse.ArgumentParser(description="FM2026 산출물 신규 등록 감시 및 분석")
    parser.add_argument("--review",        action="store_true", help="근거+미리보기 출력")
    parser.add_argument("--post-comments", action="store_true", help="실제 댓글 등록")
    parser.add_argument("--reset",         action="store_true", help="상태파일 초기화 (전체 재분석)")
    parser.add_argument("--issue",         metavar="KEY", help="특정 이슈만")
    args = parser.parse_args()

    dry_run = not args.post_comments

    if args.post_comments:
        log.info("=== LIVE 모드 — 신규 산출물 분석 댓글 실제 등록 ===")
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
