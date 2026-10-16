from observability.tracer import Trace, Tracer


def test_span_nesting_and_cost_sum():
    t = Tracer()
    with t.trace("run") as tr:
        with t.span("stage", kind="stage"):
            with t.span("llm", kind="llm", cost_usd=0.01, input_tokens=5, output_tokens=7):
                pass
            with t.span("llm2", kind="llm", cost_usd=0.02):
                pass
    assert tr.root.children[0].name == "stage" and len(tr.root.children[0].children) == 2
    assert tr.total_cost_usd == 0.03 and tr.total_tokens == (5, 7)


def test_error_propagates_and_marks_span():
    t = Tracer()
    try:
        with t.trace("run"):
            with t.span("boom"):
                raise RuntimeError("x")
    except RuntimeError:
        pass
    span = t.latest().root.children[0]
    assert span.status == "error" and span.events[0]["name"] == "exception"


def test_json_roundtrip():
    t = Tracer()
    with t.trace("run", meta=1) as tr:
        with t.span("a", kind="llm", prompt_user="hi"):
            pass
    back = Trace.from_json(tr.to_json())
    assert back.trace_id == tr.trace_id and back.root.children[0].attributes["prompt_user"] == "hi"
