import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from core.config import Settings  # noqa: E402
from observability.tracer import Tracer  # noqa: E402


_APP_ENV = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_MODEL", "ANTHROPIC_MODEL", "PREFLIGHT_FORCE_DEMO", "PREFLIGHT_REQUEST_TIMEOUT_S", "PREFLIGHT_MAX_RETRIES",
            "PREFLIGHT_RUN_BUDGET_USD", "PREFLIGHT_AUTO_DECISION_CONFIDENCE", "PREFLIGHT_BUILD", "RENDER_GIT_COMMIT")


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """개발자 셸에 PREFLIGHT_FORCE_DEMO=1 이나 실제 API 키가 있어도 테스트 결과가 달라지지 않게, 앱이 읽는 환경변수를 모든 테스트에서 비운다.
    환경변수가 필요한 테스트는 자기 monkeypatch.setenv 로 직접 넣는다 (이 픽스처가 먼저 실행된다)."""
    for name in _APP_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def demo_settings() -> Settings:
    return Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True)


@pytest.fixture
def tracer() -> Tracer:
    return Tracer()
