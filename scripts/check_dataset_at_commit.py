"""이전 커밋의 코드 검사(결정론 계층)가 어떤 데이터 파일을 라벨대로 판정했는지 센다 — '수정 전 N/M' 같은 수치를 문서에 손으로 적지 않고 다시 재현하기 위한 스크립트.

    git worktree add /tmp/old <커밋>          # 비교할 이전 커밋을 따로 꺼낸다
    cd /tmp/old && PYTHONPATH=/tmp/old python <이 저장소>/scripts/check_dataset_at_commit.py <이 저장소>/evals/datasets/conditions_extra.jsonl
    git worktree remove --force /tmp/old

데이터 파일은 현재 저장소의 것을 쓰고, 판정은 꺼낸 커밋의 `guardrails/` 가 한다. 한 사례가 '라벨대로' 라는 것은 판정이 라벨과 같고, 라벨이 정한 규칙이 모두 나왔고,
정상(APPROVE) 라벨이면 어떤 규칙도 걸리지 않았다는 뜻이다. 이유만 다른 사례도 '라벨과 다름' 으로 센다.
"""
import sys
from pathlib import Path

from evals.runner import load_dataset
from guardrails import output_guard
from guardrails.policy import get_catalog

CAT = get_catalog()


def main(paths: list[str]) -> int:
    for ds in paths:
        cases = load_dataset(Path(ds))
        wrong = []
        for c in cases:
            copy, product = c.to_copy(), c.to_product()
            cats = CAT.effective_categories(product.category, copy.full_text())
            det = output_guard.check(copy, product, cats, c.landing_text, [], CAT)
            rules = {f.rule_id for f in det.findings}
            ok = det.decision.value == c.label and all(r in rules for r in c.rule_ids) and (c.label != "APPROVE" or not rules)
            if not ok:
                wrong.append((c.id, c.label, det.decision.value))
        unsafe = sum(1 for _, label, pred in wrong if label != "APPROVE" and pred == "APPROVE")
        blocked = sum(1 for _, label, pred in wrong if label == "APPROVE" and pred != "APPROVE")
        print(f"{Path(ds).name}: {len(cases)}건 중 라벨과 다르게 판정 {len(wrong)}건 (문제 있는 문구를 통과시킴 {unsafe} · 정상 문구를 막음 {blocked} · 이유만 다름 {len(wrong) - unsafe - blocked})")
        print("  ", ", ".join(w[0] for w in wrong) or "-")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
