"""유료 호출 전에 실행 예약을 Git에 기록한다. 키나 API는 사용하지 않는다.

동일 manifest의 재실행은 허용하지 않고 전체 후속 작업 예약액을 $10 이내로
제한한다. 중단/실패 실행도 예약액 전부를 유지하므로 불명확한 청구를 재사용하지 않는다.
workflow는 이 파일의 결과를 원격 브랜치에 푸시한 다음에만 API 키를 전달한다.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

LEDGER = Path("evals/reports/live_features/reservations.json")
TOTAL_CAP = Decimal("10")
BRANCH = "codex/close-ads-gaps-20261020"


def reserve(request: dict, previous: dict | None, *, workflow_run_id: str, request_sha256: str) -> dict:
    ledger = previous or {"version": 1, "total_cap_usd": "10", "reservations": []}
    if ledger.get("version") != 1 or Decimal(str(ledger.get("total_cap_usd"))) != TOTAL_CAP:
        raise ValueError("invalid durable budget ledger")
    rows = list(ledger["reservations"])
    if any(row["run_id"] == request["run_id"] for row in rows):
        raise ValueError("this evaluation run has already reserved its budget")
    amount = Decimal(str(request["cost_cap_usd"]))
    if not amount.is_finite() or not 0 < amount <= TOTAL_CAP:
        raise ValueError("invalid requested budget")
    existing = [Decimal(str(row["reserved_cap_usd"])) for row in rows]
    if any(not cap.is_finite() or cap <= 0 for cap in existing):
        raise ValueError("invalid prior reservation")
    total = sum(existing, Decimal("0")) + amount
    if total > TOTAL_CAP:
        raise ValueError("cumulative approved budget would be exceeded")
    rows.append({"run_id": request["run_id"], "reserved_cap_usd": str(amount),
                 "workflow_run_id": workflow_run_id, "manifest_sha256": request_sha256,
                 "reserved_at": datetime.now(timezone.utc).isoformat()})
    return {"version": 1, "total_cap_usd": "10", "reserved_total_usd": str(total),
            "policy": "예약액은 성공·실패·중단 여부와 관계없이 다시 사용하지 않음", "reservations": rows}


def main() -> None:
    if os.environ.get("GITHUB_REF_NAME") != BRANCH:
        raise ValueError("paid evaluation is restricted to the approved branch")
    sha = lambda ref: subprocess.check_output(["git", "rev-parse", ref], text=True).strip()
    if sha("HEAD") != sha("FETCH_HEAD"):
        raise ValueError("branch moved; old workflow cannot reserve or spend again")
    path = Path("evals/live_request_20261020.json")
    raw = path.read_bytes()
    old = json.loads(LEDGER.read_text()) if LEDGER.exists() else None
    result = reserve(json.loads(raw), old, workflow_run_id=os.environ["GITHUB_RUN_ID"],
                     request_sha256=hashlib.sha256(raw).hexdigest())
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Reserved cumulative cap: ${result['reserved_total_usd']} / $10; no provider request made yet")


if __name__ == "__main__":
    main()
