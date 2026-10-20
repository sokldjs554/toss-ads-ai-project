"""배너 구성·점수(core/banner.py): WCAG 대비 계산, 글자 폭 추정·줄바꿈, 넘침 표시(조용히 자르지 않음), 점수 상한, 추천의 결정론·다양화.

점수는 읽기 쉬운 정도를 이 프로젝트가 정한 기준으로 센 값이다 — 광고 성과나 플랫폼 심사 결과가 아니다.
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET

import pytest

from core.banner import (AA_RATIO, ASSUMED_BANNER_SIZES, ASSUMED_MIN_FONT_PX, CAP_DISCLOSURE_HIDDEN, CAP_FIT_VIOLATION, CAP_LOW_CONTRAST, PALETTES, SCORE_WEIGHTS, TEMPLATES,
                         Palette, char_em, compose, contrast_ratio, estimate_width, min_font_px, parse_color, recommend, relative_luminance, score, verify_palettes, wrap_text)
from core.images import ImageAsset, load_image, svg_hazards
from core.schemas import AdCopy

SVG = "{http://www.w3.org/2000/svg}"
SHORT = AdCopy(headline="월 3만원부터 시작", body="자동이체로 편하게 모아 보세요", cta="지금 확인")
TYPICAL = AdCopy(headline="월 3만원부터 시작하는 생활비 통장", body="자동이체로 편하게 모아 보세요. 가입비와 유지 수수료가 없습니다.", cta="지금 확인",
                 disclosures=["금리는 가입 시점 기준이며 변동될 수 있습니다."])
MAXLEN = AdCopy(headline="가" * 30, body="나다라마 " * 18, cta="지금 신청하기하기", disclosures=["금리는 가입 시점 기준이며 변동될 수 있습니다. 상품 설명서를 확인하세요."])
HUGE = AdCopy(headline="아주 긴 제목 " * 12, body="끝없이 이어지는 본문 문장입니다 " * 120, cta="자세히 알아보기 지금 바로")


def svg_text(banner) -> str:
    root = ET.fromstring(banner.svg)
    return " ".join("".join(t.itertext()) for t in root.iter(SVG + "text"))


# --------------------------------------------------------------------- WCAG
def test_wcag_contrast_matches_hand_calculated_known_pairs():
    """손으로 계산한 알려진 쌍 (W3C WCAG 2.x 상대 휘도 식): 검정/흰색 21:1 · #767676 4.54:1 · #777777 4.48:1 · 빨강/흰색 4.00:1 · 파랑/흰색 8.59:1 · 같은 색 1:1."""
    assert contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21.0, abs=1e-9)
    assert contrast_ratio("#767676", "#FFFFFF") == pytest.approx(4.54, abs=0.005)     # 4.5 를 겨우 넘는 가장 밝은 회색
    assert contrast_ratio("#777777", "#FFFFFF") == pytest.approx(4.48, abs=0.005)     # 4.5 에 못 미친다
    assert contrast_ratio("#FF0000", "#FFFFFF") == pytest.approx(4.00, abs=0.005)
    assert contrast_ratio("#0000FF", "#FFFFFF") == pytest.approx(8.59, abs=0.005)
    assert contrast_ratio("#123456", "#123456") == pytest.approx(1.0)
    assert contrast_ratio("#FFFFFF", "#000000") == contrast_ratio("#000000", "#FFFFFF")      # 순서와 무관
    assert contrast_ratio("#767676", "#FFFFFF") >= AA_RATIO > contrast_ratio("#777777", "#FFFFFF")


def test_relative_luminance_endpoints_and_the_linearization_threshold():
    assert relative_luminance("#FFFFFF") == pytest.approx(1.0) and relative_luminance("#000000") == 0.0
    # 0.2126·R + 0.7152·G + 0.0722·B : 순수한 채널의 계수
    assert relative_luminance("#FF0000") == pytest.approx(0.2126, abs=1e-4)
    assert relative_luminance("#00FF00") == pytest.approx(0.7152, abs=1e-4)
    assert relative_luminance("#0000FF") == pytest.approx(0.0722, abs=1e-4)
    assert relative_luminance("#0A0A0A") == pytest.approx((10 / 255) / 12.92, abs=1e-6)     # 낮은 값은 선형 구간
    with pytest.raises(ValueError):
        relative_luminance("url(#grad)")


def test_parse_color_forms():
    assert parse_color("#fff") == (255, 255, 255) and parse_color("#2B5BD7") == (43, 91, 215) and parse_color("rgb(1, 2, 3)") == (1, 2, 3)
    assert parse_color("white") == (255, 255, 255) and parse_color("none") is None and parse_color("currentColor") is None and parse_color("url(#a)") is None
    assert parse_color("rgb(300,0,0)") is None


# --------------------------------------------------------------------- 크기·템플릿·팔레트
def test_presets_meet_the_minimums_and_are_labelled_as_assumptions():
    assert len(ASSUMED_BANNER_SIZES) >= 3 and len(TEMPLATES) >= 3 and len(PALETTES) >= 4
    import core.banner as banner_module

    src = open(banner_module.__file__, encoding="utf-8").read()
    assert "가정한 규격" in src and "실제 토스애즈 콘솔의 배너 규격과 다를 수 있다" in src       # 규격은 가정이라고 적혀 있다
    assert "FONT_FAMILY" in src and "Pretendard, 'Noto Sans KR', 'Malgun Gothic', sans-serif" in src


def test_every_palette_meets_aa_contrast_verified_by_code():
    assert verify_palettes() == []
    for p in PALETTES.values():
        assert contrast_ratio(p.body, p.bg) >= AA_RATIO and contrast_ratio(p.title, p.bg) >= AA_RATIO and contrast_ratio(p.button_text, p.accent) >= AA_RATIO
    weak = Palette("weak", "약한 팔레트", "#FFFFFF", "#191F28", "#CCCCCC", "#2B5BD7", "#FFFFFF", "#4E5968")
    assert any("weak.body" in m for m in verify_palettes([weak]))                      # 검증 함수가 나쁜 팔레트를 실제로 잡는다


@pytest.mark.parametrize("color", [
    'white"/><script>alert(1)</script><rect fill="white',
    "url(https://example.invalid/paint.svg)",
    "\x0cwhite",
])
@pytest.mark.parametrize("field", ["bg", "title", "body", "accent", "button_text", "disclosure"])
def test_custom_palette_rejects_values_that_are_not_supported_colors(field, color):
    from dataclasses import replace

    palette = replace(PALETTES["paper"], **{field: color})
    with pytest.raises(ValueError, match="색"):
        compose(TYPICAL, palette=palette)


def test_every_size_template_palette_combination_keeps_aa_contrast_on_every_block():
    n = 0
    for sz in ASSUMED_BANNER_SIZES:
        for tk in TEMPLATES:
            for pk in PALETTES:
                b = compose(TYPICAL, size=sz, template=tk, palette=pk)
                for blk in b.blocks:
                    assert contrast_ratio(blk.fg, blk.bg) >= AA_RATIO, (sz, tk, pk, blk.role)
                n += 1
    assert n == len(ASSUMED_BANNER_SIZES) * len(TEMPLATES) * len(PALETTES) >= 36


# --------------------------------------------------------------------- 글자 폭·줄바꿈
def test_char_width_estimates_follow_the_stated_ratios():
    assert char_em("가") == 1.0 and char_em("漢") == 1.0 and char_em("ㄱ") == 1.0                    # 한글·한자 1em
    assert char_em("7") == 0.58 and char_em("a") == 0.55 and char_em("Z") == 0.55 and char_em(" ") == 0.3 and char_em("%") == 0.5 and char_em("!") == 0.5
    assert char_em("​") == 0.0                                                                    # 폭 없는 문자는 폭이 없다
    assert estimate_width("가나다 12", 10) == pytest.approx((3 + 0.3 + 1.16) * 10)


def test_wrap_prefers_word_boundaries_and_never_loses_text():
    text = "월 3만원부터 시작하는 생활비 통장 입니다"
    lines = wrap_text(text, 40, 300)
    assert " ".join(lines).split() == text.split()                                   # 어절이 쪼개지지 않고 모두 남는다
    assert all(estimate_width(ln, 40) <= 300 for ln in lines)
    assert wrap_text("", 40, 300) == [] and wrap_text("   ", 40, 300) == []


def test_a_word_longer_than_a_line_is_split_by_characters_without_dropping_any():
    word = "가나다라마바사아자차카타파하" * 3
    lines = wrap_text(word, 40, 200)
    assert len(lines) > 1 and "".join(lines) == word
    lines = wrap_text("https://example.com/" + "a" * 60, 30, 200)
    assert "".join(lines) == "https://example.com/" + "a" * 60


def test_punctuation_does_not_start_a_line_when_splitting_a_long_word():
    lines = wrap_text("가나다라마바사아자차.", 40, 200)
    assert "".join(lines) == "가나다라마바사아자차." and not any(ln[0] in ".,!?" for ln in lines)


# --------------------------------------------------------------------- compose
@pytest.mark.parametrize("size", list(ASSUMED_BANNER_SIZES))
@pytest.mark.parametrize("template", list(TEMPLATES))
def test_svg_is_valid_xml_with_real_text_title_desc_and_no_external_resources(size, template):
    b = compose(TYPICAL, size=size, template=template, palette="paper")
    root = ET.fromstring(b.svg)
    assert root.tag == SVG + "svg" and root.get("width") == str(ASSUMED_BANNER_SIZES[size].width) and root.get("viewBox").endswith(f"{ASSUMED_BANNER_SIZES[size].height}")
    assert root.find(SVG + "title") is not None and root.find(SVG + "desc") is not None and root.get("role") == "img"
    texts = list(root.iter(SVG + "text"))
    assert len(texts) >= 3 and all(t.get("font-family") == "Pretendard, 'Noto Sans KR', 'Malgun Gothic', sans-serif" for t in texts)
    assert svg_hazards(b.svg) == [] and "http" not in b.svg.replace("http://www.w3.org/2000/svg", "") and "<image" not in b.svg and "<script" not in b.svg
    # 글은 진짜 글자(<text>) 로 들어 있고 문구와 같다
    flat = svg_text(b)
    for piece in ("월 3만원부터 시작하는 생활비 통장", "지금 확인"):
        assert piece.replace(" ", "") in flat.replace(" ", "")


def test_banner_svg_passes_the_upload_sanitizer_untouched():
    for sz in ASSUMED_BANNER_SIZES:
        a = load_image(compose(TYPICAL, size=sz).svg.encode("utf-8"), "banner.svg")
        assert isinstance(a, ImageAsset) and a.sanitized_notes == () and a.svg_unread_shapes == 0 and a.svg_embedded_images == 0


def test_text_is_xml_escaped():
    c = AdCopy(headline='A&B <b>"큰" 혜택</b>', body="a < b && c > d", cta="신청 & 확인")
    b = compose(c, size="landscape")
    root = ET.fromstring(b.svg)                                                   # 이스케이프가 틀렸다면 XML 파싱이 실패한다
    flat = " ".join("".join(t.itertext()) for t in root.iter(SVG + "text"))
    assert 'A&B <b>"큰" 혜택</b>'.split()[0] in flat and "a < b && c > d".replace(" ", "") in flat.replace(" ", "")
    assert "<b>" not in b.svg and "&amp;" in b.svg and "&lt;" in b.svg
    c2 = AdCopy(headline="정상\x00제목\x0b", body="본문", cta="확인")
    ET.fromstring(compose(c2).svg)                                                # 제어 문자는 걷어낸다 (XML 에 쓸 수 없음)


def test_compose_is_deterministic():
    a = compose(TYPICAL, size="square", template="split", palette="navy")
    b = compose(TYPICAL, size="square", template="split", palette="navy")
    assert a.svg == b.svg and a.blocks == b.blocks


def test_compose_rejects_unknown_choices_and_empty_copy():
    for kw in ({"size": "huge"}, {"template": "x"}, {"palette": "neon"}):
        with pytest.raises(ValueError):
            compose(TYPICAL, **kw)
    with pytest.raises(ValueError):
        compose(AdCopy(headline="", body="", cta=""))


def test_blocks_stay_inside_the_safe_area_when_nothing_overflows():
    for sz in ("landscape", "square"):
        b = compose(SHORT, size=sz)
        m = b.meta["margin"]
        assert not b.fit_violation
        for blk in b.blocks:
            assert blk.x >= m - 0.5 and blk.y >= m - 0.5 and blk.x + blk.w <= b.size.width - m + 0.5 and blk.y + blk.h <= b.size.height - m + 0.5, (sz, blk.role)


def test_font_shrinks_from_max_toward_min_to_fit():
    short = compose(SHORT, size="landscape", template="left")
    typical = compose(MAXLEN, size="landscape", template="left")
    assert short.block("title").font_px > typical.block("title").font_px >= min_font_px(1200, "title")
    assert short.block("body").font_px >= typical.block("body").font_px >= min_font_px(1200, "body") == ASSUMED_MIN_FONT_PX
    assert not typical.fit_violation


def test_min_font_scales_with_banner_width():
    assert min_font_px(1200) == ASSUMED_MIN_FONT_PX and min_font_px(600) == ASSUMED_MIN_FONT_PX / 2
    assert min_font_px(1200, "title") > min_font_px(1200, "body") > min_font_px(1200, "disclosure")


def test_overflow_is_flagged_and_text_is_never_silently_cut():
    """최소 글자 크기에서도 안 들어가면 자르지 않고 넘침(fit_violation)으로 표시한다. 글은 하나도 빠지지 않고 SVG 에 그대로 있다."""
    for sz in ASSUMED_BANNER_SIZES:
        b = compose(HUGE, size=sz, template="left")
        assert b.fit_violation and b.meta["overflow_roles"], sz
        flat = svg_text(b).replace(" ", "")
        for field in (HUGE.headline, HUGE.body, HUGE.cta):
            assert field.replace(" ", "") in flat, (sz, field[:10])                 # 한 글자도 잘리지 않았다
        assert "…" not in b.svg and "..." not in b.svg
        over = [blk for blk in b.blocks if blk.overflow]
        assert over and all(blk.font_px == pytest.approx(min_font_px(b.size.width, blk.role)) for blk in over if blk.role in ("title", "body"))   # 최소 크기까지 줄인 뒤의 넘침이다
    assert not compose(SHORT, size="landscape").fit_violation


# --------------------------------------------------------------------- score
def test_score_items_have_value_score_and_plain_explanation():
    sc = score(compose(TYPICAL, size="landscape"))
    keys = {i.key for i in sc.items}
    assert {"fit", "contrast_title", "contrast_body", "contrast_button", "min_font", "safe_margin", "density", "button", "lines", "disclosure"} <= keys
    for it in sc.items:
        assert 0.0 <= it.score <= 1.0 and it.label and it.detail and it.value_text
        assert not any(j in it.detail + it.label for j in ("WCAG", "finding", "IMG-"))
    assert 0 <= sc.total <= 1 and set(SCORE_WEIGHTS) >= {"fit", "contrast_title", "min_font"}
    assert sc.item("contrast_title").value == pytest.approx(contrast_ratio(PALETTES["paper"].title, PALETTES["paper"].bg))


def test_overflow_caps_the_total_even_when_everything_else_is_good():
    """좋은 점수가 나쁜 가독성을 가리지 못한다 — 넘치면 나머지가 아무리 좋아도 상한을 넘지 못한다."""
    b = compose(HUGE, size="strip", template="left")
    sc = score(b)
    assert sc.flags["fit_violation"] and sc.raw > CAP_FIT_VIOLATION and sc.total <= CAP_FIT_VIOLATION and sc.capped
    assert any(w.rule_id == "IMG-FIT-01" for w in sc.warnings) and sc.item("fit").score == 0.0
    ok = score(compose(SHORT, size="landscape"))
    assert ok.total > CAP_FIT_VIOLATION and not ok.capped


def test_low_contrast_caps_the_total():
    weak = Palette("weak", "약함", "#FFFFFF", "#191F28", "#CCCCCC", "#2B5BD7", "#FFFFFF", "#4E5968")
    b = compose(SHORT, size="landscape", palette=weak)
    sc = score(b)
    assert sc.flags["low_contrast"] and sc.total <= CAP_LOW_CONTRAST and sc.raw > CAP_LOW_CONTRAST
    assert sc.item("contrast_body").score < 0.5 and "비슷해" in sc.item("contrast_body").detail
    assert any(w.rule_id == "IMG-CONTRAST-01" for w in sc.warnings)
    assert not score(compose(SHORT, size="landscape", palette="paper")).flags["low_contrast"]


def test_score_recomputes_from_blocks_and_does_not_trust_compose_flags():
    """점수는 compose 가 meta 에 남긴 표시가 아니라 블록의 실제 값에서 다시 계산한다."""
    from dataclasses import replace

    b = compose(SHORT, size="landscape")
    assert not score(b).flags["fit_violation"]
    bad_block = replace(b.blocks[0], overflow=True)
    tampered = replace(b, blocks=(bad_block,) + b.blocks[1:])                      # meta 는 '문제 없음' 그대로
    assert score(tampered).flags["fit_violation"] and score(tampered).total <= CAP_FIT_VIOLATION
    low = replace(b.blocks[1], fg="#EEEEEE")
    assert score(replace(b, blocks=(b.blocks[0], low) + b.blocks[2:])).flags["low_contrast"]


def test_disclosure_visible_hidden_and_not_applicable():
    shown = compose(TYPICAL, size="landscape")
    assert shown.block("disclosure") and not shown.block("disclosure").overflow
    s = score(shown)
    assert s.item("disclosure").score == 1.0 and not s.flags["disclosure_hidden"]
    omitted = score(compose(TYPICAL, size="landscape", include_disclosures=False))     # 광고 문구에는 고지가 있는데 배너에 안 넣음
    assert omitted.flags["disclosure_hidden"] and omitted.total <= CAP_DISCLOSURE_HIDDEN
    w = [x for x in omitted.warnings if x.rule_id == "IMG-DISC-01"]
    assert w and "실제 규정 해석이 아닙니다" in w[0].text                               # 이 프로젝트의 규칙임을 밝힌다
    assert not score(compose(SHORT, size="landscape")).item("disclosure").applicable   # 고지가 없는 광고는 해당 없음
    cut = compose(AdCopy(headline="제목", body="본문", cta="확인", disclosures=["아주 긴 안내 문구입니다 " * 40]), size="strip")
    assert score(cut).flags["disclosure_hidden"]


def test_score_is_a_weighted_average_of_applicable_items():
    sc = score(compose(SHORT, size="landscape"))
    num = sum(SCORE_WEIGHTS[i.key] * i.score for i in sc.items if i.applicable)
    den = sum(SCORE_WEIGHTS[i.key] for i in sc.items if i.applicable)
    assert sc.raw == pytest.approx(num / den, abs=1e-3)


# --------------------------------------------------------------------- recommend
def test_recommend_is_deterministic_sorted_and_diverse():
    a = recommend(TYPICAL, 3)
    b = recommend(TYPICAL, 3)
    assert [(c.size_key, c.template_key, c.palette_key) for c in a] == [(c.size_key, c.template_key, c.palette_key) for c in b] and [c.banner.svg for c in a] == [c.banner.svg for c in b]
    assert len(a) == 3 and [c.rank for c in a] == [1, 2, 3]
    keys = [(c.size_key, c.template_key, c.palette_key) for c in a]
    for i in range(3):
        for j in range(i + 1, 3):
            assert not (keys[i][0] == keys[j][0] and keys[i][1] == keys[j][1])        # 같은 크기+같은 템플릿 중복 없음
            assert keys[i][2] != keys[j][2] or keys[i][1] != keys[j][1]
    assert len({k[0] for k in keys}) == 3 and len({k[1] for k in keys}) == 3 and len({k[2] for k in keys}) == 3     # 가능하면 모두 다르다


def test_recommend_ties_follow_a_fixed_order_and_every_candidate_is_the_best_in_its_group():
    c = recommend(SHORT, 3)
    best_overall = max(score(compose(SHORT, size=s, template=t, palette=p)).total for s in ASSUMED_BANNER_SIZES for t in TEMPLATES for p in PALETTES)
    assert c[0].score.total == pytest.approx(best_overall)                              # 첫 후보는 전체 최고점
    first_in_order = next((s, t, p) for s in ASSUMED_BANNER_SIZES for t in TEMPLATES for p in PALETTES
                          if score(compose(SHORT, size=s, template=t, palette=p)).total == best_overall)
    assert (c[0].size_key, c[0].template_key, c[0].palette_key) == first_in_order       # 동점이면 정해진 순서(크기→템플릿→팔레트)의 첫 것


def test_recommend_respects_sizes_and_handles_more_requests_than_combinations():
    only = recommend(SHORT, 3, sizes=["strip"])
    assert {c.size_key for c in only} == {"strip"} and len(only) == 3
    many = recommend(SHORT, 100, sizes=["landscape"])
    assert len(many) == len(TEMPLATES) * len(PALETTES)
    assert len({(c.template_key, c.palette_key) for c in many}) == len(many)


def test_recommend_long_copy_ranks_non_overflowing_candidates_first_and_is_fast():
    t = time.perf_counter()
    cands = recommend(MAXLEN, 3)
    assert time.perf_counter() - t < 2.0
    assert not cands[0].banner.fit_violation
    huge = recommend(HUGE, 3)
    assert all(c.score.total <= CAP_FIT_VIOLATION for c in huge if c.banner.fit_violation)
