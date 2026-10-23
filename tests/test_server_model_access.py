import pytest
from core.config import Settings


def settings(**kw):
    return Settings.from_env(openai_api_key="server-test-key",anthropic_api_key="",force_demo=False,**kw)


def test_server_funded_live_request_requires_configured_matching_access_key():
    from core.live_access import require_server_access
    s=settings()
    for provided,expected in [("", ""), ("wrong", "a"*32), ("a"*32, "b"*32), ("short", "short")]:
        with pytest.raises(PermissionError):
            require_server_access(s,s,provided,expected)
    require_server_access(s,s,"a"*32,"a"*32)


def test_demo_and_personally_funded_request_do_not_use_server_authority():
    from dataclasses import replace
    from core.live_access import require_server_access
    server=settings()
    require_server_access(replace(server,force_demo=True),server,"","")
    require_server_access(replace(server,openai_api_key="personal-test-key"),server,"","")
