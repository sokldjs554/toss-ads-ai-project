"""Safety Intercept — 광고주 요청과 상품 설명을 파이프라인에 넣기 전에 가로챈다 (FM-09·10·21).

에러를 던지지 않는다. 언제나 InterceptNotice 를 돌려주고, 파이프라인은 그 안의 `hard_block` 만 본다.
- 인젝션: 문장을 제거하고 알린다.
- 금지 업종·사행성·불법 금융: hard block.
- 정책 위반 의도('원금 보장이라고 써 줘'): 위반 구절을 걷어낸 요청 + 준수 방향 안내로 리다이렉트.
  라우터가 있으면 창의 모델(Claude)에게 더 자연스러운 안내문·제안문을 받되, 제안문을 다시 스캔해 통과한 것만 쓴다 (FM-19).
"""
from __future__ import annotations

import re
from typing import Optional

from core.llm_schemas import RedirectOut
from core.schemas import CampaignBrief, Finding, InterceptKind, InterceptNotice, Layer, Severity
from guardrails import injection
from guardrails.lexical import scan, strip_spans
from guardrails.policy import PolicyCatalog, get_catalog

HARD_BLOCK_RULES = {"TA-RESTRICTED-01", "FIN-UNREGISTERED-01", "FIN-ADVICE-01", "FIN-CRYPTO-01", "LOAN-SLANG-01", "FIN-GOV-01", "TA-GAMBLING-01"}


_SENT = re.compile(r"(?<=[.!?。])\s+|\n+")
_WRITE_VERB = re.compile(r"(써|넣|적|강조|표현|포함|말해|언급|홍보)[가-힣]*\s*(줘|주세요|주시고|해\s*줘|할\s*것|라고)?")


def _clean_intent(text: str, findings: list[Finding]) -> tuple[str, list[str]]:
    """위반 구절을 걷어낸다. '…라고 써 줘' 처럼 구절을 쓰라고 지시하는 문장은 문장째 지운다 (조각이 남지 않게)."""
    removed = [text[f.start:f.end] for f in findings if f.start >= 0 and f.end > f.start]
    kept: list[str] = []
    cursor = 0
    for sent in [x for x in _SENT.split(text) if x and x.strip()]:
        idx = text.find(sent, cursor)
        if idx < 0:
            kept.append(sent)
            continue
        s_start, s_end = idx, idx + len(sent)
        cursor = s_end
        hits = [f for f in findings if f.start >= 0 and f.start < s_end and f.end > s_start]
        if not hits:
            kept.append(sent)
        elif _WRITE_VERB.search(sent):
            continue  # 지시 문장 전체 삭제
        else:
            local = [Finding(rule_id=f.rule_id, severity=f.severity, layer=f.layer, quote=f.quote, start=f.start - s_start, end=f.end - s_start) for f in hits]
            stripped, _ = strip_spans(sent, local)
            if stripped:
                kept.append(stripped)
    out = " ".join(k.strip() for k in kept).strip(" ,.")
    return out, removed


def _guidance(findings: list[Finding], catalog: PolicyCatalog) -> str:
    hints: list[str] = []
    for f in findings:
        r = catalog.get(f.rule_id)
        if r and r.fix_hint and r.fix_hint not in hints:
            hints.append(r.fix_hint)
    return " / ".join(hints[:4])


def intercept(brief: CampaignBrief, catalog: Optional[PolicyCatalog] = None, router=None) -> tuple[InterceptNotice, str, str, set[str]]:
    """반환: (notice, effective_input, sanitized_description, effective_categories)."""
    catalog = catalog or get_catalog()
    notice = InterceptNotice()
    text = brief.advertiser_input
    desc = brief.product.raw_description or ""

    # 0) 업종 추론 (선언 업종 + 단서)
    categories = catalog.effective_categories(brief.product.category, text, desc, " ".join(f.text for f in brief.product.facts))
    mismatch = catalog.inferred_mismatch(brief.product.category, categories)
    if mismatch:
        rule = catalog.get("CAT-MISMATCH-01")
        if rule:
            notice.findings.append(Finding(rule_id=rule.id, severity=rule.severity, layer=Layer.STRUCTURAL, quote="",
                                           explanation=f"선언 업종 '{brief.product.category.value}' 이지만 {', '.join(mismatch)} 단서가 있어 금융 규칙을 함께 적용"))

    # 1) 인젝션 (요청문 + 상품 설명)
    inj_text = injection.scan_text(text, catalog)
    inj_desc = injection.scan_text(desc, catalog)
    if inj_text.detected or inj_desc.detected:
        notice.triggered = True
        notice.kind = InterceptKind.INJECTION
        notice.findings.extend(inj_text.findings + inj_desc.findings)
        notice.removed_phrases.extend(inj_text.removed + inj_desc.removed)
        text = inj_text.sanitized
        desc = inj_desc.sanitized

    # 2) 정책 위반 의도 / 금지 업종
    lex = scan(text, categories, catalog) + [f for f in scan(desc, categories, catalog) if f.rule_id in HARD_BLOCK_RULES]
    hard = [f for f in lex if f.rule_id in HARD_BLOCK_RULES and f.severity == Severity.BLOCK]
    intent = [f for f in lex if f.rule_id not in HARD_BLOCK_RULES and f.severity in (Severity.BLOCK, Severity.FIX)]
    if hard:
        notice.triggered = True
        notice.hard_block = True
        notice.kind = InterceptKind.PROHIBITED_CATEGORY
        notice.findings.extend(hard)
        notice.message = ("요청에 플랫폼에서 집행할 수 없는 업종·표현이 포함되어 있어 캠페인을 만들 수 없어요: "
                          + ", ".join(f"'{f.quote}'" for f in hard[:3])
                          + ". 집행 가능한 상품·업종으로 다시 요청해 주세요.")
        notice.suggested_input = ""
        return notice, text, desc, categories
    if intent:
        notice.triggered = True
        notice.kind = notice.kind or InterceptKind.POLICY_INTENT
        notice.findings.extend(intent)
        stripped, removed = _clean_intent(text, intent)
        notice.removed_phrases.extend(removed)
        guidance = _guidance(intent, catalog)
        notice.suggested_input = (stripped or "같은 상품·예산·목표로 캠페인 구성") + f" (가이드: {guidance})"
        notice.message = ("요청하신 표현 중 " + ", ".join(f"'{p}'" for p in removed[:4])
                          + " 은(는) 광고 가이드상 쓸 수 없어요. 같은 목표를 지키는 방향으로 요청을 고쳐 두었어요 — 아래 제안을 확인해 주세요.")
        # 창의 모델에게 더 자연스러운 안내를 받되, 제안문을 다시 검사한다 (FM-19)
        if router is not None:
            try:
                from llm.base import LLMRequest, Task

                req = LLMRequest(
                    task=Task.REDIRECT,
                    system=("너는 광고 플랫폼의 가이드라인 안내 도우미다. 광고주를 꾸짖지 말고, 왜 그 표현이 안 되는지 한 문장으로 설명한 뒤 "
                            "같은 마케팅 목표를 지키는 '준수 방향' 요청문을 다시 써 준다. 반드시 JSON {\"message\": str, \"suggested_input\": str} 만 출력."),
                    user=(f"원래 요청: {brief.advertiser_input}\n제거해야 할 표현: {removed}\n위반 규칙: "
                          + "; ".join(f"{f.rule_id} {catalog.get(f.rule_id).title if catalog.get(f.rule_id) else ''}" for f in intent)
                          + f"\n수정 힌트: {guidance}"),
                    schema=RedirectOut, max_tokens=600,
                    context={"removed_phrases": removed, "suggested_input": notice.suggested_input}, label="redirect:intercept",
                )
                resp = router.complete(req)
                if resp.parsed is not None:
                    cand = resp.parsed.suggested_input.strip()
                    re_scan = [f for f in scan(cand, categories, catalog) if f.severity in (Severity.BLOCK, Severity.FIX)]
                    if cand and not re_scan:
                        notice.suggested_input = cand
                        notice.message = resp.parsed.message.strip() or notice.message
            except Exception:  # noqa: BLE001 — 안내문 생성 실패는 결정론 제안으로 충분히 대체된다
                pass
        text = notice.suggested_input
    return notice, text, desc, categories
