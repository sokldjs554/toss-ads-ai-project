"""이미지 받아들이기(core/images.py): 매직 바이트 판별, 머리글 크기, 거부 사유, 크기·픽셀 상한, SVG 정화.

PIL 없이 표준 라이브러리만 쓰는 코드가 맞게 읽는지 본다. 픽스처는 evals/datasets/images/make_fixtures.py 가 코드로 만든다 (실제 광고 이미지 아님).
"""
from __future__ import annotations

import hashlib
import time

import pytest

from core.images import (MAX_PIXELS, MAX_RASTER_BYTES, MAX_SIDE_PX, MAX_SVG_BYTES, ImageAsset, ImageRejection, load_image, safe_name, sanitize_svg, sniff_kind,
                         svg_hazards, svg_scene, to_image_input)
from tests.test_image_common import chunk, mf, png_with_size


def ok(name: str, data: bytes | None = None, **kw):
    return load_image(mf.build(name) if data is None else data, name, **kw)


# --------------------------------------------------------------------- 종류·크기 판별
@pytest.mark.parametrize("name,fmt,w,h", [("ok_png.png", "png", 64, 48), ("ok_jpeg.jpg", "jpeg", 64, 48), ("ok_gif.gif", "gif", 32, 24), ("ok_webp.webp", "webp", 1, 1)])
def test_raster_kind_and_size_come_from_the_header(name, fmt, w, h):
    a = ok(name)
    assert isinstance(a, ImageAsset) and a.kind == "raster" and a.fmt == fmt and (a.width, a.height) == (w, h)
    assert a.mime == {"png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}[fmt]
    assert a.sha256 == hashlib.sha256(mf.build(name)).hexdigest() and a.bytes_len == len(mf.build(name))


def test_svg_is_detected_by_content_and_size_from_viewbox_or_width():
    a = ok("ok_svg.svg")
    assert a.kind == "svg" and a.fmt == "svg" and (a.width, a.height) == (600, 314) and a.mime == "image/svg+xml"
    only_viewbox = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 80"><rect width="1" height="1"/></svg>'
    b = load_image(only_viewbox, "v.svg")
    assert (b.width, b.height) == (120, 80) and not b.sanitized_notes
    none = load_image(b'<svg xmlns="http://www.w3.org/2000/svg"/>', "n.svg")
    assert (none.width, none.height) == (300, 150) and any("기본 크기" in n for n in none.sanitized_notes)   # 크기를 지어내지 않고 기본값을 썼다고 알린다


def test_kind_is_decided_by_magic_bytes_not_by_name_or_content_type():
    assert sniff_kind(mf.build("ok_png.png")) == "png" and sniff_kind(b"hello") is None and sniff_kind(b"") is None
    r = load_image(mf.build("ok_png.png"), "photo.jpg")
    assert isinstance(r, ImageRejection) and r.code == "extension_mismatch"          # 이름은 jpg, 내용은 png
    r = load_image(mf.build("ok_png.png"), "photo.png", claimed_mime="image/jpeg")
    assert r.code == "type_mismatch"
    r = load_image(b"MZ\x90\x00 not an image", "evil.png")
    assert r.code == "unsupported"
    assert isinstance(load_image(mf.build("ok_jpeg.jpg"), "p.JPEG", claimed_mime="image/jpg"), ImageAsset)   # 별칭·대소문자는 받아들인다
    assert isinstance(load_image(mf.build("ok_png.png"), ""), ImageAsset)       # 이름이 없으면 이름 검사를 건너뛴다 (프로그램 호출)


def test_empty_text_and_unknown_files_are_rejected_in_plain_words():
    for name, code in (("empty.png", "empty"), ("text_file.png", "unsupported")):
        r = ok(name)
        assert isinstance(r, ImageRejection) and r.code == code and r.message and "Traceback" not in r.message
    assert load_image("문자열", "a.png").code == "unsupported"       # 바이트가 아닌 입력도 예외 없이 거부


# --------------------------------------------------------------------- 크기·픽셀 상한 (압축 폭탄)
def test_pixel_caps_reject_before_decoding():
    assert ok("wide_png_9000.png").code == "too_many_pixels"                 # 한 변 8192 초과
    assert ok("bomb_png_header_30000.png").code == "too_many_pixels"
    assert ok("bomb_jpeg_header_8000.jpg").code == "too_many_pixels"          # 8000×8000 = 64M > 40M (한 변은 한도 안)
    assert ok("bomb_webp_header_16384.webp").code == "too_many_pixels"
    real = ok("bomb_png_8192.png")                                              # 몇십 KB 파일이 디코딩하면 64MB — 곱 상한으로 걸린다
    assert real.code == "too_many_pixels" and len(mf.build("bomb_png_8192.png")) < 100_000
    assert isinstance(load_image(png_with_size(MAX_SIDE_PX, MAX_PIXELS // MAX_SIDE_PX), "e.png"), ImageAsset)   # 상한 경계는 받는다 (8192×4882)
    assert load_image(png_with_size(MAX_SIDE_PX, MAX_PIXELS // MAX_SIDE_PX + 1), "e.png").code == "too_many_pixels"


def test_file_size_caps_for_raster_and_svg():
    r = ok("oversize_png.png")
    assert r.code == "too_large" and len(mf.build("oversize_png.png")) > MAX_RASTER_BYTES
    r = ok("oversize_svg.svg")
    assert r.code == "too_large" and len(mf.build("oversize_svg.svg")) > MAX_SVG_BYTES


# --------------------------------------------------------------------- 손상·polyglot
@pytest.mark.parametrize("name", ["corrupt_png_crc.png", "truncated_png.png", "corrupt_jpeg_no_eoi.jpg"])
def test_corrupt_files_are_rejected(name):
    r = ok(name)
    assert isinstance(r, ImageRejection) and r.code == "corrupt" and "손상" in r.message


def test_bad_headers_are_corrupt_not_crashes():
    png = bytearray(mf.build("ok_png.png"))
    zero_w = bytes(png[:16]) + b"\x00\x00\x00\x00" + bytes(png[20:])           # 가로 0 (CRC 도 어긋남)
    assert load_image(zero_w, "a.png").code == "corrupt"
    ihdr0 = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", b"\x00\x00\x00\x00\x00\x00\x00\x10\x08\x00\x00\x00\x00") + chunk(b"IDAT", b"x") + chunk(b"IEND", b"")
    assert load_image(ihdr0, "a.png").code == "corrupt"                          # CRC 는 맞지만 가로 0
    assert load_image(b"\xff\xd8\xff\xd9", "a.jpg").code == "corrupt"            # SOF 없음
    assert load_image(b"GIF89a\x00\x00\x00\x00\x00\x00\x00;", "a.gif").code == "corrupt"
    assert load_image(b"RIFF\x04\x00\x00\x00WEBP", "a.webp").code == "corrupt"


@pytest.mark.parametrize("name", ["polyglot_png_html.png", "polyglot_png_svgchunk.png", "polyglot_gif_zip.gif", "polyglot_jpeg_comment_script.jpg"])
def test_polyglot_files_are_rejected(name):
    r = ok(name)
    assert isinstance(r, ImageRejection) and r.code == "polyglot", (name, r)
    assert "다른 데이터" in r.message


def test_normal_jpeg_with_embedded_exif_thumbnail_style_segments_is_accepted():
    """정상 JPEG 은 COM 같은 일반 구간이 있어도 받는다 — 스크립트·다른 형식 표지가 없을 때."""
    assert isinstance(load_image(mf.jpeg_with_comment(b"Created by a camera"), "a.jpg"), ImageAsset)


def test_scanning_a_5mb_valid_png_is_fast():
    big = mf.png_bytes(8, 8)
    big = big[:-12] + chunk(b"tEXt", b"Comment\x00" + b"A" * (5 * 1024 * 1024 - 500)) + big[-12:]
    t = time.perf_counter()
    r = load_image(big, "a.png")
    assert isinstance(r, ImageAsset) and time.perf_counter() - t < 3.0


# --------------------------------------------------------------------- SVG 정화
def sanitized(name: str):
    a = ok(name)
    assert isinstance(a, ImageAsset), (name, a)
    return a, a.data.decode("utf-8")


@pytest.mark.parametrize("name,needle", [
    ("svg_script.svg", "스크립트"), ("svg_foreignobject.svg", "다른 문서를 끼워 넣는 요소"), ("svg_onload.svg", "클릭 등에 반응"), ("svg_javascript_href.svg", "javascript:"),
    ("svg_external_href.svg", "바깥 주소"), ("svg_import_css.svg", "스타일 속의 바깥 주소"),
])
def test_dangerous_svg_parts_are_removed_and_the_user_is_told(name, needle):
    raw = mf.build(name).decode()
    assert svg_hazards(raw), name                                            # 원본에는 위험 요소가 있었다
    a, out = sanitized(name)
    assert svg_hazards(out) == [], (name, svg_hazards(out), out)             # 따로 훑는 검사로 확인: 남은 것이 없다
    assert any(needle in n for n in a.sanitized_notes), (name, a.sanitized_notes)
    assert "월 3만원부터 시작하는 통장" in out                                  # 정상 글은 그대로


def test_clean_svg_is_unchanged_in_meaning_and_has_no_notes():
    a, out = sanitized("ok_svg.svg")
    assert a.sanitized_notes == () and svg_hazards(out) == [] and "월 3만원부터 시작하는 통장" in out
    assert [t.text for t in svg_scene(a).texts if t.kind == "text"][0] == "월 3만원부터 시작하는 통장"


@pytest.mark.parametrize("name", ["svg_entity_bomb.svg", "svg_xxe.svg"])
def test_doctype_and_entities_are_refused_not_expanded(name):
    r = ok(name)
    assert isinstance(r, ImageRejection) and r.code == "svg_dangerous" and "DOCTYPE" in r.message


def test_entity_declaration_in_any_case_or_spacing_is_refused():
    for decl in ("<!DOCTYPE svg>", "<!doctype svg>", "<! DOCTYPE svg>", '<!ENTITY a "b">', "<!ELEMENT a ANY>"):
        data = f'<?xml version="1.0"?>{decl}<svg xmlns="http://www.w3.org/2000/svg"/>'.encode()
        assert load_image(data, "a.svg").code == "svg_dangerous", decl


def test_broken_or_non_svg_roots_are_refused():
    assert ok("svg_broken.svg").code == "svg_invalid"
    assert ok("svg_html_root.svg").code == "svg_invalid"
    assert load_image(b"\xff\xfe<\x00s\x00v\x00g\x00>\x00", "a.svg").code == "unsupported"      # UTF-16 은 받지 않는다
    deep = ("<g>" * 60 + "</g>" * 60)
    assert load_image(f'<svg xmlns="http://www.w3.org/2000/svg">{deep}</svg>'.encode(), "a.svg").code == "svg_invalid"   # 너무 깊음
    many = "<g/>" * 6000
    assert load_image(f'<svg xmlns="http://www.w3.org/2000/svg">{many}</svg>'.encode(), "a.svg").code == "svg_invalid"   # 요소가 너무 많음


def test_obfuscated_javascript_urls_and_css_tricks_are_removed():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="10" height="10">'
           '<a xlink:href="  jav&#x09;ascript:alert(1)"><text x="1" y="1">a</text></a>'
           '<rect width="5" height="5" style="fill:red;background:url(http://e.example/x);behavior:url(x.htc)"/>'
           '<set attributeName="href" to="javascript:alert(1)"/><animate attributeName="x" from="0" to="1"/>'
           '<rect width="5" height="5" fill="url(http://e.example/grad)"/><use href="#a"/></svg>').encode()
    a = load_image(svg, "a.svg")
    out = a.data.decode()
    assert svg_hazards(out) == [] and "<set" not in out and "<animate" not in out and "javascript" not in out.lower()
    assert 'href="#a"' in out                                                   # 안쪽(#) 참조는 남긴다


def test_embedded_images_and_outlined_shapes_are_counted_because_text_inside_them_is_unreadable():
    a = ok("svg_embedded_raster.svg")
    assert a.svg_embedded_images == 1 and a.svg_unread_shapes >= 1
    b = ok("svg_outlined_text.svg")
    assert b.svg_embedded_images == 0 and b.svg_unread_shapes == 1
    c = ok("ok_svg.svg")
    assert c.svg_embedded_images == 0 and c.svg_unread_shapes == 0


def test_svg_scene_reads_text_and_hidden_title_desc_and_rect_backgrounds():
    a = ok("svg_hidden_desc.svg")
    scene = svg_scene(a)
    visible = [t.text for t in scene.texts if t.kind == "text"]
    meta = [t.text for t in scene.texts if t.kind == "meta"]
    assert "월 3만원부터 시작하는 통장" in visible and any("원금 보장" in m for m in meta)   # 화면에 안 보이는 desc 의 글도 읽는다
    assert scene.rects and scene.rects[0].fill == "#FFFFFF" and (scene.width, scene.height) == (600, 314)


def test_sanitize_svg_returns_bytes_notes_and_info_or_a_rejection():
    res = sanitize_svg(mf.build("svg_script.svg"))
    assert isinstance(res, tuple) and isinstance(res[0], bytes) and res[1] and res[2]["width"] == 600
    assert isinstance(sanitize_svg(b"<html/>"), ImageRejection)


# --------------------------------------------------------------------- 모델에 보내기·기록
def test_to_image_input_is_raster_only_and_does_not_carry_the_file_name():
    a = load_image(mf.build("ok_png.png"), "홍길동-계약서-주민번호.png")
    img = to_image_input(a)
    assert img.mime == "image/png" and img.data == mf.build("ok_png.png")
    assert "홍길동" not in img.name and img.name.startswith("upload-") and img.name.endswith(".png")     # 사용자의 파일 이름은 처리 기록에 남지 않는다
    with pytest.raises(ValueError):
        to_image_input(ok("ok_svg.svg"))                                       # SVG 는 모델에 보내지 않는다


def test_asset_log_fields_have_hash_and_size_only():
    a = ok("ok_png.png")
    f = a.log_fields()
    assert set(f) == {"sha256", "bytes", "kind", "format", "width", "height"} and len(f["sha256"]) == 16
    assert "name" not in f and "data" not in f
    assert "data" not in repr(a) and mf.build("ok_png.png")[:12].hex() not in repr(a)        # 바이트가 repr 로 새지 않는다


def test_safe_name_strips_paths_and_control_characters():
    assert safe_name("C:\\Users\\a\\b.png") == "b.png" and safe_name("../../etc/pass\x00wd.png") == "passwd.png"
    assert len(safe_name("가" * 500 + ".png")) == 80


# --------------------------------------------------------------------- 정화 보강: CSS 이스케이프·use 폭탄·검증기
def _mk(body: str) -> bytes:
    return f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="10" height="10">{body}</svg>'.encode()


def test_css_backslash_escapes_cannot_hide_an_external_url():
    a = load_image(_mk('<rect width="5" height="5" style="fill:u\\72l(http://e.example/x)"/><style>.a{background:u\\72l(http://e.example/y)}</style>'), "a.svg")
    out = a.data.decode()
    assert "e.example" not in out and "\\" not in out and any("스타일" in n for n in a.sanitized_notes)


def test_use_chains_that_explode_or_loop_are_refused():
    defs = '<g id="g0"><rect width="1" height="1"/></g>' + "".join(f'<g id="g{i}">' + "".join(f'<use href="#g{i - 1}"/>' for _ in range(10)) + "</g>" for i in range(1, 13))
    bomb = load_image(_mk(f"<defs>{defs}</defs><use href='#g12'/>"), "a.svg")                          # 요소는 150개뿐이지만 그리면 10^12 개
    assert isinstance(bomb, ImageRejection) and bomb.code == "svg_dangerous" and "겹쳐 부르는" in bomb.message
    loop = load_image(_mk('<g id="a"><use href="#b"/></g><g id="b"><use href="#a"/></g><use href="#a"/>'), "a.svg")
    assert isinstance(loop, ImageRejection) and loop.code == "svg_dangerous"
    fine = load_image(_mk('<g id="a"><rect width="1" height="1"/></g><use href="#a"/><use href="#a"/>'), "a.svg")
    assert isinstance(fine, ImageAsset)                                                                  # 정상적인 재사용은 받는다


def test_unread_shape_counting_covers_paths_and_polygons_not_basic_shapes():
    assert load_image(_mk('<rect width="1" height="1"/><circle r="1"/><text x="1" y="1">a</text>'), "a.svg").svg_unread_shapes == 0
    assert load_image(_mk('<polygon points="0,0 1,0 1,1"/><path d="M0 0L1 1"/>'), "a.svg").svg_unread_shapes == 2


def test_independent_hazard_scanner_flags_real_hazards_and_accepts_internal_links():
    assert svg_hazards('<a href="#x"/><use xlink:href="#y"/><image href="data:image/png;base64,AAAA"/>') == []
    assert svg_hazards('<a href="http://x"/>') == ["external-href"] and svg_hazards("<a href=http://x>") == ["external-href"]
    assert "script" in svg_hazards("<SCRIPT>1</SCRIPT>") and "event" in svg_hazards('<g onclick="x()"/>') and "javascript:" in svg_hazards('<a href="JavaScript:1"/>')
    assert svg_hazards('<rect style="fill:url(#g)"/>') == [] and svg_hazards('<rect fill="url(http://e/x)"/>') == ["external-url"]


def test_any_bytes_after_the_end_marker_are_refused_even_without_script_markers():
    """이미지 끝 뒤에 붙은 데이터는 내용이 무엇이든 거부한다 (표지 검색에 걸리지 않는 우회도 막는다)."""
    for name in ("ok_png.png", "ok_jpeg.jpg", "ok_gif.gif"):
        data = mf.build(name) + b"\x00\x01\x02 appended bytes without any markers \x03"
        r = load_image(data, name)
        assert isinstance(r, ImageRejection) and r.code == "polyglot", name
        assert "끝난 뒤" in r.detail
    assert isinstance(load_image(mf.build("ok_webp.webp") + b"xx", "a.webp"), ImageRejection)           # WEBP 는 RIFF 크기가 어긋나므로 손상으로 거부된다


def test_script_markers_hidden_inside_a_structurally_valid_file_are_refused():
    """구조는 맞지만 안에 스크립트·다른 형식 표지가 든 파일: JPEG 주석 구간·PNG 보조 덩어리."""
    for name in ("polyglot_jpeg_comment_script.jpg", "polyglot_png_svgchunk.png"):
        r = load_image(mf.build(name), name)
        assert r.code == "polyglot" and "표지" in r.detail, name
