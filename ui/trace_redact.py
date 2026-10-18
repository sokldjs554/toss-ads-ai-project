"""처리 기록 화면용 가림 처리 — 재검사를 통과하지 못한 자동 수정안의 문구는 어느 화면에도 보여 주지 않는다 (FM-19).

처리 기록은 모델에 보낸 프롬프트와 받은 응답을 원문 그대로 남긴다. 그 안에는 두 곳에 수정안 문구가 들어 있다.
- 검토 호출의 응답(response_text)에 든 `rewrite` 값: 모델이 제안한 수정안 전체.
- 수정안 검사 호출(rewrite_recheck 아래)의 프롬프트(prompt_user)에 든 `[소재]` 블록: 검사를 받는 수정안 전체.
수정안이 재검사를 통과해 '수정안 확인 완료' 로 화면에 나오는 경우(상태 verified)에는 이미 보이는 문구이므로 가리지 않는다 — 그 수정안을 제안한 1차 검토 호출의 응답에 한해서다.
교차 확인 호출의 응답에 든 수정안은 어디에도 채택되지 않으므로 상태와 관계없이 가린다.
상태를 알 수 없는 경우(같은 기록 안에 판정 단계가 없거나 상태가 저장돼 있지 않은 과거 기록)는 통과하지 못한 것으로 본다.

저장된 기록·내려받는 JSON 은 고치지 않는다. 화면에 그릴 사본에서만 가린다 (redact_trace_dict 는 사본을 돌려준다).
pure Python — streamlit 에 기대지 않아 정적 재생 페이지 빌드(scripts/build_replay.py)도 쓸 수 있다.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any, Optional

from observability.tracer import Trace

REWRITE_HIDDEN = "(재검사를 통과하지 못했거나 통과 여부를 확인하지 못한 자동 수정안이라 이 화면에는 보여 주지 않습니다)"
AD_HIDDEN = "(재검사를 통과하지 못했거나 통과 여부를 확인하지 못한 자동 수정안이라 검사를 받은 문구를 이 화면에는 보여 주지 않습니다)"
PROMPT_HIDDEN = "(재검사를 통과하지 못했거나 통과 여부를 확인하지 못한 자동 수정안의 검사 요청이라 프롬프트를 이 화면에는 보여 주지 않습니다)"
RESPONSE_HIDDEN = "(응답에 수정안이 들어 있는지 읽을 수 없고 그 수정안이 재검사를 통과하지 못했을 수 있어 응답 원문을 이 화면에는 보여 주지 않습니다)"

_AD_BLOCK = re.compile(r"(\[소재\]\n)(.*?)(\n\n\[)", re.DOTALL)
_RAW_REWRITE_KEY = re.compile(r'"rewrite"\s*:\s*(?!null\b)')


def _stored_status(node: dict) -> Optional[str]:
    """이 단계 바로 아래의 판정(adjudicate) 단계가 저장한 수정안 상태. 판정 단계가 없으면 None, 있는데 상태가 없으면 ''."""
    for c in node.get("children") or []:
        if c.get("name") == "adjudicate":
            return str((c.get("attributes") or {}).get("rewrite_status") or "")
    return None


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        pass
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)   # 코드 울타리로 감싼 응답
    if m:
        try:
            return json.loads(m.group(1))
        except ValueError:
            return None
    return None


def _has_rewrite(value: Any) -> bool:
    return value not in (None, "", {}, [])


def redact_llm_attributes(attrs: dict, *, rewrite_verified: bool, rewrite_check_call: bool, primary_review: bool = True) -> None:
    """모델 호출 스팬의 속성(제자리 수정). rewrite_verified: 같은 기록의 판정 단계가 저장한 수정안 상태가 verified 인가.
    rewrite_check_call: 이 호출이 수정안을 검사하려고 보낸 호출(rewrite_recheck 아래)인가. primary_review: 수정안을 제안한 1차 검토 호출인가(교차 확인 호출이 아닌가)."""
    shown_as_accepted = rewrite_verified and primary_review and not rewrite_check_call   # 화면에 '수정안 확인 완료' 로 나오는 수정안은 1차 검토 호출의 응답에 든 그 수정안뿐이다
    resp = attrs.get("response_text")
    if isinstance(resp, str) and resp and not shown_as_accepted:
        obj = _parse_json(resp)
        if isinstance(obj, dict):
            if _has_rewrite(obj.get("rewrite")):
                obj["rewrite"] = REWRITE_HIDDEN
                attrs["response_text"] = json.dumps(obj, ensure_ascii=False, indent=2)
        elif _RAW_REWRITE_KEY.search(resp):   # JSON 으로 읽히지 않는 응답 — 수정안이 들었는지 가려낼 수 없으면 원문을 보여 주지 않는다
            attrs["response_text"] = RESPONSE_HIDDEN
    prompt = attrs.get("prompt_user")
    if rewrite_check_call and not rewrite_verified and isinstance(prompt, str) and prompt:
        if _AD_BLOCK.search(prompt):
            attrs["prompt_user"] = _AD_BLOCK.sub(lambda m: m.group(1) + AD_HIDDEN + m.group(3), prompt, count=1)
        else:   # 예상한 모양이 아니면 수정안이 어디에 들었는지 알 수 없다 — 프롬프트 전체를 가린다
            attrs["prompt_user"] = PROMPT_HIDDEN


def _visit(node: dict, status: Optional[str], in_rewrite_check: bool) -> None:
    here = _stored_status(node)
    if here is not None:
        status = here
    if node.get("name") == "rewrite_recheck":
        in_rewrite_check = True
    if node.get("kind") == "llm":
        attrs = node.setdefault("attributes", {})
        name = str(node.get("name", ""))
        check = in_rewrite_check or name.endswith(":rewrite")
        task = attrs.get("task")
        primary = task == "review" if task is not None else name.startswith("review:")   # 과거 기록에는 task 속성이 없을 수 있다
        redact_llm_attributes(attrs, rewrite_verified=(status == "verified"), rewrite_check_call=check, primary_review=primary)
    for c in node.get("children") or []:
        _visit(c, status, in_rewrite_check)


def redact_trace_dict(trace: dict) -> dict:
    """저장된 기록(Trace.to_dict 모양)의 사본을 돌려준다. 원본은 바꾸지 않는다."""
    out = copy.deepcopy(trace)
    if out.get("root"):
        _visit(out["root"], None, False)
    return out


def redacted_copy(trace: Trace) -> Trace:
    """화면에 그릴 사본. 집계(시간·비용·토큰·단계 수)는 그대로이고 모델 입출력 원문만 가려진다."""
    return Trace.from_dict(redact_trace_dict(trace.to_dict()))
