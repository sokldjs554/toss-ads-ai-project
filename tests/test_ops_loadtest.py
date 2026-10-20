"""처리량 측정 스크립트(scripts/loadtest.py): 모델·네트워크 호출 없이 도는지, 결과 형식과 한계 문구, 저장된 측정 결과 파일.

측정값 자체(처리량·지연)는 기계마다 달라 숫자를 고정하지 않는다 — 구조와 불변식(p50 ≤ p95 ≤ p99 ≤ 최대, 오류 0, 모델 호출 0)만 본다.
"""
from __future__ import annotations

import importlib.util
import json
import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STORED = ROOT / "evals" / "reports" / "loadtest.json"


@pytest.fixture(scope="module")
def lt():
    spec = importlib.util.spec_from_file_location("loadtest_script", ROOT / "scripts" / "loadtest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def small_report(lt):
    return lt.run_loadtest(n=12, warmup=2, concurrency=(1, 3), repeats=2, seed=0)


def test_report_structure_and_invariants(small_report):
    r = small_report
    assert r["kind"] == "loadtest" and r["model_calls"] == 0 and r["network_calls"] == 0
    assert set(r["workloads"]) == {"guardrails", "pipeline_mock"}
    assert r["config"]["n_per_repeat"] == 12 and r["config"]["warmup_calls"] == 2 and r["config"]["concurrency"] == [1, 3] and r["config"]["repeats"] == 2
    for name, w in r["workloads"].items():
        assert [x["concurrency"] for x in w["results"]] == [1, 3]
        for x in w["results"]:
            lat = x["latency_ms"]
            assert x["errors"] == 0 and lat["n"] == 12 * 2
            assert 0 < lat["p50"] <= lat["p95"] <= lat["p99"] <= lat["max"] and lat["mean"] > 0
            assert x["throughput_per_s_median"] > 0 and len(x["throughput_per_s_all"]) == 2 and x["warmup_calls"] == 2
        m = w["memory"]
        assert len(m["retained_after_each_third_kb"]) == 3 and m["calls"] == 12 and "growth_per_call_second_to_last_third_bytes" in m
    json.dumps(r, ensure_ascii=False, allow_nan=False)


def test_report_states_what_it_does_and_does_not_measure_and_records_the_environment(small_report):
    r = small_report
    assert "모델 지연이 없" in r["what_this_is"] and "우리 코드의 처리량" in r["what_this_is"]
    joined = " ".join(r["what_this_is_not"])
    assert "실제 모델을 부르는 서비스" in joined and "용량 계획" in joined and "GIL" in joined
    env = r["environment"]
    assert env["python"].count(".") >= 1 and env["cpu_count"] and env["cpu_model"] and "load_average_at_start" in env and env["implementation"]
    assert r["command"].startswith("python scripts/loadtest.py")


def test_loadtest_makes_no_network_connections(lt, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("처리량 측정이 네트워크를 썼다")

    monkeypatch.setattr(socket.socket, "connect", boom)
    r = lt.run_loadtest(n=4, warmup=1, concurrency=(1,), repeats=1)
    assert all(x["errors"] == 0 for w in r["workloads"].values() for x in w["results"])


def test_invalid_arguments_are_refused(lt):
    for kw in ({"n": 0}, {"repeats": 0}, {"warmup": -1}, {"concurrency": ()}, {"concurrency": (0,)}):
        with pytest.raises(ValueError):
            lt.run_loadtest(**{"n": 3, "warmup": 0, "concurrency": (1,), "repeats": 1, **kw})


def test_single_workload_selection(lt):
    r = lt.run_loadtest(n=3, warmup=0, concurrency=(1,), repeats=1, workloads=["guardrails"])
    assert list(r["workloads"]) == ["guardrails"]


def test_cli_writes_a_json_report(lt, tmp_path, capsys):
    out = tmp_path / "sub" / "lt.json"
    assert lt.main(["--n", "3", "--warmup", "0", "--concurrency", "1", "--repeats", "1", "--workloads", "guardrails", "--out", str(out)]) == 0
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["kind"] == "loadtest" and "--workloads guardrails" in d["command"]
    assert "처리량" in capsys.readouterr().out


def test_percentile_helper(lt):
    assert lt.percentile([1, 2, 3, 4, 5], 0.5) == 3 and lt.percentile([1, 2, 3, 4, 5], 0.95) == pytest.approx(4.8)
    s = lt.summarize_ms([1.0, 2.0, 3.0])
    assert s["p50"] == 2.0 and s["max"] == 3.0 and lt.summarize_ms([])["p50"] is None


def test_throughput_counts_successes_and_uses_the_median_for_even_repeats(lt, monkeypatch):
    """Known wall times isolate rate arithmetic from machine speed; tasks still use the real report builder."""
    runs = iter([
        {"wall_s": 1.0, "latencies_ms": [1.0] * 6, "errors": ["failed"] * 2},
        {"wall_s": 2.0, "latencies_ms": [1.0] * 6, "errors": ["failed"] * 4},
    ])
    monkeypatch.setattr(lt, "run_once", lambda task, n, concurrency: next(runs))
    report = lt.measure_workload("test", lambda seed: (lambda i: None, 1, "local test"),
                                 n=6, warmup=0, concurrency=(1,), repeats=2, seed=0)
    result = report["results"][0]
    assert result["throughput_per_s_all"] == [4.0, 1.0]
    assert result["throughput_per_s_median"] == 2.5
    assert result["attempt_throughput_per_s_all"] == [6.0, 3.0]
    assert result["attempt_throughput_per_s_median"] == 4.5
    assert result["successful_calls"] == 6 and result["attempted_calls"] == 12 and result["errors"] == 6


@pytest.mark.parametrize("failed_phase", ["warmup", "timed", "memory"])
def test_cli_saves_failures_from_each_phase_and_returns_nonzero(lt, monkeypatch, tmp_path, failed_phase):
    calls = 0

    def task(i):
        nonlocal calls
        calls += 1
        phase = "warmup" if calls == 1 else "timed" if calls <= 4 else "memory"
        if phase == failed_phase:
            raise RuntimeError(f"local {phase} failure")

    monkeypatch.setattr(lt, "WORKLOADS", {"test": lambda seed: (task, 1, "local test")})
    out = tmp_path / "failures.json"
    result = lt.main(["--n", "3", "--warmup", "1", "--concurrency", "1", "--repeats", "1", "--out", str(out)])
    assert result == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    workload = report["workloads"]["test"]
    assert calls == 7
    assert workload["warmup"]["errors"] == (1 if failed_phase == "warmup" else 0)
    assert workload["results"][0]["errors"] == (3 if failed_phase == "timed" else 0)
    assert workload["memory"]["errors"] == (3 if failed_phase == "memory" else 0)
    if failed_phase == "timed":
        assert workload["results"][0]["throughput_per_s_median"] == 0.0
    assert report["model_calls"] == 0 and report["network_calls"] == 0


# --- 저장된 측정 결과 ---------------------------------------------------------------------------
def test_stored_report_exists_with_environment_and_the_required_caveats():
    assert STORED.exists(), "evals/reports/loadtest.json 이 없다 — python scripts/loadtest.py 로 만든다"
    d = json.loads(STORED.read_text(encoding="utf-8"))
    assert d["kind"] == "loadtest" and d["model_calls"] == 0 and d["network_calls"] == 0
    assert "모델 지연이 없" in d["what_this_is"] and any("실제 모델을 부르는 서비스" in x for x in d["what_this_is_not"])
    assert d["environment"]["python"] and d["environment"]["cpu_model"] and d["environment"]["cpu_count"]
    c = d["config"]
    assert c["n_per_repeat"] >= 300 and c["warmup_calls"] >= 1 and c["repeats"] >= 2 and 1 in c["concurrency"] and max(c["concurrency"]) > 1
    for w in d["workloads"].values():
        for x in w["results"]:
            lat = x["latency_ms"]
            assert x["errors"] == 0 and lat["p50"] <= lat["p95"] <= lat["p99"] <= lat["max"]
        assert w["memory"]["calls"] >= 300
