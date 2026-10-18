"""비용이 누구 계정에서 나가는가 — 서버가 가진 키가 하나라도 쓰이면 서버가 정한 비용 상한이 적용된다.

화면에서 사용자가 한쪽 제공자의 키만 넣어도 다른 쪽 제공자는 서버 키를 그대로 쓴다(app.make_settings 가 칸마다 '사용자 키 또는 서버 키' 로 채운다).
그래서 '키를 하나라도 넣었다' 가 아니라 '서버 키로 호출되는 제공자가 있는가' 로 판단해야 서버 상한을 우회하지 못한다.
키 값은 다루지 않는다 — 서버에 키가 있는지(불리언)와 사용자가 입력했는지(빈 문자열 여부)만 본다.
"""
from __future__ import annotations


def funded_by_server(server_status: dict, user_openai_key: str, user_anthropic_key: str) -> bool:
    return (bool(server_status.get("openai")) and not (user_openai_key or "").strip()) or (bool(server_status.get("anthropic")) and not (user_anthropic_key or "").strip())


def server_cap(server_status: dict) -> float:
    return float(server_status.get("run_budget_usd") or 0.5)
