"""
LLM Harness — 클로드/제미나이 교차검증(합의) 모듈
==========================================

동일한 질의를 클로드와 제미나이에 각각 전달해 독립적으로 분석시킨 뒤,
두 결과가 핵심 결론에서 실질적으로 합의하는지 판정한다. 불일치 시
`agreement=False`와 판정 사유를 반환하므로 호출 측에서 재검토 트리거로
사용할 수 있다.

기존 fm2026_*.py 스크립트와는 독립된 범용 모듈이며, 아직 어느 파이프라인에도
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


def _call_claude(prompt: str, system: str = "") -> ModelResponse:
    try:
        client = anthropic.Anthropic()
        kwargs = {"system": system} if system else {}
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        return ModelResponse(model=CLAUDE_MODEL, text=text)
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        log.error("Claude 호출 실패: %s", e)
        return ModelResponse(model=CLAUDE_MODEL, text="", error=str(e))


def _call_gemini(prompt: str, system: str = "") -> ModelResponse:
    try:
        client = genai.Client()
        config = {"system_instruction": system} if system else None
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=config,
        )
        return ModelResponse(model=GEMINI_MODEL, text=response.text or "")
    except Exception as e:
        log.error("Gemini 호출 실패: %s", e)
        return ModelResponse(model=GEMINI_MODEL, text="", error=str(e))


def _judge_agreement(claude_text: str, gemini_text: str) -> tuple[bool, str]:
    """클로드를 심판으로 세워 두 분석 결과의 실질적 합의 여부를 판정한다."""
    judge_prompt = f"""다음 두 AI의 독립적인 분석 결과가 핵심 결론에서 실질적으로 일치하는지 판단하세요.
표현 차이가 아니라 결론(예: 승인/반려, 충족/미충족, 근본 원인)의 일치 여부만 보세요.

[분석 A]
{claude_text}

[분석 B]
{gemini_text}

다른 설명 없이 아래 JSON 형식으로만 답하세요:
{{"agree": true 또는 false, "reason": "판정 이유 한 문장"}}"""

    judged = _call_claude(judge_prompt)
    if judged.error:
        return False, f"판정 호출 실패: {judged.error}"

    raw = judged.text.strip()
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


def cross_validate(prompt: str, system: str = "") -> ConsensusResult:
    """클로드/제미나이에 동일 질의를 던지고 합의 여부를 판정해 반환한다."""
    claude_result = _call_claude(prompt, system)
    gemini_result = _call_gemini(prompt, system)

    if claude_result.error or gemini_result.error:
        agreement, reason = False, "한쪽 모델 호출이 실패해 합의 판정이 불가합니다."
    else:
        agreement, reason = _judge_agreement(claude_result.text, gemini_result.text)

    return ConsensusResult(
        prompt=prompt,
        claude=claude_result,
        gemini=gemini_result,
        agreement=agreement,
        reason=reason,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="클로드/제미나이 교차검증 하네스")
    parser.add_argument("--prompt", required=True, help="두 모델에 전달할 질의")
    parser.add_argument("--system", default="", help="시스템 프롬프트 (선택)")
    args = parser.parse_args()

    result = cross_validate(args.prompt, args.system)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
