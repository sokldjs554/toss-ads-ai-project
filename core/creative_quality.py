"""소재 퀄리티 신호 — 문구가 '글로서' 얼마나 잘 쓰였는지를 모델 없이 규칙으로 재는 8개 신호.

"이 광고가 좋다는 걸 어떻게 알지?" 에 대한 이 프로젝트의 답 중 '소재 퀄리티' 부분이다. 정책 준수는 심사(guardrails·adjudicator)가 따로 잰다.

정책 판정과 완전히 분리된다
- 이 모듈은 판정 함수(`agents.adjudicator.decide`)·심사 모델·파이프라인을 import 하지 않고, 그쪽도 이 모듈을 import 하지 않는다(tests/test_creative_quality_separation.py 가 못 박는다).
  품질 점수가 높아도 위반은 통과되지 않고, 낮아도 정책상 문제없는 광고가 막히지 않는다. 점수는 사람이 문구를 고칠 때 보는 참고 신호다.
- 코드는 `guardrails.grounding`(조건 비교)과 `guardrails.lexical`(표현 사전)의 공개 함수를 '읽기만' 한다. 비교 규칙을 새로 만들지 않는다.

이 숫자가 의미하지 않는 것
- 클릭률·전환율·광고주 만족과의 상관이 아니다. 그런 상관은 측정하지 않았다(실제 성과 데이터가 없다).
- 가중치·구간·단어 목록은 이 프로젝트 작성자가 가상 문구 56쌍(dev)을 보며 정했다. 작성자 한 사람의 선호이며 독립 라벨러는 없다(evals/quality_signals.py 참조).
- 글자 수 규격(ASSUMED_LIMITS)은 이 프로젝트가 가정한 값이다 — 실제 토스애즈 콘솔 규격과 다를 수 있다.
- 형태소 분석기·사전 없이 정규식과 단어 목록으로 읽는다. 한국어 어미·한자어·명사 나열을 정확히 가리지 못하고, 목록에 없는 표현은 보지 못한다.

결정론·순수 함수다 — 같은 입력이면 같은 출력이고, 난수·시계·네트워크·파일을 쓰지 않는다. 빈 문구·아주 긴 문구·이모지·영어만 있는 문구에도 예외 없이 돌아가며,
계산할 수 없는 신호는 점수를 None 으로 두고 종합에서 뺀 뒤 그 사실을 `excluded` 에 남긴다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from core.schemas import AdCopy, Fact, ProductFacts, Severity
from guardrails.grounding import condition_issues, extract_quantities, referenced_facts
from guardrails.lexical import scan

# ---------------------------------------------------------------------------
# 상수 — 모두 이 프로젝트가 가정하거나 정한 값이다
# ---------------------------------------------------------------------------
MAX_FIELD_CHARS = 1_000     # 신호 계산에 쓰는 글자 수 상한(필드별). 이보다 긴 글은 앞부분만 읽고 notes 에 알린다. 길이 신호는 잘리기 전 글자 수를 쓴다


@dataclass(frozen=True)
class PlatformLimits:
    """소재 글자 수 규격. 이 프로젝트가 가정한 값이다 — 실제 토스애즈 콘솔 규격과 다를 수 있다.

    *_max 는 policy.yaml 의 length_limits(심사의 글자 수 규칙)와 같은 값이고, *_best 는 이 프로젝트가 '읽기 좋다' 고 가정한 권장 구간이다(근거 있는 규격이 아님).
    """

    headline_max: int = 30
    body_max: int = 90
    cta_max: int = 12
    headline_best: tuple[int, int] = (8, 24)
    body_best: tuple[int, int] = (20, 70)
    cta_best: tuple[int, int] = (3, 8)


ASSUMED_LIMITS = PlatformLimits()

# 신호 키 — 표시·가중치·평가가 모두 이 순서와 이름을 쓴다
SIGNAL_KEYS = ("length", "specificity", "conditions", "cta", "readability", "redundancy", "pressure", "tone")
SIGNAL_NAME = {
    "length": "길이", "specificity": "구체성", "conditions": "조건 표시", "cta": "행동 유도",
    "readability": "읽기 쉬움", "redundancy": "중복·군더더기", "pressure": "압박·과장 표현", "tone": "말투 일관성",
}
# 종합 가중치(합 1). dev 쌍을 보며 정했고, 동결한 뒤 holdout 결과를 보고 바꾸지 않았다.
WEIGHTS: dict[str, float] = {
    "length": 0.08, "specificity": 0.24, "conditions": 0.14, "cta": 0.10,
    "readability": 0.14, "redundancy": 0.10, "pressure": 0.12, "tone": 0.08,
}
GRADE_BANDS = ((0.70, "상"), (0.50, "중"), (0.0, "하"))     # 점수 구간으로만 정한다. 임계값은 표시용 띠이며 합격선이 아니다
TIE_MARGIN = 0.05                                          # 두 문구의 종합 점수 차가 이보다 작으면 '비슷함'

SIGNAL_MEASURES = {
    "length": "제목·본문·버튼의 글자 수가 가정한 규격과 권장 구간에 맞는 정도",
    "specificity": "상품 정보의 숫자·낱말을 문구가 담은 비율과 막연한 수식어의 비율",
    "conditions": "상품 정보의 조건(최소 금액·기한·횟수·자동결제·심사·제외, 코드가 비교하는 6종류)을 문구가 같은 값으로 담은 정도",
    "cta": "버튼 문구가 구체적인 행동(신청하기·확인하기·받기)을 말하는 정도 — 정해 둔 목록 기준 부분 점수",
    "readability": "한 문장의 길이, 숫자·기호 밀도, 영문·한자·외래어 비율, 명사만 이어지는 구간",
    "redundancy": "제목과 본문의 낱말 겹침, 같은 낱말의 되풀이, 힘주는 군더더기 말",
    "pressure": "압박·과장 표현이 글자 수 대비 얼마나 많은지 (점수가 높을수록 적음)",
    "tone": "합쇼체·해요체·반말이 한 소재 안에서 섞였는지",
}
SIGNAL_NOT_MEASURE = {
    "length": "실제 콘솔 규격과의 일치(가정한 값입니다)와 내용이 좋은지",
    "specificity": "담은 숫자가 사실인지(근거 확인은 심사가 합니다)와 문구가 설득력 있는지",
    "conditions": "그 6종류 밖의 조건(예: 신규 고객 한정)과 조건이 눈에 띄게 적혔는지",
    "cta": "버튼의 위치·색·눌리는 정도, 목록에 없는 문구의 좋고 나쁨",
    "readability": "독자가 실제로 이해하는지, 사전 없이는 가릴 수 없는 한자어와 명사 나열의 정확한 구분",
    "redundancy": "일부러 한 반복(강조·운율)의 효과",
    "pressure": "정책 위반 여부(위반은 심사가 판단합니다) — 이 신호는 어조를 보는 참고용입니다",
    "tone": "말투가 상품에 어울리는지, 종결 어미가 없는 명사형 문구의 말투",
}

# 막연한 수식어 — 무엇이 어떻게 좋은지 말하지 않고 분위기만 만드는 말
_VAGUE_WORDS = ("최고", "최상", "최적", "놀라운", "놀랍", "특별한", "특별", "다양한", "풍성", "가득", "풍부한", "새로운", "편리한", "간편한", "똑똑한", "스마트한", "프리미엄",
                "만나보세요", "만나 보세요", "경험해 보세요", "경험해보세요", "누려보세요", "누려 보세요", "제대로", "확실하게", "든든한", "설레는", "행복", "혁신적", "압도적", "획기적")
_VAGUE_RE = re.compile("|".join((r"최고(?!\s*(?:연|월)?\s*\d)" if w == "최고" else re.escape(w)) for w in sorted(_VAGUE_WORDS, key=len, reverse=True)))   # '연 최고 4.0%' 같은 수치 범위 표기의 '최고' 는 막연한 말이 아니다
_GENERIC_WORDS = frozenset({"혜택", "서비스", "상품", "이용", "가능", "제공", "고객", "지금", "바로", "신청", "확인", "조건", "최대", "최소", "기준", "경우", "통해", "위해", "대한", "모든",
                            "전체", "함께", "오늘", "여기", "저희", "우리", "누구", "언제", "어디", "무엇", "하는", "있는", "없는", "받을", "쓸수", "있어요", "합니다", "입니다", "해요"})
# 힘주기·군더더기 말
_FILLER_WORDS = ("정말", "진짜", "너무", "아주", "매우", "엄청", "굉장히", "완전", "그냥", "무척")
# 일상 말로 바꿀 수 있는 외래어·업계 용어 (한자어 전체를 가리는 사전이 없어 눈에 띄는 것만 목록으로 둔다)
_JARGON = ("프로모션", "인센티브", "리워드", "어드밴티지", "솔루션", "얼리버드", "시스템", "플랜", "엔진", "프레임워크", "컨시어지", "니즈", "퍼포먼스", "프리미엄", "스테이", "트래킹", "커머스")
# 압박·과장 표현 사전에 쓰는 정책 카탈로그 규칙 — 표현의 성격(최상급·긴급성·공포·특수문자·사행심)이 압박·과장인 규칙만. 법적 사실 규칙(원금 보장·고지 등)은 심사의 몫이라 뺐다
EXAGGERATION_RULE_IDS = frozenset({"TA-SUPERLATIVE-01", "TA-URGENCY-01", "TA-FEAR-01", "TA-SPECIAL-01", "TA-GAMBLING-01"})
# 정책 규칙에는 없지만 어조를 들뜨게 하는 말(경고 수준). '최고' 는 '연 최고 4.0%' 같은 수치 범위 표기를 뺀다
_MILD_PRESSURE = re.compile(
    r"최고(?!\s*(?:연|월)?\s*\d)|최상|놀라운|놀랍|믿기\s*힘든|믿기지\s*않|역대급|초특가|파격|폭탄|혁신적|혁명|압도적|획기적|엄청난|어마어마|미친|서두르|놓치지\s*마|놓치면|절대|무조건|"
    r"지금\s*바로|당장|꼭\s*(?:받|확인|써|신청)")

# 행동 유도 — 버튼 문구 목록(상수). 구체적 행동 동사 어근과, 막연한 문구
_CTA_ACTIONS = ("신청", "가입", "구매", "예약", "설치", "다운로드", "등록", "상담", "주문", "개설", "예매", "응모", "참여", "구독", "충전", "담기", "확인", "비교", "조회", "시작", "체험",
                "받기", "받아", "받으세요", "받으러", "써 보기", "써보기", "써 보세요", "써보세요", "사용", "이용", "열기", "연결", "만들기", "예치", "납입", "문의")
_CTA_ACTION_RE = re.compile("|".join(re.escape(w) for w in sorted(_CTA_ACTIONS, key=len, reverse=True)))
_CTA_VAGUE_EXACT = frozenset({"자세히보기", "더보기", "바로가기", "보러가기", "클릭", "클릭하세요", "눌러보세요", "지금보기", "보기", "구경", "구경하기", "이동", "go", "click", "clickhere", "여기", "여기클릭", "링크"})
_CTA_EXPLORE_EXACT = frozenset({"알아보기", "더알아보기", "둘러보기", "살펴보기", "알아보러가기", "자세히알아보기", "더알아보러가기"})
_CTA_ADVERBS = frozenset({"지금", "바로", "당장", "오늘", "무료로", "간편하게", "쉽게", "빠르게"})

_WORD = re.compile(r"[가-힣]+|[A-Za-z]+|\d[\d,]*(?:\.\d+)?")
_HANGUL_WORD = re.compile(r"[가-힣]{2,}")
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
_HANGUL = re.compile(r"[가-힣]")
_LATIN = re.compile(r"[A-Za-z]")
_HANJA = re.compile(r"[一-鿿㐀-䶿]")
_SENT_SPLIT = re.compile(r"(?<=[.!?。！？])\s+|[\n\r]+")
_NUMBER_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")
_SYMBOL = re.compile(r"[^\w\s.,!?%·가-힣]")        # 괄호·슬래시·물결·대괄호 같은 기호 (%·가운뎃점·쉼표·마침표는 문장 안에서 흔해서 뺀다)
_PARTICLE_END = re.compile(r"(?:은|는|이|가|을|를|에|의|도|로|와|과|에서|으로|부터|까지|만|께|처럼|보다|라고|요|다|죠|까|네|고|며|면|니|지|서|해|세요|해요|돼요|니다)$")
_STRIP_PUNCT = ".,!?~…;:"

# 말투 — 종결 어미 (정규식, 단어 끝). 명사형은 어느 쪽도 아니다
_TONE_HAPSYO = re.compile(r"(?:니다|니까|십시오|시오|옵니다)$")
_TONE_HAEYO = re.compile(r"(?:(?<![필중수주개소긴강])요|죠)$")      # '필요'·'중요'·'수요' 같은 명사는 해요체가 아니다
_TONE_BANMAL = re.compile(r"(?:(?<![바마니])다|했어|해라|하자|해봐|잖아|이야|할래|해줘|(?:신청|가입|확인|시작|구매|예약|이용|다운로드|클릭|등록|설치|결제|주문)해|(?:하|받|눌러|사|써)라)$")   # 평어체('~다')·반말. '바다'·'마다' 같은 말은 뺀다


# ---------------------------------------------------------------------------
# 결과 형식
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Signal:
    key: str
    name: str
    value: Any                      # 실측치 (숫자 또는 숫자 사전). 계산할 수 없으면 None
    score: Optional[float]          # 0~1 (높을수록 좋음). 계산할 수 없으면 None — 종합에서 빠진다
    explanation: str                # 쉬운 한국어 한두 문장
    measures: str
    does_not_measure: str
    evidence: tuple[str, ...] = ()  # 점수의 근거가 된 문구 조각 (화면에는 HTML 이스케이프해서 보인다)

    def to_dict(self) -> dict:
        return {"key": self.key, "name": self.name, "value": self.value, "score": None if self.score is None else round(self.score, 4), "explanation": self.explanation,
                "measures": self.measures, "does_not_measure": self.does_not_measure, "evidence": list(self.evidence)}


@dataclass(frozen=True)
class QualityCard:
    signals: tuple[Signal, ...]
    overall: Optional[float]                  # 계산된 신호들의 가중 평균(0~1). 계산된 신호가 하나도 없으면 None
    grade: str                                # 상 | 중 | 하 | "" (종합이 없을 때)
    weights_used: dict                        # 종합에 실제로 쓴 가중치(제외한 신호는 빼고 다시 합 1)
    excluded: tuple[tuple[str, str], ...]     # (신호 키, 제외한 이유) — 종합에서 뺀 신호를 숨기지 않는다
    notes: tuple[str, ...] = ()

    def signal(self, key: str) -> Signal:
        for s in self.signals:
            if s.key == key:
                return s
        raise KeyError(key)

    def scores(self) -> dict[str, Optional[float]]:
        return {s.key: s.score for s in self.signals}

    def to_dict(self) -> dict:
        return {"overall": None if self.overall is None else round(self.overall, 4), "grade": self.grade, "weights_used": {k: round(v, 4) for k, v in self.weights_used.items()},
                "excluded": [list(e) for e in self.excluded], "notes": list(self.notes), "signals": [s.to_dict() for s in self.signals]}


@dataclass(frozen=True)
class Comparison:
    winner: str                               # "a" | "b" | "tie" | "unknown"(비교할 신호가 없음)
    score_a: Optional[float]
    score_b: Optional[float]
    diff: Optional[float]                     # a - b
    basis: tuple[str, ...]                    # 두 카드 모두에서 계산된 신호들 (비교는 이 신호들로만 한다)
    reasons: tuple[tuple[str, float], ...]    # (신호 키, 가중 점수 차: 양수면 a 쪽이 유리) — 차이가 큰 순서

    def to_dict(self) -> dict:
        return {"winner": self.winner, "score_a": None if self.score_a is None else round(self.score_a, 4), "score_b": None if self.score_b is None else round(self.score_b, 4),
                "diff": None if self.diff is None else round(self.diff, 4), "basis": list(self.basis), "reasons": [[k, round(v, 4)] for k, v in self.reasons]}


# ---------------------------------------------------------------------------
# 작은 도구
# ---------------------------------------------------------------------------
def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def _down(x: float, ok: float, bad: float) -> float:
    """x 가 ok 이하면 1, bad 이상이면 0, 사이는 직선으로 내려간다 (작을수록 좋은 값을 0~1 점수로)."""
    if bad <= ok:
        return 1.0 if x <= ok else 0.0
    return _clamp((bad - x) / (bad - ok))


def _clip(text: Any) -> str:
    return str(text or "").strip()[:MAX_FIELD_CHARS]


def _norm_cats(categories: Optional[Iterable[Any]]) -> set[str]:
    return {str(getattr(c, "value", c)) for c in (categories or ())}


def _words(text: str) -> list[str]:
    return _WORD.findall(text)


def _stem(word: str) -> str:
    """겹침 비교용 어근 — 한글은 앞 두 글자(조사·어미를 떼려는 거친 근사), 영문은 소문자, 숫자는 그대로."""
    if _HANGUL.match(word):
        return word[:2]
    return word.lower()


_GENERIC_STEMS = frozenset(_stem(g) for g in _GENERIC_WORDS)   # 겹침 비교에서 뺄 흔한 낱말의 어근


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text) if s and s.strip(" .!?。！？")]


def _fmt(x: float) -> str:
    return f"{x:.0%}"


# ---------------------------------------------------------------------------
# 신호 1 — 길이
# ---------------------------------------------------------------------------
def _field_length_score(n: int, best: tuple[int, int], mx: int) -> float:
    lo, hi = best
    if lo <= n <= hi:
        return 1.0
    if n < lo:
        return 0.3 + 0.7 * (n / lo)                 # 너무 짧으면 담을 정보가 적다
    if n <= mx:
        return 1.0 - 0.3 * (n - hi) / max(1, mx - hi)  # 권장 구간을 넘어 한도까지는 완만하게
    return max(0.0, 0.7 - 0.7 * (n - mx) / mx)       # 한도를 넘으면 가파르게 (한도의 두 배면 0)


def _length(copy: AdCopy, platform: PlatformLimits) -> Signal:
    spec = (("headline", "제목", copy.headline, platform.headline_best, platform.headline_max, 0.4),
            ("body", "본문", copy.body, platform.body_best, platform.body_max, 0.4),
            ("cta", "버튼", copy.cta, platform.cta_best, platform.cta_max, 0.2))
    value: dict[str, int] = {}
    parts: list[tuple[float, float]] = []
    notes: list[str] = []
    for key, label, text, best, mx, w in spec:
        n = len((text or "").strip())
        value[key] = n
        if n == 0:
            continue
        s = _field_length_score(n, best, mx)
        parts.append((s, w))
        if n > mx:
            notes.append(f"{label} {n}자는 가정한 상한 {mx}자를 넘어요")
        elif n > best[1]:
            notes.append(f"{label} {n}자는 권장 구간({best[0]}~{best[1]}자)보다 길어요")
        elif n < best[0]:
            notes.append(f"{label} {n}자는 권장 구간({best[0]}~{best[1]}자)보다 짧아 담긴 정보가 적을 수 있어요")
    if not parts:
        return Signal("length", SIGNAL_NAME["length"], None, None, "제목·본문·버튼이 모두 비어 있어 길이를 계산할 수 없어요.", SIGNAL_MEASURES["length"], SIGNAL_NOT_MEASURE["length"])
    score = sum(s * w for s, w in parts) / sum(w for _, w in parts)
    empty = [label for key, label, text, *_ in spec if not (text or "").strip()]
    head = f"제목 {value['headline']}자 · 본문 {value['body']}자 · 버튼 {value['cta']}자"
    expl = head + (" — 모두 권장 구간 안이에요." if not notes else " — " + ", ".join(notes) + ".")
    if empty:
        expl += f" ({', '.join(empty)}은(는) 비어 있어 계산에서 뺐어요. 길이 규격은 이 프로젝트가 가정한 값이에요.)"
    else:
        expl += " (길이 규격은 이 프로젝트가 가정한 값이에요.)"
    return Signal("length", SIGNAL_NAME["length"], value, score, expl, SIGNAL_MEASURES["length"], SIGNAL_NOT_MEASURE["length"])


# ---------------------------------------------------------------------------
# 신호 2 — 구체성
# ---------------------------------------------------------------------------
def _fact_vocabulary(facts: list[Fact]) -> list[str]:
    vocab: list[str] = []
    for f in facts:
        for w in _HANGUL_WORD.findall(_clip(f.text)) + _LATIN_WORD.findall(_clip(f.text)):
            if w not in _GENERIC_WORDS:
                vocab.append(w)
    return vocab


def _anchored(word: str, vocab_stems: set[str]) -> bool:
    return _stem(word) in vocab_stems


def _specificity(copy: AdCopy, facts: list[Fact]) -> Signal:
    name, meas, notm = SIGNAL_NAME["specificity"], SIGNAL_MEASURES["specificity"], SIGNAL_NOT_MEASURE["specificity"]
    text = _clip(copy.headline) + "\n" + _clip(copy.body)
    if not text.strip():
        return Signal("specificity", name, None, None, "제목과 본문이 비어 있어 구체성을 계산할 수 없어요.", meas, notm)
    if not [f for f in facts if _clip(f.text)]:
        return Signal("specificity", name, None, None, "비교할 상품 정보가 없어 문구가 상품 정보를 얼마나 담았는지 계산할 수 없어요.", meas, notm)
    vocab_stems = {_stem(w) for w in _fact_vocabulary(facts)}
    fact_q = [q for f in facts for q in extract_quantities(_clip(f.text))]
    quantities = extract_quantities(text)
    supported = [q for q in quantities if any(fq.supports(q) for fq in fact_q)]
    unsupported = [q for q in quantities if q not in supported]
    words = [w for w in _HANGUL_WORD.findall(text) + _LATIN_WORD.findall(text) if w not in _GENERIC_WORDS]
    anchored_words = [w for w in words if _anchored(w, vocab_stems)]
    content = len(words) + len(quantities)
    if content == 0:
        return Signal("specificity", name, None, None, "읽을 수 있는 낱말이나 숫자가 없어 구체성을 계산할 수 없어요.", meas, notm)
    anchored = len(anchored_words) + len(supported)
    vague = _VAGUE_RE.findall(text)
    ratio = anchored / content if content else 0.0
    base = 0.55 * min(1.0, ratio / 0.5) + 0.45 * min(1.0, anchored / 4)
    vague_ratio = len(vague) / content
    score = _clamp(base - min(0.4, 1.2 * vague_ratio) - min(0.3, 0.1 * len(unsupported)))
    value = {"anchored": anchored, "content": content, "anchored_ratio": round(ratio, 3), "numbers": len(quantities), "numbers_supported": len(supported), "vague": len(vague), "vague_ratio": round(vague_ratio, 3)}
    bits = [f"문구의 내용어 {content}개 중 상품 정보에서 온 것이 {anchored}개({_fmt(ratio)})"]
    if quantities:
        bits.append(f"숫자 {len(quantities)}개 중 상품 정보에 있는 것은 {len(supported)}개")
    if unsupported:
        bits.append("상품 정보에 없는 숫자(" + ", ".join(q.raw for q in unsupported[:4]) + ")는 구체성으로 치지 않아요")
    if vague:
        bits.append("막연한 말(" + ", ".join(dict.fromkeys(vague[:4])) + f")이 {len(vague)}번 나와요(내용어의 {_fmt(vague_ratio)})")
    evidence = tuple(dict.fromkeys([q.raw for q in supported[:4]] + anchored_words[:4]))
    return Signal("specificity", name, value, score, " · ".join(bits) + ".", meas, notm, evidence)


# ---------------------------------------------------------------------------
# 신호 3 — 조건 표시 (guardrails.grounding 의 공개 함수를 그대로 쓴다 — 비교 규칙을 새로 만들지 않는다)
# ---------------------------------------------------------------------------
_BLANK = AdCopy(headline="", body="", cta="")


def _conditions(copy: AdCopy, facts: list[Fact]) -> Signal:
    name, meas, notm = SIGNAL_NAME["conditions"], SIGNAL_MEASURES["conditions"], SIGNAL_NOT_MEASURE["conditions"]
    usable = [Fact(id=str(f.id), text=_clip(f.text)) for f in facts if _clip(f.text)]
    if not (copy.headline or copy.body or copy.cta):
        return Signal("conditions", name, None, None, "문구가 비어 있어 조건 표시를 계산할 수 없어요.", meas, notm)
    if not usable:
        return Signal("conditions", name, None, None, "비교할 상품 정보가 없어 조건 표시를 계산할 수 없어요.", meas, notm)
    # 문구가 '쓴' 사실: 숫자로 끌어다 쓴 사실 + 낱말이 겹치는 사실 (carried_fact_ids 는 grounding 이 '낱말이 겹칠 때만' 쓰는 입구다)
    product = ProductFacts(name="", facts=usable)
    clipped = copy.model_copy(update={"headline": _clip(copy.headline), "body": _clip(copy.body), "cta": _clip(copy.cta), "disclosures": [_clip(d) for d in copy.disclosures[:10]],
                                      "carried_fact_ids": [f.id for f in usable]})
    refs = referenced_facts(clipped, product)
    if not refs:
        return Signal("conditions", name, None, None, "문구가 상품 정보의 어느 항목도 쓰지 않아, 문구가 밝혀야 할 조건이 없어요.", meas, notm)
    # 빈 문구로 비교하면 '사실에 있는 모든 조건'이 빠짐으로 나온다 — 이것이 지켜야 할 조건의 전체 목록이다
    required = {(i.fact.id, i.kind): i.fact_raw for i in condition_issues(_BLANK, usable, refs) if i.code == "omitted"}
    issues = condition_issues(clipped, usable, refs)
    conflict_kinds = {i.kind for i in issues if i.code == "conflict"}
    for i in issues:
        if i.code == "conflict":
            required.setdefault((i.fact.id, i.kind), i.fact_raw)       # 문구가 틀린 값을 말한 조건도 지켜야 할 조건이다
    failing = {k for k in required if k[1] in conflict_kinds} | {(i.fact.id, i.kind) for i in issues if i.code == "omitted"}
    omitted = sorted(f"{kind}: {required[(fid, kind)]}" for fid, kind in failing if kind not in conflict_kinds and (fid, kind) in required)
    conflict = sorted({f"{i.kind}: 문구 '{i.ad_raw}' ↔ 상품 정보 '{i.fact_raw}'" for i in issues if i.code == "conflict"})
    if not required:
        return Signal("conditions", name, None, None, "문구가 쓴 상품 정보에 코드가 비교하는 조건(최소 금액·기한·횟수·자동결제·심사·제외)이 없어요.", meas, notm)
    stated = len(required) - len(failing)
    score = stated / len(required)
    value = {"required": len(required), "stated": stated, "omitted": omitted, "conflict": conflict}
    if not failing:
        expl = f"지켜야 할 조건 {len(required)}개를 모두 같은 값으로 담았어요."
    else:
        expl = f"지켜야 할 조건 {len(required)}개 중 {stated}개를 담았어요."
        if omitted:
            expl += " 빠진 조건 — " + "; ".join(omitted[:3]) + "."
        if conflict:
            expl += " 값이 다른 조건 — " + "; ".join(conflict[:2]) + "."
    return Signal("conditions", name, value, score, expl, meas, notm, tuple((omitted + conflict)[:4]))


# ---------------------------------------------------------------------------
# 신호 4 — 행동 유도
# ---------------------------------------------------------------------------
def _cta(copy: AdCopy) -> Signal:
    name, meas, notm = SIGNAL_NAME["cta"], SIGNAL_MEASURES["cta"], SIGNAL_NOT_MEASURE["cta"]
    raw = _clip(copy.cta)
    if not raw:
        return Signal("cta", name, None, None, "버튼 문구가 비어 있어 행동 유도를 계산할 수 없어요.", meas, notm)
    compact = re.sub(r"[\s.,!?~…]+", "", raw).lower()
    core = "".join(w for w in raw.split() if w.strip(_STRIP_PUNCT) not in _CTA_ADVERBS)     # 부사('지금'·'바로'…)는 행동의 대상이 아니다
    core = re.sub(r"[\s.,!?~…]+", "", core)
    core = re.sub(r"(?:하기|하세요|해요|해보세요|하러가기|하러가요|해주세요)$", "", core)
    m = _CTA_ACTION_RE.search(core)
    if compact in _CTA_VAGUE_EXACT:
        score, kind = 0.2, "막연한 문구(목록에 있음)"
    elif compact in _CTA_EXPLORE_EXACT:
        score, kind = 0.4, "둘러보라는 문구(목록에 있음)"
    elif m:
        score, kind = (1.0, "대상이 있는 구체적 행동") if len(core) - len(m.group(0)) >= 1 else (0.8, "행동은 분명하지만 무엇을 하는지는 말하지 않는 문구")
    else:
        score, kind = 0.5, "목록에 없는 문구(부분 점수)"
    return Signal("cta", name, {"cta": raw, "kind": kind}, score, f"버튼 '{raw}' — {kind}예요.", meas, notm, (raw,))


# ---------------------------------------------------------------------------
# 신호 5 — 가독성
# ---------------------------------------------------------------------------
def _noun_run(sentence: str) -> int:
    """조사·어미 없이 이어지는 낱말의 가장 긴 연속 길이 (명사 나열의 근사)."""
    best = run = 0
    for tok in sentence.split():
        t = tok.strip(_STRIP_PUNCT + "()[]\"'")
        if not t or not _WORD.search(t):
            continue
        if _PARTICLE_END.search(t):
            run = 0
        else:
            run += 1
            best = max(best, run)
    return best


def _readability(copy: AdCopy) -> Signal:
    name, meas, notm = SIGNAL_NAME["readability"], SIGNAL_MEASURES["readability"], SIGNAL_NOT_MEASURE["readability"]
    text = _clip(copy.headline) + "\n" + _clip(copy.body)
    letters = len(_HANGUL.findall(text)) + len(_LATIN.findall(text)) + len(_HANJA.findall(text))
    if letters == 0:
        return Signal("readability", name, None, None, "읽을 수 있는 글자(한글·영문)가 없어 가독성을 계산할 수 없어요.", meas, notm)
    sents = _sentences(text)
    lens = [len(s.strip(" .!?。！？")) for s in sents]
    avg_len = sum(lens) / len(lens)
    compact = re.sub(r"\s+", "", text)
    sym = len(_SYMBOL.findall(text)) / max(1, len(compact))
    per_sent_numbers = max((len(_NUMBER_TOKEN.findall(s)) for s in sents), default=0)
    latin_ratio = len(_LATIN.findall(text)) / letters
    hanja_ratio = len(_HANJA.findall(text)) / letters
    jargon = [w for w in _JARGON if w in text]
    chain = max((_noun_run(s) for s in sents), default=0)
    s_len = _down(avg_len, 24, 48)
    s_sym = 0.5 * _down(sym, 0.04, 0.15) + 0.5 * _down(per_sent_numbers, 3, 6)
    s_for = min(_down(latin_ratio, 0.10, 0.5), _down(hanja_ratio, 0.0, 0.08), _down(len(jargon), 0, 4))
    s_chain = _down(chain, 4, 8)
    score = 0.35 * s_len + 0.25 * s_sym + 0.20 * s_for + 0.20 * s_chain
    value = {"avg_sentence_chars": round(avg_len, 1), "symbol_density": round(sym, 3), "max_numbers_per_sentence": per_sent_numbers, "latin_ratio": round(latin_ratio, 3),
             "hanja_ratio": round(hanja_ratio, 3), "jargon": len(jargon), "longest_noun_run": chain}
    bits = [f"한 문장 평균 {avg_len:.0f}자"]
    if s_sym < 0.7:
        bits.append(f"기호가 많거나 한 문장에 숫자가 {per_sent_numbers}개 들어 있어요")
    if s_for < 0.7:
        bits.append("영문·한자·외래어 비율이 높아요" + (f"({', '.join(jargon[:3])})" if jargon else ""))
    if s_chain < 0.7:
        bits.append(f"조사 없이 낱말만 {chain}개 이어진 곳이 있어요")
    if len(bits) == 1:
        bits.append("기호·외래어·낱말 나열 없이 읽기 쉬워요")
    return Signal("readability", name, value, score, " · ".join(bits) + ".", meas, notm, tuple(jargon[:3]))


# ---------------------------------------------------------------------------
# 신호 6 — 중복·군더더기
# ---------------------------------------------------------------------------
def _redundancy(copy: AdCopy) -> Signal:
    name, meas, notm = SIGNAL_NAME["redundancy"], SIGNAL_MEASURES["redundancy"], SIGNAL_NOT_MEASURE["redundancy"]
    h, b, c = _clip(copy.headline), _clip(copy.body), _clip(copy.cta)

    def stems(text: str) -> list[str]:
        out = []
        for w in _words(text):
            if (_HANGUL.match(w) and len(w) >= 2) or w[0].isdigit() or (w.isascii() and len(w) >= 2):
                st = _stem(w)
                if st not in _GENERIC_STEMS:
                    out.append(st)
        return out

    sh, sb, sc = stems(h), stems(b), stems(c)
    allstems = sh + sb + sc
    if len(allstems) < 2:
        return Signal("redundancy", name, None, None, "문구가 너무 짧아 중복을 계산할 수 없어요.", meas, notm)
    parts: list[tuple[float, float]] = []
    overlap = None
    if sh and sb:
        a, bb = set(sh), set(sb)
        if a and bb:
            overlap = len(a & bb) / min(len(a), len(bb))
            parts.append((_down(overlap, 0.35, 0.85), 0.4))
    counts: dict[str, int] = {}
    for s in allstems:
        counts[s] = counts.get(s, 0) + 1
    repeated = sum(n - 1 for n in counts.values() if n > 1)
    rep_ratio = repeated / len(allstems)
    parts.append((_down(rep_ratio, 0.10, 0.40), 0.4))
    text = h + " " + b
    filler = [w for w in _FILLER_WORDS if w in text]
    filler_n = sum(text.count(w) for w in _FILLER_WORDS)
    parts.append((_down(filler_n / max(1, len(allstems)), 0.0, 0.25), 0.2))
    score = sum(s * w for s, w in parts) / sum(w for _, w in parts)
    value = {"headline_body_overlap": None if overlap is None else round(overlap, 3), "repeated_ratio": round(rep_ratio, 3), "filler": filler_n}
    bits = []
    if overlap is not None:
        bits.append(f"제목의 낱말 {_fmt(overlap)}가 본문에 다시 나와요")
    bits.append(f"같은 낱말이 되풀이된 비율 {_fmt(rep_ratio)}")
    if filler_n:
        bits.append("힘주는 말(" + ", ".join(filler) + ")이 있어요")
    rep_words = sorted((s for s, n in counts.items() if n > 1), key=lambda s: -counts[s])[:3]
    return Signal("redundancy", name, value, score, " · ".join(bits) + ".", meas, notm, tuple(rep_words))


# ---------------------------------------------------------------------------
# 신호 7 — 압박·과장 표현 밀도 (정책 위반 판정이 아니다)
# ---------------------------------------------------------------------------
def _pressure(copy: AdCopy, categories: set[str]) -> Signal:
    name, meas, notm = SIGNAL_NAME["pressure"], SIGNAL_MEASURES["pressure"], SIGNAL_NOT_MEASURE["pressure"]
    text = "\n".join(t for t in (_clip(copy.headline), _clip(copy.body), _clip(copy.cta)) if t)
    compact_len = len(re.sub(r"\s+", "", text))
    if compact_len == 0:
        return Signal("pressure", name, None, None, "문구가 비어 있어 압박·과장 표현을 계산할 수 없어요.", meas, notm)
    hits = [f for f in scan(text, categories) if f.rule_id in EXAGGERATION_RULE_IDS or f.severity == Severity.WARN]   # 표현이 압박·과장인 규칙의 적중 + 정책상 '주의' 수준(WARN) 적중. 등급은 읽지 않고 개수만 센다
    spans = [(f.start, f.end) for f in hits]
    quotes = [f.quote for f in hits]
    weighted = float(len(hits))
    for m in _MILD_PRESSURE.finditer(text):
        if any(s <= m.start() < e or s < m.end() <= e for s, e in spans):
            continue                                   # 정책 표현이 이미 잡은 자리는 한 번만 센다
        weighted += 0.5
        quotes.append(m.group(0))
    bangs = [m for m in re.finditer(r"[!！]", text) if not any(st <= m.start() < e for st, e in spans)]    # 느낌표는 하나까지는 보통이고, 정책 표현('!!')이 이미 잡은 자리는 빼고 센다
    weighted += 0.5 * max(0, len(bangs) - 1)
    density = weighted * 30 / compact_len
    score = 1.0 / (1.0 + 1.5 * density)
    value = {"weighted_hits": round(weighted, 2), "chars": compact_len, "per_30_chars": round(density, 3), "phrases": quotes[:6]}
    if not weighted:
        expl = "압박·과장으로 읽힐 표현이 없어요."
    else:
        expl = "압박·과장으로 읽힐 수 있는 표현 " + ", ".join(f"'{q}'" for q in dict.fromkeys(quotes[:4])) + f" — 글자 수 대비 밀도 {density:.2f}."
    expl += " 정책 위반인지는 심사가 따로 판단해요."
    return Signal("pressure", name, value, score, expl, meas, notm, tuple(dict.fromkeys(quotes[:4])))


# ---------------------------------------------------------------------------
# 신호 8 — 말투 일관성
# ---------------------------------------------------------------------------
def _tone_class(token: str) -> str:
    t = token.strip(_STRIP_PUNCT + "()[]\"'")
    if not t or not _HANGUL.search(t):
        return ""
    if _TONE_HAPSYO.search(t):
        return "합쇼체"
    if _TONE_HAEYO.search(t):
        return "해요체"
    if _TONE_BANMAL.search(t):
        return "반말"
    return ""


def _tone(copy: AdCopy) -> Signal:
    name, meas, notm = SIGNAL_NAME["tone"], SIGNAL_MEASURES["tone"], SIGNAL_NOT_MEASURE["tone"]
    text = "\n".join(t for t in (_clip(copy.headline), _clip(copy.body), _clip(copy.cta)) if t)
    if not _HANGUL.search(text):
        return Signal("tone", name, None, None, "한글 문구가 없어 말투를 계산할 수 없어요.", meas, notm)
    counts = {"합쇼체": 0, "해요체": 0, "반말": 0}
    examples: dict[str, list[str]] = {k: [] for k in counts}
    for tok in text.split():
        cls = _tone_class(tok)
        if cls:
            counts[cls] += 1
            examples[cls].append(tok.strip(_STRIP_PUNCT))
    present = [k for k, n in counts.items() if n]
    total = sum(counts.values())
    if len(present) <= 1:
        score = 1.0
        expl = (f"{present[0]}로 일관돼요." + (" (일관성만 본 점수예요. 반말을 써도 되는지는 심사가 따로 봐요.)" if present[0] == "반말" else "") if present else "종결 어미가 없는 명사형 문구라 섞일 말투가 없어요.")
    else:
        minority = (total - max(counts.values())) / total
        score = 0.15 if "반말" in present else 0.6 - 0.2 * min(1.0, 2 * minority)
        expl = "말투가 섞여 있어요 — " + ", ".join(f"{k} {counts[k]}곳({', '.join(examples[k][:2])})" for k in present) + "."
    ev = tuple(w for k in present for w in examples[k][:2]) if len(present) > 1 else ()
    return Signal("tone", name, dict(counts), score, expl, meas, notm, ev)


# ---------------------------------------------------------------------------
# 종합
# ---------------------------------------------------------------------------
def overall_score(signals: Iterable[Signal], *, exclude: Iterable[str] = (), weights: Optional[dict[str, float]] = None) -> tuple[Optional[float], dict[str, float]]:
    """계산된 신호들의 가중 평균. 점수가 없는(None) 신호와 exclude 에 든 신호는 빼고 가중치를 다시 합 1 로 맞춘다. 쓸 신호가 없으면 (None, {})."""
    w = WEIGHTS if weights is None else weights
    skip = set(exclude)
    usable = [(s.key, s.score) for s in signals if s.score is not None and s.key not in skip and w.get(s.key, 0.0) > 0]
    total = sum(w[k] for k, _ in usable)
    if not usable or total <= 0:
        return None, {}
    used = {k: w[k] / total for k, _ in usable}
    return sum(sc * used[k] for k, sc in usable), used


def grade_of(overall: Optional[float]) -> str:
    if overall is None:
        return ""
    for floor, name in GRADE_BANDS:
        if overall >= floor:
            return name
    return GRADE_BANDS[-1][1]


def quality_card(copy: AdCopy, facts: list[Fact], categories: set[str], *, platform: PlatformLimits = ASSUMED_LIMITS) -> QualityCard:
    """광고안 하나의 소재 퀄리티 카드 — 신호 8개와 종합. 정책 판정과 무관하다(모듈 설명 참조)."""
    facts = [f for f in (facts or []) if f is not None]
    cats = _norm_cats(categories)
    signals = (_length(copy, platform), _specificity(copy, facts), _conditions(copy, facts), _cta(copy),
               _readability(copy), _redundancy(copy), _pressure(copy, cats), _tone(copy))
    overall, used = overall_score(signals)
    excluded = tuple((s.key, s.explanation) for s in signals if s.score is None)
    notes: list[str] = []
    if any(len((t or "").strip()) > MAX_FIELD_CHARS for t in (copy.headline, copy.body, copy.cta)):
        notes.append(f"{MAX_FIELD_CHARS:,}자를 넘는 글은 앞부분만 읽고 계산했어요.")
    return QualityCard(signals, overall, grade_of(overall), used, excluded, tuple(notes))


def compare_cards(a: QualityCard, b: QualityCard, *, exclude: Iterable[str] = (), margin: float = TIE_MARGIN, weights: Optional[dict[str, float]] = None) -> Comparison:
    """두 카드를 같은 기준으로 비교한다 — 두 카드 모두에서 계산된 신호만 쓰고 가중치를 다시 합 1 로 맞춘다
    (한쪽에서만 계산된 신호를 넣으면 신호가 많은 쪽이 달리 평가되어 공정하지 않다). 점수 차가 margin 보다 작으면 'tie'."""
    skip = set(exclude)
    common = [k for k in SIGNAL_KEYS if a.signal(k).score is not None and b.signal(k).score is not None and k not in skip]
    w = WEIGHTS if weights is None else weights
    total = sum(w.get(k, 0.0) for k in common)
    if not common or total <= 0:
        return Comparison("unknown", None, None, None, (), ())
    sa = sum(a.signal(k).score * w.get(k, 0.0) for k in common) / total          # type: ignore[operator]
    sb = sum(b.signal(k).score * w.get(k, 0.0) for k in common) / total          # type: ignore[operator]
    diff = sa - sb
    winner = "tie" if abs(diff) < margin else ("a" if diff > 0 else "b")
    contrib = sorted(((k, (a.signal(k).score - b.signal(k).score) * w.get(k, 0.0) / total) for k in common), key=lambda x: -abs(x[1]))  # type: ignore[operator]
    return Comparison(winner, sa, sb, diff, tuple(common), tuple(c for c in contrib if abs(c[1]) > 1e-9))


def compare_signal(a: QualityCard, b: QualityCard, key: str, *, margin: float = TIE_MARGIN) -> Optional[str]:
    """신호 하나만으로 본 승자 'a' | 'b' | 'tie'. 한쪽이라도 계산하지 못했으면 None(의견 없음)."""
    sa, sb = a.signal(key).score, b.signal(key).score
    if sa is None or sb is None:
        return None
    return "tie" if abs(sa - sb) < margin else ("a" if sa > sb else "b")
