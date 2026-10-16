import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from core.config import Settings  # noqa: E402
from observability.tracer import Tracer  # noqa: E402


@pytest.fixture
def demo_settings() -> Settings:
    return Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True)


@pytest.fixture
def tracer() -> Tracer:
    return Tracer()
