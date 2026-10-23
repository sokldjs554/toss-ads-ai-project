"""Server-funded model access is separate from team-review permissions."""
import hmac


def require_server_access(settings, server_settings, provided: str, expected: str) -> None:
    funded = ((settings.has_openai and settings.openai_api_key == server_settings.openai_api_key)
              or (settings.has_anthropic and settings.anthropic_api_key == server_settings.anthropic_api_key))
    if funded and (len(expected) < 32 or not hmac.compare_digest(provided.encode(), expected.encode())):
        raise PermissionError("server-funded model access is locked")
