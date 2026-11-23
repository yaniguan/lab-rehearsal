"""Tests for lab-rehearsal community extensions (Ideas #10 and #11) and LabRehearsalLean parity."""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from community_extensions.calm_twin_calibrator import (  # noqa: E402
    CalmAdaptiveTwin,
    StaticNominalTwin,
    run_benchmark as run_calm_twin_benchmark,
)
from community_extensions.certified_negative_ledger import (  # noqa: E402
    AttributionOutcome,
    CertifiedNegativeLedger,
    LedgerReceipt,
    PostRunTelemetry,
    admit_to_scientific_memory,
    attribute_post_run_telemetry,
    run_benchmark as run_negative_ledger_benchmark,
)

CJK_RE = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")


def test_calm_adaptive_twin_learns_viscous_dead_volume_without_retraining() -> None:
    static_twin = StaticNominalTwin(nominal_dead_vol_ul=10.0, nominal_evap_coeff=0.08)
    calm_twin = CalmAdaptiveTwin(stale_days=14)

    # Viscous 50% glycerol with true 28.5 uL dead volume at 37 C
    for _ in range(5):
        calm_twin.update_from_post_run_readout(
            liquid_name="glycerol_50pct",
            initial_vol_ul=200.0,
            dispensed_total_ul=120.0,
            temp_c=37.0,
            open_duration_min=15.0,
            measured_recoverable_vol_ul=49.7,
            gravimetric_evap_loss_ul=1.8,
        )

    static_pred = static_twin.predict_recoverable_volume_ul(200.0, 120.0, 37.0, 15.0)
    calm_pred = calm_twin.predict_recoverable_volume_ul("glycerol_50pct", 200.0, 120.0, 37.0, 15.0)

    assert abs(static_pred - 49.7) > 15.0
    assert abs(calm_pred - 49.7) < 1.5


def test_rusty_sop_gate_blocks_stale_and_epoch_shifted_sops_until_refreshed() -> None:
    twin = CalmAdaptiveTwin(stale_days=14)
    rec = twin.register_sop("SOP-VISCOUS-01", "peg_400", validated_day=1, deck_epoch=2)
    assert rec.bottom_trace is not None
    initial_hash = rec.bottom_trace.trace_sha256

    # Fresh invocation on day 10, same deck_epoch=2 -> allowed immediately
    ok_dec = twin.check_rusty_sop_gate("SOP-VISCOUS-01", current_day=10, live_deck_epoch=2)
    assert ok_dec.is_rusty is False
    assert ok_dec.allowed_full_scale is True

    # Stale invocation on day 20 (> 14 days) without micro-rehearsal -> blocked
    stale_dec = twin.check_rusty_sop_gate("SOP-VISCOUS-01", current_day=20, live_deck_epoch=2)
    assert stale_dec.is_rusty is True
    assert stale_dec.allowed_full_scale is False
    assert stale_dec.rule_triggered == "rusty_sop_requires_micro_rehearsal"

    # Deck epoch change (epoch 2 -> 3) with micro-rehearsal -> refreshes Bottom Reasoning Trace
    refreshed = twin.check_rusty_sop_gate(
        "SOP-VISCOUS-01",
        current_day=20,
        live_deck_epoch=3,
        run_micro_rehearsal=True,
        micro_rehearsal_obs=(80.0, 20.0, 45.0, 10.0, 33.5, 1.5),
    )
    assert refreshed.is_rusty is True
    assert refreshed.allowed_full_scale is True
    assert refreshed.refreshed_trace is not None
    assert refreshed.refreshed_trace.deck_epoch == 3
    assert refreshed.refreshed_trace.trace_sha256 != initial_hash


def test_idea_10_benchmark_50_viscous_heated_runs() -> None:
    report = run_calm_twin_benchmark(num_runs=50, seed=2026)
    assert report["num_runs"] == 50
    assert report["rusty_sop_events"] >= 10
    static_m = report["static_nominal_twin"]
    calm_m = report["calm_adaptive_twin"]

    assert calm_m["volume_mae_ul"] < static_m["volume_mae_ul"] * 0.25
    assert calm_m["mae_reduction_pct"] > 75.0
    assert static_m["rusty_sop_fault_prevention_rate"] < 0.25
    assert calm_m["rusty_sop_fault_prevention_rate"] == 1.0


def test_four_way_causal_attribution_and_tamper_evident_hash_chain() -> None:
    base_kwargs = {
        "run_id": "R1",
        "candidate_id": "M1",
        "pre_gate_certified": True,
        "acoustic_normal": True,
        "schedule_windows_respected": True,
        "tactile_seated_cleanly": True,
        "pos_control_passed": True,
        "neg_control_blank_clean": True,
        "spectral_residual_permille": 45,
        "max_residual_permille": 100,
        "precursor_overlap_permille": 30,
        "max_overlap_permille": 120,
        "signal_above_activity_thresh": False,
    }

    ledger = CertifiedNegativeLedger()

    # 1. Execution artifact (acoustic fault) -> NOT admitted to ledger
    t_fault = PostRunTelemetry(**dict(base_kwargs, run_id="R-ART", acoustic_normal=False))  # type: ignore[arg-type]
    out1, rec1 = ledger.process_telemetry(t_fault)
    assert out1 == AttributionOutcome.EXECUTION_ARTIFACT
    assert admit_to_scientific_memory(out1) is False
    assert rec1 is None
    assert ledger.retry_queue == ["R-ART"]

    # 2. Ambiguous characterization (precursor overlap > max) -> NOT admitted to ledger
    t_amb = PostRunTelemetry(
        **dict(base_kwargs, run_id="R-AMB", precursor_overlap_permille=250, signal_above_activity_thresh=True)  # type: ignore[arg-type]
    )
    out2, rec2 = ledger.process_telemetry(t_amb)
    assert out2 == AttributionOutcome.AMBIGUOUS_CHARACTERIZATION
    assert admit_to_scientific_memory(out2) is False
    assert rec2 is None
    assert ledger.orthogonal_assay_queue == ["R-AMB"]

    # 3. Certified negative & certified positive -> admitted with linked SHA-256 receipts
    t_neg = PostRunTelemetry(**dict(base_kwargs, run_id="R-NEG"))  # type: ignore[arg-type]
    t_pos = PostRunTelemetry(
        **dict(base_kwargs, run_id="R-POS", signal_above_activity_thresh=True)  # type: ignore[arg-type]
    )
    out3, rec3 = ledger.process_telemetry(t_neg)
    out4, rec4 = ledger.process_telemetry(t_pos)
    assert out3 == AttributionOutcome.CERTIFIED_NEGATIVE
    assert out4 == AttributionOutcome.CERTIFIED_POSITIVE
    assert rec3 is not None and rec4 is not None
    assert rec4.prev_receipt_sha256 == rec3.receipt_sha256
    assert ledger.verify_chain_integrity() is True

    # Tampering with an admitted outcome breaks SHA-256 chain verification
    ledger.receipts[0] = LedgerReceipt(
        index=rec3.index,
        run_id=rec3.run_id,
        candidate_id=rec3.candidate_id,
        outcome=AttributionOutcome.CERTIFIED_POSITIVE.value,
        telemetry_sha256=rec3.telemetry_sha256,
        prev_receipt_sha256=rec3.prev_receipt_sha256,
        receipt_sha256=rec3.receipt_sha256,
    )
    assert ledger.verify_chain_integrity() is False


def test_idea_11_benchmark_120_runs_zero_memory_poisoning() -> None:
    report = run_negative_ledger_benchmark(num_runs=120, seed=2026)
    assert report["num_runs"] == 120
    raw = report["raw_append_ledger"]
    cert = report["certified_negative_ledger"]

    assert raw["memory_poisoning_rate"] > 0.35
    assert raw["false_negative_contamination_rate"] > 0.30

    assert cert["admitted_entries"] == 70
    assert cert["routed_to_execution_retry"] == 26
    assert cert["routed_to_orthogonal_assay"] == 24
    assert cert["memory_poisoning_rate"] == 0.0
    assert cert["false_negative_contamination_rate"] == 0.0
    assert cert["hash_chain_verified"] is True


def test_pure_english_and_lean_file_integrity() -> None:
    lean_file = REPO_ROOT / "lean" / "LabRehearsalLean.lean"
    assert lean_file.exists()
    lean_src = lean_file.read_text(encoding="utf-8")
    assert "sorry" not in re.findall(r"\bsorry\b", lean_src)
    assert "axiom" not in re.findall(r"\baxiom\b", lean_src)
    assert ".sealWell" in lean_src
    assert ".thawWell" in lean_src

    checked_paths = [
        lean_file,
        REPO_ROOT / "lean" / "README.md",
        REPO_ROOT / "lean" / "lakefile.toml",
        REPO_ROOT / "community_extensions" / "__init__.py",
        REPO_ROOT / "community_extensions" / "calm_twin_calibrator.py",
        REPO_ROOT / "community_extensions" / "certified_negative_ledger.py",
        Path(__file__).resolve(),
    ]
    for path in checked_paths:
        text = path.read_text(encoding="utf-8")
        assert CJK_RE.search(text) is None, f"CJK character found in {path}"
