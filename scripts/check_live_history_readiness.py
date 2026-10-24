"""Browser-only delayed-widget regression; no network or model request."""
import ast
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1]
release = json.loads((root / "evals/live_pilot_release.json").read_text())
before = subprocess.check_output(
    ["git", "show", release["expected_sha"] + ":scripts/verify_live_pilot_ui.py"], text=True
)
after = (root / "scripts/verify_live_pilot_ui.py").read_text()

def load_history(source):
    node = next(n for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.FunctionDef) and n.name == "listed_ids")
    scope = {"args": SimpleNamespace(verify_only=True)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                 "history-readiness", "exec"), scope)
    return scope["listed_ids"]

html = """<textarea aria-label="LIVE 본문"></textarea><script>
setTimeout(() => {
  const box = document.createElement('input');
  box.setAttribute('role', 'combobox');
  box.setAttribute('aria-label', '내 저장 기록');
  box.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowDown' && !document.querySelector('[role=option]')) {
      const item = document.createElement('div');
      item.setAttribute('role', 'option'); item.textContent = 'persisted-receipt';
      document.body.appendChild(item);
    }
  });
  document.body.appendChild(box);
}, 750);
</script>"""

observed = {}
with sync_playwright() as pw:
    browser = pw.chromium.launch(channel="chrome", headless=True)
    for label, source in (("before", before), ("after", after)):
        page = browser.new_page()
        page.set_content(html)
        page.get_by_label("LIVE 본문", exact=True).wait_for()
        observed[label] = load_history(source)(page)
        page.close()
    browser.close()
assert observed["before"] == [], observed
assert observed["after"] == ["persisted-receipt"], observed
out = root / "live-pilot-evidence"
out.mkdir(exist_ok=True)
(out / "history-readiness.json").write_text(json.dumps({
    "kind": "isolated-browser-delayed-widget", "provider_calls": 0,
    "before_wrong_empty": True, "after_persisted_receipt_found": True,
    "observed": observed,
}, indent=2) + "\n")
print("PASS delayed history widget: before wrong empty; after persisted receipt; provider calls 0")
