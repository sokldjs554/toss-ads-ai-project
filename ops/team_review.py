"""Server-only shared review commands.

The caller cannot upload a queue snapshot, choose another annotation identity,
or read another reviewer's unfinished work. PostgreSQL serializes this pure
transition under a row lock (team_review_store.py). Accounts identify credentials,
not independently verified people. The state and access-key digests stay server-side.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import secrets
from uuid import UUID

from core.schemas import AdCopy, Fact, PipelineRun
from ops.dataset_tools import export_rows_report
from ops.labeling import AdjudicationRecord, Annotation, validate_annotator_name
from ops.review_queue import ReviewQueue

MAX_ITEMS = 2000
MAX_ACCOUNTS = 100
MAX_RECEIPTS = 10000
MAX_STATE_BYTES = 30_000_000
MAX_COMMAND_BYTES = 1_000_000
OWNER_ID = "acct_owner"
ROLES = {"owner", "reviewer", "adjudicator"}
READ_ACTIONS = {"overview", "view", "accounts", "disagreements", "export"}
SECURITY_ACTIONS = {"set_active", "replace_key"}


class TeamReviewError(ValueError):
    pass


class AuthenticationError(TeamReviewError):
    pass


class PermissionDenied(TeamReviewError):
    pass


class InvalidCommand(TeamReviewError):
    pass


class RequestConflict(TeamReviewError):
    pass


class CapacityReached(TeamReviewError):
    pass


def new_access_key() -> str:
    """256 random bits. The returned value is displayed once to its issuer."""
    return "pfk_" + secrets.token_urlsafe(32)


def key_digest(key: str) -> str:
    if not isinstance(key, str) or not re.fullmatch(r"pfk_[A-Za-z0-9_-]{43}", key):
        raise AuthenticationError("접근키를 확인해 주세요.")
    return hashlib.sha256(key.encode("ascii")).hexdigest()


def _digest(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise InvalidCommand("접근키 발급 정보가 올바르지 않습니다.")
    return value


def _now(now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise InvalidCommand("시간대 정보가 필요합니다.")
    return now.astimezone(timezone.utc)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def new_state(owner_digest: str, *, now: datetime | None = None) -> dict:
    now = _now(now)
    return {
        "schema_version": 1, "revision": 0,
        "queue": ReviewQueue(required_labels=2).to_jsonl(),
        "accounts": {OWNER_ID: {"id": OWNER_ID, "name": "관리자", "role": "owner", "active": True, "created_at": now.isoformat()}},
        "keys": {_digest(owner_digest): {"account_id": OWNER_ID, "expires_at": (now + timedelta(days=90)).isoformat()}},
        "receipts": {},
    }


def authenticate(state: dict, access_key: str, *, now: datetime | None = None) -> dict:
    now = _now(now)
    digest = key_digest(access_key)
    record = state.get("keys", {}).get(digest)
    account = state.get("accounts", {}).get(record.get("account_id")) if isinstance(record, dict) else None
    if (not account or not account.get("active") or account.get("role") not in ROLES
            or datetime.fromisoformat(record["expires_at"]) <= now):
        raise AuthenticationError("접근키가 만료됐거나 사용할 수 없습니다. 관리자에게 새 키를 요청해 주세요.")
    return deepcopy(account)


def _allow(account: dict, *roles: str) -> None:
    if account["role"] not in roles:
        raise PermissionDenied("이 작업을 할 권한이 없습니다.")


def _fields(payload: dict, allowed: set[str], required: set[str] = frozenset()) -> None:
    if set(payload) - allowed or required - set(payload):
        raise InvalidCommand("요청 항목이 올바르지 않습니다.")
    types = {
        "account_id": str, "name": str, "role": str, "key_digest": str,
        "item_id": str, "dataset_id": str, "category": str, "product_name": str,
        "landing_text": str, "facts": list, "copy": dict, "run": dict,
        "decision": str, "rule_ids": list, "cited_spans": list, "rationale": str,
        "active": bool, "synthetic": bool, "include_synthetic": bool,
    }
    if any(type(value) is not types[name] for name, value in payload.items()):
        raise InvalidCommand("입력 형식이 올바르지 않아 저장하지 않았습니다. 항목을 확인해 주세요.")


def _target(state: dict, value: str) -> dict:
    if not isinstance(value, str) or value not in state["accounts"]:
        raise InvalidCommand("담당자 계정을 찾을 수 없습니다.")
    return state["accounts"][value]


def _install_key(state: dict, account: dict, digest: str, now: datetime) -> None:
    digest = _digest(digest)
    if digest in state["keys"]:
        raise InvalidCommand("이미 발급한 접근키입니다. 새 키를 만들어 주세요.")
    state["keys"] = {k: v for k, v in state["keys"].items() if v["account_id"] != account["id"]}
    days = 90 if account["role"] == "owner" else 30
    state["keys"][digest] = {"account_id": account["id"], "expires_at": (now + timedelta(days=days)).isoformat()}


def _dispatch(state: dict, q: ReviewQueue, actor: dict, action: str, p: dict, now: datetime) -> dict:
    aid = actor["id"]
    if action == "overview":
        _fields(p, set())
        result = {"account": actor,
                  "held_items": q.held_by(aid) if actor["role"] == "reviewer" else [],
                  "required_labels": q.required_labels}
        # Even aggregate agreement counts can reveal a previous label when a
        # workspace contains one item. Only owners need this operational view.
        if actor["role"] == "owner":
            result["stats"] = q.stats().to_dict()
        return result
    if action == "accounts":
        _allow(actor, "owner")
        _fields(p, set())
        return {"accounts": list(state["accounts"].values())}
    if action == "create_account":
        _allow(actor, "owner")
        _fields(p, {"account_id", "name", "role", "key_digest"}, {"account_id", "name", "role", "key_digest"})
        if len(state["accounts"]) >= MAX_ACCOUNTS:
            raise CapacityReached("담당자 계정 한도에 도달했습니다.")
        if not re.fullmatch(r"acct_[0-9a-f]{32}", str(p["account_id"])) or p["account_id"] in state["accounts"]:
            raise InvalidCommand("새 계정 ID가 필요합니다.")
        if p["role"] not in {"reviewer", "adjudicator"}:
            raise InvalidCommand("검수자 또는 조정자 역할을 선택해 주세요.")
        try:
            name = validate_annotator_name(p["name"])
        except ValueError as exc:
            raise InvalidCommand(str(exc)) from None
        if any(a["name"].casefold() == name.casefold() for a in state["accounts"].values()):
            raise InvalidCommand("이미 있는 담당자 이름입니다. 기존 계정의 키를 재발급할 수 있습니다.")
        account = {"id": p["account_id"], "name": name, "role": p["role"], "active": True, "created_at": now.isoformat()}
        _install_key(state, account, p["key_digest"], now)
        state["accounts"][account["id"]] = account
        return {"account_id": account["id"], "account": account}
    if action == "replace_key":
        _allow(actor, "owner")
        _fields(p, {"account_id", "key_digest"}, {"account_id", "key_digest"})
        account = _target(state, p["account_id"])
        _install_key(state, account, p["key_digest"], now)
        return {"account_id": account["id"]}
    if action == "set_active":
        _allow(actor, "owner")
        _fields(p, {"account_id", "active"}, {"account_id", "active"})
        account = _target(state, p["account_id"])
        if type(p["active"]) is not bool or account["role"] == "owner":
            raise InvalidCommand("관리자 계정은 이 화면에서 비활성화할 수 없습니다.")
        account["active"] = p["active"]
        if not p["active"]:
            for iid in list(q.held_by(account["id"])):
                q.release(account["id"], iid)
        return {"account_id": account["id"], "active": account["active"]}
    if action == "enqueue_ad":
        _allow(actor, "owner")
        _fields(p, {"dataset_id", "copy", "product_name", "category", "facts", "landing_text", "synthetic"}, {"dataset_id", "copy", "category", "synthetic"})
        if type(p["synthetic"]) is not bool or not isinstance(p["dataset_id"], str) or not 1 <= len(p["dataset_id"]) <= 100:
            raise InvalidCommand("출처 ID와 합성 예시 여부를 확인해 주세요.")
        item = q.enqueue_ad(AdCopy.model_validate(p["copy"]), dataset_id=p["dataset_id"], category=p["category"],
                            product_name=p.get("product_name", ""), facts=[Fact.model_validate(f) for f in p.get("facts", [])],
                            landing_text=p.get("landing_text", ""), synthetic=p["synthetic"])
        return {"item_id": item.id}
    if action == "enqueue_run":
        _allow(actor, "owner")
        _fields(p, {"run", "landing_text", "synthetic"}, {"run", "synthetic"})
        if type(p["synthetic"]) is not bool:
            raise InvalidCommand("합성 예시 여부를 확인해 주세요.")
        run = PipelineRun.model_validate(p["run"])
        result = q.enqueue_from_run(run, landing_text=p.get("landing_text", ""))
        for iid in result.added:
            q.get(iid).synthetic = p["synthetic"]
        return {"added": result.added, "duplicates": result.duplicates, "not_eligible": result.not_eligible}
    if action == "claim":
        _allow(actor, "reviewer")
        _fields(p, set())
        item = q.claim(aid, hide_prelabel=True)
        return {"item": item.model_dump(mode="json") if item else None}
    if action == "view":
        _allow(actor, "reviewer")
        _fields(p, {"item_id"}, {"item_id"})
        if p["item_id"] not in q.held_by(aid):
            raise PermissionDenied("현재 본인에게 배정된 광고만 볼 수 있습니다.")
        return {"item": q.view(p["item_id"], aid, hide_prelabel=True).model_dump(mode="json")}
    if action == "release":
        _allow(actor, "reviewer")
        _fields(p, {"item_id"}, {"item_id"})
        q.release(aid, p["item_id"])
        return {"item_id": p["item_id"], "released": True}
    if action == "submit":
        _allow(actor, "reviewer")
        _fields(p, {"item_id", "decision", "rule_ids", "cited_spans", "rationale"}, {"item_id", "decision"})
        annotation = Annotation.model_validate({**p, "annotator": aid, "created_at": now, "prelabel_mode": "hidden"})
        result = q.submit(annotation)
        # Do not reveal a second reviewer's decision, even after this submission.
        return {"item_id": result.item_id, "submitted": True, "warnings": result.warnings}
    if action == "disagreements":
        _allow(actor, "adjudicator")
        _fields(p, set())
        return {"items": [{"item_id": it.id, "ad_copy": it.ad_copy.model_dump(mode="json"),
                           "facts": [f.model_dump(mode="json") for f in it.facts],
                           "annotations": [a.model_dump(mode="json") for a in it.annotations]}
                          for it in q.disagreements() if aid not in it.labelers()]}
    if action == "adjudicate":
        _allow(actor, "adjudicator")
        _fields(p, {"item_id", "decision", "rule_ids", "cited_spans", "rationale"}, {"item_id", "decision", "rationale"})
        record = AdjudicationRecord.model_validate({**{k: v for k, v in p.items() if k != "rationale"},
                                                   "reason": p["rationale"], "adjudicator": aid, "created_at": now})
        q.adjudicate(record)
        return {"item_id": p["item_id"], "adjudicated": True}
    if action == "export":
        _allow(actor, "owner", "adjudicator")
        _fields(p, {"include_synthetic"})
        if type(p.get("include_synthetic", False)) is not bool:
            raise InvalidCommand("내보내기 범위를 확인해 주세요.")
        result = export_rows_report(q.items(), include_synthetic=p.get("include_synthetic", False), allow_single_labeler=False, required_labels=2)
        return {"rows": result.rows, "synthetic_included": p.get("include_synthetic", False),
                "identity_verified": False, "independence_verified": False,
                "note": "서로 다른 계정의 기록입니다. 실제 참여자와 독립 검수 여부는 별도 확인이 필요합니다."}
    raise InvalidCommand("지원하지 않는 공동 검수 요청입니다.")


def execute(state: dict, access_key: str, action: str, payload: dict, *, request_id: str, now: datetime | None = None) -> tuple[dict, dict]:
    """Return a new state only on success. Store adapters commit it atomically.

Authentication always precedes receipt lookup, so revocation/expiry also denies
retries. IDs are scoped to immutable account identity, not display names or keys.
"""
    now = _now(now)
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise InvalidCommand("공동 저장 형식을 확인할 수 없습니다.")
    actor = authenticate(state, access_key, now=now)
    try:
        if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
            raise ValueError
        if not isinstance(action, str) or not isinstance(payload, dict):
            raise ValueError
        command = _json({"action": action, "payload": payload})
    except (TypeError, ValueError) as exc:
        raise InvalidCommand("요청 ID 또는 내용이 올바르지 않습니다.") from exc
    if len(command.encode("utf-8")) > MAX_COMMAND_BYTES:
        raise CapacityReached("한 번에 보낼 수 있는 광고 정보 크기를 넘었습니다.")
    fingerprint = hashlib.sha256(command.encode("utf-8")).hexdigest()
    receipt_id = actor["id"] + ":" + request_id
    previous = state["receipts"].get(receipt_id)
    if previous:
        if previous["fingerprint"] != fingerprint:
            raise RequestConflict("이미 사용한 요청 ID의 내용이 달라 저장하지 않았습니다.")
        return state, deepcopy(previous["result"])
    security_command = action in SECURITY_ACTIONS and actor["role"] == "owner"
    if action not in READ_ACTIONS and not security_command and len(state["receipts"]) >= MAX_RECEIPTS:
        raise CapacityReached("이 공간의 작업 기록 한도에 도달했습니다. 관리자에게 문의해 주세요.")
    updated = deepcopy(state)
    q = ReviewQueue.from_jsonl(updated["queue"], clock=lambda: now)
    q.reap_expired()
    result = _dispatch(updated, q, actor, action, payload, now)
    if len(q) > MAX_ITEMS:
        raise CapacityReached("이 공간의 광고 한도에 도달했습니다.")
    updated["queue"] = q.to_jsonl()
    changed = updated != state or action not in READ_ACTIONS
    updated["revision"] = state["revision"] + int(changed)
    result = {**result, "revision": updated["revision"]}
    if action not in READ_ACTIONS:
        updated["receipts"][receipt_id] = {"fingerprint": fingerprint, "action": action, "at": now.isoformat(), "result": deepcopy(result)}
    # These are admission limits for normal work. Only a strictly bounded,
    # owner-authorized revocation/key replacement can exceed them, so filling
    # the work journal can never prevent access from being withdrawn.
    if action not in READ_ACTIONS and not security_command and len(_json(updated).encode("utf-8")) > MAX_STATE_BYTES:
        raise CapacityReached("이 공간의 저장 크기 한도에 도달했습니다.")
    return updated, result
