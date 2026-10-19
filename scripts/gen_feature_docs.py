"""docs/FEATURES.md 안의 숫자 표를 저장된 평가 리포트(evals/reports/*.json)에서 다시 만든다 — 숫자를 손으로 옮겨 적지 않는다.

    python scripts/gen_feature_docs.py          # 파일을 갱신
    python scripts/gen_feature_docs.py --check  # 어긋나 있으면 종료 코드 1 (테스트가 같은 검사를 한다)

문서 안의 `<!-- generated:<키>:start -->` 와 `<!-- generated:<키>:end -->` 사이만 바뀐다. 표의 행은 화면의 '새 기능 평가' 와 같은 함수(ui/new_evals.py)가 만든다.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DOC = ROOT / "docs" / "FEATURES.md"
KEYS = ("dialogs", "urls", "images", "quality", "insights")
PATTERN = re.compile(r"(<!-- generated:(\w+):start -->\n)(.*?)(<!-- generated:\2:end -->)", re.S)


def table_md(key: str) -> str:
    from ui import new_evals

    rows = new_evals.rows_for(key)
    if rows is None:
        return f"_저장된 평가 결과가 없습니다 ({new_evals.ROWS[key][0]})._\n"
    lines = ["| 지표 | 개발용 | 최종 확인용 |", "|---|---|---|"]
    lines += [f"| {a} | {b} | {c} |" for a, b, c in rows]
    return "\n".join(lines) + "\n"


def render(text: str) -> str:
    def repl(m: re.Match) -> str:
        key = m.group(2)
        if key not in KEYS:
            raise SystemExit(f"알 수 없는 생성 블록: {key}")
        return f"{m.group(1)}{table_md(key)}{m.group(4)}"

    return PATTERN.sub(repl, text)


def main(argv: list[str]) -> int:
    cur = DOC.read_text(encoding="utf-8")
    new = render(cur)
    if "--check" in argv:
        if new != cur:
            print("docs/FEATURES.md 의 숫자 표가 저장된 평가 결과와 다릅니다 — python scripts/gen_feature_docs.py 로 갱신하세요.")
            return 1
        print("docs/FEATURES.md 의 숫자 표가 평가 결과와 같습니다.")
        return 0
    if new != cur:
        DOC.write_text(new, encoding="utf-8")
        print("docs/FEATURES.md 갱신")
    else:
        print("변경 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
