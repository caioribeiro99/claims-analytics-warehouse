"""The synthetic generator: deterministic, pinned, and covering every case the model handles."""

import json
from pathlib import Path

import pytest

from claims_warehouse import synthetic

MANIFEST = Path(__file__).resolve().parent.parent / "data" / "synthetic-manifest.json"


def test_output_matches_the_pinned_manifest(synthetic_build):
    pinned = json.loads(MANIFEST.read_text())
    assert synthetic.manifest(synthetic_build.source, synthetic.DEFAULT_SEED) == pinned


def test_regenerating_with_the_same_seed_gives_identical_bytes(tmp_path, synthetic_build):
    synthetic.generate(tmp_path / "again")
    assert synthetic.file_hashes(tmp_path / "again") == synthetic.file_hashes(
        synthetic_build.source
    )


def test_a_different_seed_gives_different_data(tmp_path, synthetic_build):
    synthetic.generate(tmp_path / "other", seed=7)
    ours = synthetic.file_hashes(synthetic_build.source)
    theirs = synthetic.file_hashes(tmp_path / "other")
    assert ours["claims/claims-2025-01.json"] != theirs["claims/claims-2025-01.json"]


def test_every_edge_case_the_model_handles_is_present(synthetic_build):
    expected = synthetic_build.expected
    assert set(expected["lookup_status"]) == {
        "no_claim",
        "resolved",
        "claim_not_found",
        "claim_out_of_network",
        "claim_ambiguous_id",
        "claim_rejected",
    }
    assert set(expected["reversal_status"]) == {
        "applied",
        "claim_not_found",
        "claim_out_of_network",
        "claim_ambiguous_id",
        "claim_rejected",
    }
    claims = expected["claims"]
    assert set(claims["reference_cost_status"]) == {
        "matched",
        "before_first_effective_date",
        "no_cost_history",
    }
    for case in (
        "reversed",
        "unattributed",
        "attributed_unknown_terms",
        "zero_service_fee",
        "negative_retained_fee",
        "ambiguous_claim_ids",
    ):
        assert claims[case] > 0, case

    change_points = expected["reference_costs"]["change_points"]
    assert any(point["was_restated"] for point in change_points)
    assert any(
        point["effective_date"] > expected["window"]["end_exclusive"] for point in change_points
    )

    claim_rejects = expected["rejects"]["claims"].values()
    reasons = {
        reason for stream in expected["rejects"].values() for rs in stream.values() for reason in rs
    }
    assert {
        "out_of_network",
        "ambiguous_duplicate_id",
        "bad_gross_amount",
        "missing_service_fee",
    } <= reasons
    assert any(len(record_reasons) > 1 for record_reasons in claim_rejects)
    assert expected["injected"]["reversals"]["reused_reversal_id_pair"] > 0
    assert expected["injected"]["reversals"]["second_reversal_same_claim"] > 0
    assert expected["injected"]["reversals"]["dated_before_claim"] > 0
    assert expected["injected"]["lookups"]["looked_up_after_claim"] > 0
    assert expected["injected"]["claims"]["exact_redelivery"] > 0
    assert expected["injected"]["claims"]["broken_copy_out_of_network"] > 0
    assert "duplicate_redelivery" in reasons


def test_the_generator_refuses_to_overwrite_a_directory_it_did_not_create(tmp_path):
    (tmp_path / "keep.txt").write_text("not generated")
    with pytest.raises(SystemExit):
        synthetic.generate(tmp_path)
    assert (tmp_path / "keep.txt").exists()
