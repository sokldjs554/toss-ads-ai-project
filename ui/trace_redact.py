"""처리 기록 화면용 가림 처리 — 재검사를 통과하지 못한 자동 수정안의 문구는 어느 화면에도 보여 주지 않는다 (FM-19).

처리 기록은 모델에 보낸 프롬프트와 받은 응답을 원문 그대로 남긴다. 그 안에는 두 곳에 수정안 문구가 들어 있다.
- 검토 호출의 응답(response_text)에 든 `rewrite` 값: 모델이 제안한 수정안 전체.
- 수정안 검사 호출(rewrite_recheck 아래)의 프롬프트(prompt_user)에 든 `[소재]` 블록: 검사를 받는 수정안 전체.
  블록의 끝은 수정안 문구 안의 빈 줄이나 대괄호 줄이 아니라 프롬프트가 정한 다음 구역 표지(`[광고주 사실 목록]`, 앞에 빈 줄·뒤에 줄바꿈)다 — agents/reviewer.py `_user_prompt` 의 구역 순서 그대로이며,
  수정안 문구가 같은 표지를 흉내 낼 수 있으므로 프롬프트에서 마지막으로 나오는 표지를 끝으로 본다(더 많이 가리는 쪽). 프롬프트의 모양을 알아볼 수 없으면 프롬프트 전체를 가린다.
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

# agents/reviewer.py `_user_prompt` 가 만드는 프롬프트의 구역 순서: [소재] → [광고주 사실 목록] → ([랜딩 페이지 텍스트(발췌)]) → [결정론 검사가 찾은 확정 finding].
# 구역 표지는 '줄 전체가 정확히 그 표지' 인 줄이다. 수정안 문구(헤드라인·본문·CTA·고지·주장)는 어떤 줄이든 담을 수 있어 첫 표지를 믿을 수 없다.
_AD_OPEN = "[소재]\n헤드라인: "
_FACTS_HEADER = "\n\n[광고주 사실 목록]\n"
# 응답 JSON 의 rewrite 키 (JSON 은 큰따옴표, 파이썬 repr 은 작은따옴표). 비어 있는 값(null·{}·[]·"")은 가릴 문구가 없다
_RAW_REWRITE_KEY = re.compile(r"""["']rewrite["']\s*:(?!\s*(?:null\b|\{\s*\}|\[\s*\]|""|''))\s*\S""", re.IGNORECASE)   # 공백 길이를 줄여 보는 되돌림으로 null 을 지나치지 않게 공백은 앞보기 안에 둔다
_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _stored_status(node: dict) -> Optional[str]:
    """이 단계 바로 아래의 판정(adjudicate) 단계가 저장한 수정안 상태. 판정 단계가 없으면 None, 있는데 상태가 없으면 ''."""
    for c in node.get("children") or []:
        if c.get("name") == "adjudicate":
            return str((c.get("attributes") or {}).get("rewrite_status") or "")
    return None


def _loads(text: str, seen: list) -> Any:
    """JSON 으로 읽는다. 같은 키가 겹쳐 앞의 값이 사라지는 경우까지 보려고, 읽는 도중 `rewrite` 키에 든 값을 모두 seen 에 모은다."""
    def pairs(items):
        out = {}
        for k, v in items:
            if isinstance(k, str) and k.strip().lower() == "rewrite" and _has_rewrite(v):
                seen.append(v)
            out[k] = v   # 같은 키가 또 나오면 뒤의 값이 남는다 (앞의 값은 seen 에만 남는다)
        return out
    return json.loads(text, object_pairs_hook=pairs)


def _parse_json(text: str, seen: list) -> Any:
    try:
        return _loads(text, seen)
    except (TypeError, ValueError, RecursionError):
        pass
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)   # 코드 울타리로 감싼 응답
    if m:
        try:
            return _loads(m.group(1), seen)
        except (ValueError, RecursionError):
            return None
    return None


def _has_rewrite(value: Any) -> bool:
    return value not in (None, "", {}, [])


def _scrub(value: Any) -> Any:
    """읽은 JSON 에서 어느 깊이에 있든 `rewrite` 키의 값을 가린다 (최상위에만 있다고 가정하지 않는다)."""
    if isinstance(value, dict):
        return {k: (REWRITE_HIDDEN if isinstance(k, str) and k.strip().lower() == "rewrite" and _has_rewrite(v) else _scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _raw_has_rewrite_key(text: str) -> bool:
    """읽히지 않는 응답에 비어 있지 않은 `rewrite` 값이 있는가 — 유니코드 이스케이프(\\u0072)와 이스케이프된 따옴표(\\")로 숨긴 키도 본다."""
    plain = _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)
    return any(_RAW_REWRITE_KEY.search(t) for t in (text, plain, plain.replace('\\"', '"')))


def _redact_response(resp: str) -> str:
    seen: list = []
    obj = _parse_json(resp, seen)
    if obj is not None and seen:
        return json.dumps(_scrub(obj), ensure_ascii=False, indent=2)   # 겹친 키로 사라진 값도, JSON 밖의 글도 다시 쓴 사본에는 없다
    if _raw_has_rewrite_key(resp):
        return RESPONSE_HIDDEN   # JSON 으로 읽히지 않거나, 읽은 구조 밖(두 번째 코드 울타리·문장 속)에 rewrite 값이 있다 — 가려낼 수 없으면 원문을 보여 주지 않는다
    return resp


def _redact_prompt(prompt: str) -> str:
    """수정안 검사 프롬프트의 [소재] 블록(수정안 전체)을 가린다. 블록의 끝은 마지막 `[광고주 사실 목록]` 구역 표지 — 수정안 문구가 흉내 낸 표지는 그 앞에 있다.
    예상한 모양(`[소재]` 로 시작하고 그 뒤에 사실 목록 표지가 있음)이 아니면 수정안이 어디에 들었는지 알 수 없으므로 프롬프트 전체를 가린다."""
    if not prompt.startswith(_AD_OPEN):
        return PROMPT_HIDDEN
    cut = prompt.rfind(_FACTS_HEADER)
    if cut < 0:
        return PROMPT_HIDDEN
    return "[소재]\n" + AD_HIDDEN + prompt[cut:]


def redact_llm_attributes(attrs: dict, *, rewrite_verified: bool, rewrite_check_call: bool, primary_review: bool = True) -> None:
    """모델 호출 스팬의 속성(제자리 수정). rewrite_verified: 같은 기록의 판정 단계가 저장한 수정안 상태가 verified 인가.
    rewrite_check_call: 이 호출이 수정안을 검사하려고 보낸 호출(rewrite_recheck 아래)인가. primary_review: 수정안을 제안한 1차 검토 호출인가(교차 확인 호출이 아닌가)."""
    shown_as_accepted = rewrite_verified and primary_review and not rewrite_check_call   # 화면에 '수정안 확인 완료' 로 나오는 수정안은 1차 검토 호출의 응답에 든 그 수정안뿐이다
    resp = attrs.get("response_text")
    if isinstance(resp, str) and resp and not shown_as_accepted:
        try:
            attrs["response_text"] = _redact_response(resp)
        except Exception:   # noqa: BLE001 — 가리는 도중 무엇이 틀어져도 원문을 내보내지 않는다
            attrs["response_text"] = RESPONSE_HIDDEN
    prompt = attrs.get("prompt_user")
    if rewrite_check_call and not rewrite_verified and isinstance(prompt, str) and prompt:
        try:
            attrs["prompt_user"] = _redact_prompt(prompt)
        except Exception:   # noqa: BLE001
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
