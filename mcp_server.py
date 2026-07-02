"""
Antigravity MCP 서버 — llm_harness의 클로드/제미나이 교차검증 기능을
Model Context Protocol 도구로 노출한다.

Google Antigravity(제미나이 기반 에이전틱 개발 플랫폼)의 에이전트가 작업 중
"클로드의 검증을 받아봐"처럼 지시받으면, 이 서버의 cross_validate_with_claude
도구를 호출해 클로드+제미나이 양쪽 답변과 합의 여부를 받아볼 수 있다.

등록 방법 (Antigravity):
  ~/.gemini/config/mcp_config.json (Windows: %userprofile%\\.gemini\\config\\mcp_config.json)
  에 아래 항목을 추가한 뒤 Antigravity IDE의 "Manage MCP Servers" 화면에서
  Refresh한다.

  {
    "mcpServers": {
      "claude-harness": {
        "command": "python",
        "args": ["/절대경로/karas/mcp_server.py"],
        "env": {
          "ANTHROPIC_API_KEY": "...",
          "GEMINI_API_KEY": "..."
        }
      }
    }
  }

필수 환경변수:
  ANTHROPIC_API_KEY, GEMINI_API_KEY (llm_harness.py와 동일)

로컬 동작 확인:
  python mcp_server.py
  (stdio 트랜스포트로 대기 상태에 들어간다. Antigravity가 이 프로세스를
   자식 프로세스로 띄워 표준입출력으로 통신한다.)
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from llm_harness import cross_validate

mcp = FastMCP("claude-harness")


@mcp.tool()
def cross_validate_with_claude(question: str, system: str = "") -> dict:
    """클로드와 제미나이에 동일한 질문을 독립적으로 던져 답변을 받고,
    두 답변이 핵심 결론에서 실질적으로 합의하는지 판정해 반환한다.

    현재 진행 중인 코드 변경, 설계 판단, 분석 결과에 대해 다른 모델(클로드)의
    검증이 필요할 때 사용한다. agreement가 false면 두 모델의 결론이 갈렸다는
    뜻이므로 재검토 신호로 해석해야 한다.

    Args:
        question: 검증받고 싶은 질문 또는 판단 내용.
        system: 두 모델에 공통으로 적용할 시스템 프롬프트 (선택).
    """
    result = cross_validate(question, system)
    return result.to_dict()


if __name__ == "__main__":
    mcp.run()
