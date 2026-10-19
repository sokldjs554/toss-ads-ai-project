"""이미지 입력: 제공자가 이미지를 올바른 모양으로 싣고, 이미지 없는 요청은 이전과 똑같이 글만 보내는지."""
import base64
import types

from llm.anthropic_provider import AnthropicProvider, _user_content as anthropic_content
from llm.base import ImageInput, LLMRequest, Task
from llm.openai_provider import OpenAIProvider, _user_content as openai_content
from llm.router import Router
from core.config import Settings
from observability.tracer import Tracer

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _req(images=None) -> LLMRequest:
    return LLMRequest(task=Task.VISION, system="s", user="읽어 줘", images=images or [])


def test_openai_without_images_sends_plain_text():
    assert openai_content(_req()) == "읽어 줘"


def test_openai_image_goes_as_data_url_after_text():
    parts = openai_content(_req([ImageInput(PNG, "image/png", "a.png")]))
    assert parts[0] == {"type": "text", "text": "읽어 줘"}
    url = parts[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,") and base64.b64decode(url.split(",", 1)[1]) == PNG


def test_anthropic_without_images_sends_plain_text():
    assert anthropic_content(_req()) == "읽어 줘"


def test_anthropic_image_blocks_come_before_text():
    parts = anthropic_content(_req([ImageInput(PNG, "image/png")]))
    assert parts[0]["type"] == "image" and parts[0]["source"]["media_type"] == "image/png"
    assert base64.b64decode(parts[0]["source"]["data"]) == PNG and parts[-1] == {"type": "text", "text": "읽어 줘"}


def test_openai_provider_passes_image_parts_to_the_client():
    seen = {}

    class _Completions:
        def create(self, **kw):
            seen.update(kw)
            msg = types.SimpleNamespace(content="ok", refusal=None)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")],
                                         usage=types.SimpleNamespace(prompt_tokens=3, completion_tokens=1))

    prov = OpenAIProvider("k", "gpt-4o")
    prov._client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=_Completions()))
    prov.complete(_req([ImageInput(PNG, "image/png")]))
    user = seen["messages"][1]["content"]
    assert isinstance(user, list) and user[1]["type"] == "image_url"


def test_router_trace_records_image_metadata_not_bytes():
    from tests.fakes import FakeProvider

    tracer = Tracer()
    r = Router(Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True), providers={"mock": FakeProvider("mock")}, tracer=tracer)
    with tracer.trace("t"):
        r.complete(_req([ImageInput(PNG, "image/png", "banner.png")]))
    span = tracer.latest().llm_spans()[0]
    imgs = span.attributes["images"]
    assert imgs == [{"name": "banner.png", "mime": "image/png", "bytes": len(PNG), "sha256": imgs[0]["sha256"]}]
    blob = repr(tracer.latest().to_dict())
    assert PNG.hex() not in blob and base64.b64encode(PNG).decode() not in blob
