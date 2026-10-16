"""평가 지표. 모두 순수 함수 — 입력은 EvalRecord 목록."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

UNSAFE_LABELS = {"REJECT", "REVISE"}
FLAG_PREDS = {"REJECT", "REVISE"}


@dataclass
class EvalRecord:
    case_id: str
    label: str                 # APPROVE | REVISE | REJECT
    predicted: str             # APPROVE | REVISE | REJECT | HUMAN_REVIEW
    confidence: float
    label_rules: set[str] = field(default_factory=set)
    predicted_rules: set[str] = field(default_factory=set)
    difficulty: str = "clear"
    category: str = "other"
    latency_ms: float = 0.0
    cost_usd: float = 0.0

    @property
    def abstained(self) -> bool:
        return self.predicted == "HUMAN_REVIEW"

    @property
    def correct(self) -> bool:
        return self.predicted == self.label

    @property
    def unsafe_approve(self) -> bool:
        return self.label in UNSAFE_LABELS and self.predicted == "APPROVE"

    @property
    def false_flag(self) -> bool:
        return self.label == "APPROVE" and self.predicted in FLAG_PREDS

    @property
    def severity_ok(self) -> bool:
        """정확한 등급은 아니어도 '잡아야 할 것은 잡았다' (REJECT ↔ REVISE 혼동은 허용)."""
        if self.abstained:
            return False
        if self.label == "APPROVE":
            return self.predicted == "APPROVE"
        return self.predicted in FLAG_PREDS


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(max(0.0, center - half), 4), round(min(1.0, center + half), 4))


def rate(k: int, n: int) -> dict:
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None, "wilson95": wilson(k, n)}


def summarize(records: list[EvalRecord]) -> dict:
    n = len(records)
    decided = [r for r in records if not r.abstained]
    unsafe_pool = [r for r in records if r.label in UNSAFE_LABELS]
    benign_pool = [r for r in records if r.label == "APPROVE"]
    out = {
        "n": n,
        "coverage": rate(len(decided), n),                                    # 자동 판정 비율 (1 - 사람 검토율)
        "accuracy_decided": rate(sum(r.correct for r in decided), len(decided)),  # 자동 판정 중 정확히 같은 등급
        "catch_rate_decided": rate(sum(r.severity_ok for r in decided), len(decided)),
        "unsafe_approve": rate(sum(r.unsafe_approve for r in unsafe_pool), len(unsafe_pool)),   # 낮을수록 좋음
        "false_flag": rate(sum(r.false_flag for r in benign_pool), len(benign_pool)),           # 낮을수록 좋음
        "human_review": rate(sum(r.abstained for r in records), n),
        "by_difficulty": {},
        "by_category": {},
        "rules": rule_prf(records),
        "calibration": calibration(records),
        "coverage_curve": coverage_curve(records),
        "cost_usd": round(sum(r.cost_usd for r in records), 4),
        "latency_ms_mean": round(sum(r.latency_ms for r in records) / n, 1) if n else 0.0,
    }
    for key, getter in (("by_difficulty", lambda r: r.difficulty), ("by_category", lambda r: r.category)):
        groups: dict[str, list[EvalRecord]] = {}
        for r in records:
            groups.setdefault(getter(r), []).append(r)
        for g, rs in sorted(groups.items()):
            up = [r for r in rs if r.label in UNSAFE_LABELS]
            bp = [r for r in rs if r.label == "APPROVE"]
            out[key][g] = {"n": len(rs), "unsafe_approve": rate(sum(r.unsafe_approve for r in up), len(up)),
                           "false_flag": rate(sum(r.false_flag for r in bp), len(bp)),
                           "human_review": rate(sum(r.abstained for r in rs), len(rs))}
    return out


def rule_prf(records: Iterable[EvalRecord]) -> dict:
    tp: dict[str, int] = {}
    fp: dict[str, int] = {}
    fn: dict[str, int] = {}
    for r in records:
        for rid in r.predicted_rules & r.label_rules:
            tp[rid] = tp.get(rid, 0) + 1
        for rid in r.predicted_rules - r.label_rules:
            fp[rid] = fp.get(rid, 0) + 1
        for rid in r.label_rules - r.predicted_rules:
            fn[rid] = fn.get(rid, 0) + 1
    rules = sorted(set(tp) | set(fp) | set(fn))
    out = {}
    for rid in rules:
        t, p, f = tp.get(rid, 0), fp.get(rid, 0), fn.get(rid, 0)
        prec = t / (t + p) if (t + p) else None
        rec = t / (t + f) if (t + f) else None
        f1 = (2 * prec * rec / (prec + rec)) if prec and rec else (0.0 if (prec is not None and rec is not None) else None)
        out[rid] = {"tp": t, "fp": p, "fn": f, "precision": None if prec is None else round(prec, 3),
                    "recall": None if rec is None else round(rec, 3), "f1": None if f1 is None else round(f1, 3)}
    return out


def calibration(records: list[EvalRecord], bins: int = 10, high_conf: float = 0.8) -> dict:
    """confidence = P(판정이 맞음) 으로 보고 reliability diagram / ECE / Brier / 고신뢰 오류율을 계산한다.
    사람 검토(abstain)는 판정이 아니므로 제외."""
    decided = [r for r in records if not r.abstained]
    if not decided:
        return {"n": 0, "ece": None, "brier": None, "bins": [], "high_confidence_error": rate(0, 0)}
    edges = [i / bins for i in range(bins + 1)]
    rows = []
    ece = 0.0
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        grp = [r for r in decided if (lo <= r.confidence < hi) or (i == bins - 1 and r.confidence == 1.0)]
        if not grp:
            rows.append({"lo": lo, "hi": hi, "n": 0, "confidence": None, "accuracy": None})
            continue
        conf = sum(r.confidence for r in grp) / len(grp)
        acc = sum(r.correct for r in grp) / len(grp)
        ece += abs(acc - conf) * len(grp) / len(decided)
        rows.append({"lo": lo, "hi": hi, "n": len(grp), "confidence": round(conf, 3), "accuracy": round(acc, 3)})
    brier = sum((r.confidence - (1.0 if r.correct else 0.0)) ** 2 for r in decided) / len(decided)
    hc = [r for r in decided if r.confidence >= high_conf]
    return {"n": len(decided), "ece": round(ece, 4), "brier": round(brier, 4), "bins": rows,
            "high_confidence_error": rate(sum(not r.correct for r in hc), len(hc)),
            "confidently_wrong": [r.case_id for r in hc if not r.correct]}


def coverage_curve(records: list[EvalRecord]) -> list[dict]:
    """임계값 τ 를 바꿔 가며: conf ≥ τ 만 자동 판정, 나머지는 사람 검토. 각 τ 에서 커버리지와 위험 승인율.
    '위험 승인율 0% 를 유지하면서 몇 % 를 자동 처리할 수 있나' 를 읽는 표."""
    out = []
    for i in range(10, 20):
        tau = i / 20  # 0.50 .. 0.95
        auto = [r for r in records if not r.abstained and r.confidence >= tau]
        unsafe_pool = [r for r in auto if r.label in UNSAFE_LABELS]
        benign_pool = [r for r in auto if r.label == "APPROVE"]
        out.append({"tau": tau, "coverage": round(len(auto) / len(records), 3) if records else 0.0,
                    "unsafe_approve": round(sum(r.unsafe_approve for r in unsafe_pool) / len(unsafe_pool), 3) if unsafe_pool else 0.0,
                    "false_flag": round(sum(r.false_flag for r in benign_pool) / len(benign_pool), 3) if benign_pool else 0.0,
                    "n_auto": len(auto)})
    return out


def recommend_threshold(records: list[EvalRecord], max_unsafe: float = 0.0) -> float | None:
    """위험 승인율 ≤ max_unsafe 를 만족하는 가장 낮은 τ (= 가장 높은 커버리지)."""
    best = None
    for row in coverage_curve(records):
        if row["n_auto"] and row["unsafe_approve"] <= max_unsafe:
            best = row["tau"] if best is None else min(best, row["tau"])
    return best
