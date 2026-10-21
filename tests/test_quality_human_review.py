"""Controlled submission fixtures only: no human ratings are authored by these tests."""
from __future__ import annotations

import csv
import hashlib
import importlib
import io
import json
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evals/datasets/quality_pairs.jsonl"


def _module():
    assert importlib.util.find_spec("evals.quality_human_review") is not None, "blinded packet and provenance-checked import must exist"
    return importlib.import_module("evals.quality_human_review")


def _packet(tmp_path):
    q = _module()
    out = tmp_path / "packet"
    q.export_packet(DATASET, out, packet_id="controlled-test", seed="test-only-randomization")
    return q, out


def _submitted_csv(out, mutate=None):
    rows = list(csv.DictReader((out / "reviewer/labels.csv").open(encoding="utf-8")))
    for row in rows:
        row.update(preference="left", confidence="medium", reason="CONTROLLED TEST FIXTURE; not a human judgment")
    if mutate:
        mutate(rows)
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=["packet_id", "case_id", "preference", "confidence", "reason"])
    writer.writeheader()
    writer.writerows(rows)
    submitted = out / "controlled-submission.csv"
    submitted.write_text(buf.getvalue(), encoding="utf-8")
    return submitted


def _import(q, out, submitted, **kwargs):
    args = dict(reviewer_id="controlled-test-persona-not-human", attest_human=True,
                attest_independent=True, attest_blinded=True)
    args.update(kwargs)
    return q.import_labels(submitted, out / "reviewer/cases.json", out / "coordinator-manifest.json", DATASET, **args)


def test_export_blinds_labels_ids_scores_and_leaves_every_rating_empty(tmp_path):
    q, out = _packet(tmp_path)
    packet = json.loads((out / "reviewer/cases.json").read_text())
    manifest = json.loads((out / "coordinator-manifest.json").read_text())
    raw = (out / "reviewer/cases.json").read_text()
    assert len(packet["cases"]) == len(manifest["mapping"]) == 28
    for forbidden in ("QP-H", '"better"', '"reason_code"', '"notes"', '"split"', "model_winner", "signal_winner"):
        assert forbidden not in raw
    rows = list(csv.DictReader((out / "reviewer/labels.csv").open(encoding="utf-8")))
    assert len(rows) == 28 and all(not row["preference"] and not row["confidence"] and not row["reason"] for row in rows)
    assert {row["left_source"] for row in manifest["mapping"]} == {"a", "b"}
    assert manifest["dataset_sha256"] == hashlib.sha256(DATASET.read_bytes()).hexdigest()
    with zipfile.ZipFile(out / "reviewer-packet.zip") as z:
        assert set(z.namelist()) == {"cases.json", "pairs.md", "labels.csv", "instructions.md"}
        assert all("QP-H" not in z.read(name).decode() for name in z.namelist())


def test_import_maps_sides_without_mixing_author_or_model_labels(tmp_path):
    q, out = _packet(tmp_path)
    imported = _import(q, out, _submitted_csv(out))
    manifest = json.loads((out / "coordinator-manifest.json").read_text())
    by_case = {row["case_id"]: row for row in manifest["mapping"]}
    assert imported["label_origin"] == "human_submission"
    assert imported["identity_verified"] is False and imported["independence_verified"] is False
    assert imported["attestations"] == {"human": True, "independent": True, "blinded": True}
    assert len(imported["labels"]) == 28
    for row in imported["labels"]:
        assert row["preference"] == by_case[row["case_id"]]["left_source"]
        assert row["pair_id"] == by_case[row["case_id"]]["pair_id"]
        assert not {"expected", "better", "author_label", "model_winner"} & set(row)
    assert imported["submission_sha256"]


@pytest.mark.parametrize("mutation, message", [
    (lambda rows: rows.pop(), "complete packet"),
    (lambda rows: rows.append(dict(rows[0])), "duplicate"),
    (lambda rows: rows[0].update(case_id="unseen-case"), "unknown case"),
    (lambda rows: rows[0].update(packet_id="another-packet"), "packet id"),
    (lambda rows: rows[0].update(preference="A"), "preference"),
    (lambda rows: rows[0].update(preference=""), "preference"),
    (lambda rows: rows[0].update(confidence="0.99"), "confidence"),
    (lambda rows: rows[0].update(reason=""), "reason"),
])
def test_invalid_partial_or_duplicate_submission_is_rejected(tmp_path, mutation, message):
    q, out = _packet(tmp_path)
    with pytest.raises(ValueError, match=message):
        _import(q, out, _submitted_csv(out, mutation))


@pytest.mark.parametrize("attestation", ["attest_human", "attest_independent", "attest_blinded"])
def test_no_identity_or_independence_is_inferred_from_a_csv(tmp_path, attestation):
    q, out = _packet(tmp_path)
    with pytest.raises(ValueError, match="explicit attestations"):
        _import(q, out, _submitted_csv(out), **{attestation: False})


def test_modified_packet_and_original_case_mismatch_are_rejected(tmp_path):
    q, out = _packet(tmp_path)
    submitted = _submitted_csv(out)
    path = out / "reviewer/cases.json"
    packet = json.loads(path.read_text())
    packet["cases"][0]["left"]["headline"] = "changed headline"
    path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="packet hash"):
        _import(q, out, submitted)
    # Re-signing the local packet alone cannot hide a changed source copy.
    manifest_path = out / "coordinator-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["packet_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="source case"):
        _import(q, out, submitted)


def test_export_never_overwrites_a_packet_or_submissions(tmp_path):
    q, out = _packet(tmp_path)
    original = (out / "reviewer/labels.csv").read_bytes()
    with pytest.raises(FileExistsError):
        q.export_packet(DATASET, out, packet_id="controlled-test", seed="new-seed")
    assert (out / "reviewer/labels.csv").read_bytes() == original


def test_changed_reading_material_cannot_be_imported_as_the_original_packet(tmp_path):
    q, out = _packet(tmp_path)
    submitted = _submitted_csv(out)
    with (out / "reviewer/pairs.md").open("a", encoding="utf-8") as handle:
        handle.write("\nChanged instructions or copy text\n")
    with pytest.raises(ValueError, match="review material hash"):
        _import(q, out, submitted)


def test_source_dataset_drift_is_rejected_even_with_valid_packet_and_csv(tmp_path):
    q, out = _packet(tmp_path)
    drifted = tmp_path / "changed-dataset.jsonl"
    drifted.write_bytes(DATASET.read_bytes() + b"\n# drift\n")
    with pytest.raises(ValueError, match="dataset hash"):
        q.import_labels(_submitted_csv(out), out / "reviewer/cases.json", out / "coordinator-manifest.json", drifted,
                        reviewer_id="controlled-test-persona-not-human", attest_human=True,
                        attest_independent=True, attest_blinded=True)
