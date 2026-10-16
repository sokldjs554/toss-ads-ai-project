"""회귀 게이트. 기준선 대비 '안전 방향' 지표가 나빠지면 실패한다 — 정확도 평균이 아니라 방향을 본다.

게이트 (기본값):
  - 위험 승인율: 기준선보다 조금이라도 오르면 실패 (절대 0.0 허용 오차)
  - 오탐율(false_flag): 기준선 + 5%p 초과면 실패
  - 사람 검토율: 기준선 + 15%p 초과면 실패 (전부 사람에게 넘기면 '안전' 하지만 쓸모없다)
  - 규칙별 recall: 기준선에서 1.0 이던 규칙이 1.0 아래로 떨어지면 경고(실패 아님)
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GateResult:
    passed: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    deltas: dict = field(default_factory=dict)


def _r(summary: dict, key: str) -> float:
    v = summary.get(key, {}).get("rate")
    return float(v) if v is not None else 0.0


def gate(current: dict, baseline: dict | None, unsafe_tol: float = 0.0, false_flag_tol: float = 0.05,
         human_review_tol: float = 0.15) -> GateResult:
    res = GateResult(passed=True)
    cur = current["summary"]
    if baseline is None:
        res.warnings.append("기준선 없음 — 이번 결과를 기준선으로 저장하세요")
        return res
    base = baseline["summary"]
    for key, tol, direction in (("unsafe_approve", unsafe_tol, "up"), ("false_flag", false_flag_tol, "up"), ("human_review", human_review_tol, "up")):
        c, b = _r(cur, key), _r(base, key)
        res.deltas[key] = round(c - b, 4)
        if c > b + tol + 1e-9:
            res.passed = False
            res.failures.append(f"{key}: {b:.3f} → {c:.3f} (허용 +{tol:.2f})")
    for rid, m in base.get("rules", {}).items():
        if m.get("recall") == 1.0:
            cr = cur.get("rules", {}).get(rid, {}).get("recall")
            if cr is not None and cr < 1.0:
                res.warnings.append(f"{rid} recall 1.0 → {cr}")
    cur_mode, base_mode = current.get("mode"), baseline.get("mode")
    if cur_mode != base_mode:
        res.warnings.append(f"모드가 다름 (기준선 {base_mode}, 현재 {cur_mode}) — 비교는 참고용")
    return res
