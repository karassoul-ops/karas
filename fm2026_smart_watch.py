"""
FM2026 스마트 적응형 감시 시스템 (Smart Adaptive Watch)
=========================================================
[농정원] 2026년 팜맵 갱신 및 활용서비스 운영·개선

■ 동작 원리
  ┌─ 정기 시각(09:00/17:00) 도달
  │   └→ 전체 PMO 점검 + 실시간 감시 재개 (카운터 리셋)
  │
  ├─ 실시간 감시 활성 상태 (1시간 단위)
  │   ├─ 변화 있음 → 요약 보고 + 필수 확인 산출물 알림 + 다음 1시간 후 재검
  │   └─ 변화 없음 → 무변화 카운터 +1
  │       └─ 2회 연속 무변화 → 실시간 감시 일시 중지
  │           └→ 다음 정기 시각까지 대기
  │
  └─ 대기 상태 → 다음 09:00 또는 17:00 도달 시 재개

■ 변화 감지 항목
  - 이슈 상태 변경 (백로그→진행 중→해결됨)
  - 신규 첨부파일 등록
  - 댓글 추가
  - 기한·담당자·우선순위 변경

■ 사업담당자 필수 확인 산출물
  - 산출물 정의서, WBS, 과업대비표, 사업수행계획서 (변경 시 즉시 현행화 알림)

사용법:
  python fm2026_smart_watch.py                 # 1회 점검 (dry-run)
  python fm2026_smart_watch.py --post-comments # 실제 댓글 등록 모드
  python fm2026_smart_watch.py --review        # 결과 미리보기 (댓글 미등록)
  python fm2026_smart_watch.py --reset         # 감시 상태 전체 초기화
"""

from __future__ import annotations

import os
import sys
import json
import argparse
import logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict

import fm2026_pmo_auto as core
import fm2026_deliverable_watch as dwatch

KST  = core.KST
log  = logging.getLogger("smart_watch")

# ─── 상태 파일 ───────────────────────────────────────────────────────────────
WATCH_STATE_FILE = ".fm2026_watch_state.json"

# 무변화 허용 횟수: 이 값 초과 시 감시 중지 플래그
MAX_IDLE_COUNT = 2

# ─── 필수 확인 산출물 목록 (변경 감지 시 강조 알림) ─────────────────────────
MANDATORY_DELIVERABLES = [
    "산출물 정의서", "WBS", "과업대비표", "사업수행계획서",
    "제안요청서", "기술협상", "품질계획서", "운영계획서",
]

# ─── 상태 관리 ───────────────────────────────────────────────────────────────

def _load_watch_state() -> dict:
    if os.path.exists(WATCH_STATE_FILE):
        try:
            return json.load(open(WATCH_STATE_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {
        "idle_count":        0,
        "watching":          True,
        "last_check":        "",
        "last_change":       "",
        "issue_snapshots":   {},   # {key: {status, att_count, comment_count, updated}}
        "total_checks":      0,
        "total_changes":     0,
    }


def _save_watch_state(state: dict) -> None:
    state["last_check"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    with open(WATCH_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ─── 변화 감지 ───────────────────────────────────────────────────────────────

@dataclass
class IssueChange:
    key:     str
    summary: str
    changes: list[str] = field(default_factory=list)   # 변경 항목 설명
    new_attachments: list[str] = field(default_factory=list)
    mandatory_hit: list[str] = field(default_factory=list)  # 필수 확인 산출물
    url: str = ""


def _take_snapshot(issue: dict) -> dict:
    fields = issue["fields"]
    atts   = fields.get("attachment") or []
    cmts   = (fields.get("comment") or {}).get("comments", [])
    return {
        "status":        (fields.get("status") or {}).get("name", ""),
        "att_count":     len(atts),
        "att_ids":       sorted(str(a.get("id", "")) for a in atts),
        "comment_count": len(cmts),
        "duedate":       fields.get("duedate", ""),
        "assignee":      (fields.get("assignee") or {}).get("displayName", ""),
        "priority":      (fields.get("priority") or {}).get("name", ""),
        "updated":       fields.get("updated", ""),
    }


def _detect_changes(issue: dict, old_snap: dict) -> IssueChange | None:
    key     = issue["key"]
    summary = issue["fields"].get("summary", "")
    new_snap = _take_snapshot(issue)
    changes  = []
    new_atts = []
    mandatory_hit = []

    # 상태 변경
    if old_snap["status"] != new_snap["status"]:
        changes.append(f"상태 변경: '{old_snap['status']}' → '{new_snap['status']}'")

    # 신규 첨부파일
    old_ids = set(old_snap.get("att_ids", []))
    new_ids = set(new_snap.get("att_ids", []))
    added_ids = new_ids - old_ids
    if added_ids:
        atts = issue["fields"].get("attachment") or []
        for att in atts:
            if str(att.get("id", "")) in added_ids:
                fname = att.get("filename", "")
                new_atts.append(fname)
                changes.append(f"첨부파일 신규: '{fname}'")
                # 필수 확인 산출물 체크
                for mkey in MANDATORY_DELIVERABLES:
                    if mkey.replace(" ", "") in fname.replace(" ", ""):
                        mandatory_hit.append(fname)

    # 댓글 증가
    delta_cmt = new_snap["comment_count"] - old_snap.get("comment_count", 0)
    if delta_cmt > 0:
        changes.append(f"댓글 {delta_cmt}건 추가")

    # 기한 변경
    if old_snap.get("duedate") != new_snap["duedate"]:
        changes.append(
            f"기한 변경: '{old_snap.get('duedate', '없음')}' → '{new_snap['duedate'] or '없음'}'"
        )

    # 담당자 변경
    if old_snap.get("assignee") != new_snap["assignee"] and new_snap["assignee"]:
        changes.append(
            f"담당자 변경: '{old_snap.get('assignee', '없음')}' → '{new_snap['assignee']}'"
        )

    if not changes:
        return None

    return IssueChange(
        key=key, summary=summary, changes=changes,
        new_attachments=new_atts,
        mandatory_hit=mandatory_hit,
        url=core.issue_url(key),
    )


# ─── 변화 보고서 출력 ────────────────────────────────────────────────────────

def print_change_report(
    changed: list[IssueChange],
    check_time: datetime,
    idle_count: int,
    watching: bool,
) -> None:
    sep = "═" * 72
    print(f"\n{sep}")
    print(f"  FM2026 스마트 감시 점검 결과")
    print(f"  점검 시각: {check_time.strftime('%Y-%m-%d %H:%M KST')}")
    print(sep)

    if not changed:
        print(f"\n  ✅ 변화 없음  (연속 무변화: {idle_count}회/{MAX_IDLE_COUNT}회)")
        if not watching:
            print(f"  💤 연속 {MAX_IDLE_COUNT}회 무변화 — 다음 정기 스케줄에서 재개")
        print()
        return

    print(f"\n  🔔 변화 감지: {len(changed)}건\n")

    # 필수 확인 산출물이 포함된 이슈를 맨 위로 정렬
    mandatory_issues = [c for c in changed if c.mandatory_hit]
    normal_issues    = [c for c in changed if not c.mandatory_hit]

    if mandatory_issues:
        print("  ━━━ 🚨 사업담당자 필수 확인 산출물 ━━━")
        for ic in mandatory_issues:
            _print_issue_change(ic, urgent=True)

    if normal_issues:
        print("  ━━━ 변경 이슈 목록 ━━━")
        for ic in normal_issues:
            _print_issue_change(ic, urgent=False)

    print()


def _print_issue_change(ic: IssueChange, urgent: bool) -> None:
    prefix = "🚨" if urgent else "📌"
    print(f"\n  {prefix} [{ic.key}] {ic.summary}")
    print(f"       링크: {ic.url}")
    for ch in ic.changes:
        print(f"       • {ch}")
    if ic.mandatory_hit:
        print(f"       ⚠️  필수 확인 산출물: {', '.join(ic.mandatory_hit)}")
        print(f"          → WBS·과업대비표·사업수행계획서·산출물 정의서와 정합성 즉시 검토 요망")


# ─── 필수 확인 산출물 댓글 ───────────────────────────────────────────────────

def make_mandatory_check_comment(ic: IssueChange, fname: str) -> core.CommentAction:
    key     = ic.key
    summary = ic.summary
    rationale = (
        f"이슈 [{key}]에 필수 확인 산출물 '{fname}' 신규 등록 감지.\n"
        f"WBS·과업대비표·사업수행계획서 등 핵심 관리 문서와 정합성 즉시 검토 필요."
    )
    body = f"""## 🚨 [PMO 필수확인 | 긴급] 핵심 관리 산출물 등록 — 즉시 검토 요망

**이슈**: [{key}]({ic.url})
**제목**: {summary}
**등록된 산출물**: `{fname}`

---

### ✅ 사업담당자 즉시 확인 체크리스트

아래 문서와의 **정합성을 48시간 이내**에 검토하고 불일치 시 현행화하세요.

| 핵심 문서 | 확인 항목 | 조치 기한 |
|----------|----------|---------|
| **산출물 정의서** | 해당 산출물이 목록에 등재되어 있는지, 버전·제출 기한 일치 여부 | 즉시 |
| **WBS** | 일정·담당자·산출물명 일치 여부, 신규 등록에 따른 완료율 갱신 | 48h |
| **과업대비표** | 제안서 과업 대비 해당 산출물 완료 반영 여부 | 48h |
| **사업수행계획서** | 추진 방향 및 방법론 변경사항 반영 여부 | 48h |

### 📋 산출물 품질 확인 항목
- [ ] 버전 표기 (v1.0 등) 및 작성일 기재
- [ ] 원본 파일 + PDF 동시 등록
- [ ] 팜맵 2026 추진 방향과 일치
- [ ] RFP·기술협상서 해당 요건 충족
- [ ] 상위 문서(요구사항 등)와 추적성 확보

> 본 산출물은 PMO 필수 확인 대상으로 지정되어 있습니다.
> 검토 완료 후 본 댓글에 '확인 완료' 답글을 남겨주세요.

*자동 생성: FM2026 스마트 감시 | {core.TS()}*"""

    return core.CommentAction(
        issue_key=key, summary=summary,
        action_type="mandatory_check",
        title=f"필수확인 산출물: {fname}",
        rationale=rationale,
        comment_body=body,
        priority="긴급",
        marker=f"PMO 필수확인 | 긴급] 핵심 관리 산출물",
    )


# ─── 단일 점검 실행 ──────────────────────────────────────────────────────────

def run_check(state: dict, dry_run: bool) -> tuple[list[IssueChange], bool]:
    """
    단일 점검 실행.
    returns: (변경된 이슈 목록, 변화 있었는지 여부)
    """
    log.info("점검 시작")
    state["total_checks"] = state.get("total_checks", 0) + 1

    # 이슈 조회
    if not core.JIRA_EMAIL or not core.JIRA_API_TOKEN:
        issues = _sim_issues_with_change()
    else:
        issues = core.fetch_all_issues()

    snapshots = state.get("issue_snapshots", {})
    changed:  list[IssueChange] = []
    all_actions: list[core.CommentAction] = []
    new_snapshots: dict = {}

    for issue in issues:
        key      = issue["key"]
        old_snap = snapshots.get(key, {})
        new_snap = _take_snapshot(issue)
        new_snapshots[key] = new_snap

        # 첫 실행(old_snap 없음)이면 변화 없음으로 처리 (베이스라인 기록만)
        if not old_snap:
            continue

        ic = _detect_changes(issue, old_snap)
        if ic:
            changed.append(ic)
            state["total_changes"] = state.get("total_changes", 0) + 1

            for fname in ic.mandatory_hit:
                act = make_mandatory_check_comment(ic, fname)
                if not core.already_commented(issue, act.marker):
                    all_actions.append(act)

            for att in (issue["fields"].get("attachment") or []):
                fname = att.get("filename", "")
                if fname in ic.new_attachments:
                    readable, unreadable = core.analyze_attachments(issue)
                    for a in readable + unreadable:
                        if a["filename"] == fname:
                            act2 = dwatch.analyze_new_deliverable(issue, a)
                            if not core.already_commented(issue, act2.marker):
                                all_actions.append(act2)
                            deep = dwatch.deep_analyze_deliverable(issue, a, is_change=True)
                            if not core.already_commented(issue, deep.marker):
                                all_actions.append(deep)

    # 스냅샷 갱신 (변화 감지에 쓰일 베이스라인)
    state["issue_snapshots"] = new_snapshots

    # 댓글 등록
    for act in all_actions:
        core.execute_comment(act, dry_run)

    # 신규 첨부파일 상태파일도 갱신 (deliverable_watch와 공유)
    if not dry_run:
        dwatch_state = dwatch.load_seen()
        all_att_ids = []
        for issue in issues:
            for att in (issue["fields"].get("attachment") or []):
                all_att_ids.append(str(att.get("id", "")))
        dwatch_state["attachment_ids"] = sorted(set(dwatch_state.get("attachment_ids", [])) | set(all_att_ids))
        dwatch.save_seen(dwatch_state)

    has_change = len(changed) > 0
    if has_change:
        state["last_change"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    return changed, has_change


# ─── 스마트 감시 (1회 실행) ─────────────────────────────────────────────────

def run_smart_watch(dry_run: bool, reset: bool) -> None:
    if reset:
        for f in (WATCH_STATE_FILE, dwatch.STATE_FILE):
            if os.path.exists(f):
                os.remove(f)
        log.info("감시 상태 초기화 완료")

    state = _load_watch_state()
    now   = datetime.now(KST)

    changed, has_change = run_check(state, dry_run)
    if has_change:
        state["idle_count"] = 0
    else:
        state["idle_count"] = state.get("idle_count", 0) + 1

    state["watching"] = state["idle_count"] < MAX_IDLE_COUNT
    _save_watch_state(state)
    print_change_report(changed, now, state["idle_count"], state["watching"])


# ─── 시뮬레이션 데이터 ───────────────────────────────────────────────────────

def _sim_issues_with_change() -> list[dict]:
    """테스트용 — 매 호출마다 약간의 변화를 시뮬레이션"""
    import random
    statuses = ["백로그", "진행 중", "해결됨"]
    return [
        {
            "key": "FM2026-153",
            "fields": {
                "summary": "[시스템개선] 1.분석 - 1.9.요구사항 이해 (면담을 통한 요구사항 도출)",
                "status": {"name": random.choice(statuses)},
                "duedate": "2026-07-10",
                "assignee": {"displayName": "윤병석"},
                "priority": {"name": "보통"},
                "attachment": [
                    {"id": "24289", "filename": "(SYS01_04)면담계획서.hwp",
                     "size": 384512, "created": "2026-06-23T21:32:37+0900",
                     "author": {"displayName": "윤병석"}, "content": "x",
                     "mimeType": "application/x-ole-storage"},
                    {"id": "24290", "filename": "(SYS01_04)면담계획서.pdf",
                     "size": 182956, "created": "2026-06-23T21:32:37+0900",
                     "author": {"displayName": "윤병석"}, "content": "x",
                     "mimeType": "application/pdf"},
                ] + ([{
                    "id": "99999", "filename": "WBS_v1.1.xlsx",
                    "size": 204800, "created": "2026-06-24T09:00:00+0900",
                    "author": {"displayName": "윤병석"}, "content": "x",
                    "mimeType": "application/vnd.ms-excel",
                }] if random.random() > 0.5 else []),
                "comment": {"comments": []},
                "description": None,
                "updated": datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+0900"),
            },
        },
        {
            "key": "FM2026-150",
            "fields": {
                "summary": "[시스템개선] 1.분석 - 1.1.단계 준비",
                "status": {"name": "해결됨"},
                "duedate": "2026-06-12",
                "assignee": {"displayName": "윤병석"},
                "priority": {"name": "보통"},
                "attachment": [],
                "comment": {"comments": []},
                "description": None,
                "updated": "2026-06-12T10:00:00+0900",
            },
        },
    ]


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    parser = argparse.ArgumentParser(
        description="FM2026 스마트 적응형 감시 시스템",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--post-comments", action="store_true",
                        help="실제 Jira 댓글 등록")
    parser.add_argument("--review",        action="store_true",
                        help="미리보기 (댓글 미등록, 결과만 출력)")
    parser.add_argument("--reset",         action="store_true",
                        help="감시 상태 전체 초기화")
    args = parser.parse_args()

    dry_run = not args.post_comments

    if args.post_comments:
        log.info("=== LIVE 모드 — 실제 Jira 댓글 등록 ===")
    else:
        log.info("=== DRY-RUN 모드 (기본) — 댓글 미등록 ===")

    run_smart_watch(dry_run=dry_run, reset=args.reset)


if __name__ == "__main__":
    main()
