"""
LLM Harness — 클로드/제미나이 교차검증(합의) 모듈
==========================================

동일한 질의를 클로드와 제미나이에 각각 전달해 독립적으로 분석시킨 뒤,
두 결과가 핵심 결론에서 실질적으로 합의하는지 판정한다. 불일치 시
`agreement=False`와 판정 사유를 반환하므로 호출 측에서 재검토 트리거로
사용할 수 있다.

FM2026 프로젝트 전용이 아닌 범용 모듈이며, 어떤 주제의 질문에도 사용할 수
있다. 기존 fm2026_*.py 스크립트와는 독립되어 있고 아직 어느 파이프라인에도
연결되어 있지 않다.

필수 환경변수:
  ANTHROPIC_API_KEY  : Anthropic API 키
  GEMINI_API_KEY      : Gemini API 키

선택 환경변수:
  HARNESS_CLAUDE_MODEL : 기본값 claude-opus-4-8
  HARNESS_GEMINI_MODEL : 기본값 gemini-2.5-pro

사용법:
  python llm_harness.py --prompt "이 산출물이 과업대비표를 충족하는가?"
  python llm_harness.py --prompt "..." --system "당신은 PMO 검토자입니다."
  python llm_harness.py --chat                          # 대화형 모드 (무엇이든 질문)
  python llm_harness.py --chat --system "친절한 조수처럼 답하라"
"""

from __future__ import annotations

import os
import sys
import json
import argparse
import logging
from dataclasses import dataclass, asdict
from typing import Optional

import anthropic
from google import genai

CLAUDE_MODEL = os.environ.get("HARNESS_CLAUDE_MODEL", "claude-opus-4-8")
GEMINI_MODEL = os.environ.get("HARNESS_GEMINI_MODEL", "gemini-2.5-pro")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


@dataclass
class ModelResponse:
    model: str
    text: str
    error: Optional[str] = None


@dataclass
class ConsensusResult:
    prompt: str
    claude: ModelResponse
    gemini: ModelResponse
    agreement: bool
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def _judge_agreement(claude_text: str, gemini_text: str) -> tuple[bool, str]:
    """클로드를 심판으로 세워 두 응답의 실질적 합의 여부를 판정한다 (대화 이력과 무관한 단발 호출)."""
    judge_prompt = f"""다음 두 AI의 독립적인 답변이 핵심 결론에서 실질적으로 일치하는지 판단하세요.
표현 차이가 아니라 결론(사실관계, 권장 조치, 판단)의 일치 여부만 보세요.

[답변 A]
{claude_text}

[답변 B]
{gemini_text}

다른 설명 없이 아래 JSON 형식으로만 답하세요:
{{"agree": true 또는 false, "reason": "판정 이유 한 문장"}}"""

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": judge_prompt}],
        )
        raw = next((b.text for b in response.content if b.type == "text"), "").strip()
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        log.error("합의 판정 호출 실패: %s", e)
        return False, f"판정 호출 실패: {e}"

    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        parsed = json.loads(raw)
        return bool(parsed.get("agree")), str(parsed.get("reason", ""))
    except json.JSONDecodeError:
        return False, f"판정 응답 파싱 실패: {raw[:200]}"


class ChatSession:
    """클로드/제미나이 양쪽에 대화 이력을 유지하며 턴마다 교차검증하는 세션.

    한 번 만든 세션으로 `ask()`를 여러 번 호출하면 각 모델이 이전 질문·답변을
    기억한 채 이어서 답한다. 주제 제한 없이 어떤 질문에도 사용할 수 있다.
    """

    def __init__(self, system: str = ""):
        self.system = system
        self._claude_messages: list[dict] = []
        self._gemini_chat = None  # 첫 질문 시점에 지연 생성

    def _claude_turn(self, user_text: str) -> ModelResponse:
        self._claude_messages.append({"role": "user", "content": user_text})
        try:
            client = anthropic.Anthropic()
            kwargs = {"system": self.system} if self.system else {}
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=4096,
                messages=self._claude_messages,
                **kwargs,
            )
            text = next((b.text for b in response.content if b.type == "text"), "")
            self._claude_messages.append({"role": "assistant", "content": text})
            return ModelResponse(model=CLAUDE_MODEL, text=text)
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            log.error("Claude 호출 실패: %s", e)
            self._claude_messages.pop()  # 실패한 사용자 턴은 이력에서 제거
            return ModelResponse(model=CLAUDE_MODEL, text="", error=str(e))

    def _gemini_turn(self, user_text: str) -> ModelResponse:
        try:
            if self._gemini_chat is None:
                client = genai.Client()
                config = {"system_instruction": self.system} if self.system else None
                self._gemini_chat = client.chats.create(model=GEMINI_MODEL, config=config)
            response = self._gemini_chat.send_message(user_text)
            return ModelResponse(model=GEMINI_MODEL, text=response.text or "")
        except Exception as e:
            log.error("Gemini 호출 실패: %s", e)
            return ModelResponse(model=GEMINI_MODEL, text="", error=str(e))

    def ask(self, user_text: str) -> ConsensusResult:
        claude_result = self._claude_turn(user_text)
        gemini_result = self._gemini_turn(user_text)

        if claude_result.error or gemini_result.error:
            agreement, reason = False, "한쪽 모델 호출이 실패해 합의 판정이 불가합니다."
        else:
            agreement, reason = _judge_agreement(claude_result.text, gemini_result.text)

        return ConsensusResult(
            prompt=user_text,
            claude=claude_result,
            gemini=gemini_result,
            agreement=agreement,
            reason=reason,
        )


def cross_validate(prompt: str, system: str = "") -> ConsensusResult:
    """단발 질의용 편의 함수 — 어떤 주제든 클로드/제미나이 교차검증 답변을 받는다."""
    return ChatSession(system).ask(prompt)


def _print_result(result: ConsensusResult) -> None:
    print(f"\n[클로드]\n{result.claude.error or result.claude.text}")
    print(f"\n[제미나이]\n{result.gemini.error or result.gemini.text}")
    verdict = "일치" if result.agreement else "불일치"
    print(f"\n[합의: {verdict}] {result.reason}\n")


def _run_chat(system: str) -> None:
    print("대화형 교차검증 모드입니다. 무엇이든 물어보세요. (종료: exit/quit)")
    session = ChatSession(system)
    while True:
        try:
            user_text = input("\n질문> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_text:
            continue
        if user_text.lower() in ("exit", "quit", "종료"):
            break
        result = session.ask(user_text)
        _print_result(result)


def main() -> None:
    parser = argparse.ArgumentParser(description="클로드/제미나이 교차검증 하네스 (범용 질의응답)")
    parser.add_argument("--prompt", help="두 모델에 전달할 단발 질의")
    parser.add_argument("--system", default="", help="시스템 프롬프트 (선택)")
    parser.add_argument("--chat", action="store_true", help="대화형 모드로 실행")
    parser.add_argument("--json", action="store_true", help="단발 모드 결과를 JSON으로 출력")
    args = parser.parse_args()

    if args.chat:
        _run_chat(args.system)
        return

    if not args.prompt:
        parser.error("--prompt 또는 --chat 중 하나는 필요합니다")

    result = cross_validate(args.prompt, args.system)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_result(result)


if __name__ == "__main__":
    main()
