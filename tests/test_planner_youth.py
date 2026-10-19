"""플래너의 미성년 타겟 검사: '만 19세 이상' 은 통과하고 만 19세 미만 표현은 계속 걸린다."""
import pytest

from agents.planner import targets_minors


@pytest.mark.parametrize("text", ["만 19세 이상 직장인", "만 19세 이상 30대", "만 25세~39세", "만 19~34세 남성", "20대 직장인", "만 100세 이상", "만 190세"])
def test_adult_targets_are_not_flagged(text):
    assert not targets_minors(text)


@pytest.mark.parametrize("text", ["10대 학생", "청소년 대상", "미성년자 포함", "고등학생 누구나", "만 14세 이상", "만 18세 이하 학생", "만 1세 유아 부모", "만 9세 어린이", "만14세"])
def test_minor_targets_are_flagged(text):
    assert targets_minors(text)
