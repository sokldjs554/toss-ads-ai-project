"""자유 텍스트에서 JSON 을 꺼내는 방어적 파서 (FM-01 의 1차 완충)."""
from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Any:
    """펜스 안 → 전체 → 첫 균형 잡힌 {…}/[...] 순으로 시도. 전부 실패하면 ValueError."""
    candidates: list[str] = []
    for m in _FENCE.finditer(text):
        candidates.append(m.group(1).strip())
    candidates.append(text.strip())
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start != -1:
            depth = 0
            in_str = False
            esc = False
            for i in range(start, len(text)):
                ch = text[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        candidates.append(text[start : i + 1])
                        break
    last_err: Exception | None = None
    for c in candidates:
        try:
            return json.loads(c)
        except Exception as e:  # noqa: BLE001
            last_err = e
            # 흔한 오류: 뒤따르는 쉼표
            try:
                return json.loads(re.sub(r",\s*([}\]])", r"\1", c))
            except Exception as e2:  # noqa: BLE001
                last_err = e2
    raise ValueError(f"JSON 을 찾지 못했습니다: {last_err}")
