"""docs/FAILURE_MODES.md · docs/POLICY.md 를 코드에서 생성한다 (손으로 두 번 적지 않는다).
    python scripts/gen_docs.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from guardrails.failure_modes import FAILURE_MODES, FEATURES, by_feature  # noqa: E402
from guardrails.policy import get_catalog  # noqa: E402


def _code_range(modes) -> str:
    nums = sorted(int(fm.code.split("-")[1]) for fm in modes)
    return f"FM-{nums[0]:02d} ~ FM-{nums[-1]:02d}" if nums else "-"


def failure_modes_md() -> str:
    groups = by_feature()
    n_core, n_all = len(groups["core"]), len(FAILURE_MODES)
    lines = ["# 실패 모드 카탈로그", "",
             "`guardrails/failure_modes.py` 에서 생성됨. 각 항목은 **탐지 → 방어 → 보장 테스트** 를 가진다. "
             "파이프라인이 실제로 밟은 실패 모드는 `PipelineRun.failures` 와 트레이스 이벤트에 코드로 남는다.", "",
             f"기능별로 묶었다. 모두 {n_all}개 — 핵심 심사 {n_core}개, 나머지 {n_all - n_core}개는 기능(대화형 셋업·상품 페이지 가져오기·이미지·소재 퀄리티·성과 리포트·라벨링·운영)을 더하면서 같은 형식으로 등록했다. 코드는 FM-01 부터 이어서 붙이며, FailureEvent 와 테스트는 이 정식 코드만 쓴다. "
             "`fail-closed` 칸이 `아님` 인 항목은 알려진 한계가 남아 있어 그 방어선만으로는 막히지 않는 경로가 있다는 뜻이며, 방어 칸에 그 한계를 적었다.", "",
             "| 기능 | 코드 | 개수 |", "|---|---|---|"]
    for key, label in FEATURES.items():
        lines.append(f"| {label} | {_code_range(groups[key])} | {len(groups[key])} |")
    for key, label in FEATURES.items():
        lines += ["", f"## {label} ({_code_range(groups[key])})", "",
                  "| 코드 | 이름 | 단계 | 증상 | 탐지 | 방어 | 테스트 | fail-closed |", "|---|---|---|---|---|---|---|---|"]
        for fm in groups[key]:
            lines.append(f"| {fm.code} | {fm.name} | {fm.stage} | {fm.symptom} | {fm.detection} | {fm.defense} | `{fm.test}` | {'예' if fm.fail_closed else '아님'} |")
    return "\n".join(lines) + "\n"


def policy_md() -> str:
    cat = get_catalog()
    lines = [f"# 정책 규칙 카탈로그 v{cat.version}", "",
             "`guardrails/policy.yaml` 에서 생성됨. 토스애즈 공개 광고주 가이드(공통·소재별·업종별·광고 불가 업종)와 "
             "금융소비자보호법 제22조·대부업법 제9조·표시광고법의 공개 조문을 읽고 **이 프로젝트가 실행 가능한 규칙으로 재구성한 것** 이다. "
             "실제 플랫폼의 심사 기준·내부 정책이 아니며 법률 자문도 아니다.", "",
             f"규칙 {len(cat.rules)}개 · 심각도: block(반려) / fix(수정 후 재심사) / warn(주의)", "",
             "| ID | 제목 | 심각도 | 계층 | 적용 업종 | 출처(가이드 항목) | 수정 힌트 |", "|---|---|---|---|---|---|---|"]
    for r in cat.rules.values():
        lines.append(f"| `{r.id}` | {r.title} | {r.severity.value} | {r.layer.value} | {', '.join(sorted(r.applies))} | {r.section} | {r.fix_hint} |")
    lines += ["", "## 업종별 필수 고지 체크리스트", ""]
    for c, spec in cat.disclosures.items():
        lines.append(f"### {c} → `{spec.rule_id}`")
        for it in spec.items:
            lines.append(f"- **{it.label}** — 기본 문장: “{it.canonical}”")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    docs = ROOT / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "FAILURE_MODES.md").write_text(failure_modes_md(), encoding="utf-8")
    (docs / "POLICY.md").write_text(policy_md(), encoding="utf-8")
    print("docs generated")


if __name__ == "__main__":
    main()
