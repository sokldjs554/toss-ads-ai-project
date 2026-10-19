"""상품 URL 이해 평가 — 합성 페이지 위에서 이름·가격·사실·숨은 지시문 격리·읽기 불가 판정을 잰다.

    python -m evals.url_ingest_eval                 # 개발용 + 최종 확인용(holdout) 모두, 표로 출력
    python -m evals.url_ingest_eval --gate          # 게이트 실패 시 종료 코드 1
    python -m evals.url_ingest_eval --split holdout # 한쪽만
    python -m evals.url_ingest_eval --json          # 기계가 읽는 형식

이 숫자가 의미하지 않는 것 (반드시 같이 읽을 것)
- 모두 **합성 페이지** 위의 값이다. 페이지도 기대값도 이 프로젝트를 만든 사람이 직접 썼고, 독립 검증자는 없다.
  실제 쇼핑몰 페이지(광고·추천·스크립트·템플릿이 뒤섞인 수천 가지 구조)에서의 정확도가 아니다.
- dev 페이지는 추출 코드를 만들고 고치는 동안 본 것이라 점수가 높게 나오는 것이 당연하다 (회귀 감시용). 일반화 성능의 근거로 쓰면 안 된다.
- holdout 페이지는 추출 코드를 고정한 뒤에 만들었고 결과를 보고 코드를 고치지 않았다. 그래도 같은 사람이 같은 생각으로 썼기 때문에 실제 페이지보다 쉽다.
  `index.json` 의 holdout 해시와 지금 코드의 해시가 다르면, 그 뒤에 코드가 바뀌었다는 뜻이므로 holdout 도 더는 '고정 전에 만든 것' 이 아니다 (이 리포트가 알려 준다).
- '숨은 지시문 격리율 100%' 는 이 페이지들에서 사실로 새어 든 구절이 없다는 뜻이지, 모든 페이지에서 지시문이 걸러진다는 보장이 아니다.
  화면에 보이지 않는 글은 탐지 여부와 상관없이 쓰지 않지만, **보이는** 본문 속 바꿔 쓴 지시문은 패턴 탐지에 기대므로 놓칠 수 있다 (holdout 에 그런 사례를 일부러 넣었다).
- 사실 재현율·정밀도는 문자열 포함 기준이다: 재현율 = 기대 구절이 사실 후보 어딘가에 들어 있는 비율, 정밀도 = 사실 후보 중 기대 구절 또는 허용 구절을 하나라도 포함한 비율.
  '후보가 좋다' 의 측정이 아니라 '정해 둔 구절을 찾았는가' 의 측정이다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Callable, Optional

from core import url_ingest as ui
from core.schemas import ProductCategory

PAGES = ui.PAGES_DIR
CORE_FILE = Path(ui.__file__)
REPRODUCE = "python -m evals.url_ingest_eval --gate"

# 개발용 페이지에서 지켜야 할 바닥값 — 회귀 감시용이다 (코드를 만들며 맞춘 페이지라 '성능' 이 아니다).
DEV_FLOORS = {"status_accuracy": 1.0, "name_accuracy": 1.0, "price_accuracy": 1.0, "fact_recall": 0.90, "flags_exact": 1.0}


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "")).casefold()


def logic_sha256() -> str:
    """추출 코드(core/url_ingest.py)의 해시. holdout 을 만든 시점의 값과 비교해 그 뒤에 코드가 바뀌었는지 본다."""
    return hashlib.sha256(CORE_FILE.read_bytes()).hexdigest()


def load_pages(split: str = "all") -> list[dict]:
    pages = ui.load_sample_index().get("pages", [])
    return [p for p in pages if split == "all" or p.get("split", "dev") == split]


def holdout_integrity() -> dict:
    meta = ui.load_sample_index().get("_meta", {}).get("holdout") or {}
    frozen = meta.get("logic_sha256")
    now = logic_sha256()
    return {"frozen_sha256": frozen, "current_sha256": now, "created": meta.get("created"),
            "unchanged": (frozen == now) if frozen else None, "note": meta.get("note", "")}


Ingest = Callable[[str], "ui.ProductDraft"]


def default_ingest(name: str) -> "ui.ProductDraft":
    return ui.ingest_url(f"{ui.SAMPLE_PREFIX}{name}")


def _page_row(page: dict, ingest: Ingest) -> dict:
    name = page["id"]
    row: dict = {"id": name, "split": page.get("split", "dev"), "expected_status": page["status"], "problems": []}
    t0 = time.perf_counter()
    try:
        d = ingest(name)
    except Exception as e:   # noqa: BLE001 — 어떤 페이지도 예외로 끝나면 안 된다
        row.update(crashed=True, seconds=round(time.perf_counter() - t0, 3), problems=[f"예외: {type(e).__name__}"])
        return row
    row["seconds"] = round(time.perf_counter() - t0, 3)
    row["crashed"] = False
    row["status"] = d.status.value
    row["status_ok"] = d.status.value == page["status"]
    if not row["status_ok"]:
        row["problems"].append(f"상태 {d.status.value} (기대 {page['status']})")
    flags = sorted(f.value for f in d.flags)
    want = sorted(page.get("flags", []))
    row["flags"], row["flags_expected"], row["flags_exact"] = flags, want, flags == want
    if flags != want:
        row["problems"].append(f"플래그 {flags} (기대 {want})")

    # 숨은 지시문 격리: 이름·사실·랜딩 문구·가격에 금지 구절이 하나라도 들어갔는가. 사용자가 모두 고른 경우의 ProductFacts 까지 본다.
    blob_parts = [d.name, d.landing_text, *[f.text for f in d.facts], *[p.text for p in d.price_candidates], *d.candidates]
    try:
        pf = ui.confirm(d, [f.id for f in d.facts], category=ProductCategory.OTHER, name=d.name or "상품") if d.facts else None
        row["confirm_error"] = None
    except ui.DraftError as e:
        pf, row["confirm_error"] = None, str(e)
        row["problems"].append(f"모두 고르기 확정 실패: {e}")
    if pf is not None:
        blob_parts += [pf.name, pf.raw_description, *[f.text for f in pf.facts]]
    blob = _norm("\n".join(blob_parts))
    forbidden = page.get("forbidden", [])
    leaked = [x for x in forbidden if _norm(x) in blob]
    row["forbidden_total"], row["leaked"] = len(forbidden), leaked
    if leaked:
        row["problems"].append(f"새어 든 구절: {leaked}")
    gap = page.get("forbidden_report_only", [])        # 패턴 탐지의 한계를 재려고 일부러 넣은 구절 — 게이트에는 쓰지 않고 따로 보고한다
    row["gap_total"], row["gap_leaked"] = len(gap), [x for x in gap if _norm(x) in blob]
    if row["gap_leaked"]:
        row["problems"].append(f"알려진 한계 구절이 새어 듦(게이트 제외): {row['gap_leaked']}")

    if page["status"] == "ok":
        exp_name = page.get("expected_name") or ""
        row["name_ok"] = _norm(d.name) == _norm(exp_name)
        if not row["name_ok"]:
            row["problems"].append(f"이름 {d.name!r} (기대 {exp_name!r})")
        exp_price = page.get("expected_price")
        got = d.price.amount if d.price else None
        row["price_ok"] = got == exp_price
        # '엉뚱한 값' = 가격을 냈는데 기대와 다른 경우. 기대가 '가격 없음' 인데 값을 낸 경우도 센다 (없는 가격을 만들어 내는 쪽이 더 위험하다)
        row["price_wrong_value"] = got is not None and got != exp_price
        if not row["price_ok"]:
            row["problems"].append(f"가격 {got} (기대 {exp_price})")
        texts = _norm("\n".join(f.text for f in d.facts))
        exp_facts = page.get("expected_facts", [])
        found = [x for x in exp_facts if _norm(x) in texts]
        row["facts_expected"], row["facts_found"] = len(exp_facts), len(found)
        missing = [x for x in exp_facts if x not in found]
        if missing:
            row["problems"].append(f"찾지 못한 기대 구절: {missing}")
        good = [_norm(x) for x in [*exp_facts, *page.get("acceptable_facts", [])]]
        right = [f for f in d.facts if any(g in _norm(f.text) for g in good)]
        row["facts_total"], row["facts_right"] = len(d.facts), len(right)
        wrong = [f.text for f in d.facts if f not in right]
        row["wrong_facts"] = wrong
        row["facts_min_ok"] = len(d.facts) >= page.get("facts_min", 0)
        if not row["facts_min_ok"]:
            row["problems"].append(f"사실 후보 {len(d.facts)}개 (최소 {page.get('facts_min')})")
        want_cat = page.get("category")
        if want_cat:
            row["category_expected"] = want_cat
            row["category_guess"] = d.category_guess.value
            row["category_asked"] = d.needs_category_confirmation
            row["category_ok"] = (d.category_guess.value == want_cat) and not d.needs_category_confirmation
    else:
        row["facts_unexpected"] = len(d.facts)
        if d.facts or d.name or d.landing_text or d.image_urls:
            row["problems"].append("읽지 못한/모호한 페이지인데 내용이 만들어짐")
    return row


def _rate(k: int, n: int) -> Optional[float]:
    return round(k / n, 4) if n else None


def summarize(rows: list[dict]) -> dict:
    ok_rows = [r for r in rows if not r.get("crashed") and r["expected_status"] == "ok"]
    done = [r for r in rows if not r.get("crashed")]
    sums = lambda key: sum(r.get(key, 0) for r in ok_rows)   # noqa: E731
    forb_total = sum(r.get("forbidden_total", 0) for r in done)
    leaked = sum(len(r.get("leaked", [])) for r in done)
    inj = [r for r in done if "injection_suspected" in r["flags_expected"]]
    non_inj = [r for r in done if "injection_suspected" not in r["flags_expected"]]
    cat_rows = [r for r in ok_rows if "category_ok" in r]
    unreadable_rows = [r for r in done if r["expected_status"] != "ok"]
    out = {
        "pages": len(rows), "crashes": sum(1 for r in rows if r.get("crashed")),
        "status_accuracy": _rate(sum(r["status_ok"] for r in done), len(done)),
        "unreadable_detection": {"k": sum(r["status_ok"] for r in unreadable_rows), "n": len(unreadable_rows)},
        "name_accuracy": _rate(sum(r["name_ok"] for r in ok_rows), len(ok_rows)),
        "price_accuracy": _rate(sum(r["price_ok"] for r in ok_rows), len(ok_rows)),
        "price_wrong_value": sum(bool(r.get("price_wrong_value")) for r in ok_rows),
        "fact_recall": _rate(sums("facts_found"), sums("facts_expected")),
        "fact_precision": _rate(sums("facts_right"), sums("facts_total")),
        "facts_counts": {"found": sums("facts_found"), "expected": sums("facts_expected"), "right": sums("facts_right"), "total": sums("facts_total")},
        "facts_min_ok": _rate(sum(bool(r.get("facts_min_ok")) for r in ok_rows), len(ok_rows)),
        "leak_count": leaked, "forbidden_total": forb_total,
        "isolation_rate": _rate(forb_total - leaked, forb_total),
        "known_gap": {"leaked": sum(len(r.get("gap_leaked", [])) for r in done), "total": sum(r.get("gap_total", 0) for r in done)},
        "flags_exact": _rate(sum(r["flags_exact"] for r in done), len(done)),
        "injection_flag_recall": {"k": sum("injection_suspected" in r["flags"] for r in inj), "n": len(inj)},
        "injection_false_alarm": {"k": sum("injection_suspected" in r["flags"] for r in non_inj), "n": len(non_inj)},
        "category": {"k": sum(bool(r["category_ok"]) for r in cat_rows), "n": len(cat_rows), "asked": sum(bool(r["category_asked"]) for r in cat_rows)},
        "slowest_seconds": max((r.get("seconds", 0) for r in rows), default=0),
    }
    return out


def evaluate(split: str = "all", ingest: Optional[Ingest] = None) -> dict:
    """페이지마다 읽어서 지표를 계산한다. `ingest` 는 테스트에서 일부러 새는 읽기 함수를 넣어 게이트가 실제로 막는지 볼 때 쓴다."""
    ingest = ingest or default_ingest
    pages = load_pages(split)
    rows = [_page_row(p, ingest) for p in pages]
    rep = {"split": split, "reproduce": REPRODUCE, "rows": rows, "summary": summarize(rows),
           "by_split": {s: summarize([r for r in rows if r["split"] == s]) for s in sorted({r["split"] for r in rows})},
           "holdout_integrity": holdout_integrity(), "logic_sha256": logic_sha256(), "synthetic": True}
    return rep


def gate(rep: dict) -> tuple[bool, list[str]]:
    """게이트: (1) 숨은/보이는 지시문이 사실·이름·랜딩 문구로 새면 실패 — 모든 페이지 (2) 예외로 끝난 페이지가 있으면 실패
    (3) 개발용 페이지는 바닥값 아래로 떨어지면 실패 (회귀 감시). holdout 의 정확도는 게이트에 쓰지 않는다 — 보고만 한다."""
    fails: list[str] = []
    s = rep["summary"]
    if s["leak_count"]:
        leaked = [(r["id"], r["leaked"]) for r in rep["rows"] if r.get("leaked")]
        fails.append(f"숨은 지시문/금지 구절이 사실로 새어 들어옴 {s['leak_count']}건: {leaked}")
    if s["crashes"]:
        fails.append(f"예외로 끝난 페이지 {s['crashes']}개: {[r['id'] for r in rep['rows'] if r.get('crashed')]}")
    dev = rep["by_split"].get("dev")
    if dev:
        for key, floor in DEV_FLOORS.items():
            v = dev.get(key)
            if v is not None and v < floor:
                fails.append(f"dev {key} {v} < 바닥값 {floor}")
    return (not fails), fails


def _pct(k, n) -> str:
    return "대상 없음" if not n else f"{k / n:.0%} ({k}/{n})"


def format_report(rep: dict) -> str:
    out = ["상품 URL 이해 평가 — 합성 페이지 위의 값입니다 (실제 쇼핑몰 페이지의 정확도가 아님)", f"재현: {rep['reproduce']}", ""]
    for name, s in rep["by_split"].items():
        label = {"dev": "개발용(코드를 만들며 본 페이지 — 회귀 감시용)", "holdout": "최종 확인용 holdout(코드를 고정한 뒤 만든 페이지)"}.get(name, name)
        c = s["facts_counts"]
        out += [f"[{label}] 페이지 {s['pages']}개",
                f"  읽기 불가·모호 판정 정확도   {_pct(s['unreadable_detection']['k'], s['unreadable_detection']['n'])}  /  상태 전체 일치 {_pct(round((s['status_accuracy'] or 0) * s['pages']), s['pages'])}",
                f"  이름 정확도                {s['name_accuracy']}  /  가격 정확도 {s['price_accuracy']} (엉뚱한 값을 낸 수 {s['price_wrong_value']})",
                f"  사실 재현율                {s['fact_recall']} ({c['found']}/{c['expected']})  /  정밀도 {s['fact_precision']} ({c['right']}/{c['total']})",
                f"  숨은·금지 구절 격리율       {s['isolation_rate']}  (새어 든 수 {s['leak_count']} / 금지 구절 {s['forbidden_total']})   ← 게이트: 새어 든 수 = 0",
                f"  알려진 한계 구절(바꿔 쓴 지시문) 새어 든 수   {s['known_gap']['leaked']} / {s['known_gap']['total']}   ← 게이트 아님, 패턴 탐지의 한계를 보고하는 값",
                f"  플래그 정확히 일치          {s['flags_exact']}  /  지시문 표시 재현 {_pct(s['injection_flag_recall']['k'], s['injection_flag_recall']['n'])} · 오경보 {_pct(s['injection_false_alarm']['k'], s['injection_false_alarm']['n'])}",
                f"  업종 추정(확신 있게 맞춤)    {_pct(s['category']['k'], s['category']['n'])} · 되물음 {s['category']['asked']}건",
                f"  가장 느린 페이지            {s['slowest_seconds']}초   예외 {s['crashes']}건", ""]
    hi = rep["holdout_integrity"]
    if hi.get("frozen_sha256"):
        out.append("holdout 무결성: " + ("추출 코드가 holdout 작성 때와 같습니다." if hi["unchanged"] else "추출 코드가 holdout 작성 이후 바뀌었습니다 — holdout 은 더는 '고정 전에 만든 것' 이 아닙니다."))
        out.append("")
    bad = [r for r in rep["rows"] if r["problems"]]
    if bad:
        out.append("틀리거나 아쉬운 페이지")
        for r in bad:
            out.append(f"  - {r['id']} [{r['split']}]")
            out += [f"      · {p}" for p in r["problems"]]
            if r.get("wrong_facts"):
                out.append(f"      · 기대·허용 목록에 없는 사실 후보 {len(r['wrong_facts'])}개: {r['wrong_facts'][:3]}")
        out.append("")
    ok, fails = gate(rep)
    out.append("게이트: " + ("통과" if ok else "실패"))
    out += [f"  - {f}" for f in fails]
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="상품 URL 이해 평가 (합성 페이지)")
    ap.add_argument("--split", choices=["all", "dev", "holdout"], default="all")
    ap.add_argument("--gate", action="store_true", help="게이트를 통과하지 못하면 종료 코드 1")
    ap.add_argument("--json", action="store_true", help="JSON 으로 출력")
    ap.add_argument("--out", help="JSON 결과를 파일로도 저장")
    args = ap.parse_args(argv)
    rep = evaluate(args.split)
    ok, fails = gate(rep)
    rep["gate"] = {"passed": ok, "failures": fails}
    if args.out:
        Path(args.out).write_text(json.dumps(rep, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=1) if args.json else format_report(rep))
    return 1 if (args.gate and not ok) else 0


if __name__ == "__main__":
    sys.exit(main())
