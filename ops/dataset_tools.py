"""평가셋 도구 — 확정된 라벨을 평가셋 행으로 내보내고, 검증하고, 라벨 오류 의심 사례를 찾는다.

    python -m ops.dataset_tools --validate evals/datasets/golden.jsonl [--against holdout]    # 스키마·라벨-규칙 일관성·중복·누수 점검
    python -m ops.dataset_tools --suspects                                                    # 저장된 평가 결과에서 라벨 오류 의심 사례
    python -m ops.dataset_tools --demo                                                        # 합성 예시의 일치도

원칙
- 내보내기는 **새 행만** 만든다. 기존 데이터셋 파일을 덮어쓰지 않는다 (`write_jsonl_new` 는 이미 있는 파일이면 거부한다).
- 개발용(dev)/최종 확인용(holdout) 분할은 **문구 해시** 로 정한다. 같은 문구는 항상 같은 분할이고, 새 항목이 기존 항목의 분할을 바꾸지 않는다.
  해시 규칙을 고정한 뒤에 데이터를 만들고, 결과를 보고 분할을 바꾸지 않는다.
- 근접 중복은 막지 않고 **경고** 한다 (정규화한 문구의 글자 3-gram 자카드 ≥ 0.8). 개발용↔최종 확인용 사이에 걸치면 '누수 위험' 으로 따로 알린다.

이 모듈의 값이 의미하지 않는 것
- '라벨이 확정됨' 은 라벨러들이 일치했거나 조정자가 정했다는 뜻이지 라벨이 옳다는 뜻이 아니다. 라벨러가 한 명이면 `single_labeler` 로 표시되고 독립 확인이 아니다.
- 근접 중복 탐지는 글자 모양의 유사도다. 뜻이 같아도 표현이 다르면 잡지 못하고(재현율 한계), 모양이 비슷해도 뜻이 다른 쌍(예: 조건 숫자만 다른 대조 사례)은 잡는다.
- 라벨 오류 의심 점수는 '코드·모델이 라벨과 다르게 자신 있게 판정한 정도' 다. 의심 = 사람이 다시 볼 후보이지 라벨이 틀렸다는 판정이 아니다. 대역(demo) 실행 결과는 모델 판단이 아니므로 기본으로 뺀다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import unicodedata
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from core.schemas import ProductCategory, Severity
from guardrails.normalize import loose_text
from guardrails.policy import PolicyCatalog, get_catalog
from ops.labeling import GUIDELINE_VERSION, FinalLabel, LabelDecision
from ops.review_queue import FINAL_STATES, ReviewItem

ROOT = Path(__file__).resolve().parents[1]
DATASETS = ROOT / "evals" / "datasets"
REPORTS = ROOT / "evals" / "reports"
DEMO_PATH = DATASETS / "labeling_demo.jsonl"

# golden.jsonl 에서 읽지 못할 때(번들에 데이터셋 폴더가 없는 실행판 등)만 쓰는 기본 필드. 평소에는 golden.jsonl 의 첫 행에서 읽는다.
FALLBACK_FIELDS = ["id", "category", "headline", "body", "cta", "label", "rule_ids", "disclosures", "facts", "claims", "landing_text", "difficulty", "notes", "source", "split"]
PROVENANCE_FIELDS = ["labelers", "adjudicated", "agreement", "rule_agreement", "single_labeler", "synthetic", "guideline_version", "queue_item_id"]
SPLITS = ("dev", "holdout")
DEFAULT_SPLIT_SEED = "preflight-split-v1"     # 이 값과 해시 규칙을 바꾸면 모든 분할이 바뀐다 — 바꾸지 않는다
DEFAULT_HOLDOUT_FRACTION = 0.2                # 가정한 값: 최종 확인용 비율. 표본이 작을 때 이 비율이 정확히 지켜지지는 않는다(해시 배정이라 확률적)
NEAR_DUP_THRESHOLD = 0.8                      # 글자 3-gram 자카드 이 이상이면 근접 중복 (가정한 값; guardrails/duplicate.py 의 반려 유사 판단 0.6 보다 엄격)
MIN_NEAR_DUP_CHARS = 8                        # 정규화한 문구가 이보다 짧으면 자카드가 불안정해 근접 중복 비교에서 뺀다 (완전히 같은 경우만 중복)
EXACT_LIMIT = 400                             # 이 건수 이하면 모든 쌍을 정확히 비교하고, 넘으면 샤드(밴딩)로 후보만 추린 뒤 정확히 비교한다
N_PERM, N_BANDS = 64, 16                      # 샤드: 64개 해시 → 16개 밴드(밴드당 4개). 자카드 0.8 인 쌍이 같은 샤드에 한 번이라도 들어갈 확률 ≈ 99.98%
_MERSENNE = (1 << 61) - 1
MAX_ROW_TEXT = 5000


# ---------------------------------------------------------------------------
# 읽기·쓰기
# ---------------------------------------------------------------------------
def load_jsonl_rows(path: Path | str) -> list[dict]:
    """'#' 으로 시작하는 줄과 빈 줄을 건너뛰고 JSON 행을 읽는다 (evals/datasets 형식)."""
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rows.append(json.loads(line))
    return rows


def golden_fields(path: Path | str = DATASETS / "golden.jsonl") -> list[str]:
    """평가셋 행의 필드 순서 — golden.jsonl 첫 행에서 읽는다 (선택 필드 label_history 는 제외). 읽지 못하면 기본 목록."""
    try:
        first = load_jsonl_rows(path)[0]
    except (OSError, IndexError, json.JSONDecodeError):
        return list(FALLBACK_FIELDS)
    return [k for k in first if k != "label_history"]


def rows_to_jsonl(rows: Iterable[Mapping], header: str = "") -> str:
    lines = [f"# {ln}" for ln in header.splitlines() if ln.strip()]
    lines += [json.dumps(dict(r), ensure_ascii=False) for r in rows]
    return "\n".join(lines) + "\n"


def write_jsonl_new(path: Path | str, text: str) -> Path:
    """새 파일에만 쓴다 (배타적 생성). 이미 있는 파일이면 FileExistsError — 기존 평가셋을 덮어쓰지 않는다."""
    p = Path(path)
    with open(p, "x", encoding="utf-8") as f:
        f.write(text)
    return p


# ---------------------------------------------------------------------------
# 정규화·분할 해시
# ---------------------------------------------------------------------------
def normalize_key(text: str) -> str:
    """같은 문구인지 가리는 키: 표기 변형(전각·한자·보이지 않는 글자·공백)을 접고 소문자로, 글자·숫자만 남긴다."""
    t = loose_text(text or "").lower()
    return "".join(ch for ch in t if unicodedata.category(ch)[0] in ("L", "N"))


def copy_key(headline: str, body: str, cta: str) -> str:
    """분할과 근접 중복 비교에 쓰는 문구 키 — 제목·본문·버튼. 필수 고지 문구는 어느 광고에나 같게 들어가므로 뺀다."""
    return normalize_key(" ".join([headline or "", body or "", cta or ""]))


def split_for(key: str, *, seed: str = DEFAULT_SPLIT_SEED, holdout_fraction: float = DEFAULT_HOLDOUT_FRACTION) -> str:
    """문구 키 → 'dev' | 'holdout'. 키가 같으면 항상 같은 결과이고, 다른 항목의 존재에 영향받지 않는다."""
    h = int(hashlib.sha256(f"{seed}|{key}".encode("utf-8")).hexdigest()[:12], 16) / float(16 ** 12)
    return "holdout" if h < holdout_fraction else "dev"


def _row_id(core: str, disclosures: Sequence[str], facts: Sequence[Mapping], landing_text: str = "", claims: Sequence[Mapping] = ()) -> str:
    blob = "|".join([core, "/".join(normalize_key(d) for d in disclosures), "/".join(normalize_key(f.get("text", "")) for f in facts), normalize_key(landing_text),
                     "/".join(sorted(normalize_key(c.get("text", "")) for c in claims))])
    return "L-" + hashlib.sha1(blob.encode("utf-8")).hexdigest()[:8]


# ---------------------------------------------------------------------------
# 내보내기
# ---------------------------------------------------------------------------
@dataclass
class ExportResult:
    rows: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)       # {item_id, reason}
    warnings: list[str] = field(default_factory=list)
    exported_item_ids: list[str] = field(default_factory=list)


def _note(item: ReviewItem, final: FinalLabel) -> str:
    bits = []
    if final.adjudicated:
        bits.append(f"라벨러가 갈려 조정으로 확정: {final.rationale}")
    elif final.single_labeler:
        bits.append("라벨러 1명 — 독립 확인 없음")
    else:
        bits.append(f"라벨러 {len(final.labelers)}명 일치")
        if final.rationale:
            bits.append(final.rationale)
    if final.rules_unresolved:
        bits.append("규칙 선택이 라벨러마다 달라 합집합으로 기록")
    if item.notes:
        bits.append(item.notes)
    return " · ".join(bits)[:600]


def export_rows_report(items: Iterable[ReviewItem], *, guideline_version: str = GUIDELINE_VERSION, split_seed: str = DEFAULT_SPLIT_SEED,
                       holdout_fraction: float = DEFAULT_HOLDOUT_FRACTION, include_synthetic: bool = False, allow_single_labeler: bool = False,
                       finals: Optional[Mapping[str, FinalLabel]] = None, required_labels: int = 2) -> ExportResult:
    """확정된 라벨(라벨러 일치 또는 조정)만 평가셋 행으로. 검증용 항목은 이미 기존 데이터셋에 있는 문제이므로 제외한다.
    - 합성 예시(synthetic)는 기본으로 뺀다 (실제 라벨이 아니다). include_synthetic=True 면 포함하되 source 와 synthetic 필드에 표시.
    - 라벨러가 한 명뿐인 항목(required_labels=1)은 allow_single_labeler=True 일 때만. 행에 single_labeler 가 표시된다.
    - 같은 문구·고지·사실이 이미 나온 항목은 건너뛴다 (라벨이 충돌하면 경고)."""
    from ops.labeling import resolve_final

    res = ExportResult()
    fields = golden_fields()
    seen: dict[str, dict] = {}
    for it in items:
        if it.is_gold:
            res.skipped.append({"item_id": it.id, "reason": "검증용 항목은 내보내지 않음"})
            continue
        if it.synthetic and not include_synthetic:
            res.skipped.append({"item_id": it.id, "reason": "합성 예시는 평가셋에 넣지 않음"})
            continue
        if it.state not in FINAL_STATES:
            res.skipped.append({"item_id": it.id, "reason": f"라벨이 확정되지 않음({it.state.value})"})
            continue
        final = (finals or {}).get(it.id) or resolve_final(it.annotations, it.adjudication, required_labels)
        if final is None:
            res.skipped.append({"item_id": it.id, "reason": "확정된 라벨이 없음"})
            continue
        if final.single_labeler and not allow_single_labeler:
            res.skipped.append({"item_id": it.id, "reason": "라벨러 1명 — 일치도를 확인할 수 없어 제외"})
            continue
        c = it.ad_copy
        key = copy_key(c.headline, c.body, c.cta)
        if len(key) == 0:
            res.skipped.append({"item_id": it.id, "reason": "문구가 비어 있음"})
            continue
        facts = [{"id": f.id, "text": f.text} for f in it.facts]
        claims = [{"text": x.text, "fact_id": x.fact_id} for x in c.claims]
        rid = _row_id(key, c.disclosures, facts, it.landing_text, claims)
        if rid in seen:
            prev = seen[rid]
            if prev["label"] != final.decision.value:
                res.warnings.append(f"같은 문구·고지·사실인데 라벨이 다른 항목이 있습니다({prev['id']} {prev['label']} ↔ {final.decision.value}). 둘 다 확인이 필요해 뒤의 것은 제외했습니다.")
            res.skipped.append({"item_id": it.id, "reason": f"이미 내보낸 항목({rid})과 문구·고지·사실·랜딩 문구가 같음"})
            continue
        unrated = "gray" if (final.adjudicated or final.rules_unresolved) else "unrated"
        row = {"id": rid, "category": it.category, "headline": c.headline, "body": c.body, "cta": c.cta, "label": final.decision.value,
               "rule_ids": list(final.rule_ids), "disclosures": list(c.disclosures), "facts": facts,
               "claims": claims, "landing_text": it.landing_text, "difficulty": unrated,
               "notes": _note(it, final), "source": "labeling_synthetic_demo" if it.synthetic else "labeling_queue",
               "split": split_for(key, seed=split_seed, holdout_fraction=holdout_fraction),
               "labelers": list(final.labelers), "adjudicated": final.adjudicated, "agreement": final.agreement, "rule_agreement": final.rule_agreement,
               "single_labeler": final.single_labeler, "synthetic": it.synthetic, "guideline_version": guideline_version, "queue_item_id": it.id}
        ordered = {k: row[k] for k in fields if k in row}      # golden.jsonl 과 같은 필드 순서, 출처 필드는 뒤에
        for k in row:
            ordered.setdefault(k, row[k])
        seen[rid] = ordered
        res.rows.append(ordered)
        res.exported_item_ids.append(it.id)
    if any(r["single_labeler"] for r in res.rows):
        res.warnings.append("라벨러가 한 명인 행이 있습니다. 일치도를 확인할 수 없고 독립 검증이 아닙니다.")
    if any(r["synthetic"] for r in res.rows):
        res.warnings.append("합성 예시가 포함돼 있습니다. 실제 라벨이 아니므로 평가셋에 합치지 마세요.")
    return res


def export_rows(items: Iterable[ReviewItem], *, guideline_version: str = GUIDELINE_VERSION, split_seed: str = DEFAULT_SPLIT_SEED, **kw) -> list[dict]:
    """`export_rows_report(...).rows` — 평가셋 행 목록."""
    return export_rows_report(items, guideline_version=guideline_version, split_seed=split_seed, **kw).rows


def revise_label(row: Mapping, *, new_label: str, new_rule_ids: Sequence[str], reason: str, guideline_version: str) -> dict:
    """라벨을 고칠 때의 절차: 행을 복사해 새 라벨을 적고 이전 라벨을 label_history 에 남긴다 (원본 행·파일은 건드리지 않는다).
    이유가 비어 있으면 거부. 이전 평가 결과와 비교하려면 이 변경 이력과 함께 보관한다 (docs/LABELING.md)."""
    if not reason.strip():
        raise ValueError("라벨을 바꾸는 이유를 적어야 합니다")
    if new_label not in {d.value for d in LabelDecision}:
        raise ValueError(f"알 수 없는 라벨: {new_label}")
    out = dict(row)
    hist = list(out.get("label_history", []))
    hist.append({"version": guideline_version, "label": row.get("label"), "rule_ids": list(row.get("rule_ids", [])), "reason": reason.strip()})
    out["label"], out["rule_ids"], out["label_history"] = new_label, list(new_rule_ids), hist
    return out


def select_gold_rows(rows: Sequence[Mapping], n: int, *, seed: str = "gold", difficulties: Sequence[str] = ("clear", "benign_scary")) -> list[dict]:
    """검증용 항목으로 쓸 행을 고른다: 라벨 판단이 비교적 분명한 난이도만, 시드에 따라 결정론적으로. 정답은 작성자의 라벨이다."""
    pool = [dict(r) for r in rows if r.get("difficulty") in difficulties and r.get("label") in {d.value for d in LabelDecision}]
    pool.sort(key=lambda r: hashlib.sha256(f"{seed}|{r.get('id')}".encode("utf-8")).hexdigest())
    return pool[:max(0, n)]


# ---------------------------------------------------------------------------
# 근접 중복 (정규화 + 자카드 + 샤드)
# ---------------------------------------------------------------------------
def shingles(key: str, n: int = 3) -> frozenset[str]:
    if len(key) < n:
        return frozenset({key}) if key else frozenset()
    return frozenset(key[i:i + n] for i in range(len(key) - n + 1))


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _perm_params(seed: int = 7) -> list[tuple[int, int]]:
    rng = random.Random(seed)
    return [(rng.randrange(1, _MERSENNE), rng.randrange(0, _MERSENNE)) for _ in range(N_PERM)]


_PERMS = _perm_params()


def _minhash(sh: frozenset) -> list[int]:
    base = [zlib.crc32(s.encode("utf-8")) for s in sh]
    return [min((a * x + b) % _MERSENNE for x in base) for a, b in _PERMS]


def near_duplicate_pairs(keys: Sequence[str], *, threshold: float = NEAR_DUP_THRESHOLD, exact_limit: int = EXACT_LIMIT) -> list[tuple[int, int, float]]:
    """정규화한 키 목록에서 자카드 ≥ threshold 인 쌍 (i<j, 유사도). 같은 키(완전히 같은 문구)는 유사도 1.0.
    건수가 exact_limit 이하면 모든 쌍을 비교하고, 넘으면 MinHash 밴딩으로 같은 샤드에 든 후보 쌍만 정확한 자카드로 확인한다(확률적 — 재현율 ≈ 99.98% @0.8)."""
    sh = [shingles(k) if len(k) >= MIN_NEAR_DUP_CHARS else frozenset() for k in keys]
    out: list[tuple[int, int, float]] = []
    exact_same: dict[str, list[int]] = defaultdict(list)
    for i, k in enumerate(keys):
        if k:
            exact_same[k].append(i)
    for idxs in exact_same.values():
        for i, j in combinations(idxs, 2):
            out.append((i, j, 1.0))
    usable = [i for i, s in enumerate(sh) if s]
    cands: set[tuple[int, int]] = set()
    if len(usable) <= exact_limit:
        cands = {(i, j) for i, j in combinations(usable, 2)}
    else:
        rows_per_band = N_PERM // N_BANDS
        buckets: dict[tuple, list[int]] = defaultdict(list)
        for i in usable:
            mh = _minhash(sh[i])
            for b in range(N_BANDS):
                buckets[(b, tuple(mh[b * rows_per_band:(b + 1) * rows_per_band]))].append(i)
        for ids in buckets.values():
            if len(ids) > 1:
                for i, j in combinations(sorted(set(ids)), 2):
                    cands.add((i, j))
    for i, j in sorted(cands):
        if keys[i] == keys[j]:
            continue
        s = jaccard(sh[i], sh[j])
        if s >= threshold:
            out.append((i, j, round(s, 4)))
    out.sort()
    return out


# ---------------------------------------------------------------------------
# 검증
# ---------------------------------------------------------------------------
@dataclass
class Issue:
    level: str                 # error | warn | info
    code: str
    message: str
    row_ids: list[str] = field(default_factory=list)


@dataclass
class NearDup:
    a: str
    b: str
    similarity: float
    exact: bool
    same_split: bool
    same_label: bool
    a_split: str = ""
    b_split: str = ""
    from_existing: bool = False   # 한쪽이 기존 데이터셋(비교용)에서 온 행
    full_same: bool = False       # 제목·본문·버튼·고지·사실이 모두 같음


@dataclass
class DatasetValidation:
    n_rows: int
    issues: list[Issue] = field(default_factory=list)
    near_duplicates: list[NearDup] = field(default_factory=list)
    leaks: list[NearDup] = field(default_factory=list)          # 개발용↔최종 확인용에 걸친 근접/완전 중복
    label_distribution: dict[str, int] = field(default_factory=dict)
    split_distribution: dict[str, int] = field(default_factory=dict)
    category_distribution: dict[str, int] = field(default_factory=dict)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warn"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary_lines(self) -> list[str]:
        out = [f"행 {self.n_rows}건 · 오류 {len(self.errors)}건 · 경고 {len(self.warnings)}건 · 근접 중복 {len(self.near_duplicates)}쌍 · 개발↔최종 확인 누수 위험 {len(self.leaks)}쌍",
               f"라벨 분포 {dict(self.label_distribution)} · 분할 {dict(self.split_distribution)}"]
        out += [f"[{i.level}] {i.message}" for i in self.issues]
        return out


_STR_FIELDS = ("id", "category", "headline", "body", "cta", "label", "landing_text", "difficulty", "notes", "source", "split")


def _row_issues(r: Mapping, idx: int, catalog: PolicyCatalog) -> list[Issue]:
    rid = str(r.get("id", f"#{idx}"))
    out: list[Issue] = []

    def err(code: str, msg: str):
        out.append(Issue("error", code, f"{rid}: {msg}", [rid]))

    def warn(code: str, msg: str):
        out.append(Issue("warn", code, f"{rid}: {msg}", [rid]))

    for f in ("id", "category", "headline", "body", "cta", "label"):
        if f not in r:
            err("missing_field", f"필드 {f} 가 없습니다")
    for f in _STR_FIELDS:
        if f in r and not isinstance(r[f], str):
            err("bad_type", f"{f} 는 문자열이어야 합니다")
    for f in ("rule_ids", "disclosures", "facts", "claims"):
        if f in r and not isinstance(r[f], list):
            err("bad_type", f"{f} 는 목록이어야 합니다")
    if out:
        return out
    if not r["id"].strip():
        err("empty_id", "id 가 비어 있습니다")
    if r["category"] not in {c.value for c in ProductCategory}:
        err("bad_category", f"알 수 없는 업종 {r['category']}")
    if r["label"] not in {d.value for d in LabelDecision}:
        err("bad_label", f"알 수 없는 라벨 {r['label']}")
    if "split" not in r:
        warn("no_split", "split 필드가 없습니다 (개발용으로 취급)")
    elif r["split"] not in SPLITS:
        err("bad_split", f"split 은 dev 또는 holdout 이어야 합니다 ({r['split']})")
    if not (r["headline"] or r["body"]):
        err("empty_text", "제목과 본문이 모두 비어 있습니다")
    for f in r.get("facts", []):
        if not (isinstance(f, dict) and isinstance(f.get("id"), str) and isinstance(f.get("text"), str)):
            err("bad_fact", "facts 의 각 항목은 {id, text} 여야 합니다")
            break
    for c in r.get("claims", []):
        if not (isinstance(c, dict) and isinstance(c.get("text"), str)):
            err("bad_claim", "claims 의 각 항목은 {text, fact_id} 여야 합니다")
            break
    if any(len(str(r.get(f, ""))) > MAX_ROW_TEXT for f in ("headline", "body", "cta", "landing_text", "notes")):
        err("too_long", f"문구가 {MAX_ROW_TEXT}자를 넘습니다")
    rules = [x for x in r.get("rule_ids", [])]
    unknown = [x for x in rules if not isinstance(x, str) or catalog.get(x) is None]
    if unknown:
        err("unknown_rule", f"카탈로그에 없는 규칙 {unknown}")
    known = [catalog.get(x) for x in rules if isinstance(x, str) and catalog.get(x) is not None]
    if r["label"] == "APPROVE":
        blocks = [x.id for x in known if x.severity == Severity.BLOCK]
        if blocks:
            err("approve_with_block", f"APPROVE 인데 게재 불가 등급 규칙 {blocks} 가 있습니다")
        fixes = [x.id for x in known if x.severity == Severity.FIX]
        if fixes:
            warn("approve_with_fix", f"APPROVE 인데 수정 필요 등급 규칙 {fixes} 가 있습니다")
    elif r["label"] in ("REVISE", "REJECT"):
        if not rules:
            err("flag_without_rule", f"{r['label']} 인데 규칙이 하나도 없습니다")
        elif r["label"] == "REJECT" and known and not any(x.severity == Severity.BLOCK for x in known):
            warn("reject_without_block", "REJECT 인데 게재 불가 등급 규칙이 없습니다")
        elif r["label"] == "REVISE" and any(x.severity == Severity.BLOCK for x in known):
            warn("revise_with_block", "REVISE 인데 게재 불가 등급 규칙이 있습니다")
    if r.get("synthetic") is True:
        warn("synthetic", "합성 예시 행입니다 — 실제 라벨이 아닙니다")
    return out


def validate_dataset(rows: Sequence[Mapping], *, existing: Optional[Sequence[Mapping]] = None, catalog: Optional[PolicyCatalog] = None,
                     near_dup_threshold: float = NEAR_DUP_THRESHOLD) -> DatasetValidation:
    """평가셋 행을 점검한다: 스키마, 라벨-규칙 일관성, id 중복, 같은 문구 라벨 충돌, 근접 중복, 개발↔최종 확인용 누수.
    existing 을 주면 새 행과 기존 행(예: golden·holdout) 사이도 비교한다 (기존 행끼리는 비교하지 않는다)."""
    catalog = catalog or get_catalog()
    val = DatasetValidation(n_rows=len(rows))
    seen: dict[str, int] = {}
    for i, r in enumerate(rows):
        val.issues.extend(_row_issues(r, i, catalog))
        rid = str(r.get("id", ""))
        if rid in seen:
            val.issues.append(Issue("error", "duplicate_id", f"{rid}: id 가 중복입니다 ({seen[rid] + 1}번째와 {i + 1}번째)", [rid]))
        seen.setdefault(rid, i)
    val.label_distribution = dict(Counter(str(r.get("label")) for r in rows))
    val.split_distribution = dict(Counter(str(r.get("split", "dev")) for r in rows))
    val.category_distribution = dict(Counter(str(r.get("category")) for r in rows))
    if rows:
        top = max(val.label_distribution.values()) / len(rows)
        if top >= 0.9 and len(rows) >= 10:
            val.issues.append(Issue("warn", "label_skew", f"라벨이 한쪽으로 치우쳐 있습니다 (최다 라벨 {top:.0%}). 정밀도·재현율 해석에 주의하세요."))
    both = [dict(r, _existing=False) for r in rows] + [dict(r, _existing=True) for r in (existing or [])]
    keys = [copy_key(str(r.get("headline", "")), str(r.get("body", "")), str(r.get("cta", ""))) for r in both]
    sigs = [(k, tuple(normalize_key(str(d)) for d in r.get("disclosures", []) if isinstance(d, str)),
             tuple(normalize_key(str(f.get("text", ""))) for f in r.get("facts", []) if isinstance(f, dict)),
             normalize_key(str(r.get("landing_text", ""))),
             tuple(sorted(normalize_key(str(c.get("text", ""))) for c in r.get("claims", []) if isinstance(c, dict)))) for k, r in zip(keys, both)]
    new_n = len(rows)
    for i, j, sim in near_duplicate_pairs(keys, threshold=near_dup_threshold):
        if i >= new_n and j >= new_n:
            continue   # 기존 행끼리는 비교하지 않는다
        a, b = both[i], both[j]
        sa, sb = str(a.get("split", "dev")), str(b.get("split", "dev"))
        nd = NearDup(a=str(a.get("id")), b=str(b.get("id")), similarity=sim, exact=keys[i] == keys[j], same_split=sa == sb, same_label=a.get("label") == b.get("label"),
                     a_split=sa, b_split=sb, from_existing=bool(a["_existing"] or b["_existing"]), full_same=sigs[i] == sigs[j])
        val.near_duplicates.append(nd)
        if sa != sb:
            val.leaks.append(nd)
    for nd in val.near_duplicates:
        lab = "라벨 같음" if nd.same_label else "라벨 다름"
        where = f"{nd.a} ↔ {nd.b}"
        if nd.full_same:
            # 제목·본문·버튼·고지·사실·랜딩 문구·주장이 모두 같다 — 같은 문제를 두 번 센 것이거나, 라벨이 다르면 한쪽 라벨이 틀렸다
            lvl, msg = ("warn", f"{where}: 완전히 같은 행입니다 · {lab} · 분할 {nd.a_split}/{nd.b_split}") if nd.same_label else \
                       ("error", f"{where}: 완전히 같은 행인데 라벨이 다릅니다 · 분할 {nd.a_split}/{nd.b_split}")
        elif nd.exact:
            # 제목·본문·버튼이 같고 고지·사실·랜딩 문구·주장 중 일부만 다르다 — 조건 대조 사례처럼 의도된 쌍일 수 있다
            lvl, msg = "info", f"{where}: 제목·본문·버튼이 같고 고지·사실·랜딩 문구가 다릅니다(대조 쌍일 수 있음) · {lab} · 분할 {nd.a_split}/{nd.b_split}"
        else:
            lvl, msg = "warn", f"{where}: 비슷한 문구(유사도 {nd.similarity:.2f}) · {lab} · 분할 {nd.a_split}/{nd.b_split}"
        val.issues.append(Issue(lvl, "near_duplicate", msg, [nd.a, nd.b]))
    for nd in val.leaks:
        val.issues.append(Issue("warn", "split_leak", f"누수 위험: {nd.a}({nd.a_split}) ↔ {nd.b}({nd.b_split}) 가 {'같은' if nd.exact else '거의 같은'} 문구입니다. "
                                "최종 확인용 평가가 개발용에 본 문구의 영향을 받을 수 있습니다.", [nd.a, nd.b]))
    return val


# ---------------------------------------------------------------------------
# 라벨 오류 의심 사례 (저장된 평가 결과에서)
# ---------------------------------------------------------------------------
RANK = {"APPROVE": 0, "REVISE": 1, "REJECT": 2}
SUSPECT_MIN_CONFIDENCE = 0.80       # 이 이상의 확신으로 라벨과 다르게 판정한 실행이 하나는 있어야 의심 대상 (가정한 값; 자동 판정 기준 0.8 과 같다)
STEP_WEIGHT = {1: 0.6, 2: 1.0}      # 라벨과 한 단계 다름(통과↔수정, 수정↔게재 불가) / 두 단계 다름(통과↔게재 불가)


@dataclass
class Suspect:
    case_id: str
    dataset: str
    label: str
    score: float
    n_runs: int                       # 판정을 낸 실행 수 (담당자 확인·오류는 제외)
    n_disagree: int
    mean_confidence: float            # 라벨과 달랐던 실행들의 평균 확신
    predicted: dict[str, int]
    extra_rules: dict[str, int]       # 라벨에 없는데 실행이 낸 규칙 (달랐던 실행 기준)
    missing_rules: dict[str, int]     # 라벨에 있는데 실행이 내지 않은 규칙
    reports: list[str]
    note: str


def load_report_cases(path_or_report: Path | str | Mapping, *, include_demo: bool = False) -> list[dict]:
    """평가 결과(evals/reports/*.json)의 사례 기록을 읽는다. 끝까지 돌지 못한 결과는 건너뛰고, 대역(demo) 실행은 기본으로 뺀다(모델 판단이 아니므로).
    각 사례에 _report(결과 이름) · _dataset · _mode 를 덧붙인다."""
    if isinstance(path_or_report, Mapping):
        rep, name = path_or_report, str(path_or_report.get("name", "report"))
    else:
        p = Path(path_or_report)
        rep, name = json.loads(p.read_text(encoding="utf-8")), p.name
    if not isinstance(rep, Mapping) or not isinstance(rep.get("cases"), list) or rep.get("aborted"):
        return []
    mode = str(rep.get("mode", ""))
    if mode == "demo" and not include_demo:
        return []
    return [dict(c, _report=name, _dataset=str(rep.get("dataset", "")), _mode=mode) for c in rep["cases"] if isinstance(c, dict)]


def label_noise_suspects(records: Iterable[Mapping], *, min_confidence: float = SUSPECT_MIN_CONFIDENCE, top: Optional[int] = None) -> list[Suspect]:
    """저장된 평가 기록에서 '코드·모델이 높은 확신으로 라벨과 다르게 판정한' 사례를 점수순으로. 라벨 오류를 사람이 다시 볼 후보를 찾는 용도다.

    입력: 평가 결과 사례 기록(id · label · predicted · confidence · label_rules · predicted_rules · error …). 여러 결과의 기록을 섞어 줄 수 있다.
    - 판정을 못 낸 기록(HUMAN_REVIEW · 오류가 있는 기록)은 세지 않는다. 같은 (데이터셋, id, 라벨) 으로 묶는다 — 라벨이 바뀐 버전은 따로 센다.
    - 실행 하나의 점수 = 확신 × (한 단계 차이 0.6 / 두 단계 차이 1.0). 사례 점수 = 달랐던 실행들의 점수 합 / 판정을 낸 실행 수. 여러 실행이 같은 쪽으로 어긋날수록 높다.
    - 확신이 min_confidence 이상인 어긋난 실행이 하나도 없으면 의심으로 세지 않는다.
    라벨이 틀렸을 때뿐 아니라 규칙·모델이 틀렸을 때도 점수가 높다 — 의심은 확정이 아니다."""
    groups: dict[tuple[str, str, str], list[Mapping]] = defaultdict(list)
    for c in records:
        if not c.get("id") or c.get("label") not in RANK or c.get("predicted") not in RANK:
            continue
        if c.get("error") or c.get("rewrite_error"):
            continue
        groups[(str(c.get("_dataset", "")), str(c["id"]), str(c["label"]))].append(c)
    out: list[Suspect] = []
    for (ds, cid, label), runs in groups.items():
        bad = [c for c in runs if c["predicted"] != label]
        if not bad or not any(float(c.get("confidence", 0.0)) >= min_confidence for c in bad):
            continue
        score = sum(float(c.get("confidence", 0.0)) * STEP_WEIGHT[abs(RANK[c["predicted"]] - RANK[label])] for c in bad) / len(runs)
        label_rules = set(runs[0].get("label_rules") or [])
        extra, missing = Counter(), Counter()
        for c in bad:
            pr = set(c.get("predicted_rules") or [])
            extra.update(pr - label_rules)
            missing.update(label_rules - pr)
        pred = Counter(c["predicted"] for c in bad)
        mean_conf = sum(float(c.get("confidence", 0.0)) for c in bad) / len(bad)
        reports = sorted({str(c.get("_report", "")) for c in bad if c.get("_report")})
        note = (f"판정을 낸 {len(runs)}번의 실행 중 {len(bad)}번이 라벨 {label} 과 다르게 {', '.join(f'{k} {v}번' for k, v in pred.most_common())} 으로 판정(평균 확신 {mean_conf:.2f}). "
                + (f"라벨에 없는 규칙 {', '.join(k for k, _ in extra.most_common(3))} 를 반복해 냄. " if extra else "")
                + "라벨·규칙·모델 중 어느 쪽이 틀렸는지 사람이 원문을 다시 읽어 확인해야 합니다.")
        out.append(Suspect(case_id=cid, dataset=ds, label=label, score=round(score, 4), n_runs=len(runs), n_disagree=len(bad), mean_confidence=round(mean_conf, 4),
                           predicted=dict(pred), extra_rules=dict(extra), missing_rules=dict(missing), reports=reports, note=note))
    out.sort(key=lambda s: (-s.score, -s.n_disagree, s.dataset, s.case_id))
    return out[:top] if top else out


def suspects_from_files(paths: Iterable[Path | str], *, include_demo: bool = False, **kw) -> list[Suspect]:
    recs: list[dict] = []
    for p in paths:
        recs.extend(load_report_cases(p, include_demo=include_demo))
    return label_noise_suspects(recs, **kw)


# ---------------------------------------------------------------------------
# 합성 예시
# ---------------------------------------------------------------------------
def load_labeling_demo(path: Path | str = DEMO_PATH) -> list[dict]:
    """evals/datasets/labeling_demo.jsonl — 두 명의 가상 라벨러가 만든 합성 예시. 실제 라벨이 아니다. 형식이 어긋난 행은 ValueError."""
    rows = load_jsonl_rows(path)
    need = ("id", "category", "headline", "body", "cta", "annotations")
    for r in rows:
        miss = [k for k in need if k not in r]
        if miss:
            raise ValueError(f"합성 예시 행 {r.get('id', '?')} 에 {miss} 가 없습니다")
        if r.get("synthetic") is not True:
            raise ValueError(f"합성 예시 행 {r['id']} 에 synthetic: true 표시가 없습니다")
    return rows


# ---------------------------------------------------------------------------
# 명령줄
# ---------------------------------------------------------------------------
def _resolve_dataset(name: str) -> Path:
    p = Path(name)
    return p if p.exists() else DATASETS / f"{name}.jsonl"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="평가셋 점검·라벨 오류 의심 사례·합성 예시 일치도")
    ap.add_argument("--validate", metavar="DATASET", help="데이터셋 이름 또는 경로를 점검")
    ap.add_argument("--against", nargs="*", default=[], metavar="DATASET", help="새 행과 비교할 기존 데이터셋 (누수·근접 중복 점검)")
    ap.add_argument("--suspects", action="store_true", help="evals/reports 의 저장된 결과에서 라벨 오류 의심 사례")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--demo", action="store_true", help="합성 예시의 일치도")
    args = ap.parse_args(argv)
    if args.validate:
        rows = load_jsonl_rows(_resolve_dataset(args.validate))
        existing = [r for n in args.against for r in load_jsonl_rows(_resolve_dataset(n))]
        v = validate_dataset(rows, existing=existing or None)
        print("\n".join(v.summary_lines()))
        return 0 if v.ok else 1
    if args.suspects:
        paths = sorted(REPORTS.glob("*.json")) + sorted((REPORTS / "history").glob("*.json"))
        sus = suspects_from_files(paths, top=args.top)
        print(f"읽은 결과 {len(paths)}개(대역 실행·중단된 실행 제외) · 의심 사례 {len(sus)}건 (상위 {args.top}건)")
        for i, s in enumerate(sus, 1):
            print(f"{i:>2}. {s.case_id} [{s.dataset}] 라벨 {s.label} · 점수 {s.score:.3f} · 실행 {s.n_disagree}/{s.n_runs} 불일치 · {s.predicted} · 추가 규칙 {list(s.extra_rules)}")
        return 0
    if args.demo:
        from ops.labeling import agreement_report
        from ops.review_queue import build_demo_queue

        q = build_demo_queue(load_labeling_demo())
        rep = agreement_report(q.annotations())
        o = rep.overall
        print(f"합성 예시 {len(q.items())}건 · 라벨러 {rep.annotators} · 공통 {rep.n_multi_labeled}건")
        if o:
            print(f"일치율 {o.percent_agreement:.3f} · kappa {o.kappa:.3f} (95% 신뢰구간 {o.ci_low:.3f} ~ {o.ci_high:.3f}) · {o.band_ko} · 경고 {o.warnings}")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
