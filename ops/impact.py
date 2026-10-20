"""효과 모델 — '측정한 것' 과 '가정한 것' 을 섞지 않고 What-if 로 계산한다.

이 프로젝트에는 실제 운영 데이터가 없다. 그래서 효과를 '이렇게 됐다' 로 쓰지 않고 '이 가정이면 이렇게 계산된다' 로만 낸다.

섞이지 않게 하는 방식
- 측정값(`Measured`): 저장된 평가 리포트(`evals/reports/compare_live.json`, 없으면 `baseline_partial.json`)에서 *읽는다*. 실제 모델로 끝까지 돈 결과만 쓰고,
  예시 실행(대역)·코드 검사만·중단됐거나 장애가 섞인 결과는 쓰지 않는다. 읽지 못한 항목은 지어내지 않고 '측정 없음' 으로 둔다.
- 가정(`Assumptions`): 하루 검수 건수·사람 1건당 검수 분·시급·담당자 확인 건의 처리 분 등 사용자가 넣는 값. 기본값은 *예시* 이고 근거가 없다.
- 계산(`estimate`): 측정값의 신뢰구간(윌슨 95%) 하·상한과 가정의 범위를 곱해 낙관·기준·비관 값을 낸다. 모든 항목에 '측정/가정/계산' 표지와 출처를 붙인다.

이 숫자가 의미하지 않는 것
- 실제 운영 효과가 아니다. 평가 사례(저자가 쓴 가상 광고 110건 안팎)에서 잰 담당자 확인율을, 사용자가 가정한 건수·시간·시급에 곱한 값이다.
- 평가 사례의 구성(문제 있는 광고가 약 2/3, 문제없는 광고는 모두 '무서운 단어가 있는' 사례)은 실제 광고의 구성이 아니다. 담당자 확인율은 구성에 크게 좌우된다.
  `violation_share`(문제 있는 광고 비율, 가정) 를 주면 측정한 층별 비율(문제 있는 광고 / 문제없는 광고)을 그 구성으로 다시 가중하지만, 그 비율 자체도 이 평가 사례에서 잰 값이다.
- 측정한 담당자 확인율은 평가 경로의 값이다. 평가 경로에는 다른 모델 계열의 교차 확인과 가상 사용자 단계가 없다. 실제 파이프라인은 경계 사례에서 교차 확인을 하고 두 계열이 갈리면 담당자 확인으로 보내므로, 실제 확인율은 이보다 높을 수 있다(얼마나인지는 측정하지 못했다).
- 신뢰구간은 '사례가 서로 독립이고 이 평가 사례가 모집단의 표본' 이라는 가정 아래의 값이다. 규칙과 사례를 같은 저자가 만들었고 개발용 사례는 규칙을 고치는 데 썼다.
- 사람이 이 도구를 믿고 덜 꼼꼼히 보게 되는 행동 변화·광고주의 반응·정책 변경은 모델에 없다. 자동 판단으로 끝난 건의 일부를 사람이 다시 보는 비율(`spot_check_rate`)만 가정으로 넣었다.
"""
from __future__ import annotations

import itertools
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from evals.metrics import wilson

NOT_REAL_EFFECT = "이 평가 사례와 이 가정에서 계산한 값이며 실제 운영 효과가 아닙니다."
REPORTS_DIR = Path(__file__).resolve().parents[1] / "evals" / "reports"
COMPARE_FILE = "compare_live.json"
BASELINE_FILE = "baseline_partial.json"
SPLITS = ("all", "dev", "holdout")
SPLIT_LABEL = {"all": "개발용 + 최종 확인용 전체", "dev": "개발용 (규칙을 고치는 데 쓴 사례)", "holdout": "최종 확인용 (규칙을 고친 뒤 처음 쓴 사례)"}
_HOLDOUT_DATASETS = {"holdout"}


class ImpactInputError(ValueError):
    """가정 입력이 비었거나 계산할 수 없는 값일 때. messages 에 쉬운 한국어 문장이 모여 있다."""

    def __init__(self, messages: list[str]):
        super().__init__(" ".join(messages))
        self.messages = messages


# ---------------------------------------------------------------------------
# 가정
# ---------------------------------------------------------------------------
_FIELD_LABEL = {
    "daily_reviews": "하루 검수 건수", "minutes_per_review": "사람 1건당 검수 시간(분)", "hourly_wage_krw": "사람 시급(원)",
    "minutes_per_flagged": "담당자 확인 건의 처리 시간(분)", "spot_check_rate": "자동 판단 건을 사람이 다시 보는 비율", "workdays_per_month": "한 달 근무일 수",
    "usd_krw": "원/달러 환율", "violation_share": "실제 광고 중 문제 있는 광고의 비율", "what_if_human_review_rate": "직접 가정한 담당자 확인율",
}
_POSITIVE = ("daily_reviews", "minutes_per_review", "hourly_wage_krw", "minutes_per_flagged", "workdays_per_month", "usd_krw")
_UNIT_INTERVAL = ("spot_check_rate", "violation_share", "what_if_human_review_rate")


@dataclass(frozen=True)
class Assumptions:
    """사용자가 정하는 가정. 기본값(`EXAMPLE_ASSUMPTIONS`)은 예시이고 근거가 없다 — 이 프로젝트가 아는 실제 값이 아니다."""

    daily_reviews: Optional[float] = None
    minutes_per_review: Optional[float] = None
    hourly_wage_krw: Optional[float] = None
    minutes_per_flagged: Optional[float] = None
    spot_check_rate: float = 0.0
    workdays_per_month: float = 20.0
    usd_krw: float = 1400.0
    violation_share: Optional[float] = None            # None 이면 평가 사례 구성 그대로
    what_if_human_review_rate: Optional[float] = None  # 값이 있으면 측정 대신 이 값을 쓴다 (측정이 없을 때의 What-if)
    spread: float = 0.30                               # 범위를 따로 주지 않은 가정의 ± 폭 (예시 값, 근거 없음)
    ranges: Mapping[str, tuple[float, float]] = field(default_factory=dict)   # 항목별 (하한, 상한) 직접 지정


EXAMPLE_ASSUMPTIONS = Assumptions(daily_reviews=200, minutes_per_review=5, hourly_wage_krw=15_000, minutes_per_flagged=8, spot_check_rate=0.05,
                                  workdays_per_month=20, usd_krw=1400, violation_share=None, spread=0.30)
EXAMPLE_NOTE = "예시 값입니다. 근거가 없고, 토스·토스애즈의 실제 검수 건수·시간·인건비와 무관합니다. 직접 바꿔 넣으세요."


def _isnum(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def validate_assumptions(a: Assumptions) -> list[str]:
    """계산할 수 없는 입력을 쉬운 한국어 문장으로 돌려준다. 빈 리스트면 계산할 수 있다."""
    errs: list[str] = []
    for name in _POSITIVE:
        v = getattr(a, name)
        label = _FIELD_LABEL[name]
        if v is None:
            errs.append(f"‘{label}’ 값을 입력하세요.")
        elif not _isnum(v):
            errs.append(f"‘{label}’ 값은 숫자여야 합니다.")
        elif v <= 0:
            errs.append(f"‘{label}’ 값은 0보다 커야 합니다 (지금 {v:g}). 0이면 절감 시간을 비교할 기준이 없어 계산할 수 없습니다.")
    for name in _UNIT_INTERVAL:
        v = getattr(a, name)
        if v is None:
            continue
        if not _isnum(v) or not (0.0 <= v <= 1.0):
            errs.append(f"‘{_FIELD_LABEL[name]}’ 값은 0과 1 사이(예: 0.05 = 5%)여야 합니다.")
    if not _isnum(a.spread) or not (0.0 <= a.spread <= 0.95):
        errs.append("‘가정의 ± 폭’ 값은 0과 0.95 사이여야 합니다.")
    for name, rng in (a.ranges or {}).items():
        if name not in _FIELD_LABEL:
            errs.append(f"범위를 줄 수 없는 항목입니다: {name}")
            continue
        try:
            lo, hi = rng
        except (TypeError, ValueError):
            errs.append(f"‘{_FIELD_LABEL[name]}’ 범위는 (하한, 상한) 두 값이어야 합니다.")
            continue
        base = getattr(a, name)
        if not (_isnum(lo) and _isnum(hi)) or lo > hi:
            errs.append(f"‘{_FIELD_LABEL[name]}’ 범위는 하한 ≤ 상한인 숫자여야 합니다.")
        elif name in _POSITIVE and lo <= 0:
            errs.append(f"‘{_FIELD_LABEL[name]}’ 범위의 하한은 0보다 커야 합니다.")
        elif name in _UNIT_INTERVAL and not (0.0 <= lo and hi <= 1.0):
            errs.append(f"‘{_FIELD_LABEL[name]}’ 범위는 0과 1 사이여야 합니다.")
        elif _isnum(base) and not (lo <= base <= hi):
            errs.append(f"‘{_FIELD_LABEL[name]}’ 입력값 {base:g} 이 범위 {lo:g}~{hi:g} 밖입니다.")
    return errs


def _bounds(a: Assumptions, name: str, base: float) -> tuple[float, float]:
    if name in (a.ranges or {}):
        lo, hi = a.ranges[name]
        return float(lo), float(hi)
    lo, hi = base * (1 - a.spread), base * (1 + a.spread)
    if name in _UNIT_INTERVAL:
        lo, hi = max(0.0, lo), min(1.0, hi)
    return lo, hi


# ---------------------------------------------------------------------------
# 측정값
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RateMeasure:
    k: int
    n: int

    @property
    def rate(self) -> Optional[float]:
        return self.k / self.n if self.n else None

    @property
    def interval(self) -> Optional[tuple[float, float]]:
        return wilson(self.k, self.n) if self.n else None

    def __add__(self, other: "RateMeasure") -> "RateMeasure":
        return RateMeasure(self.k + other.k, self.n + other.n)

    def to_dict(self) -> dict:
        iv = self.interval
        return {"k": self.k, "n": self.n, "rate": self.rate, "wilson95": list(iv) if iv else None}


@dataclass(frozen=True)
class SystemMeasure:
    system: str
    label: str
    n: int
    human_review: RateMeasure             # 담당자 확인으로 넘긴 비율 (전체 분모)
    unsafe_pass: RateMeasure              # 문제 있는 광고를 통과시킨 비율 (분모: 문제 있는 광고)
    bad_to_human: RateMeasure             # 문제 있는 광고 중 담당자 확인으로 넘긴 비율
    good_to_human: RateMeasure            # 문제없는 광고 중 담당자 확인으로 넘긴 비율
    unnecessary_revision: RateMeasure     # 문제없는 광고에 수정·게재 불가를 요청한 비율
    correct_pass: RateMeasure             # 문제없는 광고를 올바르게 통과시킨 비율
    cost_per_case_usd: Optional[float] = None
    cost_interval_usd: Optional[tuple[float, float]] = None   # 사례별 비용이 있을 때만 (정규 근사)
    calls_per_case: Optional[float] = None
    latency_mean_ms: Optional[float] = None
    latency_p50_ms: Optional[float] = None
    latency_p95_ms: Optional[float] = None
    n_failed: int = 0

    def to_dict(self) -> dict:
        d = {k: (v.to_dict() if isinstance(v, RateMeasure) else v) for k, v in self.__dict__.items()}
        d["cost_interval_usd"] = list(self.cost_interval_usd) if self.cost_interval_usd else None
        return d


@dataclass
class Measured:
    available: bool
    split: str = "all"
    systems: dict[str, SystemMeasure] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    generated_at: str = ""
    provider: str = ""
    model: str = ""
    n_cases: int = 0
    missing: dict[str, str] = field(default_factory=dict)        # 항목 → '측정 없음' 이유
    notes: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)             # 측정 파일에 적힌 한계 문장

    def to_dict(self) -> dict:
        return {"available": self.available, "split": self.split, "sources": list(self.sources), "generated_at": self.generated_at, "provider": self.provider, "model": self.model,
                "n_cases": self.n_cases, "missing": dict(self.missing), "notes": list(self.notes), "caveats": list(self.caveats),
                "systems": {k: v.to_dict() for k, v in self.systems.items()}}


def _rm(d: Mapping[str, Any], key: str) -> RateMeasure:
    x = d.get(key)
    if not isinstance(x, dict) or any(type(x.get(f)) is not int for f in ("k", "n")) or not 0 <= x["k"] <= x["n"]:
        raise ValueError(f"{key} 의 건수는 0 ≤ k ≤ n 인 정수여야 합니다")
    return RateMeasure(x["k"], x["n"])


def _pctl(sorted_v: list[float], q: float) -> float:
    pos = q * (len(sorted_v) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_v) - 1)
    return sorted_v[lo] * (1 - (pos - lo)) + sorted_v[hi] * (pos - lo)


def _pool(blocks: list[Mapping[str, Any]], key: str) -> RateMeasure:
    total = RateMeasure(0, 0)
    for b in blocks:
        total = total + _rm(b, key)
    return total


def _system_measure(system: str, label: str, blocks: list[Mapping[str, Any]], rows: list[Mapping[str, Any]]) -> SystemMeasure:
    for b in blocks:
        if any(type(b.get(f)) is not int or b[f] < 0 for f in ("n", "calls", "failed")) or b["n"] == 0:
            raise ValueError("사례 수·호출 수·실패 수가 올바르지 않습니다")
        if b["failed"] or b.get("scored", b["n"]) != b["n"]:
            raise ValueError("호출 실패 또는 점수를 내지 못한 사례가 섞인 결과입니다")
        if any(not _isnum(b.get(f)) or b[f] < 0 for f in ("cost_usd", "latency_ms_total")):
            raise ValueError("비용·시간은 0 이상의 유한한 숫자여야 합니다")
        rates = {k: _rm(b, k) for k in ("human_review", "unsafe_pass", "bad_to_human", "good_to_human", "unnecessary_revision", "correct_pass")}
        bad, good = rates["bad_to_human"], rates["good_to_human"]
        if (rates["human_review"].n != b["n"] or bad.n + good.n != b["n"]
                or rates["unsafe_pass"].n != bad.n or any(rates[k].n != good.n for k in ("unnecessary_revision", "correct_pass"))
                or rates["human_review"].k != bad.k + good.k
                or good.k + rates["unnecessary_revision"].k + rates["correct_pass"].k != good.n):
            raise ValueError("측정 항목 사이의 사례 수가 맞지 않습니다")
    for r in rows:
        if r.get("error") or r.get("rewrite_error"):
            raise ValueError("호출 실패가 섞인 사례별 기록입니다")
        if any(not _isnum(r.get(f)) or r[f] < 0 for f in ("cost_usd", "latency_ms")):
            raise ValueError("사례별 비용·시간은 0 이상의 유한한 숫자여야 합니다")
    n = sum(int(b.get("n", 0)) for b in blocks)
    cost = sum(float(b.get("cost_usd", 0.0)) for b in blocks)
    lat_total = sum(float(b.get("latency_ms_total", 0.0)) for b in blocks)
    calls = sum(int(b.get("calls", 0)) for b in blocks)
    failed = sum(int(b.get("failed", 0)) for b in blocks)
    costs = [float(r["cost_usd"]) for r in rows if not r.get("error")]
    lats = sorted(float(r["latency_ms"]) for r in rows if not r.get("error"))
    ci = None
    if len(costs) >= 2 and len(costs) == n:
        mean = sum(costs) / len(costs)
        sd = math.sqrt(sum((c - mean) ** 2 for c in costs) / (len(costs) - 1))
        half = 1.96 * sd / math.sqrt(len(costs))
        ci = (max(0.0, mean - half), mean + half)
    rows_ok = len(lats) == n and n > 0       # 사례별 기록이 전체 분모와 맞을 때만 분위수를 쓴다
    return SystemMeasure(
        system=system, label=label, n=n, human_review=_pool(blocks, "human_review"), unsafe_pass=_pool(blocks, "unsafe_pass"), bad_to_human=_pool(blocks, "bad_to_human"),
        good_to_human=_pool(blocks, "good_to_human"), unnecessary_revision=_pool(blocks, "unnecessary_revision"), correct_pass=_pool(blocks, "correct_pass"),
        cost_per_case_usd=(cost / n) if n else None, cost_interval_usd=ci, calls_per_case=(calls / n) if n else None, latency_mean_ms=(lat_total / n) if n else None,
        latency_p50_ms=_pctl(lats, 0.5) if rows_ok else None, latency_p95_ms=_pctl(lats, 0.95) if rows_ok else None, n_failed=failed)


def _read(path: Path) -> Optional[dict]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _from_compare(d: dict, split: str, fname: str) -> tuple[Optional[Measured], str]:
    """compare_live.json → Measured. 쓸 수 없으면 (None, 이유)."""
    if d.get("kind") != "comparison":
        return None, "비교 결과 형식이 아닙니다"
    if d.get("run_kind") != "live":
        return None, f"실제 모델로 돌린 결과가 아닙니다 (run_kind={d.get('run_kind')})"
    if d.get("complete") is not True or d.get("aborted"):
        return None, "끝까지 돌지 못했거나 중단된 결과입니다"
    failures = d.get("failures", {})
    if not isinstance(failures, dict) or type(failures.get("n", 0)) is not int or failures.get("n", 0) < 0:
        return None, "호출 실패 집계의 형식이 올바르지 않습니다"
    if failures.get("n"):
        return None, "호출 실패가 섞인 결과입니다 (측정이 아닌 장애 기록)"
    by_split = d.get("overall_by_split", {})
    wanted = ["dev", "holdout"] if split == "all" else [split]
    if not isinstance(by_split, dict) or any(w in by_split and not isinstance(by_split[w], dict) for w in wanted):
        return None, "분할별 측정 결과의 형식이 올바르지 않습니다"
    if any(w not in by_split for w in wanted):
        return None, f"{split} 구간 결과가 없습니다"
    metadata = d.get("systems", {})
    datasets_meta = d.get("datasets", {})
    all_rows = d.get("rows", [])
    if (not isinstance(metadata, dict) or any(not isinstance(v, dict) for v in metadata.values())
            or not isinstance(datasets_meta, dict) or any(not isinstance(v, dict) or v.get("split") not in ("dev", "holdout") for v in datasets_meta.values())
            or not isinstance(all_rows, list) or any(not isinstance(r, dict) for r in all_rows)):
        return None, "방식·데이터셋·사례별 기록의 형식이 올바르지 않습니다"
    labels = {k: (v.get("label") or k) for k, v in metadata.items()}
    datasets = [ds for ds, meta in datasets_meta.items() if split == "all" or meta["split"] == split]
    rows = [r for r in all_rows if r.get("dataset") in datasets]
    systems: dict[str, SystemMeasure] = {}
    for sysname in ("code_only", "llm_only", "combined"):
        blocks = [by_split[w][sysname] for w in wanted if isinstance(by_split[w].get(sysname), dict)]
        if len(blocks) != len(wanted):
            continue
        systems[sysname] = _system_measure(sysname, labels.get(sysname, sysname), blocks, [r for r in rows if r.get("system") == sysname])
    if "combined" not in systems:
        return None, "합친 방식의 측정 결과가 없습니다"
    m = Measured(available="combined" in systems, split=split, systems=systems, sources=[fname], generated_at=str(d.get("generated_at", "")),
                 provider=str(d.get("provider", "")), model=str(d.get("model", "")),
                 n_cases=systems["combined"].n if "combined" in systems else 0, caveats=[str(c) for c in d.get("caveats", [])])
    for sysname, sm in systems.items():
        if sm.n_failed:
            m.notes.append(f"{labels.get(sysname, sysname)}: 호출 실패 {sm.n_failed}건이 섞여 있음")
    return m, ""


def _from_baseline(d: dict, split: str, fname: str) -> tuple[Optional[Measured], str]:
    """baseline_partial.json(실제 모델) → '합친 방식' 한 줄만. 파일의 분할을 따르고 검토 모델만·코드만 비교는 없다."""
    if d.get("mode") not in ("partial", "live"):
        return None, f"실제 모델 결과가 아닙니다 (mode={d.get('mode')})"
    failures = d.get("provider_failures", {})
    if not isinstance(failures, dict) or type(failures.get("n", 0)) is not int or failures.get("n", 0) < 0:
        return None, "호출 실패 집계의 형식이 올바르지 않습니다"
    if d.get("aborted") or failures.get("n"):
        return None, "중단되었거나 호출 실패가 섞인 결과입니다"
    baseline_split = d.get("split", "holdout" if Path(str(d.get("dataset", "golden"))).stem in _HOLDOUT_DATASETS else "dev")
    if baseline_split not in ("dev", "holdout"):
        return None, "파일의 개발용·최종 확인용 분할 정보가 올바르지 않습니다"
    split_name = "최종 확인용" if baseline_split == "holdout" else "개발용"
    if split != "all" and split != baseline_split:
        return None, f"이 파일은 {split_name} 사례만 담고 있습니다"
    cases = d.get("cases", [])
    if not isinstance(cases, list) or not cases or any(not isinstance(c, dict) for c in cases):
        return None, "사례별 결과가 없습니다"
    for c in cases:
        if c.get("label") not in ("APPROVE", "REVISE", "REJECT") or c.get("predicted") not in ("APPROVE", "REVISE", "REJECT", "HUMAN_REVIEW"):
            return None, "사례별 라벨·판정이 올바르지 않습니다"
        if c.get("error") or c.get("rewrite_error"):
            return None, "호출 실패가 섞인 사례별 기록입니다"
        if any(f in c and (not _isnum(c[f]) or c[f] < 0) for f in ("cost_usd", "latency_ms")):
            return None, "사례별 비용·시간은 0 이상의 유한한 숫자여야 합니다"
    bad = [c for c in cases if c.get("label") in ("REJECT", "REVISE")]
    good = [c for c in cases if c.get("label") == "APPROVE"]
    hr = [c for c in cases if c.get("predicted") == "HUMAN_REVIEW"]
    lat = sorted(float(c["latency_ms"]) for c in cases) if all("latency_ms" in c for c in cases) else []
    cost = sum(float(c["cost_usd"]) for c in cases) if all("cost_usd" in c for c in cases) else None
    sm = SystemMeasure(
        system="combined", label=f"합친 방식 ({split_name}만, 교차 확인·가상 사용자 없음)", n=len(cases), human_review=RateMeasure(len(hr), len(cases)),
        unsafe_pass=RateMeasure(sum(c.get("predicted") == "APPROVE" for c in bad), len(bad)), bad_to_human=RateMeasure(sum(c.get("predicted") == "HUMAN_REVIEW" for c in bad), len(bad)),
        good_to_human=RateMeasure(sum(c.get("predicted") == "HUMAN_REVIEW" for c in good), len(good)),
        unnecessary_revision=RateMeasure(sum(c.get("predicted") in ("REVISE", "REJECT") for c in good), len(good)),
        correct_pass=RateMeasure(sum(c.get("predicted") == "APPROVE" for c in good), len(good)),
        cost_per_case_usd=cost / len(cases) if cost is not None else None, latency_mean_ms=sum(lat) / len(lat) if lat else None, latency_p50_ms=_pctl(lat, 0.5) if lat else None, latency_p95_ms=_pctl(lat, 0.95) if lat else None)
    m = Measured(True, split, {"combined": sm}, [fname], str(d.get("generated_at", "")), str(d.get("provider", "")), str(d.get("model", "")), len(cases))
    m.notes.append(f"사용할 수 있는 비교 결과(compare_live.json)가 없어 단일 방식의 실행 결과로 대신했습니다 — {split_name} 사례만이고 검토 모델만·코드만 방식과의 비교는 없습니다.")
    return m, ""


def load_measured(reports_dir: Optional[Path] = None, split: str = "all") -> Measured:
    """저장된 평가 리포트에서 측정값을 읽는다. 쓸 수 있는 파일이 없으면 available=False 이고 이유를 missing 에 적는다 — 값을 만들어 내지 않는다."""
    if split not in SPLITS:
        raise ValueError(f"split 은 {SPLITS} 중 하나여야 합니다")
    base = Path(reports_dir) if reports_dir is not None else REPORTS_DIR
    reasons: dict[str, str] = {}
    for fname, fn in ((COMPARE_FILE, _from_compare), (BASELINE_FILE, _from_baseline)):
        d = _read(base / fname)
        if d is None:
            reasons[fname] = "파일이 없거나 읽을 수 없습니다"
            continue
        try:
            m, why = fn(d, split, fname)
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            m, why = None, "측정 파일의 구조나 값이 올바르지 않습니다"
        if m is not None:
            m.missing.update({f: r for f, r in reasons.items()})
            if "llm_only" not in m.systems:
                m.missing["검토 모델만 대비 비교"] = "측정 없음 — 검토 모델만 방식의 실제 모델 결과가 없습니다"
            if "code_only" not in m.systems:
                m.missing["코드 검사만 비교"] = "측정 없음"
            return m
        reasons[fname] = why
    return Measured(False, split=split, missing={**reasons, "담당자 확인율": "측정 없음 — 실제 모델로 끝까지 돈 평가 결과 파일이 없습니다 (예시 실행·코드 검사만 결과는 모델 성능이 아니라 쓰지 않습니다)"},
                    notes=["측정 없음: 값을 만들어 내지 않습니다. 담당자 확인율을 직접 가정해 보려면 '직접 가정한 담당자 확인율' 을 입력하세요 (가정으로 표시됩니다)."])


# ---------------------------------------------------------------------------
# 계산
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ImpactItem:
    name: str
    unit: str
    kind: str                 # 측정 · 가정 · 계산
    base: Optional[float]
    low: Optional[float]      # 범위의 최솟값
    high: Optional[float]     # 범위의 최댓값 (어느 쪽이 비관인지는 higher_is_worse 가 정한다)
    source: str = ""
    note: str = ""
    higher_is_worse: bool = False   # 값이 클수록 나쁜 항목(담당자 확인 건수·비용·위험) — 비관은 상한, 낙관은 하한이다

    @property
    def pessimistic(self) -> Optional[float]:
        return self.high if self.higher_is_worse else self.low

    @property
    def optimistic(self) -> Optional[float]:
        return self.low if self.higher_is_worse else self.high

    def to_dict(self) -> dict:
        return {**self.__dict__, "pessimistic": self.pessimistic, "optimistic": self.optimistic}


@dataclass
class ImpactEstimate:
    computed: bool
    headline: str
    notices: list[str]
    items: list[ImpactItem] = field(default_factory=list)
    comparison: dict = field(default_factory=dict)
    risk: dict = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)
    measured: Optional[Measured] = None
    assumptions: Optional[Assumptions] = None
    basis: str = ""           # 담당자 확인율을 어디서 가져왔는가

    def item(self, name: str) -> Optional[ImpactItem]:
        return next((i for i in self.items if i.name == name), None)

    def to_text(self) -> str:
        lines = [NOT_REAL_EFFECT, self.headline]
        for i in self.items:
            lines.append(f"[{i.kind}] {i.name}: {_fmt(i.base)} {i.unit} (비관 {_fmt(i.pessimistic)} ~ 낙관 {_fmt(i.optimistic)})" + (f" · 출처 {i.source}" if i.source else ""))
        for k, v in self.unavailable.items():
            lines.append(f"[측정 없음] {k}: {v}")
        lines.append(NOT_REAL_EFFECT)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"computed": self.computed, "notice": NOT_REAL_EFFECT, "headline": self.headline, "notices": list(self.notices), "basis": self.basis,
                "items": [i.to_dict() for i in self.items], "comparison": self.comparison, "risk": self.risk, "unavailable": dict(self.unavailable),
                "measured": self.measured.to_dict() if self.measured else None}


def _fmt(x: Optional[float]) -> str:
    if x is None:
        return "—"
    return f"{x:,.0f}" if abs(x) >= 1000 else f"{x:,.2f}"


def _outputs(v: Mapping[str, float]) -> dict[str, float]:
    """입력 변수 → 결과. 모든 결과는 변수들의 곱·합이라 각 변수에 대해 일차식(다중선형)이다 — 범위의 끝은 상자의 꼭짓점에서 정확히 나온다."""
    n, mr, mf, s, hr = v["N"], v["mr"], v["mf"], v["s"], v["hr"]
    base_min = n * mr
    with_min = n * hr * mf + n * (1 - hr) * s * mr
    saved_min = base_min - with_min
    saved_h_month = saved_min / 60.0 * v["days"]
    labor = saved_h_month * v["wage"]
    out = {"hr": hr, "human_cases_day": n * hr, "human_seen_day": n * (hr + (1 - hr) * s), "saved_min_day": saved_min, "saved_h_month": saved_h_month, "labor_krw_month": labor}
    if "cost" in v:
        ai = n * v["days"] * v["cost"] * v["fx"]
        out["ai_krw_month"] = ai
        out["net_krw_month"] = labor - ai
    return out


def _span(var_bounds: Mapping[str, tuple[float, float]], base: Mapping[str, float], derive: Any = None) -> dict[str, tuple[float, float, float]]:
    """변수 상자의 모든 꼭짓점에서 결과를 계산해 (최소, 기준, 최대) 를 낸다."""
    names = list(var_bounds)
    mins: dict[str, float] = {}
    maxs: dict[str, float] = {}
    for combo in itertools.product(*[(var_bounds[n][0], var_bounds[n][1]) if var_bounds[n][0] != var_bounds[n][1] else (var_bounds[n][0],) for n in names]):
        point = dict(zip(names, combo))
        if derive:
            point = derive(point)
        for k, val in _outputs(point).items():
            mins[k] = min(mins.get(k, math.inf), val)
            maxs[k] = max(maxs.get(k, -math.inf), val)
    b = derive(dict(base)) if derive else dict(base)
    ob = _outputs(b)
    return {k: (mins[k], ob[k], maxs[k]) for k in ob}


def estimate(a: Assumptions, measured: Measured) -> ImpactEstimate:
    """가정과 측정값으로 What-if 효과를 계산한다. 가정이 잘못되면 ImpactInputError(쉬운 한국어). 측정이 없으면 계산하지 않고 '측정 없음' 으로 답한다."""
    errs = validate_assumptions(a)
    if errs:
        raise ImpactInputError(errs)
    notices = [NOT_REAL_EFFECT]
    if measured.split in ("all", "dev"):
        notices.append("개발용 사례는 규칙·프롬프트를 고치는 데 쓴 사례라 이 사례에서의 결과는 낙관적일 수 있습니다.")
    combined = measured.systems.get("combined") if measured.available else None
    strata = None
    if a.what_if_human_review_rate is not None:
        hr_base = float(a.what_if_human_review_rate)
        hr_lo, hr_hi = _bounds(a, "what_if_human_review_rate", hr_base)
        basis, hr_kind, src = "가정(직접 입력한 담당자 확인율)", "가정", "사용자 입력"
    elif combined is None or combined.human_review.n == 0:
        why = measured.missing.get("담당자 확인율") or next(iter(measured.missing.values()), "측정 결과 파일이 없습니다")
        return ImpactEstimate(False, "측정 없음 — 담당자 확인율을 측정한 실제 모델 결과가 없어 효과를 계산하지 않았습니다. " + NOT_REAL_EFFECT, notices,
                              unavailable={"담당자 확인율": why, **{k: v for k, v in measured.missing.items() if k != "담당자 확인율"}}, measured=measured, assumptions=a)
    else:
        hr = combined.human_review
        notices.append("측정한 담당자 확인율은 평가 경로의 값으로 다른 모델 계열의 교차 확인·가상 사용자 단계가 빠져 있습니다. 실제 파이프라인은 경계 사례에서 교차 확인을 하고 두 계열이 갈리면 담당자 확인으로 보내므로, 확인율이 더 높을 수 있습니다(얼마나인지는 측정하지 못했습니다).")
        hr_base, (hr_lo, hr_hi) = hr.rate, hr.interval
        src = "·".join(measured.sources)
        basis, hr_kind = f"측정({src}, 사례 {hr.n}건, 평가 사례 구성 그대로)", "측정"
        if hr.n < 30:
            notices.append(f"측정에 쓴 사례가 {hr.n}건뿐이라 신뢰구간이 매우 넓습니다.")
        if a.violation_share is not None:
            b_, g_ = combined.bad_to_human, combined.good_to_human
            if b_.n == 0 or g_.n == 0:
                notices.append("평가 사례에 문제 있는 광고 또는 문제없는 광고가 없어 문제 있는 광고 비율 가정을 반영하지 못했습니다 — 평가 사례 구성 그대로 계산했습니다.")
            else:
                strata = (b_, g_)
                basis = (f"측정 층별 비율({src}: 문제 있는 광고 {b_.n}건 중 담당자 확인 {b_.k}건, 문제없는 광고 {g_.n}건 중 {g_.k}건)을 "
                         f"가정한 구성(문제 있는 광고 {a.violation_share:.0%})으로 다시 가중")
    # 변수 상자
    base_vars: dict[str, float] = {"N": a.daily_reviews, "mr": a.minutes_per_review, "mf": a.minutes_per_flagged, "s": a.spot_check_rate, "wage": a.hourly_wage_krw,
                                   "days": a.workdays_per_month, "fx": a.usd_krw}
    bounds: dict[str, tuple[float, float]] = {"N": _bounds(a, "daily_reviews", a.daily_reviews), "mr": _bounds(a, "minutes_per_review", a.minutes_per_review),
                                              "mf": _bounds(a, "minutes_per_flagged", a.minutes_per_flagged), "s": _bounds(a, "spot_check_rate", a.spot_check_rate),
                                              "wage": _bounds(a, "hourly_wage_krw", a.hourly_wage_krw), "days": _bounds(a, "workdays_per_month", a.workdays_per_month),
                                              "fx": _bounds(a, "usd_krw", a.usd_krw)}
    cost_note = ""
    cost_base = cost_lo = cost_hi = None
    if combined is not None and combined.cost_per_case_usd is not None and a.what_if_human_review_rate is None:
        cost_base = combined.cost_per_case_usd
        cost_lo, cost_hi = combined.cost_interval_usd or (cost_base, cost_base)
        base_vars["cost"], bounds["cost"] = cost_base, (cost_lo, cost_hi)
        cost_note = "사례당 AI 비용은 실제 모델 평가에서 잰 평균(가정 아님). 구간은 사례별 비용의 정규 근사."
    derive = None
    if strata is not None:
        b_, g_ = strata
        base_vars.update({"b": b_.rate, "g": g_.rate, "share": a.violation_share})
        bounds.update({"b": b_.interval, "g": g_.interval, "share": _bounds(a, "violation_share", a.violation_share)})

        def derive(p: dict) -> dict:           # noqa: E306 — 층별 비율과 구성 가정에서 담당자 확인율을 만든다
            q = dict(p)
            q["hr"] = q["share"] * q["b"] + (1 - q["share"]) * q["g"]
            return q
    else:
        base_vars["hr"], bounds["hr"] = hr_base, (hr_lo, hr_hi)
    spans = _span(bounds, base_vars, derive)

    hr_span = spans["hr"]
    items: list[ImpactItem] = []
    items.append(ImpactItem("담당자 확인으로 넘기는 비율", "%", hr_kind if strata is None else "계산", hr_span[1] * 100, hr_span[0] * 100, hr_span[2] * 100, src,
                            ("측정 신뢰구간(윌슨 95%)" if hr_kind == "측정" else "가정 범위") if strata is None else "층별 측정 비율(윌슨 95%)을 가정한 구성으로 가중", higher_is_worse=True))
    items.append(ImpactItem("담당자 확인 없이 끝나는 비율", "%", "계산", (1 - hr_span[1]) * 100, (1 - hr_span[2]) * 100, (1 - hr_span[0]) * 100, src, "= 100% − 담당자 확인으로 넘기는 비율"))
    items.append(ImpactItem("하루에 사람이 직접 봐야 하는 건수(담당자 확인 건)", "건/일", "계산", spans["human_cases_day"][1], spans["human_cases_day"][0], spans["human_cases_day"][2], "", "하루 검수 건수 × 담당자 확인 비율 (가정 × 측정)", higher_is_worse=True))
    items.append(ImpactItem("하루에 사람이 보는 건수(자동 판단 건의 재확인 포함)", "건/일", "계산", spans["human_seen_day"][1], spans["human_seen_day"][0], spans["human_seen_day"][2], "", "재확인 비율은 가정", higher_is_worse=True))
    items.append(ImpactItem("하루 절감 시간", "분/일", "계산", spans["saved_min_day"][1], spans["saved_min_day"][0], spans["saved_min_day"][2], "",
                            "= 건수 × 사람 검수 시간 − [담당자 확인 건 × 처리 시간 + 자동 판단 건 × 재확인 비율 × 검수 시간]. 음수면 오히려 늘어난다는 뜻."))
    items.append(ImpactItem("한 달 절감 시간", "시간/월", "계산", spans["saved_h_month"][1], spans["saved_h_month"][0], spans["saved_h_month"][2], ""))
    items.append(ImpactItem("한 달 절감 인건비 환산(시급 가정)", "원/월", "계산", spans["labor_krw_month"][1], spans["labor_krw_month"][0], spans["labor_krw_month"][2], "", "시급은 가정. 실제 인건비 절감이 아니라 시간을 돈으로 환산한 값."))
    unavailable: dict[str, str] = {}
    if "ai_krw_month" in spans:
        items.append(ImpactItem("한 달 AI 호출 비용", "원/월", "계산", spans["ai_krw_month"][1], spans["ai_krw_month"][0], spans["ai_krw_month"][2], "·".join(measured.sources), cost_note + " 환율·건수는 가정.", higher_is_worse=True))
        items.append(ImpactItem("한 달 순효과(절감 환산 − AI 호출 비용)", "원/월", "계산", spans["net_krw_month"][1], spans["net_krw_month"][0], spans["net_krw_month"][2], ""))
    else:
        unavailable["AI 호출 비용"] = "측정 없음 — 사례당 비용을 잰 실제 모델 결과가 없거나 직접 가정한 확인율을 썼습니다. 순효과는 계산하지 않았습니다."
    # 처리 지연 (측정) — 사람의 시간(가정)과 나란히 보여 주되 비율로 합치지 않는다
    if combined is not None and combined.latency_p50_ms is not None:
        items.append(ImpactItem("AI 검사 1건 지연(중앙값)", "초", "측정", combined.latency_p50_ms / 1000, None, None, "·".join(measured.sources), "평가 실행에서 잰 값. 사람 1건당 검수 시간(가정)과 비교하려면 두 값을 따로 보세요.", higher_is_worse=True))
        if combined.latency_p95_ms is not None:
            items.append(ImpactItem("AI 검사 1건 지연(95번째 백분위)", "초", "측정", combined.latency_p95_ms / 1000, None, None, "·".join(measured.sources), higher_is_worse=True))
    elif a.what_if_human_review_rate is None:
        unavailable["AI 검사 지연"] = "측정 없음 — 사례별 지연 기록이 없습니다."
    # 위험: 문제 있는 광고를 통과시킨 비율 (측정) — 절감과 항상 같이 본다
    risk: dict[str, Any] = {}
    if combined is not None and a.what_if_human_review_rate is None:
        u = combined.unsafe_pass
        risk = {"unsafe_pass": u.to_dict(), "label": "담당자 확인 없이 문제 있는 광고를 통과시킨 건수 / 문제 있는 광고 수 (측정)"}
        if u.n and a.violation_share is not None:
            up = u.interval[1]
            risk["expected_unsafe_passes_per_day_upper95"] = a.daily_reviews * a.violation_share * up
            risk["assumption"] = "하루 검수 건수 × 문제 있는 광고 비율(가정) × 윌슨 95% 상한(측정). 0건 관측이어도 상한은 0이 아니다."
        items.append(ImpactItem("문제 있는 광고를 통과시킨 비율", "%", "측정", (u.rate or 0) * 100 if u.n else None, 0.0 if u.n else None, (u.interval[1] * 100) if u.n else None, "·".join(measured.sources),
                                f"{u.k}/{u.n}건. 상한은 윌슨 95%. 이 사례에서 잰 값이며 모든 광고에 대한 보장이 아님.", higher_is_worse=True))
    # 검토 모델만 대비 불필요 수정 요청
    comparison: dict[str, Any] = {}
    if combined is not None and "llm_only" in measured.systems and a.what_if_human_review_rate is None:
        lo_, co_ = measured.systems["llm_only"], combined
        comparison = {
            "label": "문제없는 광고에 불필요하게 수정·게재 불가를 요청한 비율 (측정, 같은 사례를 두 방식으로 본 짝지은 자료)",
            "llm_only": lo_.unnecessary_revision.to_dict(), "combined": co_.unnecessary_revision.to_dict(),
            "per_1000_benign_fewer_requests": ((lo_.unnecessary_revision.rate or 0) - (co_.unnecessary_revision.rate or 0)) * 1000 if lo_.unnecessary_revision.n and co_.unnecessary_revision.n else None,
            "note": "차이 자체의 신뢰구간은 계산하지 않았다(짝지은 자료). 두 방식의 구간이 겹치는지는 참고만 한다. 1000건 환산은 비율의 단순 곱이며 예측이 아니다.",
        }
        if lo_.unnecessary_revision.n and co_.unnecessary_revision.n:
            a_iv, b_iv = lo_.unnecessary_revision.interval, co_.unnecessary_revision.interval
            comparison["intervals_overlap"] = not (a_iv[0] > b_iv[1] or b_iv[0] > a_iv[1])
    else:
        unavailable["검토 모델만 대비 불필요 수정 요청 감소"] = measured.missing.get("검토 모델만 대비 비교", "측정 없음")
    sentence = (f"하루 {a.daily_reviews:g}건을 검수한다는 가정에서, 담당자 확인 비율 {hr_span[1]:.1%}({hr_kind if strata is None else '측정 층별 비율을 가정한 구성으로 가중'})이면 "
                f"하루 약 {spans['saved_min_day'][1] / 60:.1f}시간(비관 {spans['saved_min_day'][0] / 60:.1f} ~ 낙관 {spans['saved_min_day'][2] / 60:.1f})의 검수 시간이 줄어드는 것으로 계산됩니다. ")
    return ImpactEstimate(True, sentence + NOT_REAL_EFFECT, notices, items, comparison, risk, unavailable, measured, a, basis)


# ---------------------------------------------------------------------------
# 사람 검수 용량 계획 — docs/OPERATIONS.md §9 의 공식을 코드로 옮긴 것 (가정을 넣어야 값이 나온다)
# ---------------------------------------------------------------------------
def _need_positive(**kw: float) -> None:
    bad = [k for k, v in kw.items() if not _isnum(v) or v <= 0]
    if bad:
        raise ImpactInputError([f"‘{k}’ 값은 0보다 큰 숫자여야 합니다." for k in bad])


def reviewer_minutes_per_day(daily_reviews: float, human_review_rate: float, minutes_per_flagged: float, spot_check_rate: float = 0.0, minutes_per_review: float = 0.0) -> float:
    """하루에 사람이 쓰는 검수 시간(분) = N·[h·t_f + (1−h)·s·t_r]. h: 담당자 확인율, s: 자동 판단 건을 사람이 다시 보는 비율."""
    _need_positive(daily_reviews=daily_reviews, minutes_per_flagged=minutes_per_flagged)
    if not (_isnum(human_review_rate) and 0.0 <= human_review_rate <= 1.0 and _isnum(spot_check_rate) and 0.0 <= spot_check_rate <= 1.0):
        raise ImpactInputError(["담당자 확인율과 재확인 비율은 0과 1 사이여야 합니다."])
    return daily_reviews * (human_review_rate * minutes_per_flagged + (1 - human_review_rate) * spot_check_rate * minutes_per_review)


def required_reviewers(daily_reviews: float, human_review_rate: float, minutes_per_flagged: float, *, spot_check_rate: float = 0.0, minutes_per_review: float = 0.0,
                       hours_per_day: float = 8.0, utilization: float = 0.7, peak_factor: float = 1.0) -> float:
    """필요한 상근 인원(FTE) = 하루 검수 시간 ÷ (근무 시간 × 가동률) × 피크 계수.
    가동률(예: 0.7)은 회의·휴식·전환 시간을 뺀 실제 검수 비율, 피크 계수는 가장 바쁜 시간대가 평균의 몇 배인지 — 둘 다 가정이다."""
    _need_positive(hours_per_day=hours_per_day, utilization=utilization, peak_factor=peak_factor)
    if utilization > 1:
        raise ImpactInputError(["‘utilization’ 값은 1 이하여야 합니다."])
    minutes = reviewer_minutes_per_day(daily_reviews, human_review_rate, minutes_per_flagged, spot_check_rate, minutes_per_review)
    return minutes / (hours_per_day * 60.0 * utilization) * peak_factor


def erlang_c(servers: int, offered_load: float) -> float:
    """Erlang C: 도착한 건이 기다려야 할 확률. servers: 동시에 처리하는 사람 수, offered_load = 도착률 × 건당 처리 시간 (얼랑). offered_load ≥ servers 면 대기열이 끝없이 커져 1.0."""
    if servers < 1 or offered_load < 0:
        raise ValueError("servers ≥ 1, offered_load ≥ 0 이어야 합니다")
    if offered_load >= servers:
        return 1.0
    if offered_load == 0:
        return 0.0
    term, total = 1.0, 1.0
    for k in range(1, servers):
        term *= offered_load / k
        total += term
    top = term * offered_load / servers * servers / (servers - offered_load)   # A^c / c! · c / (c − A)
    return top / (total + top)


def mean_wait_minutes(arrivals_per_hour: float, minutes_per_flagged: float, reviewers: int) -> float:
    """M/M/c 근사의 평균 대기 시간(분). 도착이 무작위(포아송)이고 처리 시간이 지수 분포라는 단순 가정이다 — 실제 대기 시간이 아니다. 과부하(이용률 ≥ 1)면 inf."""
    _need_positive(arrivals_per_hour=arrivals_per_hour, minutes_per_flagged=minutes_per_flagged)
    if reviewers < 1:
        raise ImpactInputError(["‘reviewers’ 값은 1 이상이어야 합니다."])
    lam, mu = arrivals_per_hour / 60.0, 1.0 / minutes_per_flagged        # 분당
    if lam >= reviewers * mu:
        return math.inf
    return erlang_c(reviewers, lam / mu) / (reviewers * mu - lam)


def estimate_from_files(a: Assumptions, reports_dir: Optional[Path] = None, split: str = "all") -> ImpactEstimate:
    """측정값을 파일에서 읽어 바로 계산한다."""
    return estimate(a, load_measured(reports_dir, split))


__all__ = ["Assumptions", "EXAMPLE_ASSUMPTIONS", "EXAMPLE_NOTE", "ImpactEstimate", "ImpactInputError", "ImpactItem", "Measured", "NOT_REAL_EFFECT", "RateMeasure", "SPLITS",
           "SPLIT_LABEL", "SystemMeasure", "erlang_c", "estimate", "estimate_from_files", "load_measured", "mean_wait_minutes", "required_reviewers",
           "reviewer_minutes_per_day", "validate_assumptions"]
