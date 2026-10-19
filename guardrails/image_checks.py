"""이미지 소재의 결정론 규칙 — 규격·글자 대비·글자 넘침·고지 가림, 그리고 **이미지 속 글자가 문구 규칙에 걸리는지**.

핵심은 '이미지 속 글자는 문구 심사를 우회하는 통로' 라는 점이다: 배너 안에 '원금 보장' 을 그림으로 넣으면 문구 검사가 보지 못한다.
그래서 이미지에서 읽은 글자에 **같은 규칙**(guardrails.lexical · injection · grounding · disclosure)을 그대로 적용한다 — 규칙을 다시 쓰지 않고 그 모듈을 부른다.
위치(start·end)는 이미지 글(덩어리를 줄바꿈으로 이은 글) 기준이며 광고 문구의 위치가 아니다.

규칙 ID 는 `IMG-…` 이고 policy.yaml 은 건드리지 않는다. 여기 규칙은 모두 **이 프로젝트가 정한 규칙**이며 실제 플랫폼 규정이나 법률 해석이 아니다.
이 모듈의 판정(decide_image)은 모델이 한 말을 믿지 않는다: 모델이 '봤다' 고 한 시각 요소 태그는 코드로 검증할 수 없으므로 게재 불가 근거로 쓰지 않고 담당자 확인으로만 낸다.

이 모듈이 의미하지 않는 것: 이미지에 문제가 없다는 보증이 아니다. 읽지 못한 글자·그림 속 의미는 검사하지 못했다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from core.banner import (AA_RATIO, ASSUMED_BANNER_SIZES, Banner, contrast_ratio, min_font_px, parse_color, score as banner_score)
from core.images import ImageAsset, svg_scene
from core.schemas import AdCopy, Decision, Fact, Finding, Layer, ProductCategory, ProductFacts, Severity
from guardrails import disclosure, grounding, injection
from guardrails.lexical import scan
from guardrails.normalize import loose_text
from guardrails.policy import PolicyCatalog, get_catalog

# --- 이 프로젝트가 가정한 값 (실제 광고 콘솔의 업로드 규격이 아니다) -------------------------------------------
ASSUMED_MIN_IMAGE_WIDTH_PX = 480
ASSUMED_RECOMMENDED_MAX_BYTES = 1_000_000
ASSUMED_RATIO_TOLERANCE = 0.08                 # 가정한 배너 비율과 8% 안쪽이면 '권장 비율' 로 본다
MISMATCH_COVERAGE = 0.75                       # 읽은 글 덩어리의 글자 중 선언한 글에서 찾은 비율이 이보다 낮으면 '선언에 없는 문구'
DECLARED_COVERAGE_OK = 0.70                    # 선언한 글의 글자 중 읽은 글에서 찾은 비율이 이보다 낮으면 '대조가 약함'
MIN_COMPARE_CHARS = 3                          # 이보다 짧은 덩어리(기호·한두 글자)는 대조하지 않는다
GROUNDING_RULES_FOR_IMAGE = {"GRND-NUM-01", "GRND-COND-02"}   # 조건 생략(GRND-COND-01)은 이미지 글만으로 판단하지 않는다 — 조건이 광고의 다른 글에 있을 수 있다


@dataclass(frozen=True)
class ImageRule:
    id: str
    title: str
    severity: Severity
    description: str
    needs_human: bool = False          # True 면 이 규칙에 걸린 이미지는 (게재 불가가 아니라면) 담당자 확인으로 간다
    project_rule: bool = True          # 이 프로젝트가 정한 규칙이며 실제 플랫폼 규정이 아니다


IMAGE_RULES: dict[str, ImageRule] = {r.id: r for r in (
    ImageRule("IMG-SPEC-01", "이미지 규격·용량 참고", Severity.WARN,
              "가정한 배너 규격(ASSUMED_BANNER_SIZES)·최소 가로 폭·권장 용량과 다름. 실제 콘솔 규격을 확인하지 못했으므로 참고로만 낸다."),
    ImageRule("IMG-CONTRAST-01", "글자와 바탕의 대비 부족", Severity.FIX,
              "글자색과 바탕색의 WCAG 2.x 대비가 4.5:1 미만. 이 프로젝트는 제목·버튼을 포함해 모든 글자에 이 기준을 적용한다."),
    ImageRule("IMG-FIT-01", "글자가 칸을 넘치거나 너무 작음", Severity.FIX,
              "글이 그림 칸에 들어가지 않아 잘려 보이거나, 최소 글자 크기(폭 비율)보다 작음."),
    ImageRule("IMG-DISC-01", "필수 안내 문구가 가려졌을 수 있음", Severity.WARN,
              "배너에서 안내 문구가 잘리거나 보이지 않거나 이미지 속 글에 없음. 안내가 문구란에 따로 있다면 문제가 아닐 수 있어 참고로만 낸다."),
    ImageRule("IMG-TEXT-01", "이미지 속 글이 문구 규칙에 걸림", Severity.FIX,
              "이미지에서 읽은 글에 같은 문구 규칙(표현·심사 조작·숫자 근거)을 적용해 걸린 것. 심각도는 걸린 원래 규칙의 것을 따른다."),
    ImageRule("IMG-MISMATCH-01", "선언한 글과 이미지 속 글이 다름", Severity.WARN,
              "읽은 글에 사용자가 적은 글에 없는 문구가 더 있음 — 심사를 피하려고 이미지에만 넣은 문구일 수 있다. 읽기 오류일 수도 있어 담당자 확인으로 낸다.",
              needs_human=True),
    ImageRule("IMG-READ-01", "이미지를 다 읽지 못함", Severity.WARN,
              "이미지 속 글을 읽지 못했거나 일부(도형으로 그린 글자·SVG 안의 그림)를 읽지 못함. 읽지 못한 이미지는 통과로 처리하지 않는다.", needs_human=True),
    *(ImageRule(rid, f"모델이 본 시각 요소: {label}", Severity.WARN,
                f"모델이 이미지에서 '{label}' 을(를) 봤다고 함. 모델이 본 것은 코드로 검증할 수 없어 게재 불가 근거로 쓰지 않고 담당자 확인으로만 낸다.", needs_human=True)
      for rid, label in (("IMG-VISUAL-01", "현금·돈다발"), ("IMG-VISUAL-02", "유명인 얼굴"), ("IMG-VISUAL-03", "전후 비교"), ("IMG-VISUAL-04", "미성년자로 보이는 사람"),
                         ("IMG-VISUAL-05", "다른 회사 로고"), ("IMG-VISUAL-06", "공공기관·금융기관 로고"), ("IMG-VISUAL-07", "QR 코드"), ("IMG-VISUAL-08", "카운트다운·마감 타이머"),
                         ("IMG-VISUAL-09", "의료·신체 이미지"), ("IMG-VISUAL-10", "글자가 너무 많음"))),
)}


@dataclass(frozen=True)
class TagRule:
    tag: str
    rule_id: str
    label: str
    why: str                    # 담당자가 무엇을 확인해야 하는지 (쉬운 말)


# 시각 요소 태그 → 규칙 표. 이 표는 이 프로젝트의 가정이다: 어떤 요소가 어떤 확인이 필요하다고 보는지는 실제 플랫폼 정책이 아니라 이 프로젝트가 정했다.
# 모든 태그는 WARN + 담당자 확인으로만 나간다 (모델이 본 것을 코드가 검증할 수 없기 때문).
IMAGE_TAG_RULES: dict[str, TagRule] = {t.tag: t for t in (
    TagRule("money_cash", "IMG-VISUAL-01", "현금·돈다발", "대출·투자 광고에서 과장된 이익을 떠올리게 하는 그림인지 확인하세요."),
    TagRule("celebrity_face", "IMG-VISUAL-02", "유명인 얼굴", "초상권과 유명인 추천(보증) 광고에 해당하는지 확인하세요."),
    TagRule("before_after", "IMG-VISUAL-03", "전후 비교", "효과를 과장하는 전후 비교인지, 근거가 있는지 확인하세요."),
    TagRule("minor_person", "IMG-VISUAL-04", "미성년자로 보이는 사람", "업종(금융·사행성 등)에 미성년자가 나와도 되는지 확인하세요."),
    TagRule("competitor_logo", "IMG-VISUAL-05", "다른 회사 로고", "다른 회사의 상표를 허락 없이 쓰거나 비교하고 있지 않은지 확인하세요."),
    TagRule("government_or_bank_logo", "IMG-VISUAL-06", "공공기관·금융기관 로고", "공식 기관의 상품이나 보증으로 오해하게 만드는지 확인하세요."),
    TagRule("qr_code", "IMG-VISUAL-07", "QR 코드", "QR 코드가 어디로 연결되는지 이미지만으로는 알 수 없어 확인이 필요합니다."),
    TagRule("countdown_timer", "IMG-VISUAL-08", "카운트다운·마감 타이머", "근거 없는 마감 압박으로 서두르게 하는 표현인지 확인하세요."),
    TagRule("medical_imagery", "IMG-VISUAL-09", "의료·신체 이미지", "의료 효능을 암시하는 그림인지 확인하세요."),
    TagRule("excessive_text", "IMG-VISUAL-10", "글자가 너무 많음", "글자가 많아 작은 글씨 속에 조건이 숨어 있지 않은지 확인하세요."),
)}


class ImageFinding(Finding):
    """Finding 에 이미지 검사의 부가 정보를 더한다. layer 는 언제나 Layer.IMAGE."""

    source_rule_id: str = ""     # IMG-TEXT-01 일 때 걸린 원래 규칙(상세 기록용)
    segment: int = -1            # 이미지 글 덩어리 번호
    text_source: str = ""        # svg | model | declared


def rule_of(rule_id: str) -> Optional[ImageRule]:
    return IMAGE_RULES.get(rule_id)


def rule_title(rule_id: str, catalog: Optional[PolicyCatalog] = None) -> str:
    r = IMAGE_RULES.get(rule_id)
    if r:
        return r.title
    c = (catalog or get_catalog()).get(rule_id)
    return c.title if c else rule_id


def finding_sentence(f: Finding, catalog: Optional[PolicyCatalog] = None) -> str:
    """내부 코드 없이 읽히는 한 문장 (화면용)."""
    title = rule_title(f.rule_id, catalog)
    expl = (f.explanation or "").strip()
    body = title if (not expl or expl == title) else f"{title} — {expl}"
    if f.quote:
        body = f"“{f.quote}” · {body}"
    return body


def _finding(rule_id: str, *, severity: Optional[Severity] = None, quote: str = "", start: int = -1, end: int = -1, explanation: str = "",
             source_rule_id: str = "", segment: int = -1, text_source: str = "") -> ImageFinding:
    rule = IMAGE_RULES[rule_id]
    return ImageFinding(rule_id=rule_id, severity=severity or rule.severity, layer=Layer.IMAGE, quote=quote, start=start, end=end, explanation=explanation,
                        verified_quote=True, source_rule_id=source_rule_id, segment=segment, text_source=text_source)


# ---------------------------------------------------------------------------
# 규격 · 배너 · SVG 배치
# ---------------------------------------------------------------------------
def check_spec(asset: ImageAsset) -> list[Finding]:
    """IMG-SPEC-01 — 가정한 규격과 다르면 참고(WARN)로만 낸다 (실제 규격을 모르므로 막지 않는다)."""
    out: list[Finding] = []
    if asset.width < ASSUMED_MIN_IMAGE_WIDTH_PX:
        out.append(_finding("IMG-SPEC-01", explanation=f"가로 {asset.width}px 로 작습니다 (이 프로젝트가 가정한 최소 {ASSUMED_MIN_IMAGE_WIDTH_PX}px). 화면에서 흐려 보일 수 있습니다."))
    ratio = asset.width / max(1, asset.height)
    near = [s for s in ASSUMED_BANNER_SIZES.values() if abs(ratio / s.ratio - 1) <= ASSUMED_RATIO_TOLERANCE]
    if not near:
        names = ", ".join(f"{s.label} {s.width}×{s.height}" for s in ASSUMED_BANNER_SIZES.values())
        out.append(_finding("IMG-SPEC-01", explanation=f"가로세로 비율 {ratio:.2f}:1 이 이 프로젝트가 가정한 배너 비율({names})과 다릅니다. 실제 콘솔 규격은 확인하지 못했습니다."))
    if asset.kind == "raster" and asset.bytes_len > ASSUMED_RECOMMENDED_MAX_BYTES:
        out.append(_finding("IMG-SPEC-01", explanation=f"용량이 {asset.bytes_len / 1_000_000:.1f}MB 로 큽니다 (이 프로젝트가 가정한 권장 {ASSUMED_RECOMMENDED_MAX_BYTES / 1_000_000:.0f}MB 이하). 실제 콘솔 한도는 확인하지 못했습니다."))
    return out


def check_banner(banner: Banner) -> list[Finding]:
    """compose 한 배너의 구성 문제를 규칙으로 낸다 (넘침·대비·작은 글자·고지). 점수 계산(core.banner.score)과 같은 근거를 쓴다."""
    sc = banner_score(banner)
    out: list[Finding] = []
    names = {"title": "제목", "body": "본문", "button": "버튼 글", "disclosure": "안내 문구"}
    for b in banner.blocks:
        if b.overflow:
            out.append(_finding("IMG-FIT-01", quote=b.text[:60], explanation=f"{names[b.role]}이(가) 칸에 들어가지 않아 잘려 보일 수 있습니다 (글자를 가장 작게 줄여도 넘침).", text_source="svg"))
    for b in banner.blocks:
        try:
            r = contrast_ratio(b.fg, b.bg)
        except ValueError:
            continue
        if r < AA_RATIO:
            out.append(_finding("IMG-CONTRAST-01", quote=b.text[:60], explanation=f"{names[b.role]}의 글자와 바탕 대비가 {r:.1f}:1 로 기준 4.5:1 에 못 미칩니다.", text_source="svg"))
    if sc.flags.get("tiny_font"):
        out.append(_finding("IMG-FIT-01", explanation="글자 크기가 최소 기준(폭 비율)보다 작은 글이 있습니다."))
    if sc.flags.get("disclosure_hidden"):
        out.append(_finding("IMG-DISC-01", explanation="광고 문구에 필수 안내 문구가 있는데 배너에서 잘려 보이거나 읽기 어려울 수 있습니다. 문구란에 따로 들어가는 안내라면 괜찮을 수 있습니다."))
    return out


def check_svg_layout(asset: ImageAsset) -> list[Finding]:
    """올린 SVG 의 글자 배치 점검: 대비(글자색 대 아래 깔린 단색 네모, 없으면 흰 바탕 가정) · 그림 밖 글자 · 너무 작은 글자.
    색을 해석할 수 없는 글(그라데이션·상속·변형이 걸린 글)은 건너뛴다 — 확인하지 못한 것을 통과로 세지 않으며 '확인한 글 수' 는 알리지 않는다는 한계가 있다."""
    if asset.kind != "svg":
        return []
    scene = svg_scene(asset)
    out: list[Finding] = []
    W, H = scene.width, scene.height
    for t in scene.texts:
        if t.kind != "text" or t.transformed:
            continue
        if t.x is not None and t.y is not None and (t.x < 0 or t.y < 0 or t.x > W or t.y > H + (t.font_px or 0)):
            out.append(_finding("IMG-FIT-01", quote=t.text[:60], explanation="이 글이 그림 영역 밖에 놓여 있어 보이지 않을 수 있습니다.", text_source="svg"))
            continue
        if t.font_px and t.font_px < min_font_px(W, "disclosure") * 0.8:
            out.append(_finding("IMG-FIT-01", quote=t.text[:60], explanation=f"글자가 {t.font_px:.0f}px 로 너무 작습니다 (이 그림 폭 기준 최소 약 {min_font_px(W, 'disclosure') * 0.8:.0f}px).", text_source="svg"))
        fg = parse_color(t.fill) if t.fill else (0, 0, 0)       # fill 을 안 쓰면 SVG 기본값은 검정
        if fg is None or t.x is None or t.y is None:
            continue
        bg = (255, 255, 255)
        unknown = False
        py = t.y - 0.3 * (t.font_px or 16)
        for r in scene.rects:
            if r.order < t.order and r.x <= t.x <= r.x + r.w and r.y <= py <= r.y + r.h:
                c = parse_color(r.fill)
                if c is None:
                    unknown = True
                    break
                bg = c
        if unknown:
            continue
        ratio = contrast_ratio(fg, bg)
        if ratio < AA_RATIO:
            out.append(_finding("IMG-CONTRAST-01", quote=t.text[:60], explanation=f"글자와 바탕의 대비가 {ratio:.1f}:1 로 기준 4.5:1 에 못 미칩니다.", text_source="svg"))
    return out


# ---------------------------------------------------------------------------
# 이미지 속 글자에 같은 문구 규칙 적용
# ---------------------------------------------------------------------------
@dataclass
class TextCheck:
    findings: list = field(default_factory=list)
    text: str = ""                                  # 검사한 글 (덩어리를 줄바꿈으로 이은 것) — findings 의 start·end 는 이 글 기준
    spans: list = field(default_factory=list)       # 덩어리별 [start, end)
    flagged: set = field(default_factory=set)       # 지시문으로 보여 사용하지 않은 덩어리 번호
    skipped: list = field(default_factory=list)     # 하지 않은 검사 (쉬운 말)
    categories: set = field(default_factory=set)


def effective_categories(categories: Iterable[str], text: str, facts: Sequence[Fact] = (), catalog: Optional[PolicyCatalog] = None) -> set[str]:
    """선언 업종에 글의 단서로 넓힌 업종을 더한다 (파이프라인이 문구에 하는 것과 같은 방식)."""
    catalog = catalog or get_catalog()
    cats = {str(c.value if isinstance(c, ProductCategory) else c) for c in categories} or {"other"}
    fin = {"loan", "card", "insurance", "investment", "savings", "crypto"}
    if not (cats & fin):
        cats |= catalog.effective_categories(ProductCategory.OTHER, text, " ".join(f.text for f in facts))
    return cats


def _segment_of(spans: list[tuple[int, int]], pos: int) -> int:
    for i, (s, e) in enumerate(spans):
        if s <= pos < max(e, s + 1):
            return i
    return -1


def check_text(segments: Sequence[str], *, categories: Iterable[str] = (), facts: Optional[Sequence[Fact]] = None, catalog: Optional[PolicyCatalog] = None,
               disclosure_copy: Optional[AdCopy] = None, text_source: str = "") -> TextCheck:
    """이미지에서 읽은 글(덩어리 목록)에 문구와 같은 규칙을 적용한다.

    1) 덩어리마다 심사 조작·지시문(injection) 검사 — 걸리면 그 덩어리는 사용하지 않는다(다른 검사에서 빼고 표시).
    2) 나머지 글을 줄바꿈으로 이어 표현 규칙(lexical)·상품 정보 대조(grounding; 사실이 있을 때)·필수 안내(disclosure; 참고) 를 적용한다.
    위치는 이 이어 붙인 글 기준이다.
    """
    catalog = catalog or get_catalog()
    segs = [s or "" for s in segments]
    res = TextCheck()
    pos = 0
    for s in segs:
        res.spans.append((pos, pos + len(s)))
        pos += len(s) + 1
    res.text = "\n".join(segs)
    res.categories = effective_categories(categories, res.text, facts or (), catalog)
    out: list[Finding] = []

    # 1) 인젝션
    for i, s in enumerate(segs):
        inj = injection.scan_text(s, catalog)
        if not inj.detected:
            continue
        res.flagged.add(i)
        base = res.spans[i][0]
        for f in inj.findings:
            title = rule_title(f.rule_id, catalog)
            out.append(_finding("IMG-TEXT-01", severity=f.severity, quote=f.quote, start=(f.start + base) if f.start >= 0 else -1, end=(f.end + base) if f.end >= 0 else -1,
                                explanation=f"이미지 속 글이 심사를 조작하려는 지시문처럼 보여 사용하지 않았습니다 — {title}: {f.explanation}", source_rule_id=f.rule_id,
                                segment=i, text_source=text_source))
    masked = "\n".join((" " * len(s) if i in res.flagged else s) for i, s in enumerate(segs))   # 지시문 덩어리는 공백으로 가려 위치를 보존한 채 검사에서 뺀다

    # 2) 표현 규칙
    if masked.strip():
        for f in scan(masked, res.categories, catalog):
            title = rule_title(f.rule_id, catalog)
            out.append(_finding("IMG-TEXT-01", severity=f.severity, quote=f.quote, start=f.start, end=f.end, explanation=f"{title}: {f.explanation}",
                                source_rule_id=f.rule_id, segment=_segment_of(res.spans, f.start), text_source=text_source))
        # 3) 상품 정보 대조 — 사실이 있어야 한다. 없으면 숫자를 대조할 수 없으므로 하지 않고 그 사실을 알린다.
        if facts:
            product = ProductFacts(name="이미지 검사", category=ProductCategory.OTHER, facts=list(facts))
            grd = grounding.check(AdCopy(headline="", body=masked, cta=""), product, "", catalog)
            for f in grd.findings:
                if f.rule_id not in GROUNDING_RULES_FOR_IMAGE:
                    continue
                title = rule_title(f.rule_id, catalog)
                out.append(_finding("IMG-TEXT-01", severity=f.severity, quote=f.quote, start=f.start, end=f.end, explanation=f"{title}: {f.explanation}",
                                    source_rule_id=f.rule_id, segment=_segment_of(res.spans, f.start) if f.start >= 0 else -1, text_source=text_source))
            res.skipped.append("이미지 글만으로는 상품 정보의 조건이 빠졌는지는 판단하지 않았습니다 (조건이 광고의 다른 글에 있을 수 있습니다).")
        else:
            res.skipped.append("상품 정보가 없어 이미지 속 숫자·주장이 상품 정보와 맞는지는 확인하지 못했습니다.")
        # 4) 필수 안내 — 이 업종에 필요한 안내가 광고 전체(문구+이미지 글)에 없으면 참고로 낸다
        dc = disclosure_copy
        merged = AdCopy(headline=dc.headline if dc else "", body=((dc.body + "\n") if dc else "") + masked, cta=dc.cta if dc else "",
                        disclosures=list(dc.disclosures) if dc else [])
        d = disclosure.check(merged, res.categories, catalog)
        if d.missing:
            labels = ", ".join(i.label for i in d.missing)
            out.append(_finding("IMG-DISC-01", explanation=f"이 업종에 필요한 안내 문구({labels})가 이미지 속 글{'과 광고 문구' if dc else ''}에서 보이지 않습니다. 안내가 문구란에 따로 있다면 괜찮을 수 있습니다.",
                                text_source=text_source))
    res.findings = out
    return res


# ---------------------------------------------------------------------------
# 선언한 글 대조
# ---------------------------------------------------------------------------
def _squash(text: str) -> str:
    """대조용: 정규화한 뒤 글자·숫자만 남긴다 (공백·구두점·대소문자 차이를 무시)."""
    return "".join(ch for ch in loose_text(text).casefold() if ch.isalnum())


def _coverage(a: str, b: str) -> float:
    """a 의 글자 중 b 에서 (2글자 이상 이어서) 찾은 글자의 비율. 두 글자 묶음(bigram)이 b 에 있는지로 세므로 글자 수에 비례해 빠르다 —
    같은 글자가 반복되는 입력에서 시간이 제곱으로 느는 일반적인 문자열 비교(difflib)를 쓰지 않는다. 읽기 오류(한두 글자)는 견딘다."""
    if not a:
        return 1.0
    if not b:
        return 0.0
    if a in b:
        return 1.0
    if len(a) < 2:
        return 0.0
    grams = {b[i:i + 2] for i in range(len(b) - 1)}
    covered = [False] * len(a)
    for i in range(len(a) - 1):
        if a[i:i + 2] in grams:
            covered[i] = covered[i + 1] = True
    return sum(covered) / len(a)


@dataclass
class MismatchResult:
    compared: bool = False
    extras: list = field(default_factory=list)       # 선언에 없는 문구가 든 덩어리 번호
    declared_coverage: float = 0.0
    findings: list = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        return self.compared and not self.extras and self.declared_coverage >= DECLARED_COVERAGE_OK


def compare_declared(declared: str, segments: Sequence[str], *, spans: Optional[Sequence[tuple[int, int]]] = None, skip: Iterable[int] = (),
                     text_source: str = "") -> MismatchResult:
    """사용자가 선언한 글과 이미지에서 읽은 글을 대조한다. 읽은 글에 선언에 없는 문구가 더 있으면 IMG-MISMATCH-01 (심사 우회 가능성)."""
    res = MismatchResult()
    d = _squash(declared)
    if not d:
        return res
    res.compared = True
    skip = set(skip)
    read_all = "".join(_squash(s) for i, s in enumerate(segments) if i not in skip)
    for i, s in enumerate(segments):
        if i in skip:
            continue
        sq = _squash(s)
        if len(sq) < MIN_COMPARE_CHARS:
            continue
        if _coverage(sq, d) < MISMATCH_COVERAGE:
            res.extras.append(i)
            st, en = (spans[i] if spans else (-1, -1))
            res.findings.append(_finding("IMG-MISMATCH-01", quote=s[:80], start=st, end=en, segment=i, text_source=text_source,
                                         explanation="이미지에는 이 문구가 있는데 직접 적어 주신 글에는 없습니다. 심사를 피하려고 이미지에만 넣은 문구일 수 있어 확인이 필요합니다."))
    res.declared_coverage = _coverage(d, read_all)
    return res


# ---------------------------------------------------------------------------
# 시각 태그 · 읽지 못함
# ---------------------------------------------------------------------------
def tag_findings(tags: Iterable[str]) -> list[Finding]:
    """모델이 봤다고 한 시각 요소 → 참고(WARN) + 담당자 확인. 어휘 밖의 태그는 버린다. 이 태그만으로는 절대 게재 불가·수정 필요가 되지 않는다."""
    out: list[Finding] = []
    seen: set[str] = set()
    for t in tags:
        tr = IMAGE_TAG_RULES.get(t)
        if tr is None or t in seen:
            continue
        seen.add(t)
        out.append(_finding(tr.rule_id, text_source="model",
                            explanation=f"모델이 이미지에서 '{tr.label}' 을(를) 봤다고 했습니다. 모델이 본 것은 코드로 확인할 수 없어 게재 불가 사유로 쓰지 않고 담당자 확인으로 넘깁니다. {tr.why}"))
    return out


def unread_finding(reason: str) -> Finding:
    return _finding("IMG-READ-01", explanation=reason)


# ---------------------------------------------------------------------------
# 판정 (순수 함수)
# ---------------------------------------------------------------------------
TRUST_DIRECT = "direct"                # 코드가 글자를 직접 읽음 (SVG <text>)
TRUST_CROSS = "cross_checked"          # 모델이 읽은 글과 사용자가 적은 글이 서로 맞음
TRUST_MODEL = "model_only"             # 모델이 읽은 글뿐 — 대조하지 않았다
TRUST_DECLARED = "declared_only"       # 사용자가 적은 글뿐 — 이미지를 읽지 않았다
TRUST_NONE = "none"                    # 읽은 글도 적은 글도 없다


@dataclass(frozen=True)
class ImageDecision:
    decision: Decision
    reasons: tuple


def decide_image(findings: Sequence[Finding], *, unreadable: bool = False, trust: str = TRUST_DIRECT, truncated: bool = False) -> ImageDecision:
    """이미지 하나의 판정. 위에서부터 먼저 맞는 것:
    1) 확인된 게재 불가(BLOCK) 근거가 있으면 REJECT — 읽은 글에서 걸린 것도 포함 (잘못 읽었더라도 안전한 쪽으로 틀린다).
    2) 읽지 못함·일부만 읽음·읽은 글이 대조되지 않음·담당자 확인이 필요한 규칙(모델이 본 시각 요소, 선언 불일치)이면 HUMAN_REVIEW — **통과가 아니다.**
    3) 수정 필요(FIX)가 있으면 REVISE.
    4) 그 외 APPROVE (코드가 직접 읽었거나, 모델이 읽은 글이 선언과 맞을 때만).
    모델이 본 시각 요소 태그는 WARN 이므로 1)·3) 을 만들지 못한다.
    """
    blocks = [f for f in findings if f.severity == Severity.BLOCK]
    human = [f for f in findings if (rule_of(f.rule_id) is not None and rule_of(f.rule_id).needs_human)]
    fixes = [f for f in findings if f.severity == Severity.FIX]
    reasons: list[str] = []
    if blocks:
        for f in blocks[:3]:
            reasons.append(finding_sentence(f))
        return ImageDecision(Decision.REJECT, tuple(reasons))
    if unreadable:
        reasons.append("이미지 속 글을 읽지 못해 통과로 처리하지 않습니다. 담당자가 이미지를 직접 확인해야 합니다.")
    elif trust == TRUST_MODEL:
        reasons.append("모델이 읽은 글에서 문제를 찾지 못했지만, 이미지 속에 적힌 글을 직접 적어 대조하지 않으면 모델이 빠뜨린 글자가 있는지 알 수 없어 통과로 확정하지 않습니다.")
    elif trust in (TRUST_DECLARED, TRUST_NONE):
        reasons.append("이미지에서 글을 읽은 것이 아니라서 통과로 처리하지 않습니다.")
    if truncated:
        reasons.append("읽은 글이 너무 길어 일부만 검사했습니다.")
    for f in human:
        reasons.append(f.explanation if f.rule_id == "IMG-READ-01" else finding_sentence(f))   # 읽지 못한 이유는 설명 그대로 (제목을 되풀이하지 않는다)
    if reasons:
        for f in fixes[:2]:
            reasons.append(finding_sentence(f))
        return ImageDecision(Decision.HUMAN_REVIEW, tuple(reasons))
    if fixes:
        return ImageDecision(Decision.REVISE, tuple(finding_sentence(f) for f in fixes[:3]))
    return ImageDecision(Decision.APPROVE, ("이 프로젝트의 검사 기준에서 이미지 속 글과 배치에 문제가 발견되지 않았습니다.",))
