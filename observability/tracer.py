"""경량 트레이서. OpenTelemetry 의 trace/span 모델을 따르되 의존성 없이 구현했다.

- Trace: 파이프라인 실행 1회. Span 트리를 가진다.
- Span: stage(단계) / llm(모델 호출) / guardrail(가드레일) / tool 종류. 시작·종료·상태·속성·이벤트.
- LLM 스팬은 provider·model·prompt·response·tokens·cost·latency·fallback_chain 을 속성으로 남긴다.
- JSON 으로 내보내고 다시 읽을 수 있어 '재생(replay)' 이 가능하다 — API 없이 과거 실행을 그대로 본다.

contextvars 로 현재 스팬을 추적하므로 파이프라인 코드는 `with tracer.span(...)` 만 쓰면 된다.
"""
from __future__ import annotations

import contextvars
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

SpanKind = str  # "stage" | "llm" | "guardrail" | "tool"
SpanStatus = str  # "ok" | "error" | "intercepted" | "fallback" | "running"


def _sid() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Span:
    name: str
    kind: SpanKind
    trace_id: str
    span_id: str = field(default_factory=_sid)
    parent_id: Optional[str] = None
    start_ms: float = field(default_factory=lambda: time.time() * 1000)
    end_ms: Optional[float] = None
    status: SpanStatus = "running"
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    children: list["Span"] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        end = self.end_ms if self.end_ms is not None else time.time() * 1000
        return round(end - self.start_ms, 1)

    def set(self, **attrs: Any) -> None:
        self.attributes.update(attrs)

    def event(self, name: str, **attrs: Any) -> None:
        self.events.append({"name": name, "t_ms": round(time.time() * 1000 - self.start_ms, 1), **attrs})

    def end(self, status: SpanStatus = "ok") -> None:
        self.end_ms = time.time() * 1000
        if self.status == "running":
            self.status = status

    def walk(self) -> Iterator["Span"]:
        yield self
        for c in self.children:
            yield from c.walk()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "status": self.status,
            "attributes": _jsonable(self.attributes),
            "events": self.events,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Span":
        s = cls(
            name=d["name"], kind=d["kind"], trace_id=d["trace_id"], span_id=d["span_id"],
            parent_id=d.get("parent_id"), start_ms=d["start_ms"], end_ms=d.get("end_ms"),
            status=d.get("status", "ok"), attributes=d.get("attributes", {}), events=d.get("events", []),
        )
        s.children = [cls.from_dict(c) for c in d.get("children", [])]
        return s


@dataclass
class Trace:
    name: str
    trace_id: str = field(default_factory=lambda: "tr_" + _sid())
    root: Optional[Span] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    # --- 집계 -------------------------------------------------------------
    def spans(self) -> list[Span]:
        return list(self.root.walk()) if self.root else []

    def llm_spans(self) -> list[Span]:
        return [s for s in self.spans() if s.kind == "llm"]

    @property
    def total_cost_usd(self) -> float:
        return round(sum(float(s.attributes.get("cost_usd", 0.0)) for s in self.llm_spans()), 6)

    @property
    def total_tokens(self) -> tuple[int, int]:
        i = sum(int(s.attributes.get("input_tokens", 0)) for s in self.llm_spans())
        o = sum(int(s.attributes.get("output_tokens", 0)) for s in self.llm_spans())
        return i, o

    @property
    def duration_ms(self) -> float:
        return self.root.duration_ms if self.root else 0.0

    def status_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.spans():
            out[s.status] = out.get(s.status, 0) + 1
        return out

    # --- 직렬화 -------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "metadata": _jsonable(self.metadata),
            "root": self.root.to_dict() if self.root else None,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Trace":
        t = cls(name=d["name"], trace_id=d["trace_id"], metadata=d.get("metadata", {}))
        if d.get("root"):
            t.root = Span.from_dict(d["root"])
        return t

    @classmethod
    def from_json(cls, s: str) -> "Trace":
        return cls.from_dict(json.loads(s))


_current_span: contextvars.ContextVar[Optional[Span]] = contextvars.ContextVar("preflight_span", default=None)
_current_trace: contextvars.ContextVar[Optional[Trace]] = contextvars.ContextVar("preflight_trace", default=None)


class Tracer:
    """트레이스 생성·스팬 중첩·저장소. 인스턴스 하나가 세션(또는 프로세스) 하나의 저장소다."""

    def __init__(self, max_traces: int = 50) -> None:
        self.traces: list[Trace] = []
        self.max_traces = max_traces

    # 트레이스 수명 ---------------------------------------------------------
    @contextmanager
    def trace(self, name: str, **metadata: Any) -> Iterator[Trace]:
        t = Trace(name=name, metadata=metadata)
        root = Span(name=name, kind="stage", trace_id=t.trace_id)
        t.root = root
        tok_t = _current_trace.set(t)
        tok_s = _current_span.set(root)
        try:
            yield t
            root.end("ok")
        except Exception as e:  # 루트까지 올라온 예외는 기록하고 다시 던진다
            root.event("exception", type=type(e).__name__, message=str(e)[:500])
            root.end("error")
            raise
        finally:
            _current_span.reset(tok_s)
            _current_trace.reset(tok_t)
            self.traces.append(t)
            if len(self.traces) > self.max_traces:
                self.traces.pop(0)

    # 스팬 ----------------------------------------------------------------
    @contextmanager
    def span(self, name: str, kind: SpanKind = "stage", **attrs: Any) -> Iterator[Span]:
        parent = _current_span.get()
        trace = _current_trace.get()
        trace_id = trace.trace_id if trace else "detached"
        s = Span(name=name, kind=kind, trace_id=trace_id, parent_id=parent.span_id if parent else None)
        s.attributes.update(attrs)
        if parent is not None:
            parent.children.append(s)
        tok = _current_span.set(s)
        try:
            yield s
            s.end("ok")
        except Exception as e:
            s.event("exception", type=type(e).__name__, message=str(e)[:500])
            s.end("error")
            raise
        finally:
            _current_span.reset(tok)

    @staticmethod
    def current() -> Optional[Span]:
        return _current_span.get()

    @staticmethod
    def current_trace() -> Optional[Trace]:
        return _current_trace.get()

    def latest(self) -> Optional[Trace]:
        return self.traces[-1] if self.traces else None

    def find(self, trace_id: str) -> Optional[Trace]:
        for t in self.traces:
            if t.trace_id == trace_id:
                return t
        return None


_default_tracer = Tracer()


def get_tracer() -> Tracer:
    return _default_tracer


def set_tracer(t: Tracer) -> None:
    global _default_tracer
    _default_tracer = t


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if hasattr(obj, "model_dump"):
        return _jsonable(obj.model_dump(mode="json"))
    return str(obj)
