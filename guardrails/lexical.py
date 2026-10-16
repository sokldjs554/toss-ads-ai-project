"""결정론 규칙 계층. 정규화된 두 표면(loose/tight)에서 패턴을 찾고, 원문 인용·위치를 붙인 Finding 을 낸다.

- 예외(exceptions): 매치 주변 창(window)에서 예외 정규식이 보이면 그 매치를 버린다 (예: 상품 자체의 금리 범위).
- 부정(negation_aware): 매치 직후에 부정 표현이 오면 severity 를 warn 으로 내리고 '의미 심사 필요' 로 넘긴다.
  (공격자는 '원금 보장이라는 말은 하지 않겠습니다. 그래도 손실은 없습니다' 처럼 부정문 뒤에 같은 뜻을 숨긴다 —
   그래서 두 번째 문장은 별도 패턴이 block 으로 잡는다.)
- 우회(evasion): tight 표면에서만 잡히거나 구간에 제로폭·한자가 있었으면 severity 를 한 단계 올린다 (FM-11).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from core.schemas import Finding, Layer, Severity
from guardrails.normalize import Normalized, Surface, normalize
from guardrails.policy import PolicyCatalog, Rule, get_catalog

NEGATION_AFTER = re.compile(
    r"^\s*(이라는\s*말|이란\s*말|이라고|라고|같은\s*말)?\s*(은|는|이|가|을|를|도)?\s*"
    r"(하지\s*않|않습니다|않아요|않는|없습니다|없어요|아닙니다|아니에요|안\s*합니다|안\s*해요|못\s*합니다|못\s*해요|금지|불가|되지\s*않)"
)
_WINDOW = 30
_BUMP = {Severity.WARN: Severity.FIX, Severity.FIX: Severity.BLOCK, Severity.BLOCK: Severity.BLOCK}


@dataclass
class Match:
    rule: Rule
    o_start: int
    o_end: int
    surface: str
    negated: bool
    evasion: bool


def _exception_hit(rule: Rule, surface: Surface, start: int, end: int) -> bool:
    if not rule.exceptions:
        return False
    window = surface.text if rule.exception_scope == "text" else surface.text[max(0, start - _WINDOW): end + _WINDOW]
    return any(p.search(window) for p in rule.exceptions)


def _matches(rule: Rule, norm: Normalized) -> list[Match]:
    out: list[Match] = []
    loose_spans: set[tuple[int, int]] = set()
    for surface in norm.surfaces():
        for pat in rule.patterns:
            for m in pat.finditer(surface.text):
                if m.end() <= m.start():
                    continue
                if _exception_hit(rule, surface, m.start(), m.end()):
                    continue
                o_start, o_end = surface.to_original(m.start(), m.end())
                negated = bool(rule.negation_aware and NEGATION_AFTER.match(surface.text[m.end(): m.end() + 24]))
                evasion = norm.had_evasion_in(o_start, o_end)
                if surface.name == "loose":
                    loose_spans.add((o_start, o_end))
                else:
                    # tight 에서만 잡힌 것 = 띄어쓰기·장식 기호로 쪼갠 우회
                    if not any(ls <= o_start and o_end <= le for ls, le in loose_spans):
                        evasion = True
                    else:
                        continue  # loose 에서 이미 잡은 구간
                out.append(Match(rule, o_start, o_end, surface.name, negated, evasion))
    return out


def _dedupe(matches: list[Match]) -> list[Match]:
    """같은 규칙에서 겹치는 구간은 가장 긴 것 하나만."""
    matches.sort(key=lambda m: (m.rule.id, m.o_start, -(m.o_end - m.o_start)))
    kept: list[Match] = []
    for m in matches:
        if kept and kept[-1].rule.id == m.rule.id and m.o_start < kept[-1].o_end:
            continue
        kept.append(m)
    return kept


def scan(text: str, categories: set[str], catalog: PolicyCatalog | None = None,
         layers: tuple[Layer, ...] = (Layer.LEXICAL,)) -> list[Finding]:
    """텍스트를 규칙으로 스캔한다. `layers` 로 injection 규칙 등도 같은 엔진으로 돌릴 수 있다."""
    catalog = catalog or get_catalog()
    if not text or not text.strip():
        return []
    norm = normalize(text)
    findings: list[Finding] = []
    for rule in catalog.rules_for(categories):
        if rule.layer not in layers or not rule.patterns:
            continue
        for m in _dedupe(_matches(rule, norm)):
            quote = text[m.o_start:m.o_end]
            severity = rule.severity
            notes: list[str] = []
            if m.negated:
                severity = Severity.WARN
                notes.append("부정문 안에 있어 의미 심사로 넘김")
            if m.evasion:
                severity = _BUMP[severity] if not m.negated else severity
                notes.append("표기 변형(띄어쓰기·장식 기호·제로폭·한자)으로 필터 우회 흔적")
            findings.append(Finding(
                rule_id=rule.id, severity=severity, layer=rule.layer, quote=quote, start=m.o_start, end=m.o_end,
                explanation=rule.explanation + (" — " + "; ".join(notes) if notes else ""), verified_quote=True,
            ))
    findings.sort(key=lambda f: (f.start, f.rule_id))
    return findings


def has_blocking(findings: list[Finding]) -> bool:
    return any(f.severity == Severity.BLOCK for f in findings)


def strip_spans(text: str, findings: list[Finding]) -> tuple[str, list[str]]:
    """매치 구간을 제거한 텍스트와 제거한 구절 목록 (인터셉트의 준수 제안에 쓴다)."""
    spans = sorted({(f.start, f.end) for f in findings if f.start >= 0 and f.end > f.start}, reverse=True)
    removed: list[str] = []
    out = text
    tail = re.compile(r"^\s*(이라고|라고|이란|란|으로|로)?\s*(꼭|반드시|무조건)?\s*(써|넣|적|포함|강조|표현|말)[가-힣]*\s*(줘|주세요|주시고|주십시오|해\s*줘)?[.,]?")
    for s, e in spans:
        removed.append(out[s:e])
        rest = out[e:]
        m = tail.match(rest)
        if m and m.end() > 0:
            rest = rest[m.end():]
        out = out[:s] + rest
    out = re.sub(r"\s{2,}", " ", out).strip(" ,.!")
    out = re.sub(r"(,|\.)\s*(,|\.)", r"\1", out)
    return out, list(reversed(removed))
