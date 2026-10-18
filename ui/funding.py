"""비용이 누구 계정에서 나가는가 — 서버가 가진 키가 하나라도 쓰이면 서버가 정한 비용 상한이 적용된다.

화면에서 사용자가 한쪽 제공자의 키만 넣어도 다른 쪽 제공자는 서버 키를 그대로 쓴다(app.make_settings 가 칸마다 '사용자 키 또는 서버 키' 로 채운다).
그래서 '키를 하나라도 넣었다' 가 아니라 '서버 키로 호출되는 제공자가 있는가' 로 판단해야 서버 상한을 우회하지 못한다.
키 값은 다루지 않는다 — 서버에 키가 있는지(불리언)와 사용자가 입력했는지(빈 문자열 여부)만 본다.
"""
from __future__ import annotations


def funded_by_server(server_status: dict, user_openai_key: str, user_anthropic_key: str) -> bool:
    return (bool(server_status.get("openai")) and not (user_openai_key or "").strip()) or (bool(server_status.get("anthropic")) and not (user_anthropic_key or "").strip())


DEFAULT_CAP_USD = 0.5   # 서버가 상한을 알려 주지 않을 때(설정 자체가 없을 때)만 쓰는 값


def server_cap(server_status: dict) -> float:
    """서버 키로 도는 실행의 실행당 비용 상한(USD). 0 은 '실제 모델 실행을 허용하지 않음' 이다 — 0.5 로 바꿔 읽지 않는다.
    숫자가 아니거나 0 이하(음수·NaN 포함)인 값도 허용하지 않음(0)으로 읽는다. 설정 항목이 아예 없을 때만 기본값을 쓴다."""
    raw = server_status.get("run_budget_usd")
    if raw is None:
        return DEFAULT_CAP_USD
    try:
        cap = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return cap if cap > 0 else 0.0   # NaN 은 비교가 모두 거짓이라 여기서 0 이 된다


def usd_text(value: float) -> str:
    """비용 상한 표기. 0.01 달러보다 작은 양수 상한은 소수 넷째 자리까지 적어 '$0.01' 로 부풀려 보이지 않게 한다."""
    return f"${value:.2f}" if value <= 0 or value >= 0.01 else f"${value:.4f}"
