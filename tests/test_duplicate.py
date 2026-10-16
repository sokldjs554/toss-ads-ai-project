from guardrails.duplicate import check, similarity


def test_similar_resubmission_detected():
    rejected = ["누구나 무조건 당일 승인! 신용불량자 OK, 5분 입금"]
    f, score = check("누구나 무조건 당일 승인!! 신용불량자 OK, 3분 입금", rejected)
    assert f is not None and f.rule_id == "TA-DUP-01" and score >= 0.6


def test_different_copy_not_flagged():
    f, score = check("연 4.5%~15.0% 신용대출, 조건을 확인하세요", ["누구나 무조건 당일 승인"])
    assert f is None and score < 0.2


def test_similarity_bounds():
    assert similarity("abc", "abc") == 1.0 and similarity("", "abc") == 0.0
