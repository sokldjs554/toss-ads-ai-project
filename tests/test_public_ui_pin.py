"""The public probe must pin real main and reject ambiguous caller revisions."""
import json

import pytest


SHA = "7f50da2d634f5f7d31bd321354e72db0252f0dc2"


def test_main_pin_uses_the_full_api_sha():
    from scripts.public_ui_pin import parse_main_sha

    assert parse_main_sha(json.dumps({"sha": SHA}).encode()) == SHA


@pytest.mark.parametrize("value", ["7f50da2", "main", "f" * 39, "g" * 40, "f" * 40 + "\nother=value"])
def test_bad_expected_sha_is_rejected_before_any_network(value, tmp_path, monkeypatch):
    from scripts import public_ui_pin

    def forbidden_network(*args, **kwargs):
        pytest.fail("Invalid caller SHA reached the network")

    monkeypatch.setattr(public_ui_pin.urllib.request, "urlopen", forbidden_network)
    out = tmp_path / "pin.json"
    assert public_ui_pin.main(["--expected-sha", value, "--out", str(out)]) == 1
    assert json.loads(out.read_text())["status"] == "failed"


def test_explicit_sha_must_match_the_main_snapshot():
    from scripts.public_ui_pin import parse_main_sha

    with pytest.raises(ValueError, match="does not match"):
        parse_main_sha(json.dumps({"sha": SHA}).encode(), "a" * 40)


@pytest.mark.parametrize("payload", [b"{}", b'{"sha":"main"}', b"[]", b"not json"])
def test_malformed_api_response_cannot_become_a_checkout_ref(payload):
    from scripts.public_ui_pin import parse_main_sha

    with pytest.raises(ValueError):
        parse_main_sha(payload)


def test_pin_records_default_tls_get_and_safe_action_output(tmp_path, monkeypatch):
    from scripts import public_ui_pin

    class Response:
        url = public_ui_pin.MAIN_URL

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return json.dumps({"sha": SHA}).encode()

    def public_get(request, *, timeout, context):
        assert request.full_url == public_ui_pin.MAIN_URL
        assert request.get_method() == "GET"
        assert context.check_hostname
        assert context.verify_mode == 2  # ssl.CERT_REQUIRED
        assert "Authorization" not in request.headers
        return Response()

    monkeypatch.setattr(public_ui_pin.urllib.request, "urlopen", public_get)
    out, action_output = tmp_path / "pin.json", tmp_path / "output"
    assert public_ui_pin.main(["--expected-sha", SHA, "--out", str(out), "--github-output", str(action_output)]) == 0
    record = json.loads(out.read_text())
    assert record["status"] == "pinned"
    assert record["sha"] == SHA
    assert action_output.read_text() == f"sha={SHA}\n"


def test_redirect_outside_own_repo_is_rejected_and_recorded(tmp_path, monkeypatch):
    from scripts import public_ui_pin

    class Response:
        url = "https://example.org/commits/main"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(public_ui_pin.urllib.request, "urlopen", lambda *a, **k: Response())
    out = tmp_path / "pin.json"
    assert public_ui_pin.main(["--out", str(out)]) == 1
    assert "redirect" in json.loads(out.read_text())["error"]["message"]
