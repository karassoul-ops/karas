"""
FM2026 PMO 자동화 스케줄러 v2.0
=================================
주기적으로 fm2026_pmo_auto.py 를 실행하여 만료 임박 이슈를 놓치지 않습니다.

실행 방식:
  A) 로컬 스케줄러 (직접 실행)
     python fm2026_pmo_scheduler.py --daily --post-comments   # 매일 09:00, 17:00 자동 실행
     python fm2026_pmo_scheduler.py --daily --times 09:00 13:00 17:00  # 시각 직접 지정
     python fm2026_pmo_scheduler.py                    # 60분 간격, dry-run
     python fm2026_pmo_scheduler.py --post-comments    # 60분 간격, 실제 댓글 등록
     python fm2026_pmo_scheduler.py --interval 30      # 30분 간격
     python fm2026_pmo_scheduler.py --once             # 1회만 실행

  B) GitHub Actions (.github/workflows/fm2026_pmo.yml 사용)
     저장소 → Actions → FM2026 PMO 자동화 → Run workflow

  C) Linux/Mac cron 등록 예시 (매일 오전 9시)
     0 9 * * 1-5 cd /path/to/karas && python fm2026_pmo_scheduler.py --once --post-comments >> pmo_cron.log 2>&1

  D) Windows 작업 스케줄러
     트리거: 매일 09:00
     동작: python C:\\path\\to\\karas\\fm2026_pmo_scheduler.py --once --post-comments
"""

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))


def run_once(post: bool = False, review: bool = False, issue: str = "",
             script: str = "fm2026_pmo_auto.py") -> int:
    cmd = [sys.executable, script]
    if post:
        cmd.append("--post-comments")
    if review:
        cmd.append("--review")
    if issue:
        cmd.extend(["--issue", issue])

    ts = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    mode = "LIVE" if post else ("REVIEW" if review else "DRY-RUN")
    label = "산출물 감시" if "watch" in script else "PMO 자동화"
    print(f"\n[{ts}] FM2026 {label} 실행 ({mode})")
    print(f"  명령어: {' '.join(cmd)}")
    print("-" * 60)

    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"[경고] 실행 중 오류 (코드: {result.returncode})")
    else:
        print(f"[완료] 정상 종료")
    return result.returncode


def run_watch_loop(interval_min: int, post: bool, review: bool, issue: str) -> None:
    """1시간(기본) 단위로 신규 산출물 감시 실행"""
    print(f"\n산출물 감시 모드: {interval_min}분 간격으로 신규 산출물을 점검합니다.")
    run_once(post=post, review=review, issue=issue, script="fm2026_deliverable_watch.py")
    while True:
        next_run = datetime.now(KST) + timedelta(minutes=interval_min)
        print(f"\n다음 산출물 점검: {next_run.strftime('%Y-%m-%d %H:%M KST')}")
        try:
            time.sleep(interval_min * 60)
        except KeyboardInterrupt:
            print("\n산출물 감시 종료")
            break
        run_once(post=post, review=review, issue=issue, script="fm2026_deliverable_watch.py")


def main() -> None:
    parser = argparse.ArgumentParser(description="FM2026 PMO 자동화 스케줄러 v2.0")
    parser.add_argument("--interval",      type=int, default=60,
                        help="실행 간격 (분, 기본값: 60)")
    parser.add_argument("--once",          action="store_true",
                        help="1회만 실행 후 종료")
    parser.add_argument("--post-comments", action="store_true",
                        help="실제 댓글 등록 모드 (기본: dry-run)")
    parser.add_argument("--review",        action="store_true",
                        help="근거 포함 검토 리포트 출력 모드")
    parser.add_argument("--issue",         metavar="KEY", default="",
                        help="특정 이슈만 처리")
    parser.add_argument("--daily",         action="store_true",
                        help="매일 지정 시각(기본 09:00, 17:00)에 실행")
    parser.add_argument("--times",         metavar="HH:MM", nargs="+",
                        default=["09:00", "17:00"],
                        help="--daily 실행 시각 목록 (기본: 09:00 17:00)")
    parser.add_argument("--watch",         action="store_true",
                        help="1시간 단위 신규 산출물 감시·분석 모드")
    parser.add_argument("--smart",         action="store_true",
                        help="스마트 적응형 감시 (변화 없으면 자동 대기, 권장)")
    args = parser.parse_args()

    # ── 스마트 적응형 감시 모드 (권장) ────────────────────────────────────
    if args.smart:
        print("=" * 60)
        print("  FM2026 스마트 적응형 감시 모드")
        print(f"  실행 모드 : {'LIVE' if args.post_comments else 'DRY-RUN'}")
        print("  변화 있으면 1시간 단위 실시간 점검")
        print("  2시간 무변화 → 대기, 09:00/17:00 정기 점검 시 재개")
        print("=" * 60)
        cmd = [sys.executable, "fm2026_smart_watch.py"]
        if args.post_comments:
            cmd.append("--post-comments")
        if args.review:
            cmd.append("--review")
        if args.once:
            cmd.append("--once")
        subprocess.run(cmd)
        return

    # ── 산출물 감시 모드 (1시간 단위) ─────────────────────────────────────
    if args.watch:
        interval = args.interval if args.interval != 60 else 60  # 기본 60분
        print("=" * 60)
        print("  FM2026 산출물 신규 등록 감시 스케줄러")
        print(f"  실행 모드 : {'LIVE' if args.post_comments else ('REVIEW' if args.review else 'DRY-RUN')}")
        print(f"  점검 간격 : {interval}분")
        print("=" * 60)
        if args.once:
            run_once(post=args.post_comments, review=args.review,
                     issue=args.issue, script="fm2026_deliverable_watch.py")
        else:
            run_watch_loop(interval, args.post_comments, args.review, args.issue)
        return

    mode_str = "LIVE (실제 댓글 등록)" if args.post_comments else (
        "REVIEW (검토 리포트)" if args.review else "DRY-RUN (미리보기)"
    )
    print("=" * 60)
    print("  FM2026 팜맵 사업 PMO 자동화 스케줄러 v2.0")
    print(f"  실행 모드 : {mode_str}")
    if args.daily:
        print(f"  실행 방식 : 매일 고정 시각 {', '.join(args.times)} (KST)")
    elif args.once:
        print("  실행 방식 : 1회 실행")
    else:
        print(f"  실행 간격 : {args.interval}분")
    print("  종료 방법 : Ctrl+C")
    print("=" * 60)

    # ── 매일 고정 시각 실행 모드 ──────────────────────────────────────────
    if args.daily:
        run_daily(args.times, post=args.post_comments, review=args.review, issue=args.issue)
        return

    run_once(post=args.post_comments, review=args.review, issue=args.issue)

    if args.once:
        return

    while True:
        next_run = datetime.now(KST) + timedelta(minutes=args.interval)
        print(f"\n다음 실행 예정: {next_run.strftime('%Y-%m-%d %H:%M KST')}")
        try:
            time.sleep(args.interval * 60)
        except KeyboardInterrupt:
            print("\n스케줄러 종료")
            break
        run_once(post=args.post_comments, review=args.review, issue=args.issue)


def _parse_times(times: list[str]) -> list[tuple[int, int]]:
    """['09:00', '17:00'] → [(9, 0), (17, 0)]"""
    parsed = []
    for t in times:
        hh, mm = t.split(":")
        parsed.append((int(hh), int(mm)))
    return sorted(parsed)


def _next_run_at(times: list[tuple[int, int]], now: datetime) -> datetime:
    """현재 시각 이후 가장 가까운 실행 시각 계산 (KST 기준)"""
    candidates = []
    for hh, mm in times:
        candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        candidates.append(candidate)
    return min(candidates)


def run_daily(times: list[str], post: bool = False, review: bool = False, issue: str = "") -> None:
    """매일 지정된 시각마다 실행 (예: 09:00, 17:00)"""
    parsed_times = _parse_times(times)
    print(f"\n매일 {', '.join(times)} (KST)에 자동 실행됩니다.")

    while True:
        now      = datetime.now(KST)
        next_run = _next_run_at(parsed_times, now)
        wait_sec = (next_run - now).total_seconds()
        print(f"\n다음 실행 예정: {next_run.strftime('%Y-%m-%d %H:%M KST')} "
              f"(약 {wait_sec/3600:.1f}시간 후 대기)")
        try:
            time.sleep(wait_sec)
        except KeyboardInterrupt:
            print("\n스케줄러 종료")
            break
        run_once(post=post, review=review, issue=issue)


if __name__ == "__main__":
    main()
