"""
FM2026 PMO 자동화 스케줄러 v2.0
=================================
주기적으로 fm2026_pmo_auto.py 를 실행하여 만료 임박 이슈를 놓치지 않습니다.

실행 방식:
  A) 로컬 스케줄러 (직접 실행)
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


def run_once(post: bool = False, review: bool = False, issue: str = "") -> int:
    cmd = [sys.executable, "fm2026_pmo_auto.py"]
    if post:
        cmd.append("--post-comments")
    if review:
        cmd.append("--review")
    if issue:
        cmd.extend(["--issue", issue])

    ts = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    mode = "LIVE" if post else ("REVIEW" if review else "DRY-RUN")
    print(f"\n[{ts}] FM2026 PMO 자동화 실행 ({mode})")
    print(f"  명령어: {' '.join(cmd)}")
    print("-" * 60)

    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"[경고] 실행 중 오류 (코드: {result.returncode})")
    else:
        print(f"[완료] 정상 종료")
    return result.returncode


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
    args = parser.parse_args()

    mode_str = "LIVE (실제 댓글 등록)" if args.post_comments else (
        "REVIEW (검토 리포트)" if args.review else "DRY-RUN (미리보기)"
    )
    print("=" * 60)
    print("  FM2026 팜맵 사업 PMO 자동화 스케줄러 v2.0")
    print(f"  실행 모드 : {mode_str}")
    print(f"  실행 간격 : {args.interval}분" if not args.once else "  실행 방식 : 1회 실행")
    print("  종료 방법 : Ctrl+C")
    print("=" * 60)

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


if __name__ == "__main__":
    main()
