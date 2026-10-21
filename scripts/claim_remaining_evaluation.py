"""2026-10-21 후속 실측의 별도 $4 예약. 과거 $10 예약은 그대로 보존한다.

사용자는 남은 작업과 유료 호출을 계속 승인했다. $4는 이번 작업에서 정한
추가 지출 상한이며 사용자가 직접 지정한 금액이라고 주장하지 않는다.
GitHub에 예약을 저장한 다음 단계에서만 제공자 키를 전달한다.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

BRANCH = "codex/finish-ads-remaining-20261021"
SCOPE = "remaining-20261021"
TOTAL_CAP = Decimal("4")
REQUEST = Path("evals/live_request_20261021.json")
LEDGER = Path("evals/reports/live_features/reservations_remaining_20261021.json")
PRIOR_LEDGER = Path("evals/reports/live_features/reservations.json")


def _amount(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid reservation amount") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError("invalid reservation amount")
    return result


def reserve(request: dict, previous: dict | None, *, workflow_run_id: str,
            request_sha256: str, prior_ledger_sha256: str) -> dict:
    if request.get("budget_scope") != SCOPE:
        raise ValueError("wrong supplemental budget scope")
    if request.get("prior_reservation_ledger_sha256") != prior_ledger_sha256:
        raise ValueError("prior reservation ledger differs from the frozen request")
    if previous is None:
        previous = {"version": 1, "scope": SCOPE, "total_cap_usd": "4",
                    "prior_ledger_sha256": prior_ledger_sha256, "reserved_total_usd": "0", "reservations": []}
    if (previous.get("version") != 1 or previous.get("scope") != SCOPE
            or _amount(previous.get("total_cap_usd")) != TOTAL_CAP
            or previous.get("prior_ledger_sha256") != prior_ledger_sha256):
        raise ValueError("invalid durable supplemental ledger")
    rows = list(previous["reservations"])
    ids = [row["run_id"] for row in rows]
    if len(set(ids)) != len(ids) or request["run_id"] in ids:
        raise ValueError("this evaluation has already reserved its budget")
    total = sum((_amount(row["reserved_cap_usd"]) for row in rows), Decimal("0"))
    if Decimal(str(previous.get("reserved_total_usd"))) != total:
        raise ValueError("durable reservation total does not match its rows")
    amount = _amount(request["cost_cap_usd"])
    if total + amount > TOTAL_CAP:
        raise ValueError("cumulative supplemental budget would be exceeded")
    rows.append({"run_id": request["run_id"], "reserved_cap_usd": str(amount),
                 "workflow_run_id": workflow_run_id, "manifest_sha256": request_sha256,
                 "reserved_at": datetime.now(timezone.utc).isoformat()})
    return {"version": 1, "scope": SCOPE, "total_cap_usd": "4",
            "reserved_total_usd": str(total + amount), "prior_ledger_sha256": prior_ledger_sha256,
            "authorization": "Continuation and paid calls authorized by the user; additional USD4 cap chosen by the implementer.",
            "policy": "과거 $10 예약은 별도 보존. 이번 예약도 실패·중단 시 반환하거나 다시 사용하지 않음.",
            "reservations": rows}


def main() -> None:
    if os.environ.get("GITHUB_REF_NAME") != BRANCH:
        raise ValueError("paid evaluation is restricted to its authorized work branch")
    sha = lambda ref: subprocess.check_output(["git", "rev-parse", ref], text=True).strip()
    if sha("HEAD") != sha("FETCH_HEAD"):
        raise ValueError("branch moved; old workflow cannot reserve or spend again")
    raw, prior = REQUEST.read_bytes(), PRIOR_LEDGER.read_bytes()
    previous = json.loads(LEDGER.read_text()) if LEDGER.exists() else None
    result = reserve(json.loads(raw), previous, workflow_run_id=os.environ["GITHUB_RUN_ID"],
                     request_sha256=hashlib.sha256(raw).hexdigest(),
                     prior_ledger_sha256=hashlib.sha256(prior).hexdigest())
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Supplemental reservation: ${result['reserved_total_usd']} / $4. Prior reservations retained; no provider call yet.")


if __name__ == "__main__":
    main()
