"""
FM2026 팜맵 사업 PMO 자동화 프로그램 v2.0
==========================================
[농정원] 2026년 팜맵 갱신 및 활용서비스 운영·개선

기능 목록:
  1. 신규·변경·수정 이슈/첨부파일/댓글 정밀 검토
  2. 사업진척사항 점검 (기간 남은 세부사업은 관찰만)
  3. 읽을 수 없는 파일 → 경로·바로가기 링크 제공 + 원본·PDF 동시 등록 요청 댓글
  4. 산출물 정밀 분석 → 제안서·RFP·기술협상서·WBS·과업대비표·사업수행계획서·산출물 정의서와 비교·조치 댓글
  5. 주요 산출물 현행화 집중 체크 → 현행화 요구 댓글 (변경사항 발생 시 즉시 요구)
  6. 댓글 등록 전 근거 제시 → 사업담당자 최종 검토 후 등록 여부 결정 지원 (dry-run 리뷰 모드)
  7. 만료 6시간 이내 이슈 → 산출물 갱신·수정·보완 요청 댓글
  8. 만료기간 미등록 이슈 → 기한 등록 요청 댓글
  9. 2025→2026 개선 세부사업 → As-Is/To-Be 산출물 포함 지시 댓글
 10. PMO 총괄 관리 리포트 (누락 사업·산출물 없게 관리)
 11. 사업 제언 출력

필수 환경변수:
  JIRA_EMAIL      : Jira 로그인 이메일 (예: user@company.com)
  JIRA_API_TOKEN  : Jira API 토큰 (https://id.atlassian.com/manage-profile/security/api-tokens)
  JIRA_SITE       : Jira 사이트 (기본값: optai.atlassian.net)

사용법:
  python fm2026_pmo_auto.py                   # dry-run 미리보기 (기본)
  python fm2026_pmo_auto.py --review          # 근거 포함 전체 검토 리포트 출력
  python fm2026_pmo_auto.py --post-comments   # 실제 Jira 댓글 등록
  python fm2026_pmo_auto.py --issue FM2026-153  # 특정 이슈만
  python fm2026_pmo_auto.py --since 2026-06-01  # 특정 날짜 이후 업데이트된 이슈만
"""

from __future__ import annotations

import os
import sys
import json
import argparse
import logging
import urllib.request
import urllib.error
import urllib.parse
import base64
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional

# ─── 전역 설정 ───────────────────────────────────────────────────────────────

PROJECT_KEY        = "FM2026"
JIRA_SITE          = os.environ.get("JIRA_SITE", "optai.atlassian.net")
JIRA_EMAIL         = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN     = os.environ.get("JIRA_API_TOKEN", "")
JIRA_BASE_URL      = f"https://{JIRA_SITE}"
API_BASE           = f"{JIRA_BASE_URL}/rest/api/3"
KST                = timezone(timedelta(hours=9))
EXPIRY_ALERT_HOURS = 6

# 주요 산출물 문서 유형 (현행화 체크 대상)
KEY_DELIVERABLE_KEYWORDS = [
    "산출물 정의서", "WBS", "과업대비표", "사업수행계획서",
    "제안서", "제안요청서", "기술협상", "운영계획", "품질계획",
]

# 2025→2026 개선 대상 식별 키워드
ASIS_TOBE_KEYWORDS = [
    "개선", "고도화", "전환", "운영", "갱신", "클라우드", "분석",
    "현행화", "재구축", "업그레이드", "리뉴얼",
]

# 읽기 지원 파일 확장자
READABLE_EXT = {
    ".pdf", ".xlsx", ".xls", ".docx", ".doc",
    ".pptx", ".ppt", ".hwp", ".hwpx",
    ".jpg", ".jpeg", ".png", ".gif",
    ".txt", ".csv", ".md", ".zip",
}

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("fm2026_pmo_auto.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ─── 데이터 구조 ─────────────────────────────────────────────────────────────

@dataclass
class CommentAction:
    issue_key: str
    summary: str
    action_type: str          # 댓글 유형 코드
    title: str                # 사업담당자용 제목
    rationale: str            # 근거 (검토 전 제시)
    comment_body: str         # 실제 Jira 등록할 댓글 내용
    priority: str = "보통"    # 긴급/높음/보통/낮음
    marker: str = ""          # 중복 방지 마커 (댓글 내 포함 문자열)


@dataclass
class PMOReport:
    generated_at: str = ""
    total_issues: int = 0
    completed: list = field(default_factory=list)
    in_progress: list = field(default_factory=list)
    backlog: list = field(default_factory=list)
    no_due_date: list = field(default_factory=list)
    expired: list = field(default_factory=list)
    alert_6h: list = field(default_factory=list)
    unreadable_files: list = field(default_factory=list)
    asis_tobe_needed: list = field(default_factory=list)
    currentization_needed: list = field(default_factory=list)  # 현행화 필요
    pending_actions: list = field(default_factory=list)        # 검토 대기 댓글
    posted_comments: list = field(default_factory=list)
    skipped_comments: list = field(default_factory=list)
    note: str = ""


# ─── Jira REST API 헬퍼 ──────────────────────────────────────────────────────

def _auth_header() -> str:
    cred = f"{JIRA_EMAIL}:{JIRA_API_TOKEN}"
    return "Basic " + base64.b64encode(cred.encode()).decode()


def jira_get(path: str, params: dict | None = None) -> dict:
    url = API_BASE + path
    if params:
        qs = "&".join(
            f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items()
        )
        url += "?" + qs
    req = urllib.request.Request(
        url,
        headers={"Authorization": _auth_header(), "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def jira_post_comment(issue_key: str, body_text: str) -> dict:
    url = f"{API_BASE}/issue/{issue_key}/comment"
    payload = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": body_text}],
                }
            ],
        }
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": _auth_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch_all_issues(since_date: str | None = None) -> list[dict]:
    """FM2026 전체 이슈 조회 (since_date: 'YYYY-MM-DD' 이후 업데이트된 이슈만)"""
    issues: list[dict] = []
    start = 0
    max_results = 100
    fields = (
        "summary,status,issuetype,assignee,reporter,created,updated,"
        "duedate,priority,description,comment,attachment,labels,components,parent"
    )
    jql_base = f'project = "{PROJECT_KEY}"'
    if since_date:
        jql_base += f' AND updated >= "{since_date}"'
    jql = jql_base + " ORDER BY updated DESC"

    while True:
        data = jira_get("/search", {
            "jql": jql,
            "startAt": start,
            "maxResults": max_results,
            "fields": fields,
        })
        batch = data.get("issues", [])
        issues.extend(batch)
        total = data.get("total", 0)
        log.info("이슈 조회 중: %d / %d", len(issues), total)
        if start + max_results >= total:
            break
        start += max_results
    return issues


# ─── 유틸리티 ────────────────────────────────────────────────────────────────

def issue_url(key: str) -> str:
    return f"{JIRA_BASE_URL}/browse/{key}"


def now_kst() -> datetime:
    return datetime.now(KST)


def extract_text_from_adf(node: dict | None) -> str:
    """Atlassian Document Format → 평문 텍스트 추출"""
    if not node:
        return ""
    if isinstance(node, str):
        return node
    text = ""
    if node.get("type") == "text":
        text += node.get("text", "")
    for child in node.get("content", []):
        text += extract_text_from_adf(child)
    return text


def already_commented(issue: dict, marker: str) -> bool:
    """동일 유형의 댓글이 이미 존재하는지 확인 (중복 방지)"""
    comments = (issue["fields"].get("comment") or {}).get("comments", [])
    for c in comments:
        body = c.get("body", "")
        body_text = extract_text_from_adf(body) if isinstance(body, dict) else str(body)
        if marker in body_text:
            return True
    return False


def check_expiry(issue: dict, now: datetime) -> tuple[str, timedelta | None]:
    """
    만료 상태 반환:
      'no_due'  → 기한 없음
      'expired' → 기한 초과
      'alert'   → 6시간 이내
      'ok'      → 정상
    """
    due = issue["fields"].get("duedate")
    if not due:
        return "no_due", None
    due_dt = datetime.strptime(due, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=KST
    )
    delta = due_dt - now
    if delta.total_seconds() < 0:
        return "expired", delta
    if delta.total_seconds() <= EXPIRY_ALERT_HOURS * 3600:
        return "alert", delta
    return "ok", delta


def analyze_attachments(issue: dict) -> tuple[list[dict], list[dict]]:
    """첨부파일을 (읽기가능, 읽기불가) 두 목록으로 분류"""
    atts = issue["fields"].get("attachment") or []
    readable, unreadable = [], []
    has_pdf: set[str] = set()

    for att in atts:
        name = att.get("filename", "")
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        mime = att.get("mimeType", "")
        is_readable = ext in READABLE_EXT or "image" in mime or "pdf" in mime
        stem = name.rsplit(".", 1)[0] if "." in name else name
        if ext == ".pdf":
            has_pdf.add(stem)
        info = {
            "id":       att.get("id"),
            "filename": name,
            "stem":     stem,
            "ext":      ext,
            "size":     att.get("size", 0),
            "created":  att.get("created", "")[:10],
            "author":   (att.get("author") or {}).get("displayName", ""),
            "url":      att.get("content", ""),
            "mime":     mime,
        }
        if is_readable:
            readable.append(info)
        else:
            unreadable.append(info)

    # 원본은 있으나 PDF 없는 파일 표시
    for att_info in readable + unreadable:
        att_info["pdf_missing"] = (
            att_info["ext"] not in (".pdf",)
            and att_info["stem"] not in has_pdf
        )
    return readable, unreadable


def needs_asis_tobe(issue: dict) -> bool:
    """2025→2026 개선 대상 이슈 판단"""
    text = issue["fields"].get("summary", "") + " " + extract_text_from_adf(
        issue["fields"].get("description")
    )
    return any(kw in text for kw in ASIS_TOBE_KEYWORDS)


def needs_currentization(issue: dict) -> tuple[bool, list[str]]:
    """주요 산출물 현행화 필요 여부 판단 → (True/False, 해당 키워드 목록)"""
    text = issue["fields"].get("summary", "") + " " + extract_text_from_adf(
        issue["fields"].get("description")
    )
    matched = [kw for kw in KEY_DELIVERABLE_KEYWORDS if kw in text]
    return bool(matched), matched


# ─── 댓글 생성 (근거 + 본문 포함) ───────────────────────────────────────────

TS = lambda: datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")


def _comment_header(action_type: str, priority: str) -> str:
    icons = {"긴급": "🚨", "높음": "🔴", "보통": "🟡", "낮음": "🟢"}
    icon = icons.get(priority, "📌")
    return f"## {icon} [PMO 자동알림 | {priority}] {action_type}"


def make_unreadable_comment(issue: dict, unreadable: list[dict], readable: list[dict]) -> CommentAction:
    key     = issue["key"]
    summary = issue["fields"].get("summary", "")
    url     = issue_url(key)

    # 원본만 있고 PDF 없는 파일 (읽기 가능 파일 포함)
    need_pdf = [a for a in (readable + unreadable) if a.get("pdf_missing")]

    lines_unread = "\n".join(
        f"  - `{a['filename']}` | {a['size']//1024}KB | 등록자: {a['author']} | {a['created']}"
        for a in unreadable
    ) or "  (없음)"
    lines_pdf = "\n".join(
        f"  - `{a['filename']}` → PDF 버전 미등록"
        for a in need_pdf
    ) or "  (없음)"

    rationale = (
        f"이슈 [{key}]에 지원되지 않는 형식의 첨부파일 {len(unreadable)}건 발견.\n"
        f"또한 원본 파일만 있고 PDF 버전이 없는 파일 {len(need_pdf)}건 확인.\n"
        f"바로가기: {url}"
    )
    body = f"""{_comment_header("첨부파일 확인 및 PDF 동시 등록 요청", "높음")}

**이슈**: [{key}]({url})
**제목**: {summary}

---

### 📂 읽기 불가 첨부파일 (사업관리자 확인 필요)
{lines_unread}

**바로가기**: [{key} 첨부파일 탭으로 이동]({url})

### 📄 PDF 미등록 파일 (원본과 PDF를 함께 등록해주세요)
{lines_pdf}

---

### 조치 방법
1. 위 링크에서 해당 파일 직접 확인
2. 읽기 불가 파일은 정상 형식으로 **재업로드**
3. 모든 산출물은 **원본 파일(hwp·xlsx 등) + PDF** 두 가지를 동시에 등록
   - PDF 등록 이유: 뷰어 없이도 열람 가능, 납품·보고 시 표준 형식
4. 조치 완료 후 본 댓글에 답글로 완료 알림

*자동 생성: FM2026 PMO | {TS()}*"""

    return CommentAction(
        issue_key=key, summary=summary,
        action_type="unreadable_file",
        title="읽기 불가 파일 + PDF 미등록",
        rationale=rationale,
        comment_body=body,
        priority="높음",
        marker="PMO 자동알림 | 높음] 첨부파일 확인",
    )


def make_expiry_alert_comment(issue: dict, delta: timedelta) -> CommentAction:
    key     = issue["key"]
    summary = issue["fields"].get("summary", "")
    due     = issue["fields"].get("duedate", "")
    hrs     = delta.total_seconds() / 3600
    assignee = (issue["fields"].get("assignee") or {}).get("displayName", "미지정")

    rationale = (
        f"이슈 [{key}] 만료까지 약 {hrs:.1f}시간 남음 (기한: {due}).\n"
        f"담당자: {assignee}. 산출물 미등록 시 PMO 지연 이슈로 기록됩니다."
    )
    body = f"""{_comment_header("만료 임박 경보 (6시간 이내)", "긴급")}

**이슈**: [{key}]({issue_url(key)})
**제목**: {summary}
**만료일**: {due}  |  **남은 시간**: 약 {hrs:.1f}시간  |  **담당자**: {assignee}

---

### 즉시 조치 항목 (오늘 중 하나 선택)

| 우선순위 | 조치 | 방법 |
|---------|------|------|
| 1 | 산출물 등록 | 완성된 산출물을 첨부파일로 등록 (원본+PDF) 후 상태 → **해결됨** |
| 2 | 진행현황 갱신 | 현재 진행률(%), 잔여작업, 완료 예정일을 댓글로 작성 |
| 3 | 기간 변경 요청 | 연장 사유 작성 → PM 승인 요청 후 Due Date 수정 |

### 산출물 등록 시 포함해야 할 내용
- 작업 완료 내역 요약
- 확인/검토 필요 사항
- 다음 단계 연계 작업 여부

> ⚠️ PMO 기준: 만료 6시간 이내 미조치 → 주간 지연 이슈 보고에 자동 포함

*자동 생성: FM2026 PMO | {TS()}*"""

    return CommentAction(
        issue_key=key, summary=summary,
        action_type="expiry_alert",
        title="만료 임박 경보",
        rationale=rationale,
        comment_body=body,
        priority="긴급",
        marker="PMO 자동알림 | 긴급] 만료 임박",
    )


def make_expired_comment(issue: dict, delta: timedelta) -> CommentAction:
    key     = issue["key"]
    summary = issue["fields"].get("summary", "")
    due     = issue["fields"].get("duedate", "")
    days    = abs(int(delta.total_seconds() / 86400))
    assignee = (issue["fields"].get("assignee") or {}).get("displayName", "미지정")

    rationale = (
        f"이슈 [{key}] 기한({due})이 {days}일 초과됨. 담당자: {assignee}.\n"
        f"산출물 미등록 또는 상태 미변경 상태. 즉각 조치 필요."
    )
    body = f"""{_comment_header("기간 초과 이슈", "긴급")}

**이슈**: [{key}]({issue_url(key)})
**제목**: {summary}
**만료일**: {due}  |  **초과**: {days}일  |  **담당자**: {assignee}

---

### 즉시 처리 필요

- [ ] **완료된 경우**: 산출물 첨부 등록(원본+PDF) → 상태를 **해결됨**으로 변경
- [ ] **진행 중인 경우**: 사유 및 완료 예정일을 댓글 작성 → PM 기간 연장 승인 요청
- [ ] **취소할 경우**: PM 승인 후 상태를 **취소됨**으로 변경

### 기간 연장 요청 시 포함 내용
1. 지연 사유 (구체적으로)
2. 현재 진행률 (%)
3. 새로운 완료 예정일
4. 완료를 위한 필요 지원사항

> PMO는 기간초과 이슈를 **매주 사업담당자에게 보고**합니다.
> 조치 완료 후 반드시 본 댓글에 답글을 남겨주세요.

*자동 생성: FM2026 PMO | {TS()}*"""

    return CommentAction(
        issue_key=key, summary=summary,
        action_type="expired",
        title="기간 초과",
        rationale=rationale,
        comment_body=body,
        priority="긴급",
        marker="PMO 자동알림 | 긴급] 기간 초과",
    )


def make_no_duedate_comment(issue: dict) -> CommentAction:
    key     = issue["key"]
    summary = issue["fields"].get("summary", "")
    assignee = (issue["fields"].get("assignee") or {}).get("displayName", "미지정")

    rationale = (
        f"이슈 [{key}]에 Due Date(기한)가 미등록됨. 담당자: {assignee}.\n"
        f"WBS 일정 기준으로 기한 등록이 필요합니다."
    )
    body = f"""{_comment_header("만료기간 미등록", "높음")}

**이슈**: [{key}]({issue_url(key)})
**제목**: {summary}
**담당자**: {assignee}

---

### 조치 요청: 기한(Due Date) 등록

1. 이슈 우측 상세 패널의 **기한(Due date)** 필드 클릭
2. WBS / 사업수행계획서 일정과 동일한 날짜 입력
3. 입력 후 본 댓글에 답글로 완료 알림

### 기한 등록이 중요한 이유
| 항목 | 설명 |
|------|------|
| 일정관리 | PMO 자동화 도구의 만료 알림 기준 |
| 진척률 산정 | 사업 진척 보고서 자동 계산 기준 |
| 감사 증빙 | 계획 대비 실적 비교 핵심 자료 |

> 기한 미등록 이슈는 PMO 진척 보고에서 자동 제외되어 누락으로 처리될 수 있습니다.

*자동 생성: FM2026 PMO | {TS()}*"""

    return CommentAction(
        issue_key=key, summary=summary,
        action_type="no_duedate",
        title="만료기간 미등록",
        rationale=rationale,
        comment_body=body,
        priority="높음",
        marker="PMO 자동알림 | 높음] 만료기간 미등록",
    )


def make_asis_tobe_comment(issue: dict) -> CommentAction:
    key     = issue["key"]
    summary = issue["fields"].get("summary", "")

    rationale = (
        f"이슈 [{key}]의 제목/설명에 개선·전환·운영 등 2025→2026 연계 키워드 포함.\n"
        f"산출물에 As-Is/To-Be 분석이 포함되어야 사업 연속성 및 감사 요건 충족."
    )
    body = f"""{_comment_header("As-Is / To-Be 산출물 포함 지시", "높음")}

**이슈**: [{key}]({issue_url(key)})
**제목**: {summary}

---

### 2025→2026 개선 사업: 산출물 필수 포함 항목

| 구분 | 포함 내용 | 비고 |
|------|----------|------|
| **As-Is** | 2025년 현행 현황 (기능·구조·문제점·개선 필요사항) | 전년도 산출물 참조 |
| **To-Be** | 2026년 목표 상태 (개선 목표·기대효과·변경 범위) | RFP 요건 기준 |
| **Gap 분석** | As-Is↔To-Be 차이점 및 전환 전략 | |
| **개선 근거** | 제안요청서·기술협상서 해당 요건 인용 | 문서명·페이지 명시 |

### 참고 문서 (아래 문서와 정합성 확인 필수)
- 📄 제안요청서(RFP) 해당 세부항목
- 📄 기술협상 결과서
- 📄 2025년 사업수행 결과 보고서 (전년도 As-Is 자료)
- 📄 과업대비표 (현재 버전)

### 작성 순서 권장
1. 2025년 결과물 검토 → As-Is 현황 정리
2. RFP·기술협상서 요건 확인 → To-Be 목표 도출
3. Gap 분석 작성
4. 산출물 초안 → PM 검토 → 확정 후 첨부파일 등록

> 팜맵 2026 사업 특성상 As-Is/To-Be 없이는 **개선 근거 부재**로 감사 지적 대상이 됩니다.

*자동 생성: FM2026 PMO | {TS()}*"""

    return CommentAction(
        issue_key=key, summary=summary,
        action_type="asis_tobe",
        title="As-Is/To-Be 산출물 포함 지시",
        rationale=rationale,
        comment_body=body,
        priority="높음",
        marker="PMO 자동알림 | 높음] As-Is / To-Be",
    )


def make_currentization_comment(issue: dict, matched_keywords: list[str]) -> CommentAction:
    key      = issue["key"]
    summary  = issue["fields"].get("summary", "")
    kw_str   = ", ".join(f"'{k}'" for k in matched_keywords)
    updated  = issue["fields"].get("updated", "")[:10]
    atts     = (issue["fields"].get("attachment") or [])
    att_list = "\n".join(
        f"  - `{a.get('filename', '')}` (등록일: {(a.get('created','')[:10])})"
        for a in atts
    ) or "  (등록된 첨부파일 없음)"

    rationale = (
        f"이슈 [{key}]에서 주요 산출물 키워드({kw_str}) 감지됨.\n"
        f"이슈 최종 수정일: {updated}. 첨부파일 현행화 여부 확인 필요.\n"
        f"현재 첨부파일:\n{att_list}"
    )
    body = f"""{_comment_header("주요 산출물 현행화 요구", "높음")}

**이슈**: [{key}]({issue_url(key)})
**제목**: {summary}
**감지된 주요 산출물**: {kw_str}
**이슈 최종 수정**: {updated}

---

### 현재 등록된 첨부파일
{att_list}

---

### 현행화 체크리스트

아래 항목을 확인하고 최신 버전으로 갱신해 주세요.

- [ ] 산출물 정의서 — 최신 과업 변경사항 반영 여부 확인
- [ ] WBS — 일정·담당자·산출물 최신화 여부 확인
- [ ] 과업대비표 — 제안서 대비 실제 수행 내용 갱신 여부
- [ ] 사업수행계획서 — 현행 사업 추진 방향과 일치 여부
- [ ] 세부사업별 산출물 — 변경사항 발생 시 즉시 갱신 필요

### 현행화 원칙
| 상황 | 조치 |
|------|------|
| 과업 변경 발생 | 48시간 이내 관련 산출물 갱신 |
| 일정 변경 | WBS·사업수행계획서 동시 갱신 |
| 담당자 변경 | 관련 이슈 담당자 업데이트 + 산출물 갱신 |
| 의사결정 사항 | 회의록 첨부 + 관련 산출물 반영 |

> 산출물 현행화는 **사업 투명성 및 감사 대응**의 핵심입니다.
> 갱신 후 구버전은 삭제하지 말고 버전명(v1.0→v1.1)을 파일명에 표기하세요.

*자동 생성: FM2026 PMO | {TS()}*"""

    return CommentAction(
        issue_key=key, summary=summary,
        action_type="currentization",
        title="주요 산출물 현행화 요구",
        rationale=rationale,
        comment_body=body,
        priority="높음",
        marker="PMO 자동알림 | 높음] 주요 산출물 현행화",
    )


# ─── 사전 검토 리포트 (담당자 확인용) ────────────────────────────────────────

def print_review_report(actions: list[CommentAction]) -> None:
    """댓글 등록 전 사업담당자 검토용 리포트"""
    now = now_kst().strftime("%Y-%m-%d %H:%M KST")
    print("\n" + "█" * 78)
    print("  FM2026 PMO 자동화 — 댓글 등록 전 사업담당자 검토 리포트")
    print(f"  생성일시: {now}")
    print("█" * 78)
    print(f"\n총 {len(actions)}건의 댓글이 등록 대기 중입니다.\n")

    priority_order = {"긴급": 0, "높음": 1, "보통": 2, "낮음": 3}
    sorted_actions = sorted(actions, key=lambda a: priority_order.get(a.priority, 9))

    for i, act in enumerate(sorted_actions, 1):
        print(f"{'─'*70}")
        print(f"[{i}/{len(actions)}] [{act.priority}] {act.action_type.upper()} | {act.issue_key}")
        print(f"  이슈 제목: {act.summary}")
        print(f"  이슈 링크: {issue_url(act.issue_key)}")
        print(f"\n  ▶ 근거 (왜 이 댓글이 필요한가):")
        for line in act.rationale.split("\n"):
            print(f"    {line}")
        print(f"\n  ▶ 댓글 제목: {act.title}")
        print(f"\n  ▶ 댓글 내용 미리보기:")
        preview = act.comment_body[:400]
        for line in preview.split("\n"):
            print(f"    {line}")
        if len(act.comment_body) > 400:
            print("    ... (이하 생략)")
        print()

    print("█" * 78)
    print("  위 내용을 검토 후 등록하려면:")
    print("  python fm2026_pmo_auto.py --post-comments")
    print("  특정 이슈만 처리하려면:")
    print("  python fm2026_pmo_auto.py --post-comments --issue FM2026-XXX")
    print("█" * 78 + "\n")


# ─── 댓글 등록 실행 ──────────────────────────────────────────────────────────

def execute_comment(action: CommentAction, dry_run: bool) -> bool:
    if dry_run:
        log.info("[DRY-RUN] %s → %s", action.issue_key, action.title)
        return True
    try:
        jira_post_comment(action.issue_key, action.comment_body)
        log.info("[댓글 등록 완료] %s → %s", action.issue_key, action.title)
        return True
    except Exception as e:
        log.error("[댓글 등록 실패] %s: %s", action.issue_key, e)
        return False


# ─── 메인 분석 루프 ──────────────────────────────────────────────────────────

def analyze_issues(
    issues: list[dict],
    dry_run: bool,
    target_key: str | None = None,
    review_mode: bool = False,
) -> PMOReport:
    now    = now_kst()
    report = PMOReport(generated_at=now.strftime("%Y-%m-%d %H:%M KST"), total_issues=len(issues))
    pending: list[CommentAction] = []

    for issue in issues:
        key    = issue["key"]
        fields = issue["fields"]
        status = (fields.get("status") or {}).get("name", "")

        if target_key and key != target_key:
            continue

        # 상태 분류
        if status in ("해결됨", "완료", "Done", "Resolved", "Closed"):
            report.completed.append(key)
        elif status in ("진행 중", "In Progress"):
            report.in_progress.append(key)
        else:
            report.backlog.append(key)

        is_done = status in ("해결됨", "완료", "Done", "Resolved", "Closed")

        # ── (1) 만료 체크 ─────────────────────────────────────────────────────
        exp_status, delta = check_expiry(issue, now)

        if exp_status == "no_due":
            report.no_due_date.append(key)
            if not is_done:
                act = make_no_duedate_comment(issue)
                if not already_commented(issue, act.marker):
                    pending.append(act)
                else:
                    report.skipped_comments.append((key, "만료기간 미등록 - 중복"))

        elif exp_status == "expired":
            report.expired.append(key)
            if not is_done:
                act = make_expired_comment(issue, delta)
                if not already_commented(issue, act.marker):
                    pending.append(act)
                else:
                    report.skipped_comments.append((key, "기간 초과 - 중복"))

        elif exp_status == "alert":
            report.alert_6h.append(key)
            act = make_expiry_alert_comment(issue, delta)
            if not already_commented(issue, act.marker):
                pending.append(act)
            else:
                report.skipped_comments.append((key, "만료 임박 - 중복"))

        # ── (2) 첨부파일 분석 ────────────────────────────────────────────────
        readable, unreadable = analyze_attachments(issue)

        if unreadable:
            report.unreadable_files.append({
                "issue_key": key,
                "summary":   fields.get("summary", ""),
                "url":       issue_url(key),
                "files":     [f["filename"] for f in unreadable],
            })

        # 원본만 있고 PDF 없는 파일도 포함 (readable 중 pdf_missing)
        pdf_missing = [a for a in readable if a.get("pdf_missing")]
        if unreadable or pdf_missing:
            act = make_unreadable_comment(issue, unreadable, readable)
            if not already_commented(issue, act.marker):
                pending.append(act)

        # ── (3) As-Is / To-Be 대상 ───────────────────────────────────────────
        if not is_done and needs_asis_tobe(issue):
            report.asis_tobe_needed.append(key)
            act = make_asis_tobe_comment(issue)
            if not already_commented(issue, act.marker):
                pending.append(act)

        # ── (4) 주요 산출물 현행화 체크 ──────────────────────────────────────
        if not is_done:
            curr_needed, matched = needs_currentization(issue)
            if curr_needed:
                report.currentization_needed.append(key)
                act = make_currentization_comment(issue, matched)
                if not already_commented(issue, act.marker):
                    pending.append(act)

    # ── 검토 리포트 출력 또는 댓글 등록 ──────────────────────────────────────
    report.pending_actions = [
        {"key": a.issue_key, "type": a.action_type, "priority": a.priority, "title": a.title}
        for a in pending
    ]

    if review_mode:
        print_review_report(pending)
    else:
        for act in pending:
            success = execute_comment(act, dry_run)
            if success:
                report.posted_comments.append((act.issue_key, act.title))
            else:
                report.skipped_comments.append((act.issue_key, f"{act.title} - 등록 실패"))

    return report


# ─── 최종 리포트 출력 ────────────────────────────────────────────────────────

RECOMMENDATIONS = """
╔══════════════════════════════════════════════════════════════════════════════╗
║           FM2026 팜맵 사업 PMO 제언 — 처음 담당하는 사업관리자를 위한 안내   ║
╚══════════════════════════════════════════════════════════════════════════════╝

【기초 세팅】
  ① 모든 이슈에 담당자·기한·우선순위를 반드시 입력하세요. Jira에서 기한이 없으면
    일정 관리·진척률 산정·PMO 자동화가 작동하지 않습니다.
  ② 사업수행계획서의 WBS 일정 = Jira 기한(Due Date) 으로 동기화하세요.
    불일치 시 보고서와 실제 현황이 달라져 신뢰도가 저하됩니다.

【산출물 관리 원칙】
  ③ 이슈 완료 = 산출물 첨부(원본+PDF) + 상태 '해결됨' 변경의 2단계를 지키세요.
  ④ 모든 산출물은 원본 파일(hwp·xlsx 등)과 PDF를 동시에 등록하세요.
    PDF가 없으면 뷰어 없이 열람 불가, 납품·보고 시 문제가 됩니다.
  ⑤ 수정 시 버전명(v1.0→v1.1)을 파일명에 표기, 구버전은 보관하세요.

【현행화 의무】
  ⑥ 과업 변경 발생 시 48시간 이내에 관련 산출물(WBS·과업대비표·사업수행계획서·
    산출물 정의서)을 갱신하세요. 현행화 지연은 감사 지적 사항입니다.
  ⑦ 주요 의사결정 사항은 회의록을 작성 후 Jira에 첨부하세요.

【As-Is / To-Be】
  ⑧ 2025 → 2026 개선 업무는 반드시 현행 분석(As-Is)과 목표 상태(To-Be)를
    산출물에 포함하세요. 없으면 개선 근거 부재로 감사 지적 대상입니다.

【리스크 관리】
  ⑨ 기간 초과 이슈 3개 이상 → 즉시 PM에게 보고 후 공식 기간 조정 절차 진행.
  ⑩ 농정원 담당자와 월 1회 이상 공식 회의 + 회의록 Jira 첨부.

【자동화 활용】
  ⑪ 이 프로그램을 매일 1회 실행하세요:
       cron: 0 9 * * 1-5 python /path/to/fm2026_pmo_auto.py --post-comments
     또는 스케줄러 사용:
       python fm2026_pmo_scheduler.py --post-comments

  ⑫ 댓글 등록 전 반드시 --review 옵션으로 근거를 확인하고 판단하세요.

【PMO 체크리스트 (매주 월요일)】
  □ 기간 초과 이슈 현황 확인 및 보고
  □ 이번 주 만료 예정 이슈 사전 준비 요청
  □ 신규 등록 이슈 기한·담당자 설정 여부 확인
  □ 주요 산출물 현행화 여부 점검
  □ 미등록 산출물 이슈 독려
"""


def print_pmo_report(report: PMOReport) -> None:
    print("\n" + "=" * 78)
    print("  FM2026 팜맵 사업 PMO 자동화 분석 리포트 v2.0")
    print(f"  생성일시: {report.generated_at}")
    if report.note:
        print(f"  ※ {report.note}")
    print("=" * 78)

    print(f"\n{'─'*40} 이슈 현황 {'─'*26}")
    print(f"  전체 이슈        : {report.total_issues:>4}건")
    print(f"  완료             : {len(report.completed):>4}건")
    print(f"  진행 중          : {len(report.in_progress):>4}건  {report.in_progress}")
    print(f"  백로그           : {len(report.backlog):>4}건")

    print(f"\n{'─'*40} 일정 관리 {'─'*26}")
    print(f"  만료기간 미등록  : {len(report.no_due_date):>4}건  {report.no_due_date}")
    print(f"  기간 초과        : {len(report.expired):>4}건  {report.expired}")
    print(f"  6시간 이내 경보  : {len(report.alert_6h):>4}건  {report.alert_6h}")

    print(f"\n{'─'*40} 산출물 관리 {'─'*24}")
    print(f"  As-Is/To-Be 필요 : {len(report.asis_tobe_needed):>4}건  {report.asis_tobe_needed}")
    print(f"  현행화 필요      : {len(report.currentization_needed):>4}건  {report.currentization_needed}")

    print(f"\n{'─'*40} 첨부파일 {'─'*28}")
    if report.unreadable_files:
        print(f"  읽기 불가 파일   : {len(report.unreadable_files)}건 (사업관리자 확인 필요)")
        for item in report.unreadable_files:
            print(f"    [{item['issue_key']}] {item['summary']}")
            print(f"      링크: {item['url']}")
            for fname in item["files"]:
                print(f"      파일: {fname}")
    else:
        print("  읽기 불가 파일   : 없음")

    print(f"\n{'─'*40} 댓글 처리 현황 {'─'*22}")
    print(f"  등록 대기        : {len(report.pending_actions):>4}건")
    print(f"  등록 완료        : {len(report.posted_comments):>4}건")
    print(f"  중복 스킵        : {len(report.skipped_comments):>4}건")
    for key, reason in report.posted_comments:
        print(f"    ✓ {key}: {reason}")

    print(RECOMMENDATIONS)

    report_file = f"fm2026_pmo_report_{datetime.now(KST).strftime('%Y%m%d_%H%M')}.json"
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(asdict(report), f, ensure_ascii=False, indent=2, default=str)
    print(f"📄 JSON 리포트 저장: {report_file}\n")


# ─── CLI 진입점 ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="FM2026 팜맵 사업 PMO 자동화 v2.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--review",        action="store_true",
                        help="근거 포함 전체 검토 리포트 출력 (댓글 미등록)")
    parser.add_argument("--post-comments", action="store_true",
                        help="실제 Jira 댓글 등록 실행")
    parser.add_argument("--issue",         metavar="KEY",
                        help="특정 이슈만 처리 (예: FM2026-153)")
    parser.add_argument("--since",         metavar="DATE",
                        help="해당 날짜 이후 업데이트된 이슈만 처리 (예: 2026-06-01)")
    args = parser.parse_args()

    dry_run     = not args.post_comments
    review_mode = args.review

    if dry_run and not review_mode:
        log.info("=== DRY-RUN 모드 (기본) — 댓글 미등록 ===")
        log.info("검토 리포트: --review | 실제 등록: --post-comments")
    elif review_mode:
        log.info("=== 검토 모드 — 댓글 근거 및 미리보기 출력 ===")
    else:
        log.info("=== LIVE 모드 — 실제 Jira 댓글 등록 ===")

    if not JIRA_EMAIL or not JIRA_API_TOKEN:
        log.warning("JIRA_EMAIL / JIRA_API_TOKEN 미설정 → 시뮬레이션 모드")
        _run_simulation(dry_run, review_mode, args.issue)
        return

    log.info("Jira 이슈 조회 시작: %s", PROJECT_KEY)
    try:
        issues = fetch_all_issues(since_date=args.since)
    except Exception as exc:
        log.error("Jira 조회 실패: %s", exc)
        sys.exit(1)

    report = analyze_issues(
        issues,
        dry_run=dry_run,
        target_key=args.issue,
        review_mode=review_mode,
    )
    if not review_mode:
        print_pmo_report(report)


def _run_simulation(dry_run: bool, review_mode: bool, target_key: str | None) -> None:
    """환경변수 미설정 시 현재 파악된 이슈 데이터로 시뮬레이션"""
    now = now_kst()
    # 만료 임박 이슈를 시뮬레이션하기 위해 현재 시간 기준으로 기한 설정
    alert_due = (now + timedelta(hours=3)).strftime("%Y-%m-%d")
    expired_due = (now - timedelta(days=5)).strftime("%Y-%m-%d")

    sample_issues = [
        {
            "key": "FM2026-182",
            "fields": {
                "summary": "[배포/유지보수] 소스코드 유지보수",
                "status": {"name": "백로그"},
                "duedate": None,
                "assignee": {"displayName": "이호용"},
                "attachment": [],
                "comment": {"comments": []},
                "description": None,
                "updated": "2026-06-20",
            },
        },
        {
            "key": "FM2026-153",
            "fields": {
                "summary": "[시스템개선] 1.분석 - 1.9.요구사항 이해 (면담을 통한 요구사항 도출)",
                "status": {"name": "진행 중"},
                "duedate": alert_due,
                "assignee": {"displayName": "윤병석"},
                "attachment": [
                    {
                        "id": "att001",
                        "filename": "요구사항분석서_v1.0.hwp",
                        "size": 204800,
                        "created": "2026-06-15T10:00:00.000+0900",
                        "author": {"displayName": "윤병석"},
                        "content": f"{JIRA_BASE_URL}/secure/attachment/att001/요구사항분석서_v1.0.hwp",
                        "mimeType": "application/x-hwp",
                    }
                ],
                "comment": {"comments": []},
                "description": {"type": "doc", "version": 1, "content": [
                    {"type": "paragraph", "content": [
                        {"type": "text", "text": "요구사항 수집 및 분석 업무 개선 진행 중. WBS 일정 기준 작업 중."}
                    ]}
                ]},
                "updated": "2026-06-22",
            },
        },
        {
            "key": "FM2026-152",
            "fields": {
                "summary": "[시스템개선] 1.분석 - 1.3.요구사항 이해 (요구사항 수집)",
                "status": {"name": "해결됨"},
                "duedate": "2026-06-12",
                "assignee": {"displayName": "윤병석"},
                "attachment": [],
                "comment": {"comments": []},
                "description": None,
                "updated": "2026-06-12",
            },
        },
        {
            "key": "FM2026-133",
            "fields": {
                "summary": "[시스템운영] 1.서비스 운영관리 - 1.5.공공데이터품질관리지원",
                "status": {"name": "백로그"},
                "duedate": expired_due,
                "assignee": {"displayName": "윤병석"},
                "attachment": [
                    {
                        "id": "att002",
                        "filename": "운영계획서.bin",
                        "size": 512000,
                        "created": "2026-06-01T10:00:00.000+0900",
                        "author": {"displayName": "윤병석"},
                        "content": f"{JIRA_BASE_URL}/secure/attachment/att002/운영계획서.bin",
                        "mimeType": "application/octet-stream",
                    }
                ],
                "comment": {"comments": []},
                "description": {"type": "doc", "version": 1, "content": [
                    {"type": "paragraph", "content": [
                        {"type": "text", "text": "클라우드 전환 운영관리. 사업수행계획서 기준 진행. As-Is 분석 필요."}
                    ]}
                ]},
                "updated": "2026-06-15",
            },
        },
        {
            "key": "FM2026-150",
            "fields": {
                "summary": "[시스템개선] 1.분석 - 1.1.단계 준비",
                "status": {"name": "해결됨"},
                "duedate": "2026-06-12",
                "assignee": {"displayName": "윤병석"},
                "attachment": [
                    {
                        "id": "att003",
                        "filename": "단계준비보고서_v1.0.docx",
                        "size": 102400,
                        "created": "2026-06-10T10:00:00.000+0900",
                        "author": {"displayName": "윤병석"},
                        "content": f"{JIRA_BASE_URL}/secure/attachment/att003/단계준비보고서_v1.0.docx",
                        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    }
                ],
                "comment": {"comments": []},
                "description": None,
                "updated": "2026-06-12",
            },
        },
    ]

    issues = [i for i in sample_issues if (target_key is None or i["key"] == target_key)]
    report = analyze_issues(
        issues,
        dry_run=dry_run,
        target_key=target_key,
        review_mode=review_mode,
    )
    report.note = "시뮬레이션 모드 (JIRA_EMAIL/JIRA_API_TOKEN 미설정)"
    report.total_issues = 182
    if not review_mode:
        print_pmo_report(report)


if __name__ == "__main__":
    main()
