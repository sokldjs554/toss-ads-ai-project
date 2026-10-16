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
