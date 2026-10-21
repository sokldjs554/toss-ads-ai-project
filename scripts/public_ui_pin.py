"""Pin the public repository's main once, without credentials or remote writes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import ssl
import urllib.request

MAIN_URL = "https://api.github.com/repos/sokldjs554/toss-ads-ai-project/commits/main"


def validate_sha(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("Expected SHA must be the complete 40-character lowercase commit SHA")
    return value


def parse_main_sha(body: bytes, expected: str = "") -> str:
    if expected:
        validate_sha(expected)
    record = json.loads(body)
    if not isinstance(record, dict):
        raise ValueError("GitHub main response must be an object")
    value = record.get("sha")
    if not isinstance(value, str):
        raise ValueError("GitHub main response has no commit SHA")
    validate_sha(value)
    if expected and expected != value:
        raise ValueError("Expected SHA does not match the main snapshot; no other revision will be probed")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", default="")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    record = {"status": "started", "started_at": datetime.now(timezone.utc).isoformat(),
              "url": MAIN_URL, "expected_sha": args.expected_sha, "tls_verification": True}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    try:
        if args.expected_sha:
            validate_sha(args.expected_sha)
        request = urllib.request.Request(MAIN_URL, headers={"Accept": "application/vnd.github+json", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(request, timeout=30, context=ssl.create_default_context()) as response:
            if response.url != MAIN_URL:
                raise ValueError("Unexpected redirect outside the requested repository main endpoint")
            body = response.read(1_000_001)
        if len(body) > 1_000_000:
            raise ValueError("GitHub main response exceeded the read limit")
        pinned = parse_main_sha(body, args.expected_sha)
        record.update(status="pinned", sha=pinned)
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as output:
                output.write(f"sha={pinned}\n")
        print(pinned)
    except Exception as exc:
        record.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)})
    finally:
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        args.out.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return int(record["status"] != "pinned")


if __name__ == "__main__":
    raise SystemExit(main())
