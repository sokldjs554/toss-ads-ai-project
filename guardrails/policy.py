"""규칙 카탈로그 로더. YAML 을 한 번 읽어 정규식을 컴파일하고, rule_id 목록을 심사 스키마에 공급한다."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

from core.schemas import FINANCIAL_CATEGORIES, Layer, ProductCategory, ProductFacts, Severity

POLICY_PATH = Path(__file__).with_name("policy.yaml")


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    section: str
    severity: Severity
    layer: Layer
    applies: frozenset[str]
    patterns: tuple[re.Pattern, ...]
    exceptions: tuple[re.Pattern, ...]
    explanation: str
    fix_hint: str
    negation_aware: bool = False
    exception_scope: str = "window"  # window | text

    def applies_to(self, categories: set[str]) -> bool:
        return "all" in self.applies or bool(self.applies & categories)


@dataclass(frozen=True)
class DisclosureItem:
    key: str
    label: str
    any_of: tuple[re.Pattern, ...]
    canonical: str


@dataclass(frozen=True)
class DisclosureSpec:
    category: str
    rule_id: str
    items: tuple[DisclosureItem, ...]


@dataclass
class PolicyCatalog:
    version: str
    rules: dict[str, Rule]
    disclosures: dict[str, DisclosureSpec]
    slot_marketing_cues: list[str]
    category_cues: dict[str, list[re.Pattern]]
    length_limits: dict[str, int]
    path: Path = field(default=POLICY_PATH)

    # --- 조회 ---------------------------------------------------------------
    def rule_ids(self) -> list[str]:
        return list(self.rules.keys())

    def get(self, rule_id: str) -> Optional[Rule]:
        return self.rules.get(rule_id)

    def rules_for(self, categories: set[str], layer: Optional[Layer] = None) -> list[Rule]:
        out = [r for r in self.rules.values() if r.applies_to(categories)]
        if layer is not None:
            out = [r for r in out if r.layer == layer]
        return out

    def disclosure_for(self, category: str) -> Optional[DisclosureSpec]:
        return self.disclosures.get(category)

    def effective_categories(self, declared: ProductCategory, *texts: str) -> set[str]:
        """선언 업종 + 문구 단서로 추론한 업종 (CAT-MISMATCH-01). 더 엄격한 쪽을 함께 적용한다."""
        cats = {declared.value}
        if declared in FINANCIAL_CATEGORIES:
            return cats  # 금융으로 선언한 광고주는 그 업종 서류로 검증된다고 가정 — 추론으로 넓히지 않는다
        blob = " ".join(texts)
        for cat, pats in self.category_cues.items():
            if any(p.search(blob) for p in pats):
                cats.add(cat)
        return cats

    def categories_for(self, product: ProductFacts, *texts: str) -> set[str]:
        """상품과 문구에 적용할 업종 — 실행·수정안 검사·직접 재검사·평가·방식 비교가 모두 이 한 함수로 정한다 (선언 업종 + 문구·상품 설명·사실 목록의 단서)."""
        return self.effective_categories(product.category, *texts, product.raw_description or "", " ".join(f.text for f in product.facts))

    def inferred_mismatch(self, declared: ProductCategory, effective: set[str]) -> list[str]:
        if declared in FINANCIAL_CATEGORIES:
            return []
        return sorted(c for c in effective if c != declared.value and c in {c.value for c in FINANCIAL_CATEGORIES})

    def summary_table(self) -> list[dict]:
        return [
            {"id": r.id, "title": r.title, "severity": r.severity.value, "layer": r.layer.value,
             "applies": ", ".join(sorted(r.applies)), "patterns": len(r.patterns), "section": r.section}
            for r in self.rules.values()
        ]


def _compile(patterns: list[str]) -> tuple[re.Pattern, ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns or [])


def load_catalog(path: Optional[Path] = None) -> PolicyCatalog:
    path = path or POLICY_PATH
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules: dict[str, Rule] = {}
    for raw in data["rules"]:
        rule = Rule(
            id=raw["id"], title=raw["title"], section=raw.get("section", ""),
            severity=Severity(raw["severity"]), layer=Layer(raw["layer"]),
            applies=frozenset(raw.get("applies", ["all"])),
            patterns=_compile(raw.get("patterns", [])), exceptions=_compile(raw.get("exceptions", [])),
            explanation=raw.get("explanation", ""), fix_hint=raw.get("fix_hint", ""),
            negation_aware=bool(raw.get("negation_aware", False)),
            exception_scope=str(raw.get("exception_scope", "window")),
        )
        if rule.id in rules:
            raise ValueError(f"중복 rule id: {rule.id}")
        rules[rule.id] = rule
    disclosures: dict[str, DisclosureSpec] = {}
    for cat, spec in (data.get("disclosures") or {}).items():
        items = tuple(
            DisclosureItem(key=i["key"], label=i["label"], any_of=_compile(i["any_of"]), canonical=i["canonical"])
            for i in spec["items"]
        )
        disclosures[cat] = DisclosureSpec(category=cat, rule_id=spec["rule"], items=items)
    cues = {cat: list(_compile(pats)) for cat, pats in (data.get("category_cues") or {}).items()}
    return PolicyCatalog(
        version=str(data.get("version", "0")), rules=rules, disclosures=disclosures,
        slot_marketing_cues=list(data.get("disclosure_slot_marketing_cues") or []),
        category_cues=cues, length_limits=dict(data.get("length_limits") or {}), path=path,
    )


@lru_cache(maxsize=1)
def get_catalog() -> PolicyCatalog:
    return load_catalog()
