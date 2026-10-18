"""승인받은 실제 모델 비교 직전의 마지막 점검(4차 점검 뒤)에서 재현된 두 결함 — 저장할 수 없는 --out 이름, 반복문이 끝난 직후의 신호 틈.

가짜 제공자로만 돌린다: 네트워크도 실제 모델도 쓰지 않는다.
"""
from __future__ import annotations

import pytest

from evals import compare
from tests.test_round3_compare import env, files, read, router_of  # noqa: F401  (env: 픽스처)
from tests.test_round4_compare import GOLDEN, INC, LIVE_JSON, provider, run_main


# ───────────── --out: 저장할 수 없는 이름은 호출 전에 막는다 ─────────────
@pytest.mark.parametrize("out", [".", "..", "history", "notes.txt", "a/b/..", "compare_live", "baseline_partial.json"])
def test_an_unusable_out_name_is_refused_before_any_model_call(env, out, capsys):
    """예전에는 '--out .' 이 그대로 받아들여져, 돈을 쓴 완료 결과를 저장하지 못하고 출력에만 남겼다."""
    (env.reports / "history").mkdir()
    (env.reports / "baseline_partial.json").write_text("PAID-BASELINE", encoding="utf-8")        # 이미 있는 다른 결과 파일
    op = provider()
    env(router_of(op))
    assert compare.main(["--out", out]) == 2
    assert op.calls == 0                                                                           # 한 번도 부르지 않았다
    assert "실행하지 않음" in capsys.readouterr().out
    assert files(env.reports) == ["baseline_partial.json"] and (env.reports / "baseline_partial.json").read_text(encoding="utf-8") == "PAID-BASELINE"


@pytest.mark.parametrize("out", [None, "compare_live.json", "compare_custom.json", "x/compare_other.json"])
def test_a_usable_out_name_still_runs_and_saves_a_complete_result(env, out):
    op = provider()
    env(router_of(op))
    assert compare.out_problem(out) == ""
    assert compare.main(["--datasets", "contrast_g069", "--out", out] if out else ["--datasets", "contrast_g069"]) in (0, 2)     # 일부 범위라 완료로 세지 않아 2 일 수 있다 — 막히지만 않으면 된다
    assert op.calls > 0


# ───────────── 반복문 직후의 신호 틈 ─────────────
@pytest.mark.parametrize("exc", [KeyboardInterrupt(), compare.TerminatedBySignal(143)])
def test_a_signal_after_the_measuring_loop_but_before_the_report_still_keeps_every_row(env, monkeypatch, exc):
    """run_compare 가 모든 행을 측정한 뒤 리포트를 돌려주는 순간에 취소 신호가 온 경우: 예전에는 main 에서 예외가 그대로 올라가 파일이 하나도 남지 않았다(측정한 행 전부 소실)."""
    real = compare.run_compare

    def late_signal(*a, **k):
        real(*a, **k)
        raise exc

    op = provider(cost=0.01)
    env(router_of(op))
    monkeypatch.setattr(compare, "run_compare", late_signal)
    assert run_main([]) in (type(exc).__name__, "SystemExit")                                       # 저장한 뒤 프로세스는 중단된 채로 끝난다 (SIGTERM 은 종료 코드 143 의 SystemExit)
    assert files(env.reports) == [INC]                                                              # 완료된 결과 이름(compare_live.json)은 만들지 않았다
    rep = read(env.reports, INC)
    assert rep["complete"] is False and rep["run_kind"] == "live_incomplete" and "interrupted" in rep["incomplete_reason"]
    assert len(rep["rows"]) == 330 and rep["aborted"]["kind"] == "interrupted"                      # 승인된 범위 110건 × 세 방식 — 한 행도 잃지 않았다
    assert rep["cost"]["spent_usd"] == pytest.approx(0.01 * 220) and rep["case_counts"]["golden"]["completed"] == len(GOLDEN)


def test_an_error_after_the_loop_also_keeps_the_rows(env, monkeypatch):
    real = compare.run_compare

    def late_error(*a, **k):
        real(*a, **k)
        raise MemoryError("report")

    env(router_of(provider(cost=0.01)))
    monkeypatch.setattr(compare, "run_compare", late_error)
    assert run_main([]) == "MemoryError" or files(env.reports) == [INC]
    assert files(env.reports) == [INC] and len(read(env.reports, INC)["rows"]) == 330
    assert read(env.reports, INC)["aborted"]["kind"] == "harness_error"


def test_a_failure_before_any_row_is_measured_is_not_turned_into_a_report(env, monkeypatch):
    """측정이 시작되기 전의 오류(예: 잘못된 데이터 이름)에는 구조 리포트를 만들지 않는다 — 그대로 올라가고 아무 파일도 쓰지 않는다."""
    env(router_of(provider()))

    def boom(*a, **k):
        raise ValueError("시작 전 오류")

    monkeypatch.setattr(compare, "run_compare", boom)
    with pytest.raises(ValueError):
        compare.main([])
    assert files(env.reports) == []
