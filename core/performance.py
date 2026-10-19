"""광고 성과 데이터 — 행 모델, CSV 읽기, 합성 데이터 생성기, 시나리오 정답 정의.

여기 있는 것은 모두 순수 계산이다(모델·네트워크·파일 쓰기 없음). 표준 라이브러리만 쓰므로 브라우저(stlite) 실행판에서도 그대로 돈다.

세 가지 일을 한다.
1) `MetricRow` 와 파생 지표(CTR·CPC·CVR·CPA).
2) `parse_csv` — 외부에서 온 CSV 는 믿지 않는다. 행 단위로 검사하고 줄 번호와 이유를 쉬운 말로 돌려준다.
   문자열 칸은 길이를 자르고 제어문자를 지우며, `=` `+` `-` `@` 로 시작하는 칸(스프레드시트 수식 주입)은 문자로 바꿔 둔다.
3) `generate` — 진짜 광고 데이터가 없으므로 합성 데이터로 방법을 측정한다. 기준선 + 요일 계절성 + 잡음 + 사건 주입.
   각 시나리오는 정답(이상 구간·주 원인·허용/금지 액션)을 함께 가진다.

이 합성 데이터가 의미하지 않는 것
- 실제 토스·토스애즈 또는 어떤 실제 광고 계정의 성과가 아니다. 숫자 범위(노출·클릭률·전환율·클릭당 비용)는 이 프로젝트가 가정한 값이다.
- 생성기와 진단 규칙을 같은 사람이 설계했다. 사건이 데이터에 남기는 흔적을 설계자가 알고 있으므로, 이 데이터에서의 정확도는
  실제 광고 데이터에서의 정확도를 보장하지 않는다(순환 위험). 최종 확인용(holdout) 시나리오는 이를 줄이려는 시도일 뿐이다.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import math
import random
import re
import unicodedata
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Any, Iterable, Optional

# ───────────────────────────── 한도 ─────────────────────────────
MAX_ROWS = 20_000                 # 행 수 상한 — 이보다 많으면 앞부분만 읽고 알린다
MAX_BYTES = 2 * 1024 * 1024       # 크기 상한 (2MB)
MAX_TEXT_LEN = 60                 # 문자열 칸 길이 상한
MAX_COUNT = 10 ** 12              # 숫자 칸 상한 (이보다 크면 입력 실수로 본다)
MAX_REPORTED_ERRORS = 100         # 오류 목록 길이 상한 (나머지는 건수만)
NO_DIM = "(구분 없음)"             # 열이 없거나 칸이 비었을 때의 값
DIMENSIONS = ("campaign", "ad_set", "creative", "device")

_FORMULA_START = "=+-@＝＋－＠"    # 스프레드시트가 수식으로 읽는 시작 글자(전각 포함)


# ───────────────────────────── 행 모델 ─────────────────────────────
@dataclass(frozen=True)
class MetricRow:
    """하루·한 조합(캠페인·세트·소재·기기)의 성과. reach·delivery_end_hour 는 있으면 쓰는 선택 열이다."""

    date: dt.date
    campaign: str
    ad_set: str
    creative: str
    device: str
    impressions: int
    clicks: int
    conversions: int
    cost_krw: int
    reach: Optional[int] = None               # 도달(중복 없는 사람 수)
    delivery_end_hour: Optional[int] = None   # 그날 노출이 마지막으로 나간 시각(0~23). 하루 예산이 일찍 소진되면 낮은 값

    @property
    def ctr(self) -> Optional[float]:
        return self.clicks / self.impressions if self.impressions > 0 else None

    @property
    def cpc(self) -> Optional[float]:
        return self.cost_krw / self.clicks if self.clicks > 0 else None

    @property
    def cvr(self) -> Optional[float]:
        return self.conversions / self.clicks if self.clicks > 0 else None

    @property
    def cpa(self) -> Optional[float]:
        return self.cost_krw / self.conversions if self.conversions > 0 else None

    @property
    def frequency(self) -> Optional[float]:
        return self.impressions / self.reach if self.reach else None


# ───────────────────────────── 문자열 무해화 ─────────────────────────────
def clean_text(value: Any, limit: int = MAX_TEXT_LEN) -> str:
    """외부 문자열 칸 정리: 제어문자·보이지 않는 문자 제거, 공백 정리, 길이 상한. 수식 무해화는 `safe_cell` 이 맡는다."""
    s = unicodedata.normalize("NFKC", str(value)) if value is not None else ""
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Zl", "Zp"):
            out.append(" ")            # 줄바꿈·탭은 공백으로
        elif cat == "Cf":
            continue                   # 제로폭·방향 제어 문자는 숨은 지시문의 운반체
        else:
            out.append(ch)
    s = re.sub(r"\s+", " ", "".join(out)).strip()
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def safe_cell(value: Any) -> str:
    """스프레드시트 수식 주입 방어. `=` `+` `-` `@` 로 시작하는 칸은 앞에 작은따옴표를 붙여 문자로 바꾼다 (이미 붙어 있으면 그대로 — 여러 번 불러도 같다).
    화면에서는 이스케이프해 문자 그대로 보이고, CSV 로 내보내도 수식으로 실행되지 않는다."""
    s = clean_text(value)
    if s and s[0] in _FORMULA_START:
        return "'" + s
    return s


def looks_like_formula(value: Any) -> bool:
    s = clean_text(value, limit=10_000)
    return bool(s) and s[0] in _FORMULA_START


# ───────────────────────────── CSV 읽기 ─────────────────────────────
@dataclass(frozen=True)
class CsvIssue:
    """CSV 읽기 중 알릴 것. line=0 이면 파일 전체에 대한 말이다. message 는 쉬운 말로 쓴다."""

    line: int
    message: str
    column: str = ""

    def __str__(self) -> str:
        return f"{self.line}번째 줄: {self.message}" if self.line else self.message


@dataclass
class CsvResult:
    rows: list[MetricRow] = field(default_factory=list)
    errors: list[CsvIssue] = field(default_factory=list)       # 읽지 못한 행·파일 전체 문제 (최대 MAX_REPORTED_ERRORS 개만 담는다)
    error_count: int = 0                                       # 위 목록이 잘렸어도 전체 건수
    notices: list[str] = field(default_factory=list)           # 읽긴 했지만 알려 둘 것 (수식처럼 보인 칸 등)
    data_lines: int = 0                                        # 머리글을 뺀 데이터 줄 수
    columns: list[str] = field(default_factory=list)           # 찾은 열 (표준 이름)
    formula_cells: int = 0                                     # 무해화한 칸 수

    @property
    def ok(self) -> bool:
        return bool(self.rows)


_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "day", "날짜", "일자", "일별", "일시", "기준일", "보고일"),
    "campaign": ("campaign", "campaignname", "캠페인", "캠페인명", "캠페인이름"),
    "ad_set": ("adset", "adsetname", "adgroup", "adgroupname", "광고세트", "광고세트명", "광고그룹", "광고그룹명", "세트", "세트명"),
    "creative": ("creative", "creativename", "ad", "adname", "소재", "소재명", "광고소재", "광고소재명", "크리에이티브"),
    "device": ("device", "devicetype", "platform", "기기", "기기종류", "디바이스", "단말", "단말기", "운영체제", "os"),
    "impressions": ("impressions", "impression", "impr", "imps", "노출", "노출수", "노출횟수", "노출량"),
    "clicks": ("clicks", "click", "클릭", "클릭수", "클릭횟수"),
    "conversions": ("conversions", "conversion", "conv", "convs", "전환", "전환수", "전환횟수", "전환건수"),
    "cost_krw": ("costkrw", "cost", "spend", "spent", "광고비", "비용", "지출", "소진액", "집행금액", "광고비원", "총비용", "사용금액"),
    "reach": ("reach", "도달", "도달수", "도달자수", "도달사용자"),
    "delivery_end_hour": ("deliveryendhour", "lastdeliveryhour", "노출종료시각", "마지막노출시각", "노출종료시", "마지막노출시"),
}
REQUIRED_COLUMNS = ("date", "impressions", "clicks", "conversions", "cost_krw")
COLUMN_LABEL = {"date": "날짜", "campaign": "캠페인", "ad_set": "광고세트", "creative": "소재", "device": "기기", "impressions": "노출수", "clicks": "클릭수",
                "conversions": "전환수", "cost_krw": "광고비", "reach": "도달", "delivery_end_hour": "노출종료시각"}
_ALIAS_TO_CANON = {a: canon for canon, aliases in _HEADER_ALIASES.items() for a in aliases}


def _norm_header(h: str) -> str:
    s = unicodedata.normalize("NFKC", h or "").lstrip("﻿").strip().lower()
    s = re.sub(r"[\(\[（].*?[\)\]）]", "", s)        # '광고비(원)' → '광고비'
    return re.sub(r"[^0-9a-z가-힣]", "", s)


_DATE_PATTERNS = (
    re.compile(r"^(\d{4})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})\.?(?:[ T]\d.*)?$"),
    re.compile(r"^(\d{4})(\d{2})(\d{2})$"),
    re.compile(r"^(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일?$"),
)


def parse_date(raw: str) -> Optional[dt.date]:
    """여러 날짜 형식(2026-10-05 · 2026.10.5 · 2026/10/05 · 20261005 · 2026년 10월 5일)을 읽는다. 월/일 순서가 모호한 형식은 읽지 않는다."""
    s = unicodedata.normalize("NFKC", raw or "").strip()
    for pat in _DATE_PATTERNS:
        m = pat.match(s)
        if m:
            y, mo, d = (int(g) for g in m.groups())
            if not 2000 <= y <= 2100:
                return None
            try:
                return dt.date(y, mo, d)
            except ValueError:
                return None
    return None


_INT_JUNK = re.compile(r"[,₩]|\s*원$")


def parse_count(raw: str) -> tuple[Optional[int], str]:
    """정수 칸 읽기. (값, 오류 문장). '1,234' · '1234원' · '12.0' 은 허용, '12.5' · '-3' · '1e3' 은 거절한다."""
    s = unicodedata.normalize("NFKC", raw or "").strip()
    if not s:
        return None, "비어 있어요"
    s = _INT_JUNK.sub("", s)
    if s.startswith("-"):
        return None, "음수는 쓸 수 없어요"
    m = re.fullmatch(r"(\d+)(?:\.(\d+))?", s)
    if not m:
        return None, "숫자가 아니에요"
    if m.group(2) and int(m.group(2)) != 0:
        return None, "소수가 아니라 정수여야 해요"
    v = int(m.group(1))
    if v > MAX_COUNT:
        return None, "값이 너무 커요"
    return v, ""


def _pick_delimiter(first_line: str) -> str:
    counts = {d: first_line.count(d) for d in (",", "\t", ";")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] > 0 else ","


def decode_csv_bytes(data: bytes) -> str:
    """올린 파일의 글자 인코딩을 추정한다 (엑셀 한국어 CSV 는 cp949 인 경우가 많다). 어느 쪽도 아니면 깨진 글자는 버린다."""
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _header_help() -> str:
    return "첫 줄에 열 이름이 있어야 해요: 날짜, 노출수, 클릭수, 전환수, 광고비 (선택: 캠페인, 광고세트, 소재, 기기, 도달, 노출종료시각)."


def parse_csv(text: str) -> tuple[list[MetricRow], list[CsvIssue]]:
    """CSV 텍스트 → (행, 오류). 자세한 결과가 필요하면 `parse_csv_detailed`."""
    res = parse_csv_detailed(text)
    return res.rows, res.errors


def parse_csv_detailed(text: str) -> CsvResult:
    res = CsvResult()

    def add_error(line: int, msg: str, column: str = "") -> None:
        res.error_count += 1
        if len(res.errors) < MAX_REPORTED_ERRORS:
            res.errors.append(CsvIssue(line, msg, column))

    if text is None or not str(text).strip():
        add_error(0, "비어 있는 파일이에요. " + _header_help())
        return res
    text = str(text)
    if len(text) > MAX_BYTES // 3 and len(text.encode("utf-8")) > MAX_BYTES:    # 글자 수가 한도의 1/3 을 넘을 때만 바이트 수를 센다
        add_error(0, f"파일이 너무 커요 (최대 {MAX_BYTES // (1024 * 1024)}MB). 기간을 줄이거나 일별로 합쳐서 올려 주세요.")
        return res
    text = text.lstrip("﻿")
    first_line = text.split("\n", 1)[0]
    reader = csv.reader(io.StringIO(text), delimiter=_pick_delimiter(first_line))
    try:
        header = next(reader)
    except (StopIteration, csv.Error):
        add_error(0, "비어 있는 파일이에요. " + _header_help())
        return res
    # 열 이름 → 표준 이름
    colmap: dict[str, int] = {}
    for i, h in enumerate(header):
        canon = _ALIAS_TO_CANON.get(_norm_header(h))
        if canon and canon not in colmap:
            colmap[canon] = i
    res.columns = [c for c in _HEADER_ALIASES if c in colmap]
    missing = [COLUMN_LABEL[c] for c in REQUIRED_COLUMNS if c not in colmap]
    if missing:
        add_error(0, f"필요한 열이 없어요: {', '.join(missing)}. " + _header_help())
        return res

    seen: dict[tuple, int] = {}
    rows: list[MetricRow] = []

    def cell(rec: list[str], key: str) -> str:
        i = colmap.get(key)
        return rec[i] if i is not None and i < len(rec) else ""

    try:
        for rec in reader:
            line = reader.line_num
            if not rec or all(not c.strip() for c in rec):
                continue
            res.data_lines += 1
            if res.data_lines > MAX_ROWS:
                add_error(0, f"행이 너무 많아요 (최대 {MAX_ROWS:,}행). {MAX_ROWS:,}행까지만 읽었어요.")
                break
            row = _parse_row(rec, colmap, cell, line, res, add_error)
            if row is None:
                continue
            key = (row.date, row.campaign, row.ad_set, row.creative, row.device)
            if key in seen:
                add_error(line, f"같은 날짜·캠페인·광고세트·소재·기기가 {seen[key]}번째 줄에 이미 있어요. 중복을 지우고 다시 올려 주세요.")
                continue
            seen[key] = line
            rows.append(row)
    except csv.Error as e:
        add_error(0, f"CSV 형식이 깨져 있어요 ({str(e)[:60]}). 따옴표나 구분 문자를 확인해 주세요.")
    res.rows = rows
    if res.formula_cells:
        res.notices.append(f"수식처럼 보이는 칸 {res.formula_cells}개는 계산하지 않고 글자 그대로 읽었어요 (앞에 작은따옴표를 붙였어요).")
    if rows and not res.errors:
        pass
    elif not rows and not res.errors:
        add_error(0, "읽을 수 있는 데이터 줄이 없어요. " + _header_help())
    return res


def _parse_row(rec: list[str], colmap: dict[str, int], cell, line: int, res: CsvResult, add_error) -> Optional[MetricRow]:
    day = parse_date(cell(rec, "date"))
    if day is None:
        add_error(line, f"날짜를 읽을 수 없어요 ('{clean_text(cell(rec, 'date'), 20)}'). 2026-10-05 처럼 써 주세요.", "date")
        return None
    nums: dict[str, int] = {}
    for key in ("impressions", "clicks", "conversions", "cost_krw"):
        v, err = parse_count(cell(rec, key))
        if v is None:
            add_error(line, f"{COLUMN_LABEL[key]}: {err}.", key)
            return None
        nums[key] = v
    if nums["clicks"] > nums["impressions"]:
        add_error(line, f"클릭수({nums['clicks']:,})가 노출수({nums['impressions']:,})보다 커서 읽지 않았어요.", "clicks")
        return None
    if nums["conversions"] > nums["clicks"]:
        add_error(line, f"전환수({nums['conversions']:,})가 클릭수({nums['clicks']:,})보다 커서 읽지 않았어요.", "conversions")
        return None
    reach: Optional[int] = None
    if "reach" in colmap and cell(rec, "reach").strip():
        reach, err = parse_count(cell(rec, "reach"))
        if reach is None:
            add_error(line, f"도달: {err}.", "reach")
            return None
        if reach > nums["impressions"]:
            add_error(line, f"도달({reach:,})이 노출수({nums['impressions']:,})보다 커서 읽지 않았어요.", "reach")
            return None
    hour: Optional[int] = None
    if "delivery_end_hour" in colmap and cell(rec, "delivery_end_hour").strip():
        hour, err = parse_count(cell(rec, "delivery_end_hour"))
        if hour is None or hour > 23:
            add_error(line, "노출종료시각은 0~23 사이 정수여야 해요.", "delivery_end_hour")
            return None
    dims: dict[str, str] = {}
    for key in DIMENSIONS:
        raw = cell(rec, key)
        if looks_like_formula(raw):
            res.formula_cells += 1
        dims[key] = safe_cell(raw) or NO_DIM
    return MetricRow(date=day, impressions=nums["impressions"], clicks=nums["clicks"], conversions=nums["conversions"], cost_krw=nums["cost_krw"],
                     reach=reach, delivery_end_hour=hour, **dims)


def rows_to_csv(rows: Iterable[MetricRow]) -> str:
    """행 → CSV 텍스트 (내려받기용). 문자열 칸은 여기서도 한 번 더 무해화한다."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["날짜", "캠페인", "광고세트", "소재", "기기", "노출수", "클릭수", "전환수", "광고비", "도달", "노출종료시각"])
    for r in rows:
        w.writerow([r.date.isoformat(), safe_cell(r.campaign), safe_cell(r.ad_set), safe_cell(r.creative), safe_cell(r.device),
                    r.impressions, r.clicks, r.conversions, r.cost_krw, "" if r.reach is None else r.reach,
                    "" if r.delivery_end_hour is None else r.delivery_end_hour])
    return buf.getvalue()


def template_csv() -> str:
    """내려받기용 빈 양식 (예시 두 줄 포함). 숫자는 이해를 돕는 예시일 뿐 실제 성과가 아니다."""
    return ("날짜,캠페인,광고세트,소재,기기,노출수,클릭수,전환수,광고비,도달,노출종료시각\n"
            "2026-10-01,신규가입,세트A,소재1,Android,12000,260,9,98000,7000,23\n"
            "2026-10-01,신규가입,세트A,소재1,iOS,9000,210,12,84000,5200,23\n")


# ───────────────────────────── 합성 데이터 생성기 ─────────────────────────────
EVENT_KINDS = ("budget_cap", "creative_fatigue", "tracking_break", "landing_issue", "bid_competition", "audience_saturation", "mix_shift")
CONTROL_KINDS = ("seasonal_dip", "no_event")
ALL_KINDS = EVENT_KINDS + CONTROL_KINDS + ("multi",)

# 액션 이름은 core.insight_engine 의 카탈로그와 같아야 한다 (테스트가 확인).
CHANGE_ACTIONS = frozenset({"replace_creative", "add_creative", "raise_budget", "lower_budget", "raise_bid", "lower_bid", "expand_audience", "pause"})
SAFE_ACTIONS = frozenset({"check_landing", "check_tracking", "keep_watching", "collect_more_data", "review_segment_mix"})
# 원인별 정답: (허용 = 이 원인일 때 도움이 되는 액션, 금지 = 이 원인일 때 해로운 액션). 이 프로젝트가 정한 기준이며 실제 운영 정답이 아니다.
CAUSE_ACTIONS: dict[str, tuple[frozenset, frozenset]] = {
    "budget_cap": (frozenset({"raise_budget"}), frozenset({"lower_budget", "pause"})),
    "creative_fatigue": (frozenset({"replace_creative", "add_creative"}), frozenset({"raise_budget", "raise_bid", "pause"})),
    "tracking_break": (frozenset({"check_tracking"}), CHANGE_ACTIONS),   # 전환 측정이 믿을 수 없을 때는 무엇도 바꾸지 않는다
    "landing_issue": (frozenset({"check_landing"}), frozenset({"raise_budget", "raise_bid", "expand_audience"})),
    "bid_competition": (frozenset({"raise_bid", "add_creative"}), frozenset({"lower_bid", "pause"})),
    "audience_saturation": (frozenset({"expand_audience", "add_creative", "replace_creative"}), frozenset({"raise_budget", "raise_bid", "pause"})),
    "mix_shift": (frozenset({"review_segment_mix"}), frozenset({"pause", "lower_budget", "raise_bid"})),
    "seasonal_dip": (frozenset({"keep_watching"}), CHANGE_ACTIONS),       # 주말 요일 효과면 아무것도 바꾸지 않는다
    "no_event": (frozenset({"keep_watching"}), CHANGE_ACTIONS),
}
_AMBIGUOUS_ALLOWED = frozenset({"check_tracking", "check_landing", "collect_more_data", "keep_watching"})

_WEEK_SHAPE = (0.02, 0.03, 0.03, 0.02, 0.04, -0.09, -0.15)        # 월~일 노출 요일 편차 (기본 강도 1.0)
_WEEK_CVR = (0.0, 0.0, 0.0, 0.0, 0.01, -0.04, -0.06)              # 월~일 전환율 요일 편차
_WEEK_CTR = (0.0, 0.0, 0.0, 0.0, 0.0, -0.02, -0.03)


@dataclass(frozen=True)
class EventSpec:
    kind: str
    start: int                       # 시작 위치 (첫 날 = 0)
    end: Optional[int] = None        # 끝 위치(포함). None 이면 데이터 끝까지
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: str
    seed: int
    events: tuple = ()
    days: int = 42
    start_date: dt.date = dt.date(2026, 9, 7)
    scale: float = 1.0               # 물량 배율 (작을수록 표본이 적다)
    n_ad_sets: int = 2
    creatives_per_set: int = 2
    devices: tuple = ("Android", "iOS")
    first_device_share: float = 0.45    # 첫 기기(전환율이 낮은 쪽)의 노출 비중
    noise: float = 1.0               # 모든 잡음 크기 배율
    dow_amp: float = 1.0             # 요일 효과 강도
    trend_per_day: float = 0.001     # 하루당 완만한 추세
    with_reach: bool = True
    with_hour: bool = True
    collapse: tuple = ()             # 합쳐서 구분 없음으로 만들 차원 (creative · device · ad_set)
    ambiguous: bool = False          # 데이터만으로는 원인을 가릴 수 없게 설계한 시나리오
    detectable: bool = True          # False 면 표본이 너무 적어 이상 탐지 자체를 기대하지 않는다
    candidate_causes: tuple = ()     # ambiguous 일 때 헷갈릴 수 있는 원인들 (금지 액션 계산용)
    note: str = ""

    def to_dict(self) -> dict:
        return {"scenario_id": self.scenario_id, "seed": self.seed, "days": self.days, "start_date": self.start_date.isoformat(), "scale": self.scale,
                "n_ad_sets": self.n_ad_sets, "creatives_per_set": self.creatives_per_set, "devices": list(self.devices),
                "first_device_share": self.first_device_share, "noise": self.noise, "dow_amp": self.dow_amp, "trend_per_day": self.trend_per_day,
                "with_reach": self.with_reach, "with_hour": self.with_hour, "collapse": list(self.collapse), "ambiguous": self.ambiguous,
                "detectable": self.detectable, "candidate_causes": list(self.candidate_causes), "note": self.note,
                "events": [{"kind": e.kind, "start": e.start, "end": e.end, "params": dict(e.params)} for e in self.events]}

    @classmethod
    def from_dict(cls, d: dict) -> "ScenarioSpec":
        d = dict(d)
        events = tuple(EventSpec(kind=e["kind"], start=int(e["start"]), end=None if e.get("end") is None else int(e["end"]), params=dict(e.get("params") or {}))
                       for e in d.pop("events", []))
        d["start_date"] = dt.date.fromisoformat(d["start_date"])
        for k in ("devices", "collapse", "candidate_causes"):
            if k in d:
                d[k] = tuple(d[k])
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(events=events, **{k: v for k, v in d.items() if k in allowed})


def truth_for(spec: ScenarioSpec) -> dict:
    """시나리오의 정답. 사건 구간(날짜), 주 원인, 허용·금지 액션. 생성기 입력에서 계산하므로 데이터와 어긋날 수 없다."""
    incidents = []
    for e in spec.events:
        end = e.end if e.end is not None else spec.days - 1
        incidents.append({"kind": e.kind, "start": (spec.start_date + dt.timedelta(days=e.start)).isoformat(),
                          "end": (spec.start_date + dt.timedelta(days=end)).isoformat()})
    causes = [e.kind for e in spec.events]
    if spec.ambiguous:
        cand = list(spec.candidate_causes) or causes
        forbidden = frozenset().union(*[CAUSE_ACTIONS[c][1] for c in cand]) if cand else frozenset()
        allowed = _AMBIGUOUS_ALLOWED - forbidden
    elif causes:
        forbidden = frozenset().union(*[CAUSE_ACTIONS[c][1] for c in causes])
        allowed = frozenset().union(*[CAUSE_ACTIONS[c][0] for c in causes]) - forbidden
    else:
        allowed, forbidden = CAUSE_ACTIONS["no_event"]      # 대조 시나리오(요일 효과만 있거나 아무 일도 없음): 아무것도 바꾸지 않는다
    return {"incidents": incidents, "main_causes": [] if spec.ambiguous else causes, "ambiguous": spec.ambiguous,
            "candidate_causes": list(spec.candidate_causes), "detectable": spec.detectable,
            "allowed_actions": sorted(allowed), "forbidden_actions": sorted(forbidden), "is_control": not spec.events}


_ND = NormalDist()


def _z(rng: random.Random) -> float:
    """표준정규 난수 하나 — 균등 난수 하나만 써서, 사건 유무와 상관없이 난수열이 같은 자리에서 같게 소비되도록 한다."""
    u = min(max(rng.random(), 1e-9), 1 - 1e-9)
    return _ND.inv_cdf(u)


def _count(rng: random.Random, lam: float, p: float = 0.0) -> int:
    """평균 lam 인 개수 하나. 작으면 푸아송 역변환, 크면 정규 근사(분산 lam*(1-p)). 항상 균등 난수 하나만 쓴다."""
    u = min(max(rng.random(), 1e-9), 1 - 1e-9)
    if lam <= 0:
        return 0
    if lam < 30:
        pmf = math.exp(-lam)
        cum, k = pmf, 0
        while cum < u and k < 200:
            k += 1
            pmf *= lam / k
            cum += pmf
        return k
    return max(0, int(round(lam + math.sqrt(lam * max(1e-9, 1 - p)) * _ND.inv_cdf(u))))


def _structure(spec: ScenarioSpec) -> list[dict]:
    """기준선: 조합(세트·소재·기기)마다의 평균 노출·클릭률·전환율·클릭당 비용. 시드로만 정해진다."""
    srng = random.Random(spec.seed * 7919 + 13)
    combos: list[dict] = []
    set_names = ["세트A_20대", "세트B_30대", "세트C_40대", "세트D_관심사"][: spec.n_ad_sets]
    dev_share = [spec.first_device_share] + [(1 - spec.first_device_share) / max(1, len(spec.devices) - 1)] * (len(spec.devices) - 1)
    if len(spec.devices) == 1:
        dev_share = [1.0]
    cvr_by_dev = {}
    for i, dev in enumerate(spec.devices):
        cvr_by_dev[dev] = srng.uniform(0.028, 0.040) if i == 0 else srng.uniform(0.052, 0.070)
    for si, sname in enumerate(set_names):
        set_cvr = srng.uniform(0.92, 1.08)
        set_cpc = srng.uniform(280, 520)
        for ci in range(spec.creatives_per_set):
            cname = f"소재{chr(65 + si)}{ci + 1}"
            ctr = srng.uniform(0.014, 0.030)
            for di, dev in enumerate(spec.devices):
                combos.append({
                    "ad_set": sname, "creative": cname, "device": dev, "set_index": si, "creative_index": ci, "device_index": di,
                    "imp": srng.uniform(12000, 24000) * spec.scale * dev_share[di] * len(spec.devices),
                    "ctr": ctr * (0.97 if di == 0 else 1.03), "cvr": cvr_by_dev[dev] * set_cvr, "cpc": set_cpc, "freq": srng.uniform(1.3, 1.9),
                })
    return combos


def _active(ev: EventSpec, t: int, days: int) -> bool:
    return ev.start <= t <= (ev.end if ev.end is not None else days - 1)


def _ramp(ev: EventSpec, t: int, ramp_days: float) -> float:
    return 1.0 if ramp_days <= 1 else min(1.0, (t - ev.start + 1) / ramp_days)


def generate(spec: ScenarioSpec, seed: Optional[int] = None) -> list[MetricRow]:
    """시나리오 → 일별 성과 행. 같은 (spec, seed) 면 항상 같은 결과다 (파이썬 표준 난수만 쓴다)."""
    spec_seed = spec.seed if seed is None else seed
    if seed is not None and seed != spec.seed:
        d = spec.to_dict()
        d["seed"] = seed
        spec = ScenarioSpec.from_dict(d)
    combos = _structure(spec)
    rng = random.Random(spec_seed)
    campaign = "신규가입_앱설치"
    rows: list[MetricRow] = []
    noise = spec.noise
    # 세트별 기준 하루 광고비 (예산 소진 사건의 한도 계산용)
    base_cost: dict[str, float] = {}
    for c in combos:
        base_cost[c["ad_set"]] = base_cost.get(c["ad_set"], 0.0) + c["imp"] * c["ctr"] * c["cpc"]

    for t in range(spec.days):
        day = spec.start_date + dt.timedelta(days=t)
        wd = day.weekday()
        shock_imp = _z(rng) * 0.03 * noise
        shock_cvr = _z(rng) * 0.03 * noise
        demand = (1 + _WEEK_SHAPE[wd] * spec.dow_amp) * (1 + spec.trend_per_day) ** t * math.exp(shock_imp)
        cvr_dow = 1 + _WEEK_CVR[wd] * spec.dow_amp
        ctr_dow = 1 + _WEEK_CTR[wd] * spec.dow_amp
        active = [e for e in spec.events if _active(e, t, spec.days)]
        day_rows: list[dict] = []
        for c in combos:
            z_imp, z_ctr, z_cvr, z_cpc, z_f = _z(rng), _z(rng), _z(rng), _z(rng), _z(rng)
            imp_mult, ctr_mult, cvr_mult, cpc_mult, reach_mult = 1.0, 1.0, 1.0, 1.0, 1.0
            dev_weight = 1.0
            for e in active:
                p = e.params
                if e.kind == "creative_fatigue":
                    if c["set_index"] == int(p.get("ad_set_index", 0)) and c["creative_index"] == int(p.get("creative_index", 0)):
                        ctr_mult *= 1 - float(p.get("depth", 0.4)) * _ramp(e, t, float(p.get("ramp_days", 12)))
                elif e.kind == "tracking_break":
                    if p.get("device") in (None, c["device"]):
                        cvr_mult *= float(p.get("residual", 0.03))
                elif e.kind == "landing_issue":
                    if p.get("ad_set_index") in (None, c["set_index"]):
                        cvr_mult *= 1 - float(p.get("depth", 0.35))
                elif e.kind == "bid_competition":
                    cpc_mult *= 1 + float(p.get("cpc_up", 0.25))
                    imp_mult *= 1 - float(p.get("imp_down", 0.18))
                elif e.kind == "audience_saturation":
                    if p.get("ad_set_index") in (None, c["set_index"]):
                        r = _ramp(e, t, float(p.get("ramp_days", 10)))
                        ctr_mult *= 1 - float(p.get("ctr_depth", 0.22)) * r
                        imp_mult *= 1 - float(p.get("imp_down", 0.05)) * r
                        reach_mult *= 1 - float(p.get("reach_down", 0.2)) * r
                elif e.kind == "mix_shift":
                    r = _ramp(e, t, float(p.get("ramp_days", 3)))
                    base_share = spec.first_device_share
                    to_share = float(p.get("to_share", 0.65))
                    first = (base_share + (to_share - base_share) * r)
                    if len(spec.devices) >= 2:
                        rest = (1 - first) / (len(spec.devices) - 1)
                        base_rest = (1 - base_share) / (len(spec.devices) - 1)
                        dev_weight *= (first / base_share) if c["device_index"] == 0 else (rest / base_rest)
            imp_mean = c["imp"] * demand * imp_mult * dev_weight * math.exp(z_imp * 0.04 * noise)
            imp = _count(rng, imp_mean)
            ctr_t = c["ctr"] * ctr_dow * ctr_mult * math.exp(z_ctr * 0.03 * noise)
            clk = min(imp, _count(rng, imp * ctr_t, ctr_t))
            cvr_t = c["cvr"] * cvr_dow * cvr_mult * math.exp(shock_cvr + z_cvr * 0.04 * noise)
            conv = min(clk, _count(rng, clk * cvr_t, cvr_t))
            cpc_t = c["cpc"] * cpc_mult * math.exp(z_cpc * 0.03 * noise)
            cost = int(round(clk * cpc_t))
            freq = c["freq"] * (1 + 0.0 * z_f)
            reach = max(1, int(round(imp / freq * reach_mult * math.exp(z_f * 0.02 * noise)))) if imp > 0 else 0
            day_rows.append({"c": c, "imp": imp, "clk": clk, "conv": conv, "cost": cost, "reach": min(reach, imp), "hour": 23})
        for e in active:                      # 세트 단위로 일어나는 사건: 하루 예산 소진
            if e.kind != "budget_cap":
                continue
            frac = float(e.params.get("cap_frac", 0.7))
            only = e.params.get("ad_set_index")
            for si, sname in enumerate(sorted({c["ad_set"] for c in combos})):
                if only is not None and si != int(only):
                    continue
                members = [r for r in day_rows if r["c"]["ad_set"] == sname]
                raw = sum(r["cost"] for r in members)
                cap = base_cost[sname] * frac * (1 + _z(rng) * 0.004)
                if raw > cap > 0:
                    f = cap / raw
                    end_hour = 9 + int(min(max(rng.random(), 0), 0.999) * 5)
                    for r in members:
                        r["imp"], r["clk"], r["conv"], r["cost"] = int(round(r["imp"] * f)), int(round(r["clk"] * f)), int(round(r["conv"] * f)), int(round(r["cost"] * f))
                        r["clk"], r["conv"] = min(r["clk"], r["imp"]), min(r["conv"], r["clk"])
                        r["reach"] = min(int(round(r["reach"] * f)), r["imp"])
                        r["hour"] = end_hour
                else:
                    rng.random()
        for r in day_rows:
            c = r["c"]
            rows.append(MetricRow(date=day, campaign=campaign, ad_set=c["ad_set"], creative=c["creative"], device=c["device"], impressions=r["imp"],
                                  clicks=r["clk"], conversions=r["conv"], cost_krw=r["cost"],
                                  reach=r["reach"] if spec.with_reach else None, delivery_end_hour=r["hour"] if spec.with_hour else None))
    return _collapse(rows, spec.collapse) if spec.collapse else rows


def _collapse(rows: list[MetricRow], dims: Iterable[str]) -> list[MetricRow]:
    """지정한 차원을 합쳐 '구분 없음' 으로 만든다 (열이 없는 CSV 를 흉내 낸다). 도달은 단순 합이라 중복 제거된 값이 아니다."""
    dims = set(dims)
    acc: dict[tuple, dict] = {}
    for r in rows:
        key = (r.date, r.campaign, NO_DIM if "ad_set" in dims else r.ad_set, NO_DIM if "creative" in dims else r.creative, NO_DIM if "device" in dims else r.device)
        a = acc.setdefault(key, {"imp": 0, "clk": 0, "conv": 0, "cost": 0, "reach": 0, "has_reach": False, "hour": None})
        a["imp"] += r.impressions
        a["clk"] += r.clicks
        a["conv"] += r.conversions
        a["cost"] += r.cost_krw
        if r.reach is not None:
            a["reach"] += r.reach
            a["has_reach"] = True
        if r.delivery_end_hour is not None:
            a["hour"] = r.delivery_end_hour if a["hour"] is None else min(a["hour"], r.delivery_end_hour)
    return [MetricRow(date=k[0], campaign=k[1], ad_set=k[2], creative=k[3], device=k[4], impressions=a["imp"], clicks=a["clk"], conversions=a["conv"],
                      cost_krw=a["cost"], reach=a["reach"] if a["has_reach"] else None, delivery_end_hour=a["hour"]) for k, a in sorted(acc.items(), key=lambda kv: kv[0])]
