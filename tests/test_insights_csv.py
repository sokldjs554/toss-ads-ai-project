"""성과 CSV 읽기 — 외부에서 온 CSV 는 믿지 않는다 (행 단위 거절·줄 번호·쉬운 말·수식 주입 무해화·크기 상한).

방어선마다 '빼면 실패하는' 테스트가 있다 (변이 확인 대상: CSV 검증, 수식 주입).
"""
from __future__ import annotations

import datetime as dt

import pytest

from core import performance as P

HEAD = "날짜,캠페인,광고세트,소재,기기,노출수,클릭수,전환수,광고비\n"


def line(day="2026-10-01", camp="c", aset="s", cre="k", dev="Android", imp=1000, clk=30, conv=3, cost=15000):
    return f"{day},{camp},{aset},{cre},{dev},{imp},{clk},{conv},{cost}\n"


# ───────────── 정상 읽기 ─────────────
def test_reads_korean_headers_and_all_fields():
    rows, errors = P.parse_csv(HEAD + line() + line("2026-10-02", dev="iOS", imp=2000, clk=50, conv=5, cost=30000))
    assert not errors and len(rows) == 2
    r = rows[0]
    assert r.date == dt.date(2026, 10, 1) and (r.campaign, r.ad_set, r.creative, r.device) == ("c", "s", "k", "Android")
    assert (r.impressions, r.clicks, r.conversions, r.cost_krw) == (1000, 30, 3, 15000)
    assert r.ctr == pytest.approx(0.03) and r.cpc == pytest.approx(500) and r.cvr == pytest.approx(0.1) and r.cpa == pytest.approx(5000)


def test_header_aliases_bom_and_units_in_header():
    text = "﻿일자,노출 수,클릭수,전환수,광고비(원)\n2026.10.1,\"1,000\",30,3,15000원\n"
    rows, errors = P.parse_csv(text)
    assert not errors and rows[0].impressions == 1000 and rows[0].cost_krw == 15000
    assert rows[0].campaign == P.NO_DIM and rows[0].creative == P.NO_DIM            # 구분 열이 없으면 '구분 없음'


@pytest.mark.parametrize("raw", ["2026-10-05", "2026/10/05", "2026.10.5", "2026.10.05.", "20261005", "2026년 10월 5일", "2026-10-05 00:00:00", "2026-10-05T09:30"])
def test_date_formats(raw):
    assert P.parse_date(raw) == dt.date(2026, 10, 5)


@pytest.mark.parametrize("raw", ["10/05/2026", "2026-13-01", "2026-02-30", "내일", "", "1999-01-01"])
def test_ambiguous_or_impossible_dates_are_not_guessed(raw):
    assert P.parse_date(raw) is None


def test_tab_and_semicolon_delimiters():
    tab = "날짜\t노출수\t클릭수\t전환수\t광고비\n2026-10-01\t1000\t30\t3\t15000\n"
    semi = "날짜;노출수;클릭수;전환수;광고비\n2026-10-01;1000;30;3;15000\n"
    assert len(P.parse_csv(tab)[0]) == 1 and len(P.parse_csv(semi)[0]) == 1


def test_optional_columns_reach_and_end_hour():
    text = "날짜,노출수,클릭수,전환수,광고비,도달,노출종료시각\n2026-10-01,1000,30,3,15000,600,11\n"
    r = P.parse_csv(text)[0][0]
    assert r.reach == 600 and r.delivery_end_hour == 11 and r.frequency == pytest.approx(1000 / 600)
    bad = "날짜,노출수,클릭수,전환수,광고비,노출종료시각\n2026-10-01,1000,30,3,15000,31\n"
    assert "0~23" in str(P.parse_csv(bad)[1][0])


def test_decode_bytes_handles_utf8_bom_and_cp949():
    text = "날짜,노출수,클릭수,전환수,광고비\n2026-10-01,1000,30,3,15000\n"
    assert P.decode_csv_bytes(text.encode("utf-8-sig")) == text
    assert P.decode_csv_bytes(text.encode("cp949")) == text


def test_round_trip_and_template():
    rows = P.generate(P.ScenarioSpec("rt", 5, days=10))
    back, errors = P.parse_csv(P.rows_to_csv(rows))
    assert not errors and back == rows
    tpl_rows, tpl_err = P.parse_csv(P.template_csv())
    assert not tpl_err and len(tpl_rows) == 2


# ───────────── 행 단위 거절 (변이 확인: 검증을 빼면 실패) ─────────────
def test_impossible_rows_are_rejected_one_by_one_with_line_number_and_plain_reason():
    text = HEAD + line() + line("2026-10-02", clk=1500) + line("2026-10-03", conv=40) + line("2026-10-04", imp="abc") + line("2026-10-05", clk=-3) + line("2026-10-06", clk="12.5") + line("2026-10-07")
    rows, errors = P.parse_csv(text)
    assert [r.date.day for r in rows] == [1, 7]                                    # 나쁜 행만 빠지고 나머지는 읽는다
    lines = {e.line for e in errors}
    assert lines == {3, 4, 5, 6, 7}
    msgs = {e.line: str(e) for e in errors}
    assert "클릭수" in msgs[3] and "노출수" in msgs[3] and "읽지 않았어요" in msgs[3]
    assert "전환수" in msgs[4] and "클릭수" in msgs[4]
    assert "노출수" in msgs[5] and "숫자가 아니에요" in msgs[5]
    assert "음수" in msgs[6]
    assert "정수" in msgs[7]
    assert all(str(e).startswith(f"{e.line}번째 줄") for e in errors)               # 줄 번호가 앞에 온다
    assert not any(w in str(e) for e in errors for w in ("Traceback", "ValueError", "Exception"))


def test_reach_larger_than_impressions_is_rejected():
    text = "날짜,노출수,클릭수,전환수,광고비,도달\n2026-10-01,1000,30,3,15000,2000\n"
    rows, errors = P.parse_csv(text)
    assert not rows and "도달" in str(errors[0])


def test_integers_accept_commas_won_sign_and_zero_decimals_only():
    assert P.parse_count("1,234") == (1234, "") and P.parse_count("1234원") == (1234, "") and P.parse_count("₩5,000") == (5000, "")
    assert P.parse_count("12.0") == (12, "")
    for raw in ("12.5", "-1", "1e3", "", "abc", "1 2", "0x10"):
        v, why = P.parse_count(raw)
        assert v is None and why
    assert P.parse_count("１２３")[0] == 123                                            # 전각 숫자는 정규화해서 읽는다
    assert P.parse_count(str(10 ** 13))[0] is None                                   # 입력 실수로 보이는 큰 값


def test_duplicate_key_rows_are_rejected_not_summed():
    rows, errors = P.parse_csv(HEAD + line() + line() + line("2026-10-02"))
    assert len(rows) == 2 and errors[0].line == 3 and "2번째 줄" in str(errors[0])


# ───────────── 파일 전체 문제: 친절한 오류 ─────────────
@pytest.mark.parametrize("text", ["", "   \n  \n", None])
def test_empty_csv_has_a_friendly_error(text):
    rows, errors = P.parse_csv(text)
    assert not rows and len(errors) == 1 and errors[0].line == 0 and "비어 있는 파일" in str(errors[0]) and "날짜" in str(errors[0])


def test_missing_required_columns_are_listed():
    rows, errors = P.parse_csv("날짜,노출수,클릭수\n2026-10-01,1000,30\n")
    assert not rows and "전환수" in str(errors[0]) and "광고비" in str(errors[0]) and "노출수" not in str(errors[0]).split("없어요:")[1].split(".")[0]


def test_header_only_file():
    rows, errors = P.parse_csv("날짜,노출수,클릭수,전환수,광고비\n")
    assert not rows and "읽을 수 있는 데이터 줄이 없어요" in str(errors[0])


def test_row_cap_stops_reading_and_says_so():
    body = "".join(f"2026-01-01,c{i},s,k,d,10,1,0,100\n" for i in range(P.MAX_ROWS + 5))
    res = P.parse_csv_detailed("날짜,캠페인,광고세트,소재,기기,노출수,클릭수,전환수,광고비\n" + body)
    assert len(res.rows) == P.MAX_ROWS and any("행이 너무 많아요" in str(e) for e in res.errors)


def test_size_cap_refuses_big_file_without_reading_it():
    text = "날짜,노출수,클릭수,전환수,광고비\n" + "2026-10-01,1000,30,3,15000\n" * 90_000
    assert len(text.encode("utf-8")) > P.MAX_BYTES
    res = P.parse_csv_detailed(text)
    assert not res.rows and "너무 커요" in str(res.errors[0]) and "2MB" in str(res.errors[0])


def test_error_list_is_capped_but_count_is_kept():
    text = HEAD + "".join(line(imp="x") for _ in range(P.MAX_REPORTED_ERRORS + 30))
    res = P.parse_csv_detailed(text)
    assert len(res.errors) == P.MAX_REPORTED_ERRORS and res.error_count >= P.MAX_REPORTED_ERRORS + 30


def test_broken_quote_does_not_crash():
    res = P.parse_csv_detailed(HEAD + '2026-10-01,"c,s,k,d,1000,30,3,15000\n')
    assert isinstance(res.errors, list)


# ───────────── 문자열 칸: 길이·제어문자·수식 주입 (변이 확인: 무해화를 빼면 실패) ─────────────
@pytest.mark.parametrize("raw", ["=SUM(A1:A9)", "+1+1", "-2+3", "@cmd", "＝1+1", "=HYPERLINK(\"http://x\",\"y\")"])
def test_formula_cells_become_plain_text(raw):
    quoted = '"' + raw.replace('"', '""') + '"'
    rows = P.parse_csv_detailed(HEAD + line(cre=quoted)).rows
    name = rows[0].creative
    assert name.startswith("'") and name[1:].strip() == P.clean_text(raw)           # 앞에 작은따옴표 — 수식으로 실행되지 않는다
    assert not P.looks_like_formula(name)
    assert P.safe_cell(name) == name                                                  # 여러 번 불러도 같다


def test_formula_after_hidden_characters_is_still_neutralized():
    for raw in ("\t=cmd", "​=cmd", " =cmd", "\r=cmd"):
        cell = P.safe_cell(raw)
        assert cell.startswith("'=") or cell.startswith("' ="), repr(cell)


def test_notice_counts_neutralized_cells_and_export_stays_neutral():
    res = P.parse_csv_detailed(HEAD + line(cre='"=1+1"') + line("2026-10-02", dev='"@x"'))
    assert res.formula_cells == 2 and any("수식처럼 보이는 칸 2개" in n for n in res.notices)
    out = P.rows_to_csv(res.rows)
    cells = [c for ln in out.splitlines()[1:] for c in ln.split(",")]
    assert not any(c[:1] in "=+-@" and c for c in cells if not c[:1].isdigit() or c[:1] in "+-@=")
    assert "'=1+1" in out and "'@x" in out


def test_text_cells_are_cleaned_and_length_capped():
    raw = "소재‮\u0000\x07이름" + "가" * 200
    cell = P.clean_text(raw)
    assert "‮" not in cell and "\x00" not in cell and "\x07" not in cell and len(cell) <= P.MAX_TEXT_LEN
    rows, _ = P.parse_csv(HEAD + line(cre=f'"{raw}"'))
    assert len(rows[0].creative) <= P.MAX_TEXT_LEN + 1                                # 무해화 접두사 한 글자까지


def test_markup_in_names_is_kept_as_text_not_interpreted():
    rows, _ = P.parse_csv(HEAD + line(cre='"<img src=x onerror=alert(1)>"'))
    assert rows[0].creative == "<img src=x onerror=alert(1)>"                         # 데이터는 그대로, 출력할 때 화면이 이스케이프한다
