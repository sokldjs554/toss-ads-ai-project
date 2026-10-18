from guardrails.normalize import normalize


def test_zero_width_removed_and_mapped_back():
    n = normalize("원​금​보​장")
    assert n.loose.text == "원금보장"
    assert n.zero_width_count == 3
    s, e = n.loose.to_original(0, 4)
    assert (s, e) == (0, 7)  # 원문 전체 구간


def test_hanja_folded():
    n = normalize("元金 保障")
    assert n.loose.text == "원금 보장"
    assert n.folded_count == 4
    assert n.had_evasion_in(0, 5)


def test_tight_surface_removes_decoration_between_hangul_only():
    n = normalize("원.금 보 장 연 4.5%~15%")
    assert n.tight.text.startswith("원금보장")
    assert "4.5%~15%" in n.tight.text  # 숫자·기호는 보존


def test_control_and_filler_characters_are_invisible():
    n = normalize("원금\x00보장\u3164투자\u2800기회")
    assert n.loose.text == "원금보장투자기회" and n.zero_width_count == 3
    assert n.had_evasion_in(0, 5)


def test_a_run_of_decoration_between_hangul_is_dropped_as_a_whole_and_positions_follow():
    n = normalize("원 . - * 금 보 장")
    assert n.tight.text == "원금보장"
    assert [n.tight.to_original(i, i + 1)[0] for i in range(4)] == [0, 8, 10, 12]
    assert normalize("원...a").tight.text == "원...a"        # 오른쪽 이웃이 한글이 아니면 그대로
    assert normalize("a...금").tight.text == "a...금"        # 왼쪽 이웃이 한글이 아니면 그대로
    assert normalize("...원금...").tight.text == "...원금..."  # 양 끝의 장식은 이웃이 없으니 그대로


def test_a_weekday_in_parentheses_after_a_date_is_not_joined_into_a_word():
    """'1일(수)' 의 괄호까지 걷어내면 '일수' 로 붙어 정책 낱말(일수대출)에 걸린다. 숫자 + 일 바로 뒤의 요일 괄호만 남기고, 같은 모양의 우회('일(수)대출' · '원(금)보장')는 그대로 접는다."""
    for text in ("10월 1일(수) ~ 10월 31일(금)", "10월 5일 (수) 시작", "1일(월)부터"):
        assert "일수" not in normalize(text).tight.text and "일월" not in normalize(text).tight.text, text
    assert normalize("일(수)대출").tight.text == "일수대출" and normalize("원(금)보장").tight.text == "원금보장"
    assert normalize("매일(수)대출").tight.text == "매일수대출" and normalize("제1일차 일(수)").tight.text == "제1일차일수)"   # 숫자 바로 뒤의 '일' 이 아니면 요일 괄호가 아니라 우회
    assert normalize("1일 수").tight.text == "1일수" and normalize("1일.(수)").tight.text == "1일수)"
