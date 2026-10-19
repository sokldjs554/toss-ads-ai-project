"""이미지 소재 시험 공용 도구 (시험 함수는 없다). 통제된 가짜 제공자만 쓴다 — 네트워크·실제 모델 호출 없음, 실제 비전 모델의 정확도 측정이 아니다."""
from __future__ import annotations

import importlib.util
import struct
import zlib
from pathlib import Path

from core.config import Settings
from llm.base import Task
from llm.router import Router
from observability.tracer import Tracer
from tests.fakes import TaskScriptedProvider

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "evals" / "datasets" / "images"

_spec = importlib.util.spec_from_file_location("image_make_fixtures", FIXTURE_DIR / "make_fixtures.py")
mf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mf)       # 표준 라이브러리만 쓰는 모듈이라 import 시 부작용이 없다

LIVE = Settings.from_env(openai_api_key="sk-fake-not-a-real-key", anthropic_api_key="", force_demo=False)
DEMO = Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True)


def vision_router(handler, tracer: Tracer | None = None):
    """Task.VISION 에 통제된 응답(dict · 예외 · 요청을 받는 함수)을 내는 라우터. (router, provider) 를 돌려준다."""
    prov = TaskScriptedProvider("openai", {Task.VISION: handler})
    return Router(LIVE, providers={"openai": prov}, tracer=tracer or Tracer()), prov


def demo_router(tracer: Tracer | None = None) -> Router:
    return Router(DEMO, tracer=tracer or Tracer())


def vision_out(*segments: str, tags=(), readable: bool = True, notes: str = "") -> dict:
    return {"readable": readable, "text_segments": [{"text": s, "region": "center"} for s in segments], "tags": list(tags), "notes": notes}


def png_with_size(w: int, h: int) -> bytes:
    """머리글이 w×h 를 주장하는 PNG (디코딩하지 않는 검사 시험용)."""
    return mf.png_header_only(w, h)


def chunk(ctype: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF)
