"""SVG CSS must stay bounded below the upload limit and remove incomplete URLs."""
from __future__ import annotations

import subprocess
import sys

import pytest

from core.images import ImageAsset, load_image, svg_hazards


def _assert_bounded(code):
    try:
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        pytest.fail("SVG below the upload limit did not finish within 5 seconds")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("body", [
    "'<style>.note {--value:' + 'a' * 900_000 + ';}</style>'",
    "'<rect fill=\"' + 'url(' * 30_000 + '\"/>'",
    "'<style>' + '/*a' * 300_000 + '</style>'",
], ids=["large-declaration", "incomplete-url", "unterminated-comments"])
def test_css_below_the_svg_size_limit_finishes_within_a_bounded_time(body):
    # A process timeout makes a regression fail without hanging the test runner.
    # Cover a harmless declaration, incomplete attribute URLs, and CSS comments
    # that reach EOF; all remain well below the SVG upload byte limit.
    code = (
        "from core.images import ImageAsset, load_image, svg_hazards\n"
        f"body = {body}\n"
        "svg = ('<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"10\" height=\"10\">' + body + '</svg>').encode()\n"
        "result = load_image(svg, 'bounded.svg')\n"
        "assert isinstance(result, ImageAsset), result\n"
        "assert not svg_hazards(result.data.decode()), result.data[:100]\n"
    )
    _assert_bounded(code)


@pytest.mark.parametrize("width", ["'1' * 900_000 + 'x'", "'1' + ' ' * 900_000 + 'x'"], ids=["digits", "spaces"])
def test_invalid_svg_dimensions_finish_within_a_bounded_time(width):
    code = (
        "from core.images import ImageAsset, load_image\n"
        f"width = {width}\n"
        "svg = ('<svg width=\"' + width + '\" height=\"10\"/>').encode()\n"
        "result = load_image(svg, 'dimension.svg')\n"
        "assert isinstance(result, ImageAsset), result\n"
        "assert (result.width, result.height) == (300, 150), result\n"
    )
    _assert_bounded(code)


@pytest.mark.parametrize("body", [
    '<rect style="fill:url(https://example.invalid/image.svg"/>',
    '<rect fill="url(https://example.invalid/image.svg"/>',
])
def test_incomplete_external_css_urls_are_removed_and_reported(body):
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">{body}</svg>'
    result = load_image(svg.encode(), "incomplete.svg")
    assert isinstance(result, ImageAsset)
    assert "example.invalid" not in result.data.decode()
    assert not svg_hazards(result.data.decode())
    assert result.sanitized_notes


@pytest.mark.parametrize("attribute", ["fill", "filter", "clip-path"])
def test_css_escapes_in_presentation_attributes_cannot_hide_external_urls(attribute):
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
           f'<rect {attribute}="u\\72l(https://example.invalid/resource.svg)"/></svg>')
    result = load_image(svg.encode(), "escaped.svg")
    assert isinstance(result, ImageAsset)
    assert "example.invalid" not in result.data.decode()
    assert result.sanitized_notes


def test_local_css_urls_keep_quoted_and_unquoted_fragments():
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
      <defs><linearGradient id="local"/></defs>
      <rect style="fill:url('#local');stroke:url( #local )"/>
    </svg>'''
    result = load_image(svg, "local.svg")
    assert isinstance(result, ImageAsset)
    assert "url('#local')" in result.data.decode()
    assert "url( #local )" in result.data.decode()
    assert not result.sanitized_notes
