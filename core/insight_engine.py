"""성과 인사이트 엔진 — 이상 탐지 → 원인 진단 → 개선 액션. 순수 계산이며 모델을 쓰지 않고 결정론적이다.

철학: 계산은 코드가, 문장화는 모델이, 검증은 코드가 한다. 이 모듈은 앞의 '계산' 전부다.
- 탐지: 요일을 보정한 기준선(중앙값/MAD 기반, 기준 기간 안의 뚜렷한 완만한 추세는 이어 감)과 비교한다.
  구간 점수에는 기준 요일값·외삽 추세의 추정 오차를 포함하고 전체 지표와 부분 집합을 검사한 수만큼 기준을 강화한다. 표본이 모자라면 판단을 보류한다.
- 진단: 원인 가설을 미리 정의해 두고(필요 근거·맞는 근거·반대 근거·최소 표본), 근거가 충족된 비율로 확신을 계산한다.
  근거가 모자라면 억지로 1순위를 고르지 않고 '원인을 정하기 어렵다(undetermined)' 를 낸다.
- 액션: 닫힌 카탈로그에서만 고르고, 금지 조건(가드 규칙)은 코드로 강제한다.
- 보고서의 모든 숫자·기간·퍼센트는 `Evidence` 레코드(id 포함)에 저장된다. 보고서 문장은 이 id 로 되돌릴 수 있어야 한다.

이 모듈이 의미하지 않는 것
- 확신(confidence)은 '정해 둔 근거 검사 중 몇 개가 충족됐는가' 를 가중 평균한 점수이지 확률이 아니다. 평가에서 구간별 정답률로 보정을 확인하지만,
  그것도 이 프로젝트가 만든 합성 데이터에서의 값이다.
- 가설 목록과 임계값은 합성 데이터를 만든 사람이 같이 정했다. 실제 광고 데이터의 정확도를 보장하지 않는다.
- 원인을 '확정' 하지 않는다. 근거와 가능성을 보여 줄 뿐, 광고 관리자에서 직접 확인하는 일을 대신하지 않는다.
"""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import median, pstdev
from typing import Callable, Iterable, Optional

from core.performance import NO_DIM, MetricRow

# ───────────────────────────── 설정값 (이 프로젝트가 정한 값) ─────────────────────────────
RECENT_DAYS = 14                 # 살펴볼 최근 기간
BASELINE_DAYS = 28               # 그 앞의 기준 기간 (요일마다 4번씩 본다)
MIN_BASELINE_DAYS = 14           # 기준 기간이 이보다 짧으면 판단하지 않는다 (요일마다 최소 2번)
MIN_WINDOW_DAYS = 3
H_SCORE = 5.0                    # 기준선 추정 오차를 포함한 구간 점수의 기본 임계값
H_SINGLE = 6.5                   # 하루짜리 변화는 더 강해야 한다
BASELINE_ESTIMATION_FACTOR = math.pi / 2  # 정규 잡음에서 중앙값 추정 분산의 근사 배율
MIN_EFFECT = 0.08                # 통계적으로 튀어도 이 비율(8%)보다 작은 변화는 이상으로 보지 않는다
SIGMA_FLOOR = 0.02               # 잡음 크기의 하한 (로그 단위)
SIGMA_INFLATE = 1.3             # 표본 안에서 요일 보정한 잔차는 실제보다 작게 나오므로 키운다
MIN_BASE_CLICKS = 500            # 기준 기간 합계 클릭이 이보다 적으면 모든 지표 판단 보류
MIN_BASE_CLICKS_PER_DAY = 20
MIN_BASE_CONV = 100              # 기준 기간 합계 전환이 이보다 적으면 전환 관련 지표 판단 보류
MIN_BASE_CONV_PER_DAY = 4
GROUP_MIN_CLICKS = 150           # 소재·기기 같은 묶음 하나를 따로 볼 최소 표본 (기준 기간 합계)
GROUP_MIN_CONV = 30

SUPPORTED_CONFIDENCE = 0.75      # 관측 신호가 많이 일치하는 가설. 원인 확정 기준이 아니다
ASSERT_CONFIDENCE = SUPPORTED_CONFIDENCE  # 과거 보고서/외부 코드 호환용 이름; 단정 권한을 주지 않는다
LIKELY_CONFIDENCE = 0.45         # 이상이면 '가능성이 높습니다'
CANDIDATE_CONFIDENCE = 0.25      # 이상이면 '가능성이 있습니다' (후보)
SUSPECT_CONFIDENCE = 0.30        # 금지 규칙은 이보다 낮은 의심에서도 건다 (보수적으로)
TRACKING_SUSPECT_CONFIDENCE = 0.25
CAP_MISSING_CRITICAL = 0.44      # 꼭 필요한 근거를 확인하지 못했으면 '가능성이 높다' 단계로 올리지 않는다
CAP_FAILED_CRITICAL = 0.20       # 꼭 필요한 근거가 어긋났으면 후보 이하
RIVAL_MARGIN = 0.12              # 서로 배타적인 후보와 이만큼은 앞서야 단정할 수 있다
RIVAL_CAP = 0.40
CONTRADICTION_WEIGHT = 0.6

METRICS = ("impressions", "clicks", "ctr", "cpc", "cvr", "cpa", "cost", "conversions")
METRIC_LABEL = {"impressions": "노출수", "clicks": "클릭수", "ctr": "클릭률", "cpc": "클릭당 비용", "cvr": "전환율", "cpa": "전환당 비용",
                "cost": "광고비", "conversions": "전환수"}
METRIC_UNIT = {"impressions": "회", "clicks": "회", "ctr": "%", "cpc": "원", "cvr": "%", "cpa": "원", "cost": "원", "conversions": "건"}
BAD_DIRECTION = {"impressions": "down", "clicks": "down", "ctr": "down", "cpc": "up", "cvr": "down", "cpa": "up", "cost": None, "conversions": "down"}
CONV_METRICS = ("cvr", "cpa", "conversions")
OUTCOME_METRICS = ("clicks", "ctr", "cvr", "conversions", "cpc", "cpa")     # 좋아졌는지 나빠졌는지 판단하는 성과 지표

CAUSES = ("budget_cap", "creative_fatigue", "tracking_break", "landing_issue", "bid_competition", "audience_saturation", "mix_shift")
CAUSE_LABEL = {"budget_cap": "하루 예산 소진", "creative_fatigue": "소재 피로", "tracking_break": "전환 추적 오류", "landing_issue": "랜딩 페이지 문제",
               "bid_competition": "입찰 경쟁 심화", "audience_saturation": "타겟 도달 포화", "mix_shift": "구성 변화"}
CAUSE_HELP = {"budget_cap": "하루 예산이 일찍 소진되어 노출이 줄어든 상태", "creative_fatigue": "같은 소재를 오래 보여 줘서 그 소재의 반응이 줄어든 상태",
              "tracking_break": "전환이 일어났는데 측정이 끊겨 기록되지 않는 상태", "landing_issue": "광고를 눌러 도착한 페이지에서 신청까지 가는 비율이 떨어진 상태",
              "bid_competition": "다른 광고주와의 경쟁으로 같은 노출을 얻는 데 더 많은 비용이 드는 상태",
              "audience_saturation": "겨냥한 사람들에게 이미 충분히 보여 줘서 새로 닿을 사람이 줄어든 상태",
              "mix_shift": "전환율이 낮은 쪽(예: 특정 기기)으로 노출이 쏠려 전체 전환율이 내려간 것처럼 보이는 상태"}
CAUSE_VERIFICATION = {
    "budget_cap": "광고 관리자에서 예산 소진 시각과 게재 중단 기록을 대조하세요. 예산이 남아 있었는지도 확인한 뒤 증액을 검토하세요.",
    "creative_fatigue": "같은 타겟에서 기존 소재와 새 소재를 같은 조건으로 비교하세요. 반응 차이가 재현되는지 확인한 뒤 교체를 검토하세요.",
    "tracking_break": "같은 시간대의 서버 전환 로그와 광고 관리자 기록을 대조하세요. 실제 전환이 있었는데 광고 기록만 빠졌는지 확인하세요.",
    "landing_issue": "랜딩 페이지를 기기별로 열어 신청 완료까지 직접 확인하세요. 오류가 재현되는지와 서버 전환 기록을 함께 확인하세요.",
    "bid_competition": "입찰 변경 이력과 광고 관리자의 경쟁 지표를 대조하세요. 경쟁 지표가 그대로라면 게재 설정도 확인한 뒤 입찰 변경을 검토하세요.",
    "audience_saturation": "도달과 반복 노출 기록을 대조하고 새 타겟과 기존 타겟을 같은 조건으로 비교하세요. 반응 차이가 재현되는지 확인하세요.",
    "mix_shift": "같은 기간의 기기별 클릭 비중과 전환율을 나눠 비교하세요. 각 기기의 전환율도 달라졌는지 확인한 뒤 배분 변경을 검토하세요.",
}
# 서로 같은 현상을 설명할 수 있는 후보 쌍 — 한쪽이 확실히 앞서지 않으면 둘 다 단정하지 않는다
EXCLUSIVE_PAIRS = (("tracking_break", "landing_issue"), ("creative_fatigue", "audience_saturation"), ("bid_competition", "budget_cap"), ("landing_issue", "mix_shift"))


# ───────────────────────────── 근거 레코드 ─────────────────────────────
@dataclass(frozen=True)
class Num:
    """근거 문장에 나오는 숫자 하나. unit: % · 원 · 회 · 건 · 일 · 개 · 배 · date(value = 월*100+일)."""

    key: str
    value: float
    unit: str
    year: int = 0

    def to_dict(self) -> dict:
        return {"key": self.key, "value": self.value, "unit": self.unit, "year": self.year}


@dataclass(frozen=True)
class Evidence:
    id: str
    kind: str                 # incident · metric · funnel · mix · group · hypothesis · sample · note …
    text: str
    numbers: tuple = ()
    cause: str = ""           # 이 근거가 속한 원인 가설 (있으면)
    direction: str = ""       # up · down · "" — 문장의 방향 단어와 맞는지 확인하는 데 쓴다

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "text": self.text, "numbers": [n.to_dict() for n in self.numbers], "cause": self.cause, "direction": self.direction}


class EvidenceBook:
    def __init__(self) -> None:
        self.items: dict[str, Evidence] = {}
        self._keys: dict[str, str] = {}

    def add(self, kind: str, text: str, numbers: Iterable[Num] = (), cause: str = "", direction: str = "", key: str = "") -> str:
        if key and key in self._keys:
            return self._keys[key]
        eid = f"E{len(self.items) + 1:02d}"
        self.items[eid] = Evidence(eid, kind, text, tuple(numbers), cause, direction)
        if key:
            self._keys[key] = eid
        return eid


def fmt_date(d: dt.date) -> str:
    return f"{d.month}월 {d.day}일"


def date_num(key: str, d: dt.date) -> Num:
    return Num(key, d.month * 100 + d.day, "date", d.year)


def _int(x: float) -> str:
    return f"{int(round(x)):,}"


def pct_text(v: float) -> str:
    """퍼센트 값(이미 100 을 곱한 수) → 글. 1,000% 이상은 소수점 없이 쉼표로 읽기 쉽게."""
    v = abs(v)
    return f"{v:,.0f}%" if v >= 1000 else f"{v:.1f}%"


def _pct(x: float) -> str:
    return pct_text(x * 100)


def _has_batchim(word: str) -> bool:
    if word.endswith("%"):
        return False        # '퍼센트' 로 읽는다
    for ch in reversed(word):
        if "가" <= ch <= "힣":
            return (ord(ch) - 0xAC00) % 28 != 0
        if ch.isdigit():
            return ch in "013678"
        if ch.isalpha():
            return ch.lower() in "lmnr"
    return False


def josa(word: str, pair: str) -> str:
    """받침에 따라 조사를 붙인다. pair 예: '은/는' · '이/가' · '을/를'."""
    a, b = pair.split("/")
    return word + (a if _has_batchim(word) else b)


# ───────────────────────────── 집계 ─────────────────────────────
class Counts:
    __slots__ = ("imp", "clk", "conv", "cost", "reach", "hour")

    def __init__(self, imp: float = 0.0, clk: float = 0.0, conv: float = 0.0, cost: float = 0.0, reach: Optional[float] = None, hour: Optional[float] = None):
        self.imp, self.clk, self.conv, self.cost, self.reach, self.hour = imp, clk, conv, cost, reach, hour

    def add_row(self, r: MetricRow) -> None:
        self.imp += r.impressions
        self.clk += r.clicks
        self.conv += r.conversions
        self.cost += r.cost_krw
        if r.reach is not None:
            self.reach = (self.reach or 0.0) + r.reach
        if r.delivery_end_hour is not None:
            self.hour = r.delivery_end_hour if self.hour is None else min(self.hour, r.delivery_end_hour)


_ZERO = Counts()


def csum(items: Iterable[Counts]) -> Counts:
    out = Counts()
    for c in items:
        out.imp += c.imp
        out.clk += c.clk
        out.conv += c.conv
        out.cost += c.cost
        if c.reach is not None:
            out.reach = (out.reach or 0.0) + c.reach
    return out


def cmean(items: list[Counts]) -> Counts:
    n = len(items)
    s = csum(items)
    if n == 0:
        return Counts()
    return Counts(s.imp / n, s.clk / n, s.conv / n, s.cost / n, None if s.reach is None else s.reach / n)


def metric_value(metric: str, c: Counts, *, smooth: bool = True) -> Optional[float]:
    """탐지에는 가짜 관측을 더해 로그를 안정화하고, 보고서에는 smooth=False 로 실제 비율을 쓴다."""
    if metric == "impressions":
        return c.imp
    if metric == "clicks":
        return c.clk
    if metric == "conversions":
        return c.conv
    if metric == "cost":
        return c.cost
    if metric == "ctr":
        return ((c.clk + 0.5) / (c.imp + 1.0) if smooth else c.clk / c.imp) if c.imp > 0 else None
    if metric == "cpc":
        return c.cost / c.clk if c.clk > 0 else None
    if metric == "cvr":
        return ((c.conv + 0.5) / (c.clk + 1.0) if smooth else c.conv / c.clk) if c.clk > 0 else None
    if metric == "cpa":
        if not smooth:
            return c.cost / c.conv if c.conv > 0 else None
        return c.cost / max(c.conv, 0.5) if c.cost > 0 else None
    raise KeyError(metric)


def _log(v: Optional[float], metric: str) -> Optional[float]:
    if v is None:
        return None
    floor = 0.5 if metric in ("impressions", "clicks", "conversions", "cost") else 1e-9
    return math.log(max(v, floor))


@dataclass
class Table:
    dates: list[dt.date]
    total: dict[dt.date, Counts]
    present: set
    dims: dict[str, dict[str, dict[dt.date, Counts]]]
    hour_by_day: dict[dt.date, Optional[float]]
    has_reach: bool
    has_hour: bool
    creative_sets: dict = field(default_factory=dict)      # 소재 → 그 소재가 쓰인 광고세트들 (같은 광고세트의 다른 소재끼리 비교하려고)

    def dim_values(self, dim: str) -> list[str]:
        return sorted(self.dims.get(dim, {}))

    def has_dim(self, dim: str) -> bool:
        vals = [v for v in self.dims.get(dim, {}) if v != NO_DIM]
        return len(vals) >= 2


def build_table(rows: list[MetricRow], first: dt.date, last: dt.date) -> Table:
    dates = [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]
    total: dict[dt.date, Counts] = {}
    dims: dict[str, dict[str, dict[dt.date, Counts]]] = {d: defaultdict(dict) for d in ("campaign", "ad_set", "creative", "device")}
    present: set = set()
    hour_by_day: dict[dt.date, Optional[float]] = {}
    creative_sets: dict[str, set] = defaultdict(set)
    has_reach = has_hour = False
    for r in rows:
        if r.date < first or r.date > last:
            continue
        present.add(r.date)
        creative_sets[r.creative].add(r.ad_set)
        total.setdefault(r.date, Counts()).add_row(r)
        if r.reach is not None:
            has_reach = True
        if r.delivery_end_hour is not None:
            has_hour = True
            h = hour_by_day.get(r.date)
            hour_by_day[r.date] = r.delivery_end_hour if h is None else min(h, r.delivery_end_hour)
        for dim in dims:
            val = getattr(r, dim)
            dims[dim][val].setdefault(r.date, Counts()).add_row(r)
    return Table(dates=dates, total=total, present=present, dims={k: dict(v) for k, v in dims.items()}, hour_by_day=hour_by_day, has_reach=has_reach, has_hour=has_hour,
                 creative_sets=dict(creative_sets))


# ───────────────────────────── 탐지 ─────────────────────────────
@dataclass
class Span:
    start: dt.date
    end: dt.date
    direction: str            # up · down
    pct: float                # 기대 대비 변화율 (부호 있음)
    score: float              # |Σz| / √(하루 잡음 + 기준선·외삽 추세의 추정 분산)
    peak_z: float
    days: int
    segment: str = ""         # 소재·기기 같은 묶음 하나에서만 잡힌 변화면 "소재: 소재A1" 꼴 (전체 지표에서 잡힌 것은 빈 문자열)


@dataclass
class MetricDetection:
    metric: str
    status: str               # ok · withheld
    reason: str = ""
    sigma: float = 0.0
    mu: float = 0.0
    trend: float = 0.0
    dow: dict = field(default_factory=dict)
    spans: list = field(default_factory=list)
    z: dict = field(default_factory=dict)
    expected: dict = field(default_factory=dict)
    actual: dict = field(default_factory=dict)
    comparisons: int = 1
    score_threshold: float = H_SCORE


def _forecast_counts(reference: Counts, day: dt.date, detections: dict) -> Counts:
    """Use the detector's count forecasts for diagnosis and summary cards.

    Rates are derived from these same counts so CPA = CPC / CVR remains true.
    A weekday-only historical level can reverse an event's direction when the
    selected reference has a continuing trend. Reach has no forecasting model;
    do not combine an extrapolated impression count with historical reach.
    """
    values = {}
    for metric, attr in (("impressions", "imp"), ("clicks", "clk"), ("conversions", "conv"), ("cost", "cost")):
        det = detections.get(metric)
        values[attr] = det.expected.get(day, getattr(reference, attr)) if det and det.status == "ok" else getattr(reference, attr)
    imp_detection = detections.get("impressions")
    reach = None if imp_detection and imp_detection.trend else reference.reach
    return Counts(**values, reach=reach, hour=reference.hour)


def _mad(vals: list[float]) -> float:
    m = median(vals)
    return median([abs(v - m) for v in vals])


def _sampling_sd(metric: str, mean_clk: float, mean_conv: float) -> float:
    """표본 크기만으로 생기는 하루치 상대 잡음(로그 단위) — 이보다 작은 흔들림은 잡음으로 본다."""
    if metric in ("clicks", "ctr"):
        return 1.0 / math.sqrt(max(mean_clk, 1.0))
    if metric in ("cpc", "cost"):
        return 0.7 / math.sqrt(max(mean_clk, 1.0))
    if metric in ("cvr", "cpa", "conversions"):
        return 1.0 / math.sqrt(max(mean_conv, 1.0))
    return 0.0


def detect_metric(metric: str, total: dict, present: set, base_dates: list[dt.date], win_dates: list[dt.date], sample: "SampleStatus", *,
                  h_score: float = H_SCORE, h_single: float = H_SINGLE, min_effect: float = MIN_EFFECT, min_len: int = 1,
                  comparisons: int = 1) -> MetricDetection:
    """지표 하나의 변화 구간을 찾는다. total 은 날짜 → Counts (전체 또는 묶음 하나)."""
    # 한 보고서에서 더 많은 지표·묶음을 검사할수록 같은 신호에 더 엄격한 기준을 쓴다.
    # 정규 꼬리 상계의 union bound 형태다. 보정된 점수도 실서비스의 유의확률은 아니다.
    comparisons = max(1, comparisons)
    h_score = math.sqrt(h_score ** 2 + 2 * math.log(comparisons))
    h_single = math.sqrt(h_single ** 2 + 2 * math.log(comparisons))
    det = MetricDetection(metric=metric, status="ok", comparisons=comparisons, score_threshold=h_score)
    if metric in CONV_METRICS and not sample.conv_ok:
        det.status, det.reason = "withheld", "전환이 너무 적어 전환 관련 지표는 판단을 보류합니다"
        return det
    if not sample.clicks_ok:
        det.status, det.reason = "withheld", "클릭이 너무 적어 판단을 보류합니다"
        return det
    y: dict[dt.date, float] = {}
    for d in base_dates + win_dates:
        if d in present:
            v = _log(metric_value(metric, total.get(d, _ZERO)), metric)
            if v is not None:
                y[d] = v
    base_y = [(d, y[d]) for d in base_dates if d in y]
    if len(base_y) < MIN_BASELINE_DAYS:
        det.status, det.reason = "withheld", "기준 기간의 자료가 부족합니다"
        return det
    m0 = median([v for _, v in base_y])
    by_wd: dict[int, list[float]] = defaultdict(list)
    for d, v in base_y:
        by_wd[d.weekday()].append(v - m0)
    dow = {w: median(vs) for w, vs in by_wd.items()}
    n, k = len(base_y), len(by_wd)
    t0 = base_y[0][0] + (base_y[-1][0] - base_y[0][0]) / 2
    # 완만한 추세: 기준 기간 안에서 뚜렷할 때만 (Theil–Sen 기울기가 표준오차의 3배 이상) 이어 간다.
    # 이전 기준 기간을 쓰면 예측 거리가 길어진다. 추세를 중간에 멈추지 않고 아래의 예측 분산을 늘린다.
    r0 = [(d, v - dow.get(d.weekday(), 0.0)) for d, v in base_y]
    slopes = [(r0[j][1] - r0[i][1]) / (r0[j][0] - r0[i][0]).days for i in range(n) for j in range(i + 1, n) if (r0[j][0] - r0[i][0]).days >= 7]
    slope = median(slopes) if slopes else 0.0
    span = max((r0[-1][0] - r0[0][0]).days, 1)
    se = 1.4826 * _mad([r for _, r in r0]) * math.sqrt(12) / (math.sqrt(n) * span)
    trend = slope if (abs(slope) >= 3 * se and abs(slope) * span >= 0.03) else 0.0
    forecast_gap = bool(win_dates and (win_dates[0] - base_y[-1][0]).days > 1)

    def drift(d: dt.date) -> float:
        return trend * (d - t0).days if trend else 0.0

    resid = [r - drift(d) for d, r in r0]
    mu = median(resid)
    robust = 1.4826 * _mad(resid) * math.sqrt(n / max(n - k, 1))
    # 같은 요일 끼리 한 주 간격 차이로 본 잡음 — 요일 효과를 맞추는 과정에 영향받지 않는다
    ydict = dict(base_y)
    diffs = [ydict[d] - ydict[d - dt.timedelta(days=7)] for d in ydict if (d - dt.timedelta(days=7)) in ydict]
    wk = 1.4826 * _mad(diffs) / math.sqrt(2) if len(diffs) >= 6 else 0.0
    mean_clk = sum(total.get(d, _ZERO).clk for d, _ in base_y) / n
    mean_conv = sum(total.get(d, _ZERO).conv for d, _ in base_y) / n
    sigma = max(max(robust, wk) * SIGMA_INFLATE, _sampling_sd(metric, mean_clk, mean_conv), SIGMA_FLOOR)
    det.sigma, det.mu, det.dow, det.trend = sigma, mu, dow, trend
    for d in base_dates + win_dates:
        e = mu + dow.get(d.weekday(), 0.0) + drift(d)
        det.expected[d] = math.exp(e)
        if d in y:
            det.actual[d] = math.exp(y[d])
    zs: list[float] = []
    for d in win_dates:
        z = (y[d] - dow.get(d.weekday(), 0.0) - drift(d) - mu) / sigma if d in y else 0.0
        det.z[d] = z
        zs.append(z)
    # 기준선은 알려진 상수가 아니다. 같은 요일의 기준값 오차는 구간 내 여러 날에
    # 공유되므로 날짜 수의 제곱으로 누적한다. 추세를 외삽했다면 그 기울기 오차도 더한다.
    # 독립 하루 오차만 √일수로 나누던 방식은 긴 구간과 우연한 추세를 과대평가했다.
    base_times = {w: [(d - t0).days for d, _ in base_y if d.weekday() == w] for w in by_wd}
    mean_time = {w: sum(ts) / len(ts) for w, ts in base_times.items()}
    slope_information = sum((t - mean_time[w]) ** 2 for w, ts in base_times.items() for t in ts)

    def span_score(i: int, j: int, acc: float) -> float:
        weekdays: dict[int, int] = defaultdict(int)
        horizon = 0.0
        for d in win_dates[i:j + 1]:
            if d not in y:
                continue
            w = d.weekday()
            weekdays[w] += 1
            horizon += (d - t0).days - mean_time.get(w, 0.0)
        observed = sum(weekdays.values())
        variance = observed + BASELINE_ESTIMATION_FACTOR * sum(count ** 2 / len(by_wd[w]) for w, count in weekdays.items() if w in by_wd)
        # 짧고 오래된 기준선에서는 기울기가 유의하지 않다는 것이 기울기 0의 증거가 아니다.
        # 선택한 추세가 0이어도, 기간 사이에 간격이 있으면 가능한 기울기의 오차를 포함한다.
        if (trend or forecast_gap) and slope_information:
            variance += BASELINE_ESTIMATION_FACTOR * horizon ** 2 / slope_information
        return abs(acc) / math.sqrt(max(variance, 1.0))

    work = list(zs)
    for _ in range(2):                         # 한 지표에서 최대 두 구간
        best = None
        L = len(work)
        for i in range(L):
            acc = 0.0
            for j in range(i, L):
                acc += work[j]
                ln = j - i + 1
                if ln < min_len:
                    continue
                score = span_score(i, j, acc)
                if ln == 1 and score < h_single:
                    continue
                if best is None or score > best[0]:
                    best = (score, i, j)
        if best is None or best[0] < h_score:
            break
        score, i, j = best
        sign = 1 if sum(work[i:j + 1]) > 0 else -1
        while i > 0 and sign * work[i - 1] >= 2.0:       # 같은 방향으로 뚜렷한 이웃 날은 구간에 포함
            i -= 1
        while j < L - 1 and sign * work[j + 1] >= 2.0:
            j += 1
        days = [win_dates[t] for t in range(i, j + 1) if win_dates[t] in y]
        if not days:
            break
        pct = math.exp(sum(y[d] - dow.get(d.weekday(), 0.0) - drift(d) - mu for d in days) / len(days)) - 1
        if abs(pct) >= min_effect:
            det.spans.append(Span(start=win_dates[i], end=win_dates[j], direction="up" if sign > 0 else "down", pct=pct, score=score,
                                  peak_z=max((abs(w) for w in work[i:j + 1]), default=0.0), days=j - i + 1))
        for t in range(i, j + 1):
            work[t] = 0.0
    return det


@dataclass
class SampleStatus:
    clicks_ok: bool
    conv_ok: bool
    base_clicks: int
    base_conv: int
    base_days: int

    @property
    def level(self) -> str:
        if not self.clicks_ok:
            return "insufficient"
        return "ok" if self.conv_ok else "partial"


def sample_status(table: Table, base_dates: list[dt.date], series: Optional[dict] = None, *, min_clicks: int = MIN_BASE_CLICKS, min_conv: int = MIN_BASE_CONV,
                  per_day_clicks: float = MIN_BASE_CLICKS_PER_DAY, per_day_conv: float = MIN_BASE_CONV_PER_DAY) -> SampleStatus:
    series = table.total if series is None else series
    days = [d for d in base_dates if d in table.present]
    clk = sum(series.get(d, _ZERO).clk for d in days)
    conv = sum(series.get(d, _ZERO).conv for d in days)
    nd = max(len(days), 1)
    return SampleStatus(clicks_ok=clk >= min_clicks and clk / nd >= per_day_clicks,
                        conv_ok=conv >= min_conv and conv / nd >= per_day_conv, base_clicks=int(clk), base_conv=int(conv), base_days=len(days))


@dataclass
class Incident:
    index: int
    start: dt.date
    end: dt.date
    findings: list = field(default_factory=list)        # (metric, Span)
    severity: str = "low"
    polarity: str = "worse"                              # worse · better
    ongoing: bool = False

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def _cluster(findings: list[tuple[str, Span]], win_end: dt.date) -> list[Incident]:
    items = sorted(findings, key=lambda f: (f[1].start, f[1].end))
    groups: list[list[tuple[str, Span]]] = []
    cur_end = None
    for f in items:
        if groups and cur_end is not None and f[1].start <= cur_end + dt.timedelta(days=1):
            groups[-1].append(f)
            cur_end = max(cur_end, f[1].end)
        else:
            groups.append([f])
            cur_end = f[1].end
    out = []
    for i, g in enumerate(groups):
        start = min(s.start for _, s in g)
        end = max(s.end for _, s in g)
        bad = [(m, s) for m, s in g if BAD_DIRECTION.get(m) and s.direction == BAD_DIRECTION[m]]
        good = [(m, s) for m, s in g if m in OUTCOME_METRICS and s.direction != BAD_DIRECTION[m]]
        polarity = "better" if good and not bad else "worse"      # 노출수·광고비 증가만으로는 '좋아졌다' 고 하지 않는다 — 성과 지표(클릭·전환 등)가 좋아지고 나빠진 것이 없을 때만
        top = max((min(abs(s.pct) / 0.3, 1.0) * 0.6 + min(s.score / 10.0, 1.0) * 0.4 for _, s in (bad or g)), default=0.0)
        out.append(Incident(index=i, start=start, end=end, findings=g, severity="high" if top >= 0.7 else "medium" if top >= 0.4 else "low",
                            polarity=polarity, ongoing=end >= win_end))
    return out


# ───────────────────────────── 진단 ─────────────────────────────
@dataclass
class Obs:
    key: str
    ev: str = ""
    v: dict = field(default_factory=dict)
    ok: bool = False
    missing: str = ""


@dataclass(frozen=True)
class CheckDef:
    cid: str
    role: str                 # for · against
    weight: float
    obs: str
    test: Callable[[dict], bool]
    label: str                # 쉬운 말로 쓴 판단 기준
    critical: object = False  # True: 확인하지 못했으면 0.44 이하, 어긋났으면 0.20 이하로 묶는다 / "eval": 확인했는데 어긋났을 때만 0.20 이하 / "assert": 확인하지 못했거나 어긋나면 단정(0.75)만 막는다


@dataclass
class CheckResult:
    cid: str
    role: str
    weight: float
    critical: bool
    label: str
    passed: Optional[bool]    # None = 확인하지 못함
    evidence_id: str = ""
    missing: str = ""

    def to_dict(self) -> dict:
        return {"cid": self.cid, "role": self.role, "weight": self.weight, "critical": self.critical, "label": self.label, "passed": self.passed,
                "evidence_id": self.evidence_id, "missing": self.missing}


@dataclass
class Hypothesis:
    cause: str
    label: str
    confidence: float
    state: str                # supported · likely · candidate · unlikely
    evidence: list = field(default_factory=list)           # 맞는 근거 id
    contradicted_by: list = field(default_factory=list)    # 반대 근거 id
    data_missing: list = field(default_factory=list)       # 확인하지 못한 것 (쉬운 말)
    checks: list = field(default_factory=list)
    support_ratio: float = 0.0
    coverage: float = 0.0
    contradiction_ratio: float = 0.0
    rival_note: str = ""
    evidence_id: str = ""     # 이 가설의 요약 근거 (확신 숫자)
    raw_confidence: float = 0.0   # 상한을 적용하기 전 계산값
    cap_reason: str = ""          # 상한이 걸렸다면 그 이유 (쉬운 말)
    verification: str = ""        # 집계 자료만으로 가설을 확정하지 않고 실제로 확인할 일
    verification_evidence: str = ""

    def to_dict(self) -> dict:
        return {"cause": self.cause, "label": self.label, "confidence": round(self.confidence, 4), "state": self.state, "evidence": list(self.evidence),
                "contradicted_by": list(self.contradicted_by), "data_missing": list(self.data_missing), "checks": [c.to_dict() for c in self.checks],
                "support_ratio": round(self.support_ratio, 4), "coverage": round(self.coverage, 4), "contradiction_ratio": round(self.contradiction_ratio, 4),
                "rival_note": self.rival_note, "evidence_id": self.evidence_id, "raw_confidence": round(self.raw_confidence, 4), "cap_reason": self.cap_reason,
                "verification": self.verification, "verification_evidence": self.verification_evidence}


def state_of(conf: float) -> str:
    if conf >= ASSERT_CONFIDENCE:
        return "supported"
    if conf >= LIKELY_CONFIDENCE:
        return "likely"
    if conf >= CANDIDATE_CONFIDENCE:
        return "candidate"
    return "unlikely"


@dataclass
class Diagnosis:
    incident: Incident
    hypotheses: list          # 확신 순
    cause_state: str          # supported · likely · undetermined · not_applicable
    top_cause: str = ""
    undetermined_evidence: str = ""
    funnel_evidence: list = field(default_factory=list)
    primary_evidence: list = field(default_factory=list)   # 이 이상 구간을 설명하는 핵심 근거 id (구간·지표 변화)
    incident_evidence: str = ""
    metric_evidence: dict = field(default_factory=dict)    # 지표 → 근거 id
    missing_evidence: str = ""                             # 확인하지 못한 자료 안내 근거

    def hyp(self, cause: str) -> Optional[Hypothesis]:
        return next((h for h in self.hypotheses if h.cause == cause), None)


class Diagnoser:
    """한 이상 구간에 대해 관측값(근거)을 계산하고 가설별 검사를 돌린다."""

    def __init__(self, table: Table, base_dates: list[dt.date], inc: Incident, detections: dict, sample: SampleStatus, book: EvidenceBook, *,
                 onset_left_censored: bool = False):
        self.t, self.base, self.inc, self.det, self.sample, self.book = table, [d for d in base_dates if d in table.present], inc, detections, sample, book
        self.onset_left_censored = onset_left_censored
        self.days = [d for d in table.dates if inc.start <= d <= inc.end and d in table.present]
        self._obs: dict[str, Obs] = {}
        wd: dict[int, list[dt.date]] = defaultdict(list)
        for d in self.base:
            wd[d.weekday()].append(d)
        self._base_by_wd = wd
        self.L = max(len(self.days), 1)
        self.exp_total = self._exp(table.total)
        self.act_total = self._act(table.total)

    # ----- 기대/실제 합계 -----
    def _exp_day(self, series: dict, d: dt.date) -> Counts:
        days = self._base_by_wd.get(d.weekday()) or self.base
        reference = cmean([series.get(x, _ZERO) for x in days])
        return _forecast_counts(reference, d, self.det) if series is self.t.total else reference

    def _exp(self, series: dict, days: Optional[list] = None) -> Counts:
        return csum(self._exp_day(series, d) for d in (days if days is not None else self.days))

    def _act(self, series: dict, days: Optional[list] = None) -> Counts:
        return csum(series.get(d, _ZERO) for d in (days if days is not None else self.days))

    def tol(self, metric: str, floor: float = 0.08) -> float:
        det = self.det.get(metric)
        sigma = det.sigma if det is not None and det.status == "ok" else 0.1
        return max(floor, 2.5 * sigma / math.sqrt(self.L))

    def _group_ok(self, series: dict, need_conv: bool = False) -> bool:
        clk = sum(series.get(d, _ZERO).clk for d in self.base)
        conv = sum(series.get(d, _ZERO).conv for d in self.base)
        return clk >= GROUP_MIN_CLICKS and (not need_conv or conv >= GROUP_MIN_CONV)

    def _groups(self, dim: str, need_conv: bool = False) -> list[tuple[str, dict]]:
        if not self.t.has_dim(dim):
            return []
        return [(v, s) for v, s in sorted(self.t.dims[dim].items()) if v != NO_DIM and self._group_ok(s, need_conv)]

    # ----- 관측 -----
    def obs(self, key: str) -> Obs:
        if key not in self._obs:
            fn = getattr(self, "_o_" + key.split(":")[0])
            self._obs[key] = fn(*key.split(":")[1:])
        return self._obs[key]

    def _fmt_metric(self, metric: str, v: float) -> str:
        if metric in ("ctr", "cvr"):
            return f"{v * 100:.2f}%"
        return f"{_int(v)}{METRIC_UNIT[metric]}"

    def _num_metric(self, key: str, metric: str, v: float) -> Num:
        if metric in ("ctr", "cvr"):
            return Num(key, round(v * 100, 2), "%")
        return Num(key, round(v, 1), METRIC_UNIT[metric])

    def _o_metric(self, metric: str) -> Obs:
        det = self.det.get(metric)
        if det is None or det.status != "ok":
            return Obs(f"metric:{metric}", missing=(det.reason if det else "확인하지 못했습니다"))
        a, e = metric_value(metric, self.act_total, smooth=False), metric_value(metric, self.exp_total, smooth=False)
        if a is None or e is None or e <= 0:
            return Obs(f"metric:{metric}", missing=f"{METRIC_LABEL[metric]}을 계산할 수 없습니다")
        pct = a / e - 1
        label = METRIC_LABEL[metric]
        word = ("많았", "적었") if metric in ("impressions", "clicks", "conversions", "cost") else ("높았", "낮았")
        text = (f"{josa(label, '은/는')} 이 구간 {'합계' if metric in ('impressions', 'clicks', 'conversions', 'cost') else '평균'} {josa(self._fmt_metric(metric, a), '으로/로')} "
                f"기대({self._fmt_metric(metric, e)})보다 {_pct(pct)} {word[0] if pct >= 0 else word[1]}습니다.")
        eid = self.book.add("metric", text, [self._num_metric("actual", metric, a), self._num_metric("expected", metric, e), Num("change", round(pct * 100, 1), "%")],
                            direction="up" if pct > 0.005 else "down" if pct < -0.005 else "", key=f"metric:{metric}:{self.inc.index}")
        return Obs(f"metric:{metric}", eid, {"pct": pct, "act": a, "exp": e}, ok=True)

    def _o_onset(self) -> Obs:
        if self.onset_left_censored:
            return Obs("onset", missing="표시 기간 첫날에 이미 변화가 있어 실제 시작 속도를 확인하지 못했습니다")
        first = self.days[:2]
        a, e = self._act(self.t.total, first), self._exp(self.t.total, first)
        if e.conv <= 0 or not self.sample.conv_ok:
            return Obs("onset", missing="전환 변화의 시작 속도를 확인하지 못했습니다")
        ratio = a.conv / e.conv
        text = f"이상 구간 첫 {len(first)}일 동안 전환은 기대의 {ratio * 100:.1f}% 수준이었습니다."
        eid = self.book.add("onset", text, [Num("days", len(first), "일"), Num("ratio", round(ratio * 100, 1), "%")], direction="down" if ratio < 0.95 else "",
                            key=f"onset:{self.inc.index}")
        return Obs("onset", eid, {"ratio": ratio}, ok=True)

    def _o_spread(self) -> Obs:
        """전환율 변화가 어느 묶음에서나 똑같이 나타나는가 (기기 → 광고세트 → 소재 순으로 가능한 첫 차원)."""
        if not self.sample.conv_ok:
            return Obs("spread", missing="전환이 적어 묶음별 전환율을 비교하지 못했습니다")
        for dim, name in (("device", "기기"), ("ad_set", "광고세트"), ("creative", "소재")):
            groups = self._groups(dim, need_conv=True)
            if len(groups) < 2:
                continue
            changes = []
            for gname, s in groups:
                a, e = self._act(s), self._exp(s)
                if e.clk > 0 and a.clk > 0 and e.conv > 0:
                    changes.append(((a.conv / a.clk) / (e.conv / e.clk) - 1, gname))
            if len(changes) < 2:
                continue
            dropped = sum(c <= -0.5 for c, _ in changes)
            worst_pct, worst_name = min(changes)
            text = f"{name} {len(changes)}개 중 {dropped}개에서 전환율이 기대보다 절반 넘게 떨어졌습니다."
            eid = self.book.add("group", text, [Num("groups", len(changes), "개"), Num("dropped", dropped, "개")], direction="down" if dropped else "",
                                key=f"spread:{dim}:{self.inc.index}")
            return Obs("spread", eid, {"n": len(changes), "dropped": dropped, "all": dropped == len(changes), "worst_pct": worst_pct, "worst_name": worst_name, "dim_name": name}, ok=True)
        return Obs("spread", missing="기기·광고세트·소재 구분이 없어 어느 묶음에서나 같은 변화인지 확인하지 못했습니다")

    def _o_collapse(self) -> Obs:
        """전환율이 사실상 사라졌는가 — 전체로 보든, 어느 한 묶음(예: 한 기기)에서든."""
        mv = self.obs("metric:cvr")
        if not mv.ok:
            return Obs("collapse", missing=mv.missing)
        sp = self.obs("spread")
        overall = mv.v["pct"]
        if sp.ok:
            w = sp.v["worst_pct"]
            text = (f"전체 전환율은 기대보다 {_pct(overall)} {'낮았' if overall < 0 else '높았'}고, 가장 크게 변한 {sp.v['dim_name']}('{sp.v['worst_name']}')는 "
                    f"{_pct(w)} {'낮았' if w < 0 else '높았'}습니다.")
            eid = self.book.add("group", text, [Num("overall", round(overall * 100, 1), "%"), Num("worst", round(w * 100, 1), "%")],
                                direction=("down" if overall < 0 else "up") if (overall < 0) == (w < 0) else "",
                                key=f"collapse:{self.inc.index}")
            return Obs("collapse", eid, {"overall": overall, "worst": w}, ok=True)
        return Obs("collapse", mv.ev, {"overall": overall, "worst": None}, ok=True)

    def _o_concentration(self) -> Obs:
        """전환 감소분이 한 기기에 몰렸는가. 한 기기에서만 전환이 사라지면 추적 오류인지 그 기기의 랜딩 문제인지 데이터만으로는 가릴 수 없다."""
        if not self.sample.conv_ok:
            return Obs("concentration", missing="전환이 적어 기기별 전환 감소를 비교하지 못했습니다")
        groups = self._groups("device", need_conv=True)
        if len(groups) < 2:
            return Obs("concentration", missing="기기 구분이 없어 한 기기에만 몰린 문제인지 확인하지 못했습니다")
        losses, clicks = [], []
        for gname, s in groups:
            a, e = self._act(s), self._exp(s)
            r0 = e.conv / e.clk if e.clk > 0 else 0.0
            r1 = a.conv / a.clk if a.clk > 0 else r0
            losses.append((max(0.0, (r0 - r1) * a.clk), gname))
            clicks.append(a.clk)
        total = sum(l for l, _ in losses)
        if total <= 0:
            return Obs("concentration", "", {"concentrated": False, "share": 0.0}, ok=True)
        loss, gname = max(losses)
        click_share = clicks[[g for g, _ in groups].index(gname)] / max(sum(clicks), 1.0)
        share = loss / total
        text = f"전환 감소분의 {share * 100:.1f}%가 기기 '{gname}'에서 나왔습니다 (그 기기의 클릭 비중은 {click_share * 100:.1f}%)."
        eid = self.book.add("group", text, [Num("loss_share", round(share * 100, 1), "%"), Num("click_share", round(click_share * 100, 1), "%")], key=f"concentration:{self.inc.index}")
        return Obs("concentration", eid, {"concentrated": share >= 0.85 and click_share <= 0.75, "share": share, "click_share": click_share, "name": gname}, ok=True)

    def _o_mix(self) -> Obs:
        """전환율 변화를 '구성(클릭 비중) 효과' 와 '묶음 안 전환율 효과' 로 분해한다 (Kitagawa 분해 — 두 효과의 합이 전체 변화와 같다)."""
        if not self.sample.conv_ok:
            return Obs("mix", missing="전환이 적어 구성 효과를 분해하지 못했습니다")
        best = None
        for dim, name in (("device", "기기"), ("ad_set", "광고세트"), ("creative", "소재")):
            groups = self._groups(dim, need_conv=True)
            if len(groups) < 2:
                continue
            ga = [(self._act(s), self._exp(s)) for _, s in groups]
            tot_a, tot_e = sum(a.clk for a, _ in ga), sum(e.clk for _, e in ga)
            if tot_a <= 0 or tot_e <= 0:
                continue
            mix = rate = 0.0
            max_dev = 0.0
            max_share = 0.0
            for a, e in ga:
                w1, w0 = a.clk / tot_a, e.clk / tot_e
                r0 = e.conv / e.clk if e.clk > 0 else None
                r1 = a.conv / a.clk if a.clk > 0 else None
                r0 = r0 if r0 is not None else r1
                r1 = r1 if r1 is not None else r0
                if r0 is None:
                    continue
                mix += (w1 - w0) * (r0 + r1) / 2
                rate += (r1 - r0) * (w0 + w1) / 2
                if r0 > 0:
                    max_dev = max(max_dev, abs(r1 / r0 - 1))
                max_share = max(max_share, abs(w1 - w0))
            total = mix + rate
            denom = abs(mix) + abs(rate)
            if denom <= 0:
                continue
            cand = {"dim": dim, "name": name, "mix": mix, "rate": rate, "total": total, "mix_share": abs(mix) / denom, "rate_share": abs(rate) / denom,
                    "max_group_dev": max_dev, "max_share_move": max_share, "n": len(ga)}
            if best is None or cand["mix_share"] > best["mix_share"]:
                best = cand
        if best is None:
            return Obs("mix", missing="기기·광고세트·소재 구분이 없어 구성 효과를 분해하지 못했습니다")
        text = (f"전체 전환율 변화를 {best['name']}별로 나누면, 노출 비중이 바뀐 효과가 {best['mix_share'] * 100:.1f}%, "
                f"{best['name']} 안에서 전환율 자체가 바뀐 효과가 {best['rate_share'] * 100:.1f}%입니다.")
        eid = self.book.add("mix", text, [Num("mix_share", round(best["mix_share"] * 100, 1), "%"), Num("rate_share", round(best["rate_share"] * 100, 1), "%")],
                            key=f"mix:{self.inc.index}")
        best["ev"] = eid
        return Obs("mix", eid, best, ok=True)

    def _o_creative(self) -> Obs:
        groups = self._groups("creative")
        if len(groups) < 2:
            return Obs("creative", missing="소재가 하나뿐이거나 소재별 표본이 적어 소재끼리 비교하지 못했습니다")
        rows = []
        for name, s in groups:
            a, e = self._act(s), self._exp(s)
            if a.imp > 0 and e.imp > 0 and e.clk > 0:
                ctr_a, ctr_e = (a.clk + 0.5) / (a.imp + 1), (e.clk + 0.5) / (e.imp + 1)
                cvr_p = ((a.conv / a.clk) / (e.conv / e.clk) - 1) if (a.clk > 0 and e.conv > 0 and e.clk > 0) else None
                rows.append({"name": name, "ctr_pct": ctr_a / ctr_e - 1, "imp_pct": a.imp / e.imp - 1, "cvr_pct": cvr_p, "series": s})
        if len(rows) < 2:
            return Obs("creative", missing="소재별 클릭률을 계산하지 못했습니다")
        worst = min(rows, key=lambda r: r["ctr_pct"])
        # 같은 광고세트(= 같은 타겟)의 다른 소재와 비교한다. 같은 세트의 소재가 함께 떨어지면 소재 하나의 문제가 아니라 그 타겟 전체의 문제일 수 있다
        worst_sets = self.t.creative_sets.get(worst["name"], set())
        siblings = [r for r in rows if r is not worst and self.t.creative_sets.get(r["name"], set()) & worst_sets]
        ref = siblings or [r for r in rows if r is not worst]
        med_others = median([r["ctr_pct"] for r in ref])
        n_down = sum(r["ctr_pct"] <= -0.05 for r in rows)
        scope = "같은 광고세트의 나머지 소재" if siblings else "나머지 소재"
        text = (f"소재 '{worst['name']}'의 클릭률이 기대보다 {_pct(worst['ctr_pct'])} {'낮았' if worst['ctr_pct'] < 0 else '높았'}고, "
                f"{scope}의 중앙값은 {_pct(med_others)} {'낮았' if med_others < 0 else '높았'}습니다 (소재 {len(rows)}개 중 {n_down}개가 5% 넘게 하락).")
        eid = self.book.add("group", text, [Num("worst", round(worst["ctr_pct"] * 100, 1), "%"), Num("others", round(med_others * 100, 1), "%"), Num("n", len(rows), "개"),
                                            Num("n_down", n_down, "개"), Num("threshold", 5, "%")],
                            direction=("down" if worst["ctr_pct"] < 0 else "up") if (worst["ctr_pct"] < 0) == (med_others < 0) else "", key=f"creative:{self.inc.index}")
        # 가장 나빠진 소재의 일별 클릭률 비율(실제/기대) — 서서히 줄었는지 단번에 줄었는지
        ratios = []
        for d in self.days:
            a, e = self._act(worst["series"], [d]), self._exp(worst["series"], [d])
            if a.imp > 0 and e.imp > 0 and e.clk > 0:
                ratios.append(((a.clk + 0.5) / (a.imp + 1)) / ((e.clk + 0.5) / (e.imp + 1)))
        return Obs("creative", eid, {"worst": worst, "others_median": med_others, "n": len(rows), "n_down": n_down, "ratios": ratios, "rows": rows}, ok=True)

    def _o_shape(self, which: str = "creative") -> Obs:
        """하락이 서서히(여러 날에 걸쳐) 왔는가 단번에 왔는가. which=creative 는 가장 나빠진 소재의 클릭률, which=ctr 는 전체 클릭률."""
        if self.onset_left_censored:
            return Obs(f"shape:{which}", missing="표시 기간 첫날에 이미 변화가 있어 서서히 시작했는지 단번에 시작했는지 확인하지 못했습니다")
        if which == "creative":
            co = self.obs("creative")
            ratios = co.v.get("ratios", []) if co.ok else []
        else:
            ratios = []
            for d in self.days:
                a, e = self._act(self.t.total, [d]), self._exp(self.t.total, [d])
                if a.imp > 0 and e.imp > 0 and e.clk > 0:
                    ratios.append(((a.clk + 0.5) / (a.imp + 1)) / ((e.clk + 0.5) / (e.imp + 1)))
        if len(ratios) < 6:
            return Obs(f"shape:{which}", missing="이상 구간이 짧아 서서히 변했는지 단번에 변했는지 가리지 못했습니다")
        third = max(2, len(ratios) // 3)
        first2 = sum(ratios[:2]) / 2
        last = sum(ratios[-third:]) / third
        drop_total, drop_first = 1 - last, 1 - first2
        if drop_total <= 0.02:
            return Obs(f"shape:{which}", missing="클릭률 하락이 뚜렷하지 않아 변화 속도를 가리지 못했습니다")
        frac = drop_first / drop_total
        text = f"이상 구간 처음 2일의 하락폭은 마지막 구간 하락폭의 {max(frac, 0) * 100:.1f}% 수준이어서 {'단번에' if frac >= 0.75 else '서서히'} 변했습니다."
        eid = self.book.add("shape", text, [Num("days", 2, "일"), Num("frac", round(max(frac, 0) * 100, 1), "%")], key=f"shape:{which}:{self.inc.index}")
        return Obs(f"shape:{which}", eid, {"frac": frac, "gradual": frac <= 0.65 and drop_total >= 0.05, "sudden": frac >= 0.75}, ok=True)

    def _o_broad(self) -> Obs:
        """클릭률 하락이 여러 소재에 넓게 퍼졌는가 — 전체 소재 기준, 또는 가장 많이 떨어진 광고세트 안의 소재 기준(세트 전체가 함께 떨어졌는가)."""
        groups = self._groups("creative")
        scope_dim = "소재"
        if len(groups) < 2:
            groups = self._groups("ad_set")
            scope_dim = "광고세트"
        if len(groups) < 2:
            return Obs("broad", missing="소재·광고세트 구분이 없어 하락이 넓게 퍼졌는지 확인하지 못했습니다")
        ch: dict[str, float] = {}
        for gname, s in groups:
            a, e = self._act(s), self._exp(s)
            if a.imp > 0 and e.imp > 0 and e.clk > 0:
                ch[gname] = ((a.clk + 0.5) / (a.imp + 1)) / ((e.clk + 0.5) / (e.imp + 1)) - 1
        if len(ch) < 2:
            return Obs("broad", missing="소재·광고세트 구분이 없어 하락이 넓게 퍼졌는지 확인하지 못했습니다")
        best = (sum(c <= -0.05 for c in ch.values()) / len(ch), len(ch), list(ch.values()), f"{scope_dim} {len(ch)}개")
        if scope_dim == "소재":
            by_set: dict[str, list[float]] = defaultdict(list)
            for gname, c in ch.items():
                for aset in self.t.creative_sets.get(gname, ()):
                    by_set[aset].append(c)
            for aset, vals in sorted(by_set.items()):
                if len(vals) >= 2:
                    share = sum(c <= -0.05 for c in vals) / len(vals)
                    if share > best[0] + 1e-9:
                        best = (share, len(vals), vals, f"광고세트 '{aset}'의 소재 {len(vals)}개")
        share, n, vals, scope = best
        n_down = sum(c <= -0.05 for c in vals)
        med = median(vals)
        text = f"{scope} 중 {n_down}개에서 클릭률이 5% 넘게 떨어졌고, 중앙값은 {_pct(med)} {'낮았' if med < 0 else '높았'}습니다."
        eid = self.book.add("group", text, [Num("n", n, "개"), Num("n_down", n_down, "개"), Num("median", round(med * 100, 1), "%"), Num("threshold", 5, "%")],
                            direction="down" if med < 0 else "up", key=f"broad:{self.inc.index}")
        return Obs("broad", eid, {"n": n, "n_down": n_down, "median": med, "share": share}, ok=True)

    def _o_plateau(self) -> Obs:
        """이상 구간의 일별 광고비가 거의 같은 값에 붙어 있는가 (한도에 막힌 모양)."""
        if len(self.days) < 3:
            return Obs("plateau", missing="이상 구간이 너무 짧아 광고비가 한 값에 붙어 있는지 확인하지 못했습니다")
        costs = [self.t.total[d].cost for d in self.days]
        mean = sum(costs) / len(costs)
        if mean <= 0:
            return Obs("plateau", missing="광고비가 없어 확인하지 못했습니다")
        cv = pstdev(costs) / mean
        bcosts = [self.t.total[d].cost for d in self.base]
        bmean = sum(bcosts) / len(bcosts) if bcosts else 0
        bcv = pstdev(bcosts) / bmean if bmean > 0 and len(bcosts) > 1 else 0.0
        text = f"이상 구간 하루 광고비의 흔들림은 평균의 {cv * 100:.1f}%로, 기준 기간({bcv * 100:.1f}%)과 비교됩니다."
        eid = self.book.add("plateau", text, [Num("cv", round(cv * 100, 1), "%"), Num("base_cv", round(bcv * 100, 1), "%")], key=f"plateau:{self.inc.index}")
        return Obs("plateau", eid, {"cv": cv, "base_cv": bcv, "flat": cv <= 0.03 and cv <= 0.5 * max(bcv, 1e-9)}, ok=True)

    def _o_hour(self) -> Obs:
        if not self.t.has_hour:
            return Obs("hour", missing="노출 종료 시각 열이 없어 '오전만 노출됐는지' 확인하지 못했습니다")
        inc_h = [self.t.hour_by_day[d] for d in self.days if self.t.hour_by_day.get(d) is not None]
        base_h = [self.t.hour_by_day[d] for d in self.base if self.t.hour_by_day.get(d) is not None]
        if not inc_h or not base_h:
            return Obs("hour", missing="노출 종료 시각 자료가 부족합니다")
        mi, mb = sum(inc_h) / len(inc_h), sum(base_h) / len(base_h)
        text = f"하루 노출이 끝난 시각은 이 구간 평균 {mi:.1f}시, 기준 기간 평균 {mb:.1f}시입니다."
        eid = self.book.add("hour", text, [Num("inc_hour", round(mi, 1), "시"), Num("base_hour", round(mb, 1), "시")], key=f"hour:{self.inc.index}")
        return Obs("hour", eid, {"inc": mi, "base": mb, "early": mi <= 16 and mb - mi >= 4}, ok=True)

    def _o_freq(self) -> Obs:
        if not self.t.has_reach:
            return Obs("freq", missing="도달 열이 없어 같은 사람에게 반복 노출됐는지 확인하지 못했습니다")
        a, e = self.act_total, self.exp_total
        if not a.reach or not e.reach or a.imp <= 0 or e.imp <= 0:
            return Obs("freq", missing="도달 자료가 부족합니다")
        f1, f0 = a.imp / a.reach, e.imp / e.reach
        pct = f1 / f0 - 1
        text = f"한 사람에게 보여 준 평균 횟수는 {f1:.2f}회로 기대({f0:.2f}회)보다 {_pct(pct)} {'많았' if pct >= 0 else '적었'}습니다."
        eid = self.book.add("freq", text, [Num("freq", round(f1, 2), "회"), Num("expected", round(f0, 2), "회"), Num("change", round(pct * 100, 1), "%")],
                            direction="up" if pct > 0.005 else "down" if pct < -0.005 else "", key=f"freq:{self.inc.index}")
        return Obs("freq", eid, {"pct": pct}, ok=True)

    def _o_funnel(self) -> Obs:
        """전환당 비용(CPA) 변화를 클릭당 비용(CPC) 변화와 전환율(CVR) 변화로 로그 분해: ln(CPA₁/CPA₀) = ln(CPC₁/CPC₀) − ln(CVR₁/CVR₀)."""
        if self.sample.conv_ok and self.act_total.conv == 0:
            text = "이 구간의 전환이 0건이어서 전환당 비용과 비용 기여도를 계산할 수 없습니다."
            eid = self.book.add("funnel", text, [Num("conversions", 0, "건")], key=f"funnel:{self.inc.index}")
            return Obs("funnel", eid, missing=text)
        mc, mv, ma = self.obs("metric:cpc"), self.obs("metric:cvr"), self.obs("metric:cpa")
        if not (mc.ok and mv.ok and ma.ok) or any(o.v[k] <= 0 for o in (mc, mv, ma) for k in ("act", "exp")):
            return Obs("funnel", missing="전환당 비용을 클릭당 비용과 전환율로 나누지 못했습니다")
        a_cpa, e_cpa = ma.v["act"], ma.v["exp"]
        dl = math.log(a_cpa / e_cpa)
        part_cpc = math.log(mc.v["act"] / mc.v["exp"])
        part_cvr = -math.log(mv.v["act"] / mv.v["exp"])
        if abs(dl) < 0.10:
            text = f"전환당 비용은 기대와 거의 같아(변화 {_pct(math.exp(dl) - 1)}) 비용 쪽 원인 분해는 하지 않았습니다."
            eid = self.book.add("funnel", text, [Num("cpa_change", round((math.exp(dl) - 1) * 100, 1), "%")], key=f"funnel:{self.inc.index}")
            return Obs("funnel", eid, {"cpa_log": dl, "share_cpc": 0.0, "share_cvr": 0.0, "flat": True}, ok=True)
        s_cpc, s_cvr = part_cpc / dl, part_cvr / dl

        def share(x: float) -> str:
            return ("−" if x < 0 else "") + f"{abs(x) * 100:.1f}%"
        text = (f"전환당 비용이 기대보다 {_pct(math.exp(dl) - 1)} {'올랐' if dl > 0 else '내렸'}고, 그 기여도는 전환율 변화 {share(s_cvr)}, "
                f"클릭당 비용 변화 {share(s_cpc)}입니다 (두 기여도의 합은 100%).")
        eid = self.book.add("funnel", text, [Num("cpa_change", round((math.exp(dl) - 1) * 100, 1), "%"), Num("cvr_part", round(s_cvr * 100, 1), "%"),
                                             Num("cpc_part", round(s_cpc * 100, 1), "%"), Num("total", 100, "%")], direction="up" if dl > 0 else "down", key=f"funnel:{self.inc.index}")
        return Obs("funnel", eid, {"cpa_log": dl, "share_cpc": s_cpc, "share_cvr": s_cvr, "flat": False}, ok=True)

    # ----- 가설 정의 -----
    def definitions(self) -> dict[str, list[CheckDef]]:
        T = self.tol
        lo = lambda m, x: (lambda v: v["pct"] <= x)           # noqa: E731
        near = lambda m, floor: (lambda v: abs(v["pct"]) <= max(T(m), floor))   # noqa: E731
        return {
            "tracking_break": [
                CheckDef("T1", "for", 3, "collapse", lambda v: v["overall"] <= -0.70 or (v["worst"] is not None and v["worst"] <= -0.85), "전환율이 (전체 또는 한 묶음에서) 거의 0으로 떨어졌는가", critical=True),
                CheckDef("T2", "for", 1, "metric:clicks", near("clicks", 0.12), "클릭수는 평소와 비슷한가 (광고 반응은 그대로인가)"),
                CheckDef("T3", "for", 2, "onset", lambda v: v["ratio"] <= 0.45, "전환이 시작부터 단번에 사라졌는가"),
                CheckDef("T4", "for", 2, "spread", lambda v: v["all"] and v["n"] >= 2, "기기·세트·소재 어디서나 똑같이 사라졌는가", critical="assert"),
                CheckDef("K1", "against", 1, "metric:clicks", lambda v: v["pct"] <= -0.50, "클릭도 절반 넘게 줄었는가 (전환만의 문제가 아님)"),
                CheckDef("K2", "against", 1, "collapse", lambda v: v["overall"] > -0.50 and (v["worst"] is None or v["worst"] > -0.85), "전환율이 일부만 떨어졌는가 (전부 끊긴 것이 아님)"),
            ],
            "landing_issue": [
                CheckDef("L1", "for", 3, "metric:cvr", lambda v: v["pct"] <= -0.15, "전환율이 기대보다 15% 넘게 낮은가", critical=True),
                CheckDef("L2", "for", 2, "metric:clicks", near("clicks", 0.12), "클릭수는 평소와 비슷한가"),
                CheckDef("L3", "for", 1, "metric:cpc", near("cpc", 0.12), "클릭당 비용은 평소와 비슷한가"),
                CheckDef("L4", "for", 2, "mix", lambda v: v["mix_share"] <= 0.5, "전환율 하락이 노출 비중 변화(구성)만으로 설명되지 않는가", critical=True),
                CheckDef("L5", "for", 2, "metric:cvr", lambda v: v["pct"] > -0.70, "전환율이 일부만 떨어졌는가 (거의 0은 아닌가)"),
                CheckDef("L6", "for", 2, "concentration", lambda v: not v["concentrated"], "전환 감소가 한 기기에만 몰리지 않았는가", critical="eval"),
                CheckDef("K1", "against", 2, "mix", lambda v: v["mix_share"] >= 0.7, "하락 대부분이 구성 변화로 설명되는가"),
                CheckDef("K2", "against", 1, "metric:clicks", lambda v: v["pct"] <= -0.30, "클릭이 크게 줄었는가"),
            ],
            "creative_fatigue": [
                CheckDef("C1", "for", 3, "creative", lambda v: v["worst"]["ctr_pct"] <= -0.15, "특정 소재의 클릭률이 15% 넘게 떨어졌는가", critical=True),
                CheckDef("C2", "for", 3, "creative", lambda v: v["others_median"] >= -0.08, "다른 소재의 클릭률은 거의 그대로인가 (그 소재만의 문제인가)", critical=True),
                CheckDef("C3", "for", 2, "shape:creative", lambda v: v["gradual"], "하락이 서서히 진행됐는가"),
                CheckDef("C4", "for", 1, "creative", lambda v: v["worst"]["cvr_pct"] is not None and abs(v["worst"]["cvr_pct"]) <= 0.25, "그 소재를 누른 사람의 전환율은 그대로인가"),
                CheckDef("C5", "for", 1, "creative", lambda v: v["worst"]["imp_pct"] >= -0.30, "그 소재는 여전히 노출되고 있는가"),
                CheckDef("K1", "against", 2, "creative", lambda v: v["n"] >= 2 and v["n_down"] / v["n"] >= 0.7, "클릭률 하락이 대부분의 소재에 퍼졌는가"),
                CheckDef("K2", "against", 2, "shape:creative", lambda v: v["sudden"], "하락이 단번에 일어났는가"),
            ],
            "bid_competition": [
                CheckDef("B1", "for", 3, "metric:cpc", lambda v: v["pct"] >= 0.12, "클릭당 비용이 12% 넘게 올랐는가", critical=True),
                CheckDef("B2", "for", 3, "metric:impressions", lambda v: v["pct"] <= -0.10, "노출수가 10% 넘게 줄었는가", critical=True),
                CheckDef("B3", "for", 2, "metric:ctr", near("ctr", 0.10), "클릭률은 평소와 비슷한가 (소재 반응은 그대로인가)"),
                CheckDef("B4", "for", 1, "metric:cvr", near("cvr", 0.15), "전환율은 평소와 비슷한가"),
                CheckDef("K1", "against", 2, "metric:impressions", lambda v: v["pct"] >= 0.10, "노출수가 오히려 늘었는가"),
            ],
            "audience_saturation": [
                CheckDef("S1", "for", 3, "broad", lambda v: v["n"] >= 2 and v["share"] >= 0.7 and v["median"] <= -0.08, "클릭률 하락이 여러 소재·세트에 넓게 퍼졌는가", critical=True),
                CheckDef("S2", "for", 3, "freq", lambda v: v["pct"] >= 0.08, "같은 사람에게 보여 준 평균 횟수가 8% 넘게 늘었는가"),
                CheckDef("S3", "for", 2, "metric:impressions", lambda v: -0.25 <= v["pct"] <= 0.10, "노출수는 크게 줄지 않았는가"),
                CheckDef("S4", "for", 2, "shape:ctr", lambda v: v["gradual"], "하락이 서서히 진행됐는가"),
                CheckDef("S5", "for", 1, "metric:cvr", near("cvr", 0.15), "전환율은 평소와 비슷한가"),
                CheckDef("K1", "against", 3, "creative", lambda v: v["others_median"] >= -0.05 and v["worst"]["ctr_pct"] <= -0.15, "특정 소재만 떨어졌는가 (넓게 퍼진 것이 아님)"),
            ],
            "budget_cap": [
                CheckDef("U1", "for", 3, "plateau", lambda v: v["flat"], "하루 광고비가 거의 같은 값에 붙어 있는가", critical=True),
                CheckDef("U2", "for", 3, "metric:clicks", lambda v: v["pct"] <= -0.10, "클릭수가 10% 넘게 줄었는가", critical=True),
                CheckDef("U3", "for", 2, "metric:ctr", near("ctr", 0.10), "클릭률은 평소와 비슷한가"),
                CheckDef("U4", "for", 2, "hour", lambda v: v["early"], "하루 노출이 평소보다 4시간 넘게 일찍 끝났는가"),
                CheckDef("U5", "for", 1, "metric:cpa", near("cpa", 0.15), "전환당 비용은 평소와 비슷한가"),
                CheckDef("U6", "for", 1, "metric:cpc", near("cpc", 0.10), "클릭당 비용은 평소와 비슷한가"),
                CheckDef("K1", "against", 3, "metric:cpc", lambda v: v["pct"] >= 0.15, "클릭당 비용이 15% 넘게 올랐는가 (경쟁 쪽 신호)"),
                CheckDef("K2", "against", 1, "metric:cvr", lambda v: v["pct"] <= -0.15, "전환율이 크게 떨어졌는가"),
            ],
            "mix_shift": [
                # 배분 자체의 변화가 탐지됐을 때 전체 전환율이 8% 미만으로 움직여도
                # 구성 가설은 검사할 수 있다. 하락 크기는 지지 근거이며 필수 조건이 아니다.
                CheckDef("M1", "for", 2, "metric:cvr", lambda v: v["pct"] <= -0.08, "전체 전환율이 기대보다 8% 넘게 낮은가"),
                CheckDef("M2", "for", 4, "mix", lambda v: v["mix_share"] >= 0.6 and v["mix"] < 0 and v["total"] < 0,
                         "전환율 하락의 60% 넘게가 클릭 비중 변화(구성)와 맞는가", critical=True),
                CheckDef("M3", "for", 2, "mix", lambda v: v["max_group_dev"] <= max(0.15, 2.5 / math.sqrt(max(self.act_total.conv / max(v["n"], 1), 1.0))), "묶음 안의 전환율은 거의 그대로인가"),
                CheckDef("M4", "for", 1, "mix", lambda v: v["max_share_move"] >= 0.05, "어느 묶음의 클릭 비중이 5%p 넘게 움직였는가", critical=True),
                CheckDef("K1", "against", 3, "mix", lambda v: v["rate_share"] >= 0.6, "하락 대부분이 묶음 안 전환율 자체의 변화인가"),
            ],
        }

    def run_checks(self, cause: str, defs: list[CheckDef]) -> list[CheckResult]:
        results = []
        for cd in defs:
            o = self.obs(cd.obs)
            if not o.ok:
                results.append(CheckResult(cd.cid, cd.role, cd.weight, cd.critical, cd.label, None, "", o.missing))
                continue
            try:
                passed = bool(cd.test(o.v))
            except (KeyError, TypeError, ZeroDivisionError):
                results.append(CheckResult(cd.cid, cd.role, cd.weight, cd.critical, cd.label, None, o.ev, "근거 값을 계산하지 못했습니다"))
                continue
            results.append(CheckResult(cd.cid, cd.role, cd.weight, cd.critical, cd.label, passed, o.ev))
        return results

    def score(self, cause: str, results: list[CheckResult]) -> Hypothesis:
        """확신 = (충족한 가중치 / 확인된 가중치) × (확인된 가중치 / 전체 가중치) × (1 − 0.6 × 반대 근거 비율).
        꼭 필요한 근거를 확인하지 못했으면 0.44 이하, 어긋났으면 0.30 이하로 묶는다."""
        fors = [r for r in results if r.role == "for"]
        againsts = [r for r in results if r.role == "against"]
        w_all = sum(r.weight for r in fors)
        w_eval = sum(r.weight for r in fors if r.passed is not None)
        w_pass = sum(r.weight for r in fors if r.passed)
        support = w_pass / w_eval if w_eval > 0 else 0.0
        coverage = w_eval / w_all if w_all > 0 else 0.0
        a_all = sum(r.weight for r in againsts)
        a_hold = sum(r.weight for r in againsts if r.passed)
        contra = a_hold / a_all if a_all > 0 else 0.0
        raw = support * coverage * (1 - CONTRADICTION_WEIGHT * contra)
        conf, cap_reason = raw, ""
        if any(r.critical is True and r.passed is None for r in fors):
            conf, cap_reason = min(conf, CAP_MISSING_CRITICAL), "꼭 필요한 근거를 확인하지 못해 우선 가설로 올리지 않았습니다"
        if any(r.critical in (True, "eval") and r.passed is False for r in fors):
            conf, cap_reason = min(conf, CAP_FAILED_CRITICAL), "꼭 필요한 근거가 맞지 않아 후보에서 낮췄습니다"
        if any(r.critical == "assert" and not r.passed for r in fors) and conf > ASSERT_CONFIDENCE - 0.01:
            conf, cap_reason = ASSERT_CONFIDENCE - 0.01, "일치하는 근거가 충분하지 않아 우선 확인할 가설로 표시합니다"
        if conf >= raw - 1e-9:
            cap_reason = ""
        h = Hypothesis(cause=cause, label=CAUSE_LABEL[cause], confidence=round(conf, 4), state=state_of(conf), checks=results,
                       support_ratio=support, coverage=coverage, contradiction_ratio=contra, raw_confidence=raw, cap_reason=cap_reason,
                       verification=CAUSE_VERIFICATION[cause])
        h.evidence = [r.evidence_id for r in fors if r.passed and r.evidence_id]
        h.contradicted_by = [r.evidence_id for r in fors if r.passed is False and r.evidence_id and r.weight >= 2] + [r.evidence_id for r in againsts if r.passed and r.evidence_id]
        seen: list[str] = []
        for r in results:
            if r.passed is None and r.missing and r.missing not in seen:
                seen.append(r.missing)
        h.data_missing = seen
        return h

    def diagnose(self) -> Diagnosis:
        inc = self.inc
        dg = Diagnosis(incident=inc, hypotheses=[], cause_state="undetermined")
        # 이상 구간 근거 (기간·지표 변화)
        span_text = f"이상 구간: {fmt_date(inc.start)}부터 {fmt_date(inc.end)}까지 {inc.days}일간" + (" (아직 이어지는 중)" if inc.ongoing else "")
        dg.incident_evidence = self.book.add("incident", span_text, [date_num("start", inc.start), date_num("end", inc.end), Num("days", inc.days, "일")], key=f"incident:{inc.index}")
        for m in sorted({m for m, _ in inc.findings}, key=METRICS.index):
            o = self.obs(f"metric:{m}")
            if o.ok:
                dg.primary_evidence.append(o.ev)
                dg.metric_evidence[m] = o.ev
        if inc.polarity == "better":
            dg.cause_state = "not_applicable"
            return dg
        defs = self.definitions()
        hyps = [self.score(c, self.run_checks(c, defs[c])) for c in CAUSES]
        # 배타적 후보: 한쪽이 확실히 앞서지 않으면 둘 다 단정하지 않는다
        by = {h.cause: h for h in hyps}
        for a, b in EXCLUSIVE_PAIRS:
            ha, hb = by[a], by[b]
            if min(ha.confidence, hb.confidence) >= LIKELY_CONFIDENCE:
                hi, lo = (ha, hb) if ha.confidence >= hb.confidence else (hb, ha)
                if hi.confidence - lo.confidence < RIVAL_MARGIN:
                    for h in (ha, hb):
                        other = hb if h is ha else ha
                        if h.confidence > ASSERT_CONFIDENCE - 0.01:
                            h.cap_reason = "맞는 근거가 비슷한 다른 가설이 있어 함께 확인해야 합니다"
                        h.confidence = min(h.confidence, ASSERT_CONFIDENCE - 0.01)
                        h.rival_note = f"'{other.label}' 후보와 근거가 비슷해 둘 중 하나로 가르지 못했습니다."
                else:
                    if lo.confidence > RIVAL_CAP:
                        lo.cap_reason = "더 잘 맞는 경쟁 후보가 있어 낮췄습니다"
                    lo.confidence = min(lo.confidence, RIVAL_CAP)
                    lo.rival_note = f"'{hi.label}' 후보가 더 잘 맞는 것으로 보여 이 후보는 낮춰 보았습니다."
        for h in hyps:
            h.state = state_of(h.confidence)
            h.confidence = round(h.confidence, 4)
        hyps.sort(key=lambda h: (-h.confidence, CAUSES.index(h.cause)))
        # 가설 요약 근거 (확신 숫자를 id 로 묶는다)
        for h in hyps:
            n_eval = sum(1 for r in h.checks if r.role == "for" and r.passed is not None)
            n_pass = sum(1 for r in h.checks if r.role == "for" and r.passed)
            if n_eval == 0:
                continue
            text = f"'{h.label}' 가설의 근거 일치 점수는 {h.confidence * 100:.1f}%입니다 (확인한 근거 {n_eval}개 중 {n_pass}개 충족)."
            h.evidence_id = self.book.add("hypothesis", text, [Num("confidence", round(h.confidence * 100, 1), "%"), Num("checked", n_eval, "개"), Num("passed", n_pass, "개")],
                                          cause=h.cause, key=f"hyp:{h.cause}:{inc.index}")
            if h.confidence >= CANDIDATE_CONFIDENCE:
                h.verification_evidence = self.book.add("verification", h.verification, cause=h.cause, key=f"verify:{h.cause}:{inc.index}")
        dg.hypotheses = hyps
        top = hyps[0]
        if top.confidence >= LIKELY_CONFIDENCE:
            dg.top_cause = top.cause
            dg.cause_state = "supported" if top.confidence >= ASSERT_CONFIDENCE else "likely"
        else:
            dg.cause_state = "undetermined"
            dg.top_cause = ""
            missing = sorted({m for h in hyps[:3] for m in h.data_missing})
            text = (f"가장 높은 가설('{top.label}')도 근거 일치 점수가 {top.confidence * 100:.1f}%로, 우선 확인할 기준({LIKELY_CONFIDENCE * 100:.0f}%)에 못 미칩니다."
                    + (" 확인하지 못한 자료가 있습니다." if missing else ""))
            dg.undetermined_evidence = self.book.add("undetermined", text, [Num("top_confidence", round(top.confidence * 100, 1), "%"),
                                                                           Num("threshold", round(LIKELY_CONFIDENCE * 100), "%")], key=f"undet:{inc.index}")
        for key in ("funnel",):
            o = self.obs(key)
            if o.ev:
                dg.funnel_evidence.append(o.ev)
        missing: list[str] = []
        for h in hyps[:3]:
            if h.confidence >= CANDIDATE_CONFIDENCE or dg.cause_state == "undetermined":
                for m in h.data_missing:
                    if m not in missing:
                        missing.append(m)
        if missing and dg.cause_state in ("undetermined", "likely"):
            dg.missing_evidence = self.book.add("missing", "확인하지 못한 것: " + " / ".join(missing[:3]), key=f"missing:{inc.index}")
        return dg


# ───────────────────────────── 액션 ─────────────────────────────
@dataclass(frozen=True)
class ActionSpec:
    id: str
    label: str
    kind: str                 # change(설정을 바꿈) · check(점검) · wait(기다림)
    summary: str
    precondition: str         # 선행 조건 (쉬운 말)
    effect: str               # 기대 효과의 방향
    watch: tuple              # 확인할 지표
    risk: str
    forbidden_when: str       # 금지 조건 (쉬운 말)


CATALOG: dict[str, ActionSpec] = {a.id: a for a in [
    ActionSpec("replace_creative", "소재 교체", "change", "반응이 떨어진 소재를 새 소재로 바꿉니다.", "특정 소재의 클릭률이 서서히 떨어진 것이 확인될 때", "클릭률 회복",
               ("ctr", "cvr"), "새 소재도 반응이 좋을지는 해 봐야 압니다.", "원인이 불명확하거나 전환 측정이 의심될 때"),
    ActionSpec("add_creative", "소재 추가", "change", "새 소재를 함께 돌려 노출을 나눕니다.", "소재 피로·도달 포화·입찰 경쟁 중 하나가 유력할 때", "클릭률 유지, 반복 노출 완화",
               ("ctr", "impressions"), "소재를 늘리면 학습이 느려질 수 있습니다.", "원인이 불명확하거나 전환 측정이 의심될 때"),
    ActionSpec("raise_budget", "예산 상향", "change", "하루 예산을 늘립니다.", "하루 예산이 일찍 소진되어 노출이 막힌 것이 확인될 때", "노출·전환 증가",
               ("impressions", "cpa", "cost"), "효율이 나쁜 상태에서 올리면 손실만 커집니다.", "전환 추적이 의심되거나 랜딩 문제·소재 피로·도달 포화가 유력하거나 표본이 적을 때"),
    ActionSpec("lower_budget", "예산 하향", "change", "하루 예산을 줄입니다.", "도달 포화로 전환당 비용이 나빠졌을 때", "낭비 감소",
               ("cost", "cpa"), "노출·전환이 함께 줄어듭니다.", "하루 예산이 이미 한도에 막혔거나 전환 추적이 의심되거나 표본이 적을 때"),
    ActionSpec("raise_bid", "입찰 상향", "change", "입찰가를 조금 올립니다.", "경쟁 때문에 노출이 줄고 클릭당 비용이 오른 것이 확인될 때", "노출 회복",
               ("impressions", "cpc", "cpa"), "클릭당 비용이 더 오를 수 있습니다.", "전환 추적이 의심되거나 랜딩 문제·소재 피로·도달 포화·구성 변화가 유력하거나 표본이 적을 때"),
    ActionSpec("lower_bid", "입찰 하향", "change", "입찰가를 낮춥니다.", "노출 손실 없이 클릭당 비용만 오른 경우", "클릭당 비용 감소",
               ("cpc", "impressions"), "노출이 줄 수 있습니다.", "입찰 경쟁이 유력하거나 전환 추적이 의심되거나 표본이 적을 때"),
    ActionSpec("check_landing", "랜딩 점검", "check", "랜딩 페이지가 잘 열리는지, 신청 단계가 막히지 않는지 직접 확인합니다.", "클릭은 그대로인데 전환율이 떨어졌을 때", "전환율 회복",
               ("cvr", "conversions"), "점검만 하므로 위험이 거의 없습니다.", "없음 (설정을 바꾸지 않는 점검입니다)"),
    ActionSpec("check_tracking", "전환 추적 점검", "check", "전환 이벤트가 정상으로 기록되는지 추적 설정을 확인합니다.", "클릭은 그대로인데 전환이 갑자기 거의 없어졌을 때", "전환 측정 복구",
               ("conversions", "cvr"), "점검만 하므로 위험이 거의 없습니다.", "없음 (설정을 바꾸지 않는 점검입니다)"),
    ActionSpec("expand_audience", "타겟 확장", "change", "겨냥하는 사람의 범위를 넓힙니다.", "같은 사람에게 반복 노출되어 반응이 넓게 떨어졌을 때", "새로운 도달 확보",
               ("impressions", "ctr", "cvr"), "관심이 낮은 사람이 섞여 전환율이 떨어질 수 있습니다.", "전환 추적이 의심되거나 랜딩 문제가 유력하거나 표본이 적을 때"),
    ActionSpec("pause", "일시 중지", "change", "광고를 잠시 멈춥니다.", "전환율 하락이 크고 페이지 점검에서 신청 오류가 확인된 경우에만 검토", "낭비 중단",
               ("cost", "conversions"), "멈추면 노출·학습이 끊깁니다.", "전환 추적이 의심되거나 원인이 불명확하거나 표본이 적을 때"),
    ActionSpec("keep_watching", "관찰 유지", "wait", "지금은 설정을 바꾸지 않고 며칠 더 지켜봅니다.", "눈에 띄는 이상이 없거나 요일 효과로 설명될 때", "불필요한 변경 방지",
               ("ctr", "cvr", "cpa"), "없음", "없음"),
    ActionSpec("collect_more_data", "데이터 더 모으기", "wait", "표본이 쌓일 때까지 기다린 뒤 다시 확인합니다.", "클릭·전환이 너무 적어 판단하기 어려울 때", "판단 정확도 향상",
               ("clicks", "conversions"), "기다리는 동안 문제가 있었다면 늦게 발견됩니다.", "없음"),
    ActionSpec("review_segment_mix", "구성 점검", "check", "기기·세트별로 나누어 보고 노출 비중이 쏠린 이유를 확인합니다.", "전체 전환율은 떨어졌지만 묶음별 전환율은 그대로일 때", "원인 파악",
               ("cvr",), "점검만 하므로 위험이 거의 없습니다.", "없음 (설정을 바꾸지 않는 점검입니다)"),
]}
CHANGE_IDS = tuple(a.id for a in CATALOG.values() if a.kind == "change")
MONEY_IDS = ("pause", "raise_budget", "lower_budget", "raise_bid", "lower_bid", "expand_audience")


@dataclass
class ActionState:
    """가드 규칙이 보는 상태 — 엔진이 진단한 결과만으로 만든다 (정답을 보지 않는다)."""

    has_incident: bool = False
    sample_insufficient: bool = False
    sample_partial: bool = False
    undetermined: bool = False
    conf: dict = field(default_factory=dict)       # cause → 가장 높은 확신
    severe_landing: bool = False
    cpc_inflation_only: bool = False
    cpa_up: bool = False

    def suspect(self, cause: str, floor: float = SUSPECT_CONFIDENCE) -> bool:
        return self.conf.get(cause, 0.0) >= floor

    def likely(self, cause: str) -> bool:
        return self.conf.get(cause, 0.0) >= LIKELY_CONFIDENCE

    def supported(self, cause: str) -> bool:
        return self.conf.get(cause, 0.0) >= ASSERT_CONFIDENCE


@dataclass(frozen=True)
class ActionVerdict:
    action_id: str
    allowed: bool
    rule: str = ""
    reason: str = ""
    hard: bool = False      # 가드 규칙(금지) 때문에 막혔는가. False 면 선행 조건이 없어서 제안하지 않은 것


def _forbid(rule: str, reason: str, ids: Iterable[str], action_id: str) -> Optional[ActionVerdict]:
    return ActionVerdict(action_id, False, rule, reason, True) if action_id in set(ids) else None


def evaluate_action(action_id: str, st: ActionState) -> ActionVerdict:
    """액션 하나가 지금 상태에서 허용되는가. 금지 규칙(가드) 먼저, 그다음 선행 조건. 모델이 제안했든 엔진이 골랐든 같은 규칙을 지난다."""
    if action_id not in CATALOG:
        return ActionVerdict(action_id, False, "G0", "카탈로그에 없는 액션입니다.", True)
    spec = CATALOG[action_id]
    # --- 금지 규칙 (코드로 강제) ---
    if st.sample_insufficient and action_id not in ("collect_more_data", "keep_watching"):
        return ActionVerdict(action_id, False, "G1", "표본이 너무 적어 판단을 보류하는 중이라 데이터를 더 모으며 지켜보는 것 말고는 하지 않습니다.", True)
    if st.sample_partial and action_id in MONEY_IDS:
        return ActionVerdict(action_id, False, "G2", "전환 표본이 적어 예산·입찰·타겟·중지처럼 큰 변경은 하지 않습니다.", True)
    if not st.has_incident and spec.kind == "change":
        return ActionVerdict(action_id, False, "G3", "눈에 띄는 이상이 없거나 요일 효과로 설명되는 변화라 아무것도 바꾸지 않습니다.", True)
    if st.suspect("tracking_break", TRACKING_SUSPECT_CONFIDENCE) and spec.kind == "change":
        return ActionVerdict(action_id, False, "G4", "전환 추적이 끊겼을 가능성이 있어, 성과가 정확히 측정되기 전에는 예산·입찰·타겟·소재를 바꾸지 않습니다.", True)
    if st.undetermined and spec.kind == "change":
        return ActionVerdict(action_id, False, "G5", "원인을 정하기 어려운 상태라 설정을 바꾸지 않고 점검·관찰만 합니다.", True)
    v = (_forbid("G6", "랜딩 페이지에 문제가 있을 가능성이 있는데 방문을 더 늘리면 낭비가 커집니다.", ("raise_budget", "raise_bid", "expand_audience"), action_id) if st.suspect("landing_issue") else None) \
        or (_forbid("G7", "소재 피로나 도달 포화가 의심되는 상태에서 돈을 더 쓰면 같은 사람에게 반복 노출만 늘어납니다.", ("raise_budget", "raise_bid", "pause"), action_id)
            if (st.suspect("creative_fatigue") or st.suspect("audience_saturation")) else None) \
        or (_forbid("G8", "입찰 경쟁이 의심되는 상태에서 입찰을 낮추거나 멈추면 노출을 더 잃습니다.", ("lower_bid", "pause"), action_id) if st.suspect("bid_competition") else None) \
        or (_forbid("G9", "하루 예산이 막힌 것이 의심되는 상태에서 예산을 줄이거나 멈추면 노출을 더 잃습니다.", ("lower_budget", "pause"), action_id) if st.suspect("budget_cap") else None) \
        or (_forbid("G10", "전체 하락이 노출 비중 변화로 보이는 상태에서 멈추거나 예산·입찰을 바꾸면 멀쩡한 쪽까지 해칩니다.", ("pause", "lower_budget", "raise_bid"), action_id)
            if st.suspect("mix_shift") else None)
    if v is not None:
        return v
    # --- 선행 조건 ---
    pre = {
        "replace_creative": st.likely("creative_fatigue"),
        "add_creative": st.likely("creative_fatigue") or st.likely("audience_saturation") or st.likely("bid_competition"),
        "raise_budget": st.likely("budget_cap"),
        "lower_budget": st.likely("audience_saturation") and st.cpa_up,
        "raise_bid": st.likely("bid_competition"),
        "lower_bid": st.cpc_inflation_only and not st.suspect("bid_competition"),
        "check_landing": st.has_incident,
        "check_tracking": st.has_incident,
        "expand_audience": st.likely("audience_saturation"),
        "pause": st.supported("landing_issue") and st.severe_landing,
        "keep_watching": True,
        "collect_more_data": st.sample_insufficient or st.sample_partial,
        "review_segment_mix": st.likely("mix_shift"),
    }[action_id]
    if not pre:
        return ActionVerdict(action_id, False, "P", f"선행 조건이 맞지 않습니다: {spec.precondition}.", False)
    return ActionVerdict(action_id, True)


CAUSE_ACTIONS_ORDER: dict[str, tuple] = {
    "tracking_break": ("check_tracking",),
    "landing_issue": ("check_landing", "pause"),
    "creative_fatigue": ("replace_creative", "add_creative"),
    "audience_saturation": ("expand_audience", "add_creative", "lower_budget"),
    "bid_competition": ("raise_bid", "add_creative"),
    "budget_cap": ("raise_budget",),
    "mix_shift": ("review_segment_mix",),
}


@dataclass
class ProposedAction:
    action_id: str
    label: str
    cause: str                 # 이 액션을 고른 이유가 된 원인 (없으면 "")
    cause_state: str           # supported · likely · ""
    evidence_ids: list
    watch: tuple
    risk: str
    incident: int = -1

    def to_dict(self) -> dict:
        return {"action_id": self.action_id, "label": self.label, "cause": self.cause, "cause_state": self.cause_state, "evidence_ids": list(self.evidence_ids),
                "watch": list(self.watch), "risk": self.risk, "incident": self.incident}


@dataclass
class Avoid:
    action_ids: list
    rule: str
    reason: str
    evidence_ids: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"action_ids": list(self.action_ids), "rule": self.rule, "reason": self.reason, "evidence_ids": list(self.evidence_ids)}


# ───────────────────────────── 분석 결과 ─────────────────────────────
@dataclass
class Analysis:
    status: str                       # ok · partial · insufficient · insufficient_history
    window: tuple                     # (시작, 끝)
    baseline: tuple
    sample: Optional[SampleStatus]
    detections: dict                  # 지표 → MetricDetection
    incidents: list                   # Incident
    diagnoses: list                   # Diagnosis
    actions: list                     # ProposedAction
    avoid: list                       # Avoid
    state: ActionState
    book: EvidenceBook
    notes: list = field(default_factory=list)     # (근거 id) — 요일 효과·표본 안내 등
    names: list = field(default_factory=list)     # 데이터에 나온 캠페인·세트·소재·기기 이름 (문장 속 숫자 검사에서 가린다)
    has_dims: dict = field(default_factory=dict)
    status_evidence: str = ""
    overview_evidence: str = ""
    n_rows: int = 0
    series: dict = field(default_factory=dict)
    window_stats: dict = field(default_factory=dict)   # 지표 → {"actual", "expected", "pct"} (살펴본 기간 전체, 같은 요일 기준)
    reference_windows: int = 1
    reference_evidence: str = ""

    @property
    def evidence(self) -> dict:
        return self.book.items

    def ranked_causes(self) -> list[tuple[str, float]]:
        best: dict[str, float] = {}
        for dg in self.diagnoses:
            for h in dg.hypotheses:
                best[h.cause] = max(best.get(h.cause, 0.0), h.confidence)
        return sorted(best.items(), key=lambda kv: (-kv[1], CAUSES.index(kv[0])))

    def cause_conf(self) -> dict:
        return dict(self.ranked_causes())

    def to_context(self) -> dict:
        """모델·대역·화면이 받는 JSON 형태 요약. 모델은 이 안의 근거 id 와 숫자만 인용할 수 있다."""
        return {
            "status": self.status, "window": [d.isoformat() for d in self.window], "baseline": [d.isoformat() for d in self.baseline],
            "reference_windows": self.reference_windows, "reference_evidence": self.reference_evidence,
            "evidence": [e.to_dict() for e in self.book.items.values()],
            "incidents": [{
                "index": dg.incident.index, "start": dg.incident.start.isoformat(), "end": dg.incident.end.isoformat(), "days": dg.incident.days,
                "severity": dg.incident.severity, "polarity": dg.incident.polarity, "ongoing": dg.incident.ongoing, "cause_state": dg.cause_state,
                "top_cause": dg.top_cause, "incident_evidence": dg.incident_evidence, "primary_evidence": list(dg.primary_evidence),
                "metric_evidence": dict(dg.metric_evidence), "missing_evidence": dg.missing_evidence,
                "funnel_evidence": list(dg.funnel_evidence), "undetermined_evidence": dg.undetermined_evidence,
                "metrics": [{"metric": m, "label": METRIC_LABEL[m], "pct": round(s.pct, 4), "direction": s.direction, "score": round(s.score, 2), "segment": s.segment}
                            for m, s in dg.incident.findings],
                "hypotheses": [h.to_dict() for h in dg.hypotheses],
            } for dg in self.diagnoses],
            "actions": [a.to_dict() for a in self.actions],
            "avoid": [a.to_dict() for a in self.avoid],
            "notes": list(self.notes), "status_evidence": self.status_evidence, "overview_evidence": self.overview_evidence,
            "cause_confidence": {k: round(v, 4) for k, v in self.cause_conf().items()},
            "names": list(self.names),
            "thresholds": {"supported": SUPPORTED_CONFIDENCE, "likely": LIKELY_CONFIDENCE, "candidate": CANDIDATE_CONFIDENCE},
            "catalog": {k: {"label": v.label, "kind": v.kind} for k, v in CATALOG.items()},
        }


def _window_stats(table: Table, base_dates: list[dt.date], win_dates: list[dt.date], detections: dict) -> dict:
    """살펴본 기간의 실제값과 요일·추세를 반영한 기대값. 비율은 같은 예측 합계에서 계산한다."""
    base = [d for d in base_dates if d in table.present]
    days = [d for d in win_dates if d in table.present]
    if not base or not days:
        return {}
    by_wd: dict[int, list[dt.date]] = defaultdict(list)
    for d in base:
        by_wd[d.weekday()].append(d)
    act = csum(table.total[d] for d in days)
    exp = csum(_forecast_counts(cmean([table.total.get(x, _ZERO) for x in (by_wd.get(d.weekday()) or base)]), d, detections) for d in days)
    out = {}
    for m in METRICS:
        det = detections.get(m)
        if det is None or det.status != "ok":
            continue
        a, e = metric_value(m, act, smooth=False), metric_value(m, exp, smooth=False)
        if a is not None and e:
            out[m] = {"actual": a, "expected": e, "pct": a / e - 1}
    return out


def _weekday_note(table: Table, base: list[dt.date], win: list[dt.date], book: EvidenceBook) -> str:
    """주말에 평소에도 클릭이 줄어드는 패턴이면 알린다 — 이상으로 잡지 않은 이유를 보여 주는 근거."""
    def ratio(days: list[dt.date]) -> Optional[tuple[float, int, int]]:
        we = [table.total[d].clk for d in days if d in table.present and d.weekday() >= 5]
        wk = [table.total[d].clk for d in days if d in table.present and d.weekday() < 5]
        if len(we) < 2 or len(wk) < 3 or sum(wk) <= 0:
            return None
        return (sum(we) / len(we)) / (sum(wk) / len(wk)), len(we), len(wk)
    rb, rw = ratio(base), ratio(win)
    if rb is None or rw is None or rb[0] > 0.92 or rw[0] > 0.92:
        return ""
    text = (f"주말(토·일)에는 기준 기간에도 평일보다 클릭이 평균 {(1 - rb[0]) * 100:.1f}% 적었고, 이번 기간에도 {abs(1 - rw[0]) * 100:.1f}% "
            f"{'적어' if rw[0] < 1 else '많아'} 같은 요일 패턴입니다.")
    return book.add("note", text, [Num("base_gap", round((1 - rb[0]) * 100, 1), "%"), Num("win_gap", round(abs(1 - rw[0]) * 100, 1), "%")], direction="down", key="weekday")


def _reference_candidates(table: Table, base_dates: list[dt.date]) -> list[list[dt.date]]:
    """기준 기간의 앞부분들. 모든 후보에 14일 이상, 각 요일의 관측이 두 번 이상 필요하다.

    최근 표시 기간은 바꾸지 않는다. 기준 기간 중간부터 이어진 변화가 중앙값·잡음·추세에
    흡수되는 경우를 점검한다. 정답 사건이나 시나리오 정보는 받지 않는다.
    """
    out = []
    counts: dict[int, int] = defaultdict(int)
    for i, day in enumerate(base_dates[:-1]):
        if day not in table.present:
            continue
        counts[day.weekday()] += 1
        if sum(counts.values()) >= MIN_BASELINE_DAYS and all(counts[w] >= 2 for w in range(7)):
            out.append(base_dates[:i + 1])
    return out + [base_dates]


def _detect_against_reference(table: Table, base_dates: list[dt.date], win_dates: list[dt.date], comparisons: int) -> tuple:
    sample = sample_status(table, base_dates)
    detections = {m: detect_metric(m, table.total, table.present, base_dates, win_dates, sample, comparisons=comparisons) for m in METRICS}
    findings = [(m, s) for m, d in detections.items() if d.status == "ok" for s in d.spans]
    if sample.level != "insufficient":
        findings += _segment_findings(table, base_dates, win_dates, comparisons=comparisons)
    return sample, detections, findings


def _select_reference(table: Table, base_dates: list[dt.date], win_dates: list[dt.date]) -> tuple:
    candidates = _reference_candidates(table, base_dates)
    series_count = len(METRICS) + sum(len(metrics) * len([v for v in table.dims[dim] if v != NO_DIM])
                                    for dim, metrics in SEGMENT_METRICS.items() if table.has_dim(dim))
    # 직전 기준선 검사와 이전 기준선 탐색에 같은 꼬리 상계 예산을 나눈다.
    # 직전 기준선은 1/2, 이전 후보 K개는 각각 1/(2K)다. 어느 쪽에서 일찍 멈추든
    # 합계는 1을 넘지 않는다. 보통의 최근 변화가 추가 후보 수에 비례해 약해지지 않게 한다.
    earlier_count = len(candidates) - 1
    selected = _detect_against_reference(table, base_dates, win_dates, series_count * (2 if earlier_count else 1))
    if selected[2] or selected[0].level == "insufficient":
        return base_dates, *selected, len(candidates)
    best_score = 0.0
    selected_dates = base_dates
    comparisons = series_count * 2 * earlier_count
    for dates in reversed(candidates[:-1]):
        result = _detect_against_reference(table, dates, win_dates, comparisons)
        score = max((s.score / math.sqrt((SEG_H_SCORE if s.segment else H_SCORE) ** 2 + 2 * math.log(comparisons))
                     for _, s in result[2]), default=0.0)
        if score > best_score:
            selected_dates, selected, best_score = dates, result, score
    return selected_dates, *selected, len(candidates)


def choose_window(dates: list[dt.date], period: Optional[tuple], recent_days: int, baseline_days: int) -> tuple:
    last = max(dates)
    if period is not None:
        ws, we = period
        if ws > we:
            ws, we = we, ws
        we = min(we, last)
    else:
        we = last
        ws = we - dt.timedelta(days=recent_days - 1)
    bs, be = ws - dt.timedelta(days=baseline_days), ws - dt.timedelta(days=1)
    return (bs, be), (ws, we)


def analyze(rows: list[MetricRow], period: Optional[tuple] = None, *, recent_days: int = RECENT_DAYS, baseline_days: int = BASELINE_DAYS) -> Analysis:
    """성과 행 → 분석. 같은 입력이면 같은 결과다."""
    book = EvidenceBook()
    if not rows:
        none_day = dt.date(1970, 1, 1)           # 입력이 비었을 때도 같은 입력이면 같은 출력이 되도록 오늘 날짜를 쓰지 않는다
        empty = Analysis("insufficient_history", (none_day, none_day), (none_day, none_day), None, {}, [], [], [], [], ActionState(sample_insufficient=True), book,
                         status_evidence=book.add("sample", "분석할 데이터가 없습니다."))
        _finalize_actions(empty)
        return empty
    all_dates = sorted({r.date for r in rows})
    bwin, win = choose_window(all_dates, period, recent_days, baseline_days)
    table = build_table(rows, bwin[0], win[1])
    base_dates = [d for d in table.dates if bwin[0] <= d <= bwin[1]]
    win_dates = [d for d in table.dates if win[0] <= d <= win[1]]
    names = sorted({getattr(r, dim) for r in rows for dim in ("campaign", "ad_set", "creative", "device")} - {NO_DIM}, key=lambda s: (-len(s), s))
    has_dims = {dim: table.has_dim(dim) for dim in ("ad_set", "creative", "device")}
    n_base_days = sum(1 for d in base_dates if d in table.present)
    n_win_days = sum(1 for d in win_dates if d in table.present)
    state = ActionState()
    if n_base_days < MIN_BASELINE_DAYS or n_win_days < MIN_WINDOW_DAYS:
        ev = book.add("sample", f"기준 기간 자료가 {n_base_days}일, 살펴볼 기간 자료가 {n_win_days}일뿐이라 비교할 수 없습니다 (기준 {MIN_BASELINE_DAYS}일 이상, 살펴볼 기간 {MIN_WINDOW_DAYS}일 이상 필요).",
                      [Num("base_days", n_base_days, "일"), Num("win_days", n_win_days, "일"), Num("need_base", MIN_BASELINE_DAYS, "일"), Num("need_win", MIN_WINDOW_DAYS, "일")], key="history")
        state.sample_insufficient = True
        a = Analysis("insufficient_history", win, bwin, None, {}, [], [], [], [], state, book, names=names, has_dims=has_dims, status_evidence=ev, n_rows=len(rows))
        _finalize_actions(a)
        return a
    original_base_end = bwin[1]
    base_dates, sample, detections, findings, reference_windows = _select_reference(table, base_dates, win_dates)
    bwin = (base_dates[0], base_dates[-1])
    n_base_days = sample.base_days
    a = Analysis(sample.level, win, bwin, sample, detections, [], [], [], [], state, book, names=names, has_dims=has_dims, n_rows=len(rows),
                 reference_windows=reference_windows)
    if bwin[1] < original_base_end:
        a.reference_evidence = book.add(
            "reference", f"이전 기준 기간({fmt_date(bwin[0])}부터 {fmt_date(bwin[1])}까지 {n_base_days}일)과 최근 기간을 비교했습니다. "
            f"가능한 기준 기간 {reference_windows}개를 함께 점검하므로 탐지 기준을 강화했습니다. 표시한 변화 구간은 최근 기간 안의 관측이며 실제 시작일을 확정한 것은 아닙니다.",
            [date_num("base_start", bwin[0]), date_num("base_end", bwin[1]), Num("base_days", n_base_days, "일"),
             Num("reference_windows", reference_windows, "개")], key="reference")
    if sample.level != "ok":
        txt = (f"기준 기간({n_base_days}일) 합계 클릭 {sample.base_clicks:,}회, 전환 {sample.base_conv:,}건입니다. "
               + ("클릭이 너무 적어 모든 지표의 판단을 보류합니다." if sample.level == "insufficient" else "전환이 너무 적어 전환 관련 지표(전환율·전환당 비용·전환수)는 판단을 보류합니다.")
               + f" (최소 클릭 {MIN_BASE_CLICKS}회, 전환 {MIN_BASE_CONV}건 필요)")
        a.status_evidence = book.add("sample", txt, [Num("base_days", n_base_days, "일"), Num("base_clicks", sample.base_clicks, "회"), Num("base_conv", sample.base_conv, "건"),
                                                      Num("need_clicks", MIN_BASE_CLICKS, "회"), Num("need_conv", MIN_BASE_CONV, "건")], key="sample")
        state.sample_insufficient = sample.level == "insufficient"
        state.sample_partial = sample.level == "partial"
    a.incidents = _cluster(findings, win[1]) if sample.level != "insufficient" else []
    for inc in a.incidents:
        dg = Diagnoser(table, base_dates, inc, detections, sample, book,
                       onset_left_censored=bool(a.reference_evidence) and inc.start == win[0]).diagnose()
        a.diagnoses.append(dg)
    a.notes = []
    if not a.incidents:
        w = _weekday_note(table, [d for d in base_dates], win_dates, book)
        if w:
            a.notes.append(w)
    state.has_incident = bool(a.incidents) and any(i.polarity == "worse" for i in a.incidents)
    cc = a.cause_conf()
    state.conf = cc
    state.undetermined = any(dg.cause_state == "undetermined" for dg in a.diagnoses)
    _fill_state_from_evidence(a)
    a.series = _series(table, detections, base_dates, win_dates)
    a.window_stats = _window_stats(table, base_dates, win_dates, detections)
    n_ok = sum(1 for d in detections.values() if d.status == "ok")
    a.overview_evidence = book.add(
        "overview", f"살펴본 기간은 {fmt_date(win[0])}부터 {fmt_date(win[1])}까지 {len(win_dates)}일이고, 점검한 지표 {n_ok}개에서 이상 구간 {len(a.incidents)}개를 찾았습니다 (기준 기간 {n_base_days}일).",
        [date_num("start", win[0]), date_num("end", win[1]), Num("days", len(win_dates), "일"), Num("metrics", n_ok, "개"), Num("incidents", len(a.incidents), "개"),
         Num("base_days", n_base_days, "일")], key="overview")
    _finalize_actions(a)
    return a


SEG_H_SCORE = 6.0
SEG_MIN_EFFECT = 0.15
SEG_MIN_LEN = 2
SEGMENT_METRICS = {"creative": ("ctr",), "ad_set": ("ctr", "cvr"), "device": ("clicks", "cvr")}
DIM_NAME = {"creative": "소재", "ad_set": "광고세트", "device": "기기"}


def _segment_findings(table: Table, base_dates: list[dt.date], win_dates: list[dt.date], *, comparisons: int = 1) -> list:
    """전체에 묻힌 소재 반응·기기 전환율·기기별 클릭 배분 변화를 본다. 늘어난 비교 수는 탐지 기준에 반영한다."""
    out = []
    for dim, metrics in SEGMENT_METRICS.items():
        if not table.has_dim(dim):
            continue
        for value, series in sorted(table.dims[dim].items()):
            if value == NO_DIM:
                continue
            ss = sample_status(table, base_dates, series, min_clicks=GROUP_MIN_CLICKS, min_conv=GROUP_MIN_CONV, per_day_clicks=5, per_day_conv=1)
            for m in metrics:
                det = detect_metric(m, series, table.present, base_dates, win_dates, ss, h_score=SEG_H_SCORE, h_single=99.0, min_effect=SEG_MIN_EFFECT, min_len=SEG_MIN_LEN,
                                    comparisons=comparisons)
                if det.status != "ok":
                    continue
                for sp in det.spans:
                    sp.segment = f"{DIM_NAME[dim]} {value}"
                    out.append((m, sp))
    return out


def _fill_state_from_evidence(a: Analysis) -> None:
    st = a.state
    for dg in a.diagnoses:
        for m, s in dg.incident.findings:
            if m == "cvr" and s.pct <= -0.5:
                st.severe_landing = True
            if m == "cpa" and s.direction == "up":
                st.cpa_up = True
        hs = {m for m, _ in dg.incident.findings}
        if "cpc" in hs and "impressions" not in hs and not any(h.cause == "bid_competition" and h.confidence >= SUSPECT_CONFIDENCE for h in dg.hypotheses):
            if any(m == "cpc" and s.direction == "up" for m, s in dg.incident.findings):
                st.cpc_inflation_only = True


def _finalize_actions(a: Analysis) -> None:
    st = a.state
    proposals: list[ProposedAction] = []
    avoid: dict[str, Avoid] = {}
    seen: set = set()

    def consider(aid: str, cause: str, cstate: str, ev: list, inc_index: int) -> None:
        if aid in seen:
            return
        v = evaluate_action(aid, st)
        if v.allowed:
            spec = CATALOG[aid]
            seen.add(aid)
            proposals.append(ProposedAction(aid, spec.label, cause, cstate, [e for e in ev if e], spec.watch, spec.risk, inc_index))
        elif v.hard:
            av = avoid.setdefault(v.rule, Avoid([], v.rule, v.reason, []))
            if aid not in av.action_ids:
                av.action_ids.append(aid)
            for e in ev:
                if e and e not in av.evidence_ids:
                    av.evidence_ids.append(e)

    if a.status in ("insufficient", "insufficient_history"):
        consider("collect_more_data", "", "", [a.status_evidence], -1)
        consider("keep_watching", "", "", [a.status_evidence], -1)
        # 이 상태에서 하면 안 되는 것도 보여 준다
        for aid in ("pause", "raise_budget", "raise_bid"):
            consider(aid, "", "", [a.status_evidence], -1)
    elif not a.diagnoses:
        ev = list(a.notes) or ([a.status_evidence] if a.status_evidence else [])
        consider("keep_watching", "", "", ev, -1)
        if a.status == "partial":
            consider("collect_more_data", "", "", ev, -1)
        for aid in ("raise_budget", "pause", "replace_creative"):
            consider(aid, "", "", ev, -1)
    else:
        for dg in a.diagnoses:
            inc = dg.incident
            if inc.polarity == "better":
                consider("keep_watching", "", "", [dg.incident_evidence] + dg.primary_evidence[:2], inc.index)
                continue
            if dg.cause_state in ("supported", "likely"):
                for h in dg.hypotheses:
                    if h.confidence < LIKELY_CONFIDENCE:
                        continue
                    ev = [h.evidence_id, h.verification_evidence] + h.evidence[:2]
                    for aid in CAUSE_ACTIONS_ORDER[h.cause]:
                        consider(aid, h.cause, h.state, ev, inc.index)
            else:
                ev = [dg.undetermined_evidence, dg.incident_evidence]
                metrics = {m for m, _ in inc.findings}
                if metrics & {"conversions", "cvr", "cpa"}:
                    consider("check_tracking", "", "", ev, inc.index)
                if metrics & {"cvr", "cpa"}:
                    consider("check_landing", "", "", ev, inc.index)
                consider("collect_more_data", "", "", ev, inc.index)
                consider("keep_watching", "", "", ev, inc.index)
            # 하면 안 되는 것도 근거와 함께 보여 준다
            for aid in MONEY_IDS + ("replace_creative", "add_creative"):
                consider(aid, "", "", [dg.incident_evidence], inc.index)
    # 점검 → 변경 → 관찰 순으로 정렬하고 4개까지만
    if not proposals:
        # 주된 가설의 변경안이 다른 의심 신호 때문에 전부 막혀도 다음 확인 절차를 남긴다.
        ev = [dg.incident_evidence for dg in a.diagnoses] or [a.overview_evidence or a.status_evidence]
        if st.has_incident and st.suspect("tracking_break", TRACKING_SUSPECT_CONFIDENCE):
            consider("check_tracking", "", "", ev, -1)
        if st.has_incident and st.suspect("landing_issue"):
            consider("check_landing", "", "", ev, -1)
        if st.sample_partial:
            consider("collect_more_data", "", "", ev, -1)
        consider("keep_watching", "", "", ev, -1)
    order = {"check": 0, "change": 1, "wait": 2}
    proposals = [p for _, p in sorted(enumerate(proposals), key=lambda ip: (order[CATALOG[ip[1].action_id].kind], ip[0]))]
    a.actions = proposals[:4]
    a.avoid = list(avoid.values())


def _series(table: Table, det: dict, base_dates: list[dt.date], win_dates: list[dt.date]) -> dict:
    out = {}
    for m, d in det.items():
        if d.status != "ok":
            out[m] = []
            continue
        pts = []
        for day in base_dates + win_dates:
            if day not in table.present:
                continue
            act = metric_value(m, table.total[day], smooth=False)
            exp = d.expected.get(day)
            if exp is None:
                continue
            band = math.exp(2.5 * d.sigma)
            pts.append({"date": day.isoformat(), "actual": act, "expected": exp, "lo": exp / band, "hi": exp * band, "z": d.z.get(day), "window": day in set(win_dates)})
        out[m] = pts
    return out


# ───────────────────────────── 결정론 템플릿 문장 ─────────────────────────────
# 보고서는 모델 없이도 항상 나온다. 이 문장들은 근거 레코드의 문장과 숫자를 그대로 쓰거나, 숫자가 없는 고정 문장이다.
ACTION_INSTRUCTION = {
    "replace_creative": "반응이 떨어진 소재를 새 소재로 바꿔 보세요.",
    "add_creative": "새 소재를 함께 추가해 노출을 나눠 보세요.",
    "raise_budget": "전환당 비용이 유지되는 범위에서 하루 예산을 올려 보세요.",
    "lower_budget": "효율이 나빠진 세트의 하루 예산을 줄여 보세요.",
    "raise_bid": "전환당 비용이 허용되는 범위에서 입찰가를 조금 올려 보세요.",
    "lower_bid": "노출 손실이 없는지 보면서 입찰가를 조금 낮춰 보세요.",
    "check_landing": "랜딩 페이지가 정상으로 열리고 신청 단계가 막히지 않는지 점검하세요.",
    "check_tracking": "전환 이벤트가 정상으로 들어오는지 추적 설정을 먼저 점검하세요.",
    "expand_audience": "타겟 범위를 넓혀 새로운 사람에게 노출해 보세요.",
    "pause": "고쳐질 때까지 해당 광고를 잠시 멈추는 것을 검토하세요.",
    "keep_watching": "설정을 바꾸지 말고 며칠 더 지켜보세요.",
    "collect_more_data": "표본이 쌓일 때까지 설정을 바꾸지 말고 기다린 뒤 다시 확인하세요.",
    "review_segment_mix": "기기·세트별로 나누어 보고 노출 비중이 쏠린 이유를 확인하세요.",
}
CAUSE_REASON = {cause: f"{label} 가설과 맞는 신호가 있어 확인이 필요합니다." for cause, label in CAUSE_LABEL.items()}
GENERIC_WHY = {
    "check_tracking": "원인을 정하기 어렵습니다. 설정을 바꾸기 전에 전환 측정부터 확인하는 것이 안전합니다.",
    "check_landing": "원인을 정하기 어렵습니다. 전환율이 떨어졌으니 도착 페이지도 함께 확인하는 것이 안전합니다.",
    "collect_more_data": "원인을 정하기에는 자료가 부족합니다.",
    "keep_watching": "지금은 바꿀 근거가 부족합니다.",
}
_HEAD_PRIORITY = ("cpa", "cvr", "conversions", "ctr", "clicks", "impressions", "cpc", "cost")


def _sent(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?", ")")) and not text.endswith("(아직 이어지는 중)") else text + "."


def _period(a: str, b: str) -> str:
    return f"{fmt_date(dt.date.fromisoformat(a))}부터 {fmt_date(dt.date.fromisoformat(b))}까지"


def _ev_change(ev: dict) -> tuple[float, str]:
    """근거에서 '변화율' 숫자와 방향을 읽는다."""
    for n in ev["numbers"]:
        if n["key"] == "change":
            return abs(n["value"]), ev.get("direction", "")
    return 0.0, ev.get("direction", "")


def template_narrative(ctx: dict) -> dict:
    """분석 요약(ctx = Analysis.to_context()) → 보고서 문장. 모델이 없거나 실패하거나 검증을 통과하지 못했을 때, 그리고 대역(mock)이 쓴다."""
    ev = {e["id"]: e for e in ctx["evidence"]}
    findings: list[dict] = []

    def add(text: str, ids: list) -> None:
        ids = list(dict.fromkeys(i for i in ids if i and i in ev))
        if text and ids:
            findings.append({"text": _sent(text), "evidence_ids": ids})

    status = ctx["status"]
    win = _period(*ctx["window"])
    actions_in = ctx["actions"]
    reference = ctx.get("reference_evidence")
    if reference:
        add(ev[reference]["text"], [reference])
    if status in ("insufficient", "insufficient_history"):
        headline = "표본이 부족해 이번에는 성과 변화를 판단하지 않았습니다."
        add(ev[ctx["status_evidence"]]["text"], [ctx["status_evidence"]])
        summary = "데이터가 부족해 판단을 보류했습니다. 설정을 바꾸지 말고 데이터가 쌓일 때까지 기다려 주세요."
    elif not ctx["incidents"]:
        add(ev[ctx["overview_evidence"]]["text"], [ctx["overview_evidence"]])
        for n in ctx["notes"]:
            add(ev[n]["text"], [n])
        if ctx["status"] == "partial":
            headline = f"{win} 전환이 적어 전환 관련 지표는 판단하지 못했고, 나머지 지표에는 눈에 띄는 이상이 없었습니다."
            add(ev[ctx["status_evidence"]]["text"], [ctx["status_evidence"]])
            summary = "전환 표본이 쌓일 때까지 예산·입찰 같은 큰 변경은 하지 말고 기다려 주세요."
        else:
            headline = f"{win} 눈에 띄는 이상은 없었습니다."
            summary = "기준 기간의 같은 요일과 견줘 설정 변경을 뒷받침할 뚜렷한 변화 신호를 찾지 못했습니다. 현재 비교로 모든 문제가 없다고 보장할 수는 없습니다."
    else:
        add(ev[ctx["overview_evidence"]]["text"], [ctx["overview_evidence"]])
        order = {"high": 0, "medium": 1, "low": 2}
        incs = sorted(ctx["incidents"], key=lambda i: (order[i["severity"]], i["start"]))[:3]
        top = incs[0]
        def _bad_and_big(m: str) -> bool:
            pct_m, dir_m = _ev_change(ev[top["metric_evidence"][m]])
            return BAD_DIRECTION.get(m) == dir_m and pct_m >= 10.0

        bad = [x["metric"] for x in top["metrics"] if BAD_DIRECTION.get(x["metric"]) == x["direction"]]
        head_metric = next((m for m in _HEAD_PRIORITY if m in top["metric_evidence"] and _bad_and_big(m)), None) \
            or next((m for m in _HEAD_PRIORITY if m in top["metric_evidence"] and m in bad), None) \
            or next((m for m in _HEAD_PRIORITY if m in top["metric_evidence"] and any(x["metric"] == m for x in top["metrics"])), None) \
            or next(iter(top["metric_evidence"]), None)
        if head_metric:
            pct, direction = _ev_change(ev[top["metric_evidence"][head_metric]])
            label = METRIC_LABEL[head_metric]
            word = ("높았" if direction == "up" else "낮았") if head_metric in ("ctr", "cvr", "cpc", "cpa") else ("많았" if direction == "up" else "적었")
            headline = f"{_period(top['start'], top['end'])} {josa(label, '이/가')} 기대보다 {pct_text(pct)} {word}습니다."
        else:
            headline = f"{_period(top['start'], top['end'])} 성과에 변화가 있었습니다."
        cause_line = ""
        for inc in incs:
            add(ev[inc["incident_evidence"]]["text"], [inc["incident_evidence"]])
            lead = ([inc["metric_evidence"][head_metric]] if inc is top and head_metric in inc["metric_evidence"] else [])
            for eid in list(dict.fromkeys(lead + inc["primary_evidence"]))[:3]:
                add(ev[eid]["text"], [eid])
            for eid in inc["funnel_evidence"]:
                add(ev[eid]["text"], [eid])
            if inc["polarity"] == "better":
                add("좋은 방향의 변화라 설정을 바꿀 필요는 없습니다.", [inc["incident_evidence"]])
                continue
            hyps = inc["hypotheses"]
            shown = [h for h in hyps if h["confidence"] >= LIKELY_CONFIDENCE][:3]
            if shown:
                for h in shown:
                    ids = [h["evidence_id"]] + h["evidence"][:2]
                    shown_ids = {i for f in findings for i in f["evidence_ids"]}
                    line = f"{h['label']} 가설과 맞는 신호가 있습니다. 집계 자료만으로 원인을 확정할 수는 없습니다."
                    add(line, ids)
                    cause_line = cause_line or f"{h['label']} 가설을 먼저 확인할 수 있습니다."
                    if h.get("verification_evidence"):
                        add(h["verification"], [h["verification_evidence"]])
                    for eid in h["evidence"]:
                        if eid not in shown_ids and ev[eid]["kind"] in ("group", "plateau", "hour", "freq", "mix", "shape", "onset") and len(findings) < 16:
                            add(ev[eid]["text"], [eid])
                            shown_ids.add(eid)
                    if h["rival_note"]:
                        add(h["rival_note"], [h["evidence_id"]])
                top_h = shown[0]
                if top_h["contradicted_by"]:
                    add(f"반대로 보이는 신호도 있습니다: {ev[top_h['contradicted_by'][0]]['text']}", [top_h["contradicted_by"][0]])
            else:
                add("원인을 정하기 어렵습니다.", [inc["undetermined_evidence"] or inc["incident_evidence"]])
                if inc["undetermined_evidence"]:
                    add(ev[inc["undetermined_evidence"]]["text"], [inc["undetermined_evidence"]])
                cause_line = cause_line or "원인은 정하기 어렵습니다."
            if inc["missing_evidence"]:
                add(ev[inc["missing_evidence"]]["text"], [inc["missing_evidence"]])
        if ctx["status"] == "partial":
            add(ev[ctx["status_evidence"]]["text"], [ctx["status_evidence"]])
        first_label = actions_in[0]["label"] if actions_in else ""
        summary = " ".join(x for x in (f"{_period(top['start'], top['end'])} 성과가 기대와 달랐습니다.", cause_line,
                                       f"먼저 {first_label}을(를) 권합니다." if first_label else "") if x)
        summary = summary.replace("을(를) 권합니다", "을 권합니다" if first_label and _has_batchim(first_label) else "를 권합니다")
    actions: list[dict] = []
    for a in actions_in:
        if a["cause"] and a["cause_state"] in ("supported", "likely"):
            reason = CAUSE_REASON[a["cause"]]
        else:
            reason = GENERIC_WHY.get(a["action_id"], "")
        instruction = ACTION_INSTRUCTION[a["action_id"]]
        if CATALOG[a["action_id"]].kind == "change":
            instruction = "관련 설정과 기록을 먼저 확인하고 가설과 맞는 경우에만 소규모 변경을 검토하세요. " + instruction
        why = (reason + " " + instruction).strip()
        ids = list(dict.fromkeys(i for i in a["evidence_ids"] if i in ev)) or ([ctx["status_evidence"]] if ctx["status_evidence"] else [ctx["overview_evidence"]])
        actions.append({"action_id": a["action_id"], "why": why, "evidence_ids": ids})
    return {"headline": headline, "summary": summary, "findings": findings, "actions": actions}
