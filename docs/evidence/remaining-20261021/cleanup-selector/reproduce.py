"""Exercise the public QA cleanup helper against a local synthetic memory UI.

No public service, account secret, or database is used. The QA module is parsed
as text, and only its area/select_cleanup_account functions are compiled. The
module's main function and credential-reading code never run. Only dummy names,
boolean observations, source hashes, and exception class names reach the report.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

from playwright.sync_api import sync_playwright


ROOT = Path("/workspace/scratch/30d72ff77b94")
REPO = ROOT / "toss-ads-resume"
QA = REPO / "scripts/verify_public_team_review.py"
RESULT = ROOT / "ads-tools/cleanup-selector-dom-result-20261021.json"
BROWSER = ROOT / "ads-tools/browsers/chromium-1161/chrome-linux/chrome"


def main() -> int:
    source = QA.read_bytes()
    tree = ast.parse(source)
    helper_nodes = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name in {"area", "select_cleanup_account"}
    ]
    assert len(helper_nodes) == 2
    helpers: dict = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=helper_nodes, type_ignores=[])), str(QA), "exec"), helpers)
    report = {
        "status": "running",
        "test_type": "local native browser, synthetic MemoryStore",
        "qa_source_sha256": hashlib.sha256(source).hexdigest(),
        "actual_human_reviews": 0,
        "external_database_calls": 0,
        "fixture_accounts": 2,
        "checks": [],
    }
    checks = report["checks"]

    def check(name: str, passed: bool, **observed) -> None:
        checks.append({"name": name, "passed": bool(passed), **observed})
        if not passed:
            raise AssertionError(name)

    app = '''import sys
sys.path.insert(0, REPO)
sys.path.insert(0, REPO + "/tests")
import streamlit as st
from test_team_review_ui import MemoryStore
from ui import team_review_view

if "_probe_store" not in st.session_state:
    store = MemoryStore()
    store.account("DOM 검수 A")
    store.account("DOM 검수 B")
    st.session_state["_probe_store"] = store
    st.session_state["team_access_key"] = store.owner
store = st.session_state["_probe_store"]
with st.tabs(["공동 검수"])[0]:
    team_review_view.render(store=store)
selected = st.session_state.get("team_target_account")
if selected:
    account = store.state["accounts"][selected]
    st.caption("PROBE selected=" + account["name"] + "; active=" + str(account["active"]))
'''.replace("REPO", repr(str(REPO)))

    # The subprocess is not given provider, database, or application secrets.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "LD_LIBRARY_PATH") if key in os.environ}
    env.update(
        PYTHONUNBUFFERED="1",
        PLAYWRIGHT_BROWSERS_PATH=str(ROOT / "ads-tools/browsers"),
        FONTCONFIG_FILE=str(ROOT / "ads-tools/fonts.conf"),
    )
    step = "local fixture startup"
    try:
        with TemporaryDirectory(prefix="cleanup-selector-dom-", dir="/tmp") as temp:
            folder = Path(temp)
            (folder / "app.py").write_text(app)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            with (folder / "server.log").open("w") as log:
                proc = subprocess.Popen(
                    [str(REPO / ".venv311/bin/python"), "-m", "streamlit", "run", str(folder / "app.py"),
                     "--server.headless=true", "--server.address=127.0.0.1", f"--server.port={port}",
                     "--browser.gatherUsageStats=false"],
                    cwd=folder, env=env, stdout=log, stderr=subprocess.STDOUT,
                )
                try:
                    url = f"http://127.0.0.1:{port}"
                    healthy = False
                    for _ in range(100):
                        try:
                            with urlopen(url + "/_stcore/health", timeout=0.5) as response:
                                if response.status == 200:
                                    healthy = True
                                    break
                        except Exception:
                            time.sleep(0.1)
                    check("local memory server ready", healthy)
                    with sync_playwright() as pw:
                        browser = pw.chromium.launch(headless=True, executable_path=str(BROWSER))
                        try:
                            page = browser.new_page()
                            page.set_default_timeout(15000)
                            page.route("**/*", lambda route: route.continue_() if urlsplit(route.request.url).hostname == "127.0.0.1" else route.abort())
                            page.goto(url)
                            area = helpers["area"](page)
                            combo = area.get_by_role("combobox", name="관리할 담당자", exact=True)
                            page.get_by_text("PROBE selected=DOM 검수 A; active=True", exact=True).wait_for()
                            check("first active account starts selected", combo.input_value() == "DOM 검수 A")

                            for number, name in enumerate(("DOM 검수 A", "DOM 검수 B"), 1):
                                step = f"select fixture account {number}"
                                check(f"helper selects account {number}", helpers["select_cleanup_account"](page, name))
                                page.get_by_text(f"PROBE selected={name}; active=True", exact=True).wait_for()
                                check(f"server and input agree for account {number}", combo.input_value() == name)
                                step = f"deactivate fixture account {number}"
                                active = area.get_by_role("button", name="계정 비활성화", exact=True)
                                inactive = area.get_by_role("button", name="계정 다시 활성화", exact=True)
                                active.click()
                                inactive.wait_for()
                                page.get_by_text(f"PROBE selected={name}; active=False", exact=True).wait_for()
                                check(f"account {number} disabled with correct selection", combo.input_value() == name and inactive.is_visible())

                            step = "idempotent selected disabled account"
                            check("already disabled selection is idempotent", helpers["select_cleanup_account"](page, "DOM 검수 B"))
                            check("disabled account remains selected", combo.input_value() == "DOM 검수 B" and area.get_by_role("button", name="계정 다시 활성화", exact=True).is_visible())
                            report["status"] = "completed"
                        finally:
                            browser.close()
                finally:
                    proc.terminate()
                    try:
                        proc.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = {"step": step, "type": type(exc).__name__}
    report["passed_checks"] = sum(check["passed"] for check in checks)
    report["total_checks"] = len(checks)
    RESULT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "passed": report["passed_checks"], "total": report["total_checks"], "result": str(RESULT)}, ensure_ascii=False))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
