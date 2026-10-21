"""Compare two pinned stlite runtimes with the same full project archive.

Run with isolated Playwright 1.56.0 / Chromium 141 (JSPI enabled by default):
    PREFLIGHT_RUNTIME_OUT=/tmp/runtime-proof python scripts/verify_stlite_runtime.py

The app runs in browser demonstration mode and makes no paid model API calls.
Both contexts stay idle for 360 seconds, resume a sample execution, then switch
views 60 times at seven-second intervals. Read each result.json: the historical
runtime may fail and process completion is not a quality gate.

PREFLIGHT_RUNTIME_VERSIONS=1.12.0 selects only the candidate runtime.
PREFLIGHT_RUNTIME_IDLE_SECONDS=0 explicitly limits the scope to navigation.
Choose a new output directory for each run; existing evidence is not overwritten.

PREFLIGHT_TEST_IGNORE_HTTPS_ERRORS=1 is only for a test proxy replacing public TLS
certificates. Its use is recorded in the proof; the default verifies TLS.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
site_temp = tempfile.TemporaryDirectory(prefix="preflight-stlite-runtime-")
SITE = Path(site_temp.name)
OUT = Path(
    os.environ.get("PREFLIGHT_RUNTIME_OUT", "/tmp/preflight-stlite-runtime-evidence")
).resolve()
OUT.mkdir(parents=True, exist_ok=True)
if list(OUT.glob("*/result.json")):
    raise SystemExit(
        "Choose a new output directory; existing runtime evidence is preserved."
    )
VERSIONS = ("1.11.0", "1.12.0")
selected_versions = os.environ.get("PREFLIGHT_RUNTIME_VERSIONS")
if selected_versions:
    VERSIONS = tuple(selected_versions.split(","))
if not VERSIONS or any(v not in {"1.11.0", "1.12.0"} for v in VERSIONS):
    raise SystemExit("Choose supported exact runtime pins: 1.11.0,1.12.0")
IDLE_SECONDS = int(os.environ.get("PREFLIGHT_RUNTIME_IDLE_SECONDS", "360"))
if IDLE_SECONDS < 0:
    raise SystemExit("Idle duration must be nonnegative")
sys.path.insert(0, str(ROOT))
from scripts.build_site import build_zip

ARCHIVE = SITE / "src.zip"
build_zip(ARCHIVE)
SOURCE_SHA = subprocess.check_output(
    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
).strip()
IGNORE_HTTPS_ERRORS = os.environ.get("PREFLIGHT_TEST_IGNORE_HTTPS_ERRORS") == "1"
for version in VERSIONS:
    directory = SITE / version
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text(
        re.sub(
            r"@stlite/browser@[0-9]+\.[0-9]+\.[0-9]+",
            "@stlite/browser@" + version,
            (ROOT / "site/app/index.html").read_text(),
        )
    )
    shutil.copy(ARCHIVE, directory / "src.zip")


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


server = http.server.ThreadingHTTPServer(
    ("127.0.0.1", 0), functools.partial(Handler, directory=str(SITE))
)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()


def now():
    return datetime.now(timezone.utc).isoformat()


async def observe(pw, version):
    out = OUT / version
    out.mkdir(exist_ok=True)
    started = time.monotonic()
    fatal = asyncio.Event()
    r = {
        "kind": "same-app-runtime-comparison",
        "started_at": now(),
        "stlite": version,
        "pyodide_expected": "0.29.3" if version == "1.11.0" else "314.0.3",
        "app_archive_sha256": hashlib.sha256(ARCHIVE.read_bytes()).hexdigest(),
        "headless": True,
        "viewport": {"width": 1280, "height": 900},
        "source_sha": SOURCE_SHA,
        "ignore_https_errors": IGNORE_HTTPS_ERRORS,
        "idle_seconds_planned": IDLE_SECONDS,
        "scope": "idle-and-navigation" if IDLE_SECONDS else "navigation-only",
        "switches_planned": 60,
        "switch_interval_seconds": 7,
        "model_mode": "browser demonstration; provider endpoints blocked by probe",
        "blocked_model_requests": [],
        "page_errors": [],
        "console_errors": [],
        "network_errors": [],
        "loaded_pyodide_resources": [],
        "events": [],
        "checks": {},
        "switches": [],
    }

    def save():
        (out / "result.json").write_text(
            json.dumps(r, ensure_ascii=False, indent=2) + "\n"
        )

    def event(name, **kw):
        e = {
            "event": name,
            "at": now(),
            "elapsed_seconds": round(time.monotonic() - started, 2),
            **kw,
        }
        r["events"].append(e)
        save()
        print(json.dumps({"version": version, **e}, ensure_ascii=False), flush=True)

    def error(bucket, message, **kw):
        bucket.append(
            {
                "at": now(),
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "message": message[:16000],
                **kw,
            }
        )
        if any(
            word in message.lower()
            for word in (
                "fatal error",
                "table index is out of bounds",
                "memory access out of bounds",
                "null function or function signature mismatch",
            )
        ):
            fatal.set()
            event("fatal_runtime_error")

    def response(resp):
        if resp.status >= 400:
            r["network_errors"].append(
                {"at": now(), "url": resp.url, "status": resp.status}
            )
        if "pyodide" in resp.url and (
            resp.url.endswith(".mjs") or resp.url.endswith(".wasm")
        ):
            r["loaded_pyodide_resources"].append(
                {"url": resp.url, "status": resp.status}
            )

    browser = await pw.chromium.launch(headless=True)
    r["browser_version"] = browser.version
    context = await browser.new_context(
        ignore_https_errors=IGNORE_HTTPS_ERRORS, viewport=r["viewport"]
    )
    page = await context.new_page()

    async def block_model_request(route):
        r["blocked_model_requests"].append({"at": now(), "url": route.request.url})
        await route.abort()

    await page.route(
        re.compile(r"https://api\.(?:openai|anthropic)\.com/.*"), block_model_request
    )
    page.on(
        "console",
        lambda msg: (
            error(r["console_errors"], msg.text, location=msg.location)
            if msg.type == "error"
            else None
        ),
    )
    page.on("pageerror", lambda e: error(r["page_errors"], str(e)))
    page.on("response", response)
    page.on(
        "requestfailed",
        lambda req: r["network_errors"].append(
            {"at": now(), "url": req.url, "failure": req.failure}
        ),
    )
    page.on("crash", lambda _: error(r["page_errors"], "page crash"))
    try:
        await page.goto(
            f"http://127.0.0.1:{server.server_port}/{version}/",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        r["jspi"] = await page.evaluate(
            "({Suspending: typeof WebAssembly.Suspending, promising: typeof WebAssembly.promising})"
        )
        await page.get_by_role("button", name="예시로 바로 실행", exact=True).wait_for(
            timeout=180000
        )
        r["checks"]["initial_app_ready"] = True
        await page.screenshot(path=str(out / "before-idle.png"))
        event("idle_started" if IDLE_SECONDS else "initial_app_ready")
        idle_start = time.monotonic()
        for elapsed in range(0, IDLE_SECONDS, 5):
            await asyncio.sleep(min(5, IDLE_SECONDS - elapsed))
            if fatal.is_set():
                raise RuntimeError("fatal runtime error observed during idle")
            if (elapsed + 5) % 60 == 0:
                event(f"idle_minute_{(elapsed+5)//60}")
        r["idle_seconds_observed"] = round(time.monotonic() - idle_start, 2)
        if IDLE_SECONDS:
            r["checks"]["idle_completed_without_fatal"] = True
        await page.screenshot(path=str(out / "after-idle.png"))
        await page.get_by_role("button", name="예시로 바로 실행", exact=True).click(
            timeout=30000
        )
        await page.get_by_text("광고안 1", exact=False).first.wait_for(timeout=90000)
        r["checks"]["sample_execution_completed"] = True
        if await page.locator('[data-testid="stException"]').count():
            raise RuntimeError("Streamlit exception after idle resume")
        await page.screenshot(path=str(out / "after-resume.png"))
        event("navigation_started")
        views = [
            ("성과 리포트", "성과 리포트"),
            ("검사 사례", "검사 사례"),
            ("품질 확인", "품질 확인"),
            ("운영", None),
            ("광고 작업", None),
        ]
        for i in range(60):
            view, heading = views[i % len(views)]
            await page.get_by_role("radio", name=view, exact=True).click(timeout=30000)
            if heading:
                await page.get_by_role("heading", name=heading, exact=True).wait_for(
                    timeout=30000
                )
            elif view == "운영":
                await page.get_by_role("tab", name="검수 대기열", exact=True).wait_for(
                    timeout=30000
                )
            else:
                await page.get_by_role(
                    "button", name="예시로 바로 실행", exact=True
                ).wait_for(timeout=30000)
            if await page.locator('[data-testid="stException"]').count():
                raise RuntimeError("Streamlit exception during navigation")
            if fatal.is_set():
                raise RuntimeError("fatal runtime error during navigation")
            r["switches"].append(
                {"ordinal": i + 1, "view": view, "at": now(), "completed": True}
            )
            save()
            await asyncio.sleep(7)
            if (i + 1) % 5 == 0:
                event("navigation_progress", completed=i + 1)
        r["checks"]["sixty_navigation_switches_completed"] = True
        await page.screenshot(path=str(out / "after-navigation.png"))
        r["checks"]["no_page_errors"] = not r["page_errors"]
        r["checks"]["no_fatal_runtime_error"] = not fatal.is_set()
        r["checks"]["no_model_requests"] = not r["blocked_model_requests"]
        r["status"] = "passed" if all(r["checks"].values()) else "failed"
        event("finished")
    except Exception as exc:
        r["status"] = "failed"
        r["error_type"] = type(exc).__name__
        r["error"] = str(exc)
        try:
            await page.screenshot(path=str(out / "failure.png"))
            r["visible_text_at_failure"] = (await page.locator("body").inner_text())[
                -12000:
            ]
        except Exception as e:
            r["capture_error"] = str(e)
        event("failed")
    finally:
        r["finished_at"] = now()
        save()
        await context.close()
        await browser.close()
    return {
        "version": version,
        "status": r["status"],
        "checks": r["checks"],
        "switches": len(r["switches"]),
        "page_errors": len(r["page_errors"]),
        "console_errors": len(r["console_errors"]),
    }


async def main():
    try:
        async with async_playwright() as pw:
            results = await asyncio.gather(*(observe(pw, v) for v in VERSIONS))
            print(json.dumps(results, ensure_ascii=False), flush=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        site_temp.cleanup()


asyncio.run(main())
