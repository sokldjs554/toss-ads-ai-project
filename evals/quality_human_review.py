"""별도 사람 검토용 블라인드 자료와 출처 검증 CSV 가져오기. 네트워크/모델 없음.

export는 라벨을 비워 둔다. import는 제출자의 명시적 확인을 기록할 뿐 사람의
신원이나 독립성을 인증하지 않는다. 기존 AI 작성 라벨/리포트는 덮어쓰지 않는다.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import secrets
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from evals.quality_signals import PAIRS_PATH, load_pairs

CSV_FIELDS = ["packet_id", "case_id", "preference", "confidence", "reason"]
INSTRUCTIONS = """# 문구 비교 검토 안내

제공된 상품 사실을 참고해 왼쪽·오른쪽 문구의 제목, 본문, 버튼을 모두 읽어 주세요.
모든 상품과 문구는 가상의 예시입니다. 실제 상품의 승인 여부나 광고 성과를 평가하는 작업이 아닙니다.

상품/혜택과 적용 조건을 이해하기 쉬운지, 버튼의 다음 행동이 분명한지, 불필요한 길이·반복·과장·어색한 말투가 있는지 비교해 주세요.
제목에 정보를 놓았다는 이유, 숫자가 많다는 이유, 문구가 길거나 짧다는 이유만으로 한쪽을 고를 필요는 없습니다.
양쪽의 장점이 다르면 판단에 중요했던 차이를 직접 설명해 주세요. 특정 문체 취향을 정답으로 가정하지 마세요.

`pairs.md`에서 사례를 읽고 `labels.csv`의 빈 칸 세 개를 채워 주세요. packet_id와 case_id는 바꾸지 마세요.

| 열 | 허용 값 | 뜻 |
|---|---|---|
| preference | left / right / tie / abstain | 왼쪽 우세 / 오른쪽 우세 / 전달력이 비슷함 / 판단 근거 부족 |
| confidence | low / medium / high | 자신의 판단에 대한 확신. 실제 성과 확률이 아닙니다. |
| reason | 직접 쓴 짧은 문장 | 차이를 판단한 근거. abstain이면 판단하기 어려웠던 이유 |

모든 사례에 답해 주세요. 확신이 없어도 임의로 우열을 정하지 말고 abstain을 사용하면 됩니다.
독립 검토를 위해 AI 도구의 답, 기존 정답, 다른 검토자의 답을 보지 않고 직접 작성해 주세요.
제출 시 운영자가 사람의 직접 작성·독립 검토·기존 라벨 미열람 여부를 확인합니다.
이 확인은 제출자의 진술이며 소프트웨어가 사람의 신원을 인증하는 절차는 아닙니다.
"""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _md(value: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+.!|<>])", r"\\\1", value).replace("\r", " ").replace("\n", " ")


def export_packet(dataset: Path, output: Path, *, packet_id: str, seed: str | None = None, split: str = "holdout") -> dict:
    """새 디렉터리에만 내보낸다. seed는 조정자 파일에만 보관한다."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", packet_id):
        raise ValueError("invalid packet id")
    pairs = load_pairs(path=dataset, split=split)
    if not pairs:
        raise ValueError("packet needs at least one source pair")
    seed = seed if seed is not None else secrets.token_hex(32)
    if not seed:
        raise ValueError("randomization seed must not be empty")
    randomized = sorted(pairs, key=lambda p: _sha(f"{seed}:sequence:{p.id}".encode()))
    cases, mapping = [], []
    for pair in randomized:
        identity = _sha(f"{seed}:identity:{pair.id}".encode())[:20]
        case_id = f"case-{identity}"
        left = "a" if int(_sha(f"{seed}:side:{pair.id}".encode()), 16) % 2 == 0 else "b"
        right = "b" if left == "a" else "a"
        cases.append({"case_id": case_id, "facts": [{"id": f.id, "text": f.text} for f in pair.facts],
                      "left": getattr(pair, left), "right": getattr(pair, right)})
        mapping.append({"case_id": case_id, "pair_id": pair.id, "left_source": left, "right_source": right})
    packet = {"schema_version": 1, "packet_id": packet_id, "cases": cases}
    packet_bytes = _json_bytes(packet)
    manifest = {"schema_version": 1, "packet_id": packet_id,
                "dataset_sha256": _sha(Path(dataset).read_bytes()), "packet_sha256": _sha(packet_bytes),
                "split": split, "randomization_seed": seed, "mapping": mapping,
                "note": "조정자용. 검토자에게 공유하지 않는다. 원래 라벨이나 모델 답은 포함하지 않는다."}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    reviewer = output / "reviewer"
    reviewer.mkdir()
    (reviewer / "cases.json").write_bytes(packet_bytes)
    (reviewer / "instructions.md").write_text(INSTRUCTIONS, encoding="utf-8")
    with (reviewer / "labels.csv").open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows({"packet_id": packet_id, "case_id": c["case_id"]} for c in cases)
    rendered = ["# 문구 비교 사례\n", "검토 방법과 입력 값은 instructions.md를 참고해 주세요.\n"]
    for case in cases:
        rendered.extend([f"## {_md(case['case_id'])}\n", "상품 사실:\n"])
        rendered.extend(f"- {_md(f['text'])}" for f in case["facts"])
        rendered.extend(["", "| 위치 | 제목 | 본문 | 버튼 |", "|---|---|---|---|"])
        for side, name in (("left", "왼쪽"), ("right", "오른쪽")):
            rendered.append("| " + " | ".join([name, *(_md(case[side][f]) for f in ("headline", "body", "cta"))]) + " |")
        rendered.append("")
    (reviewer / "pairs.md").write_text("\n".join(rendered).rstrip("\n") + "\n", encoding="utf-8")
    manifest["review_material_sha256"] = {name: _sha((reviewer / name).read_bytes()) for name in ("pairs.md", "instructions.md")}
    (output / "coordinator-manifest.json").write_bytes(_json_bytes(manifest))
    with zipfile.ZipFile(output / "reviewer-packet.zip", "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("cases.json", "pairs.md", "labels.csv", "instructions.md"):
            archive.write(reviewer / name, arcname=name)
    return {"packet_id": packet_id, "cases": len(cases), "reviewer_packet": str(output / "reviewer-packet.zip"),
            "coordinator_manifest": str(output / "coordinator-manifest.json"), "labels_created": 0}


def _verify_packet(packet_path: Path, manifest_path: Path, dataset: Path) -> tuple[dict, dict]:
    packet_bytes = packet_path.read_bytes()
    packet, manifest = json.loads(packet_bytes), json.loads(manifest_path.read_bytes())
    if packet.get("schema_version") != 1 or manifest.get("schema_version") != 1:
        raise ValueError("unknown packet schema")
    if _sha(packet_bytes) != manifest.get("packet_sha256"):
        raise ValueError("packet hash differs from coordinator manifest")
    materials = manifest.get("review_material_sha256", {})
    if set(materials) != {"pairs.md", "instructions.md"} or any(
        not (packet_path.parent / name).is_file() or _sha((packet_path.parent / name).read_bytes()) != expected
        for name, expected in materials.items()
    ):
        raise ValueError("review material hash differs from coordinator manifest")
    if _sha(dataset.read_bytes()) != manifest.get("dataset_sha256"):
        raise ValueError("source dataset hash differs from coordinator manifest")
    if packet.get("packet_id") != manifest.get("packet_id"):
        raise ValueError("packet id differs from coordinator manifest")
    pairs = {p.id: p for p in load_pairs(path=dataset, split=manifest["split"])}
    mapped = {}
    seen_pairs = set()
    for row in manifest["mapping"]:
        case_id, pair_id = row["case_id"], row["pair_id"]
        if case_id in mapped or pair_id in seen_pairs:
            raise ValueError("duplicate coordinator mapping")
        if pair_id not in pairs or {row["left_source"], row["right_source"]} != {"a", "b"}:
            raise ValueError("invalid source case mapping")
        mapped[case_id] = row
        seen_pairs.add(pair_id)
    cases = {c["case_id"]: c for c in packet["cases"]}
    if len(cases) != len(packet["cases"]) or set(cases) != set(mapped) or seen_pairs != set(pairs):
        raise ValueError("packet must preserve the complete source case set")
    for case_id, case in cases.items():
        row = mapped[case_id]
        pair = pairs[row["pair_id"]]
        expected = {"case_id": case_id, "facts": [{"id": f.id, "text": f.text} for f in pair.facts],
                    "left": getattr(pair, row["left_source"]), "right": getattr(pair, row["right_source"])}
        if case != expected:
            raise ValueError("blinded content differs from source case")
    return packet, mapped


def import_labels(submission: Path, packet_path: Path, manifest_path: Path, dataset: Path, *, reviewer_id: str,
                  attest_human: bool, attest_independent: bool, attest_blinded: bool) -> dict:
    """한 검토자의 전체 답을 검증한다. 이 함수는 파일을 쓰거나 기존 라벨을 수정하지 않는다."""
    if any(v is not True for v in (attest_human, attest_independent, attest_blinded)):
        raise ValueError("explicit attestations for human, independent, and blinded review are required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", reviewer_id):
        raise ValueError("reviewer id must be a nonempty pseudonymous identifier")
    packet, mapping = _verify_packet(Path(packet_path), Path(manifest_path), Path(dataset))
    raw = Path(submission).read_bytes()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    if reader.fieldnames != CSV_FIELDS:
        raise ValueError("CSV columns differ from the blank template")
    labels, seen = [], set()
    for row in reader:
        if set(row) != set(CSV_FIELDS) or any(not isinstance(v, str) for v in row.values()):
            raise ValueError("malformed CSV row")
        row = {key: value.strip() for key, value in row.items()}
        if row["packet_id"] != packet["packet_id"]:
            raise ValueError("submission packet id differs")
        case_id = row["case_id"]
        if case_id in seen:
            raise ValueError("duplicate submitted case")
        if case_id not in mapping:
            raise ValueError("unknown case in submission")
        seen.add(case_id)
        preference = row["preference"]
        if preference not in {"left", "right", "tie", "abstain"}:
            raise ValueError("preference must be left/right/tie/abstain; blank is not a rating")
        if row["confidence"] not in {"low", "medium", "high"}:
            raise ValueError("confidence must be low/medium/high")
        if not row["reason"] or len(row["reason"]) > 2000:
            raise ValueError("reason must contain 1–2000 characters")
        source = mapping[case_id]
        normalized = source[f"{preference}_source"] if preference in {"left", "right"} else preference
        labels.append({"case_id": case_id, "pair_id": source["pair_id"], "preference": normalized,
                       "displayed_preference": preference, "confidence": row["confidence"], "reason": row["reason"]})
    if seen != set(mapping):
        raise ValueError("submission must contain the complete packet; use abstain for uncertain cases")
    return {"schema_version": 1, "label_origin": "human_submission", "packet_id": packet["packet_id"],
            "reviewer_id": reviewer_id, "identity_verified": False, "independence_verified": False,
            "attestations": {"human": True, "independent": True, "blinded": True},
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "submission_sha256": _sha(raw), "packet_sha256": _sha(Path(packet_path).read_bytes()),
            "manifest_sha256": _sha(Path(manifest_path).read_bytes()), "dataset_sha256": _sha(Path(dataset).read_bytes()),
            "labels": labels,
            "limitations": ["제출자의 사람 직접 작성·독립성 진술을 기록한다. 신원과 독립성을 소프트웨어가 검증하지 않았다.",
                            "가상 문구의 전달력 선호이며 실제 클릭률·전환율·광고주 만족의 정답이 아니다."]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--dataset", type=Path, default=PAIRS_PATH)
    export.add_argument("--out", type=Path, required=True)
    export.add_argument("--packet-id", required=True)
    export.add_argument("--split", choices=("dev", "holdout", "all"), default="holdout")
    collect = commands.add_parser("import")
    for name in ("submission", "packet", "manifest", "out"):
        collect.add_argument(f"--{name}", type=Path, required=True)
    collect.add_argument("--dataset", type=Path, default=PAIRS_PATH)
    collect.add_argument("--reviewer-id", required=True)
    for name in ("human", "independent", "blinded"):
        collect.add_argument(f"--attest-{name}", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "export":
        result = export_packet(args.dataset, args.out, packet_id=args.packet_id, split=args.split)
    else:
        result = import_labels(args.submission, args.packet, args.manifest, args.dataset,
                               reviewer_id=args.reviewer_id, attest_human=args.attest_human,
                               attest_independent=args.attest_independent, attest_blinded=args.attest_blinded)
        if args.out.resolve() in {args.dataset.resolve(), args.packet.resolve(), args.manifest.resolve(), args.submission.resolve()}:
            raise ValueError("import output must be a separate new file")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("xb") as handle:
            handle.write(_json_bytes(result))
        result = {"packet_id": result["packet_id"], "reviewer_id": args.reviewer_id, "labels": len(result["labels"]),
                  "output": str(args.out), "identity_verified": False, "independence_verified": False}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
