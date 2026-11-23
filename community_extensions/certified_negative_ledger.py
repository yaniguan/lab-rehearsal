"""Idea #11: 4-Way Causal Attribution & Proof-Carrying Negative Result Ledger.

Inspired by the Sakana AI Scientist-v2 negative-result memory debate and the A-Lab XRD
precursor-overlap controversy. Raw post-run readouts frequently conflate robotic/control
execution failures (clogged tips, missed incubation windows, failed positive controls) or
ambiguous analytical spectra with genuine scientific inactivity, poisoning downstream
Bayesian Optimization and RLVR memory with false negatives and false positives.

This module classifies every run into one of four mutually exclusive epistemic states:
  - EXECUTION_ARTIFACT
  - AMBIGUOUS_CHARACTERIZATION
  - CERTIFIED_POSITIVE
  - CERTIFIED_NEGATIVE
and writes tamper-evident hash-chained SHA-256 receipts strictly for certified results.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

GENESIS_PREV_HASH = "0" * 64


class AttributionOutcome(str, Enum):
    """4-way epistemic attribution mirroring LabRehearsalLean.AttributionOutcome."""

    EXECUTION_ARTIFACT = "EXECUTION_ARTIFACT"
    AMBIGUOUS_CHARACTERIZATION = "AMBIGUOUS_CHARACTERIZATION"
    CERTIFIED_POSITIVE = "CERTIFIED_POSITIVE"
    CERTIFIED_NEGATIVE = "CERTIFIED_NEGATIVE"


@dataclass(frozen=True)
class PostRunTelemetry:
    """Post-run control, robotics, and analytical identifiability telemetry."""

    run_id: str
    candidate_id: str
    pre_gate_certified: bool
    acoustic_normal: bool
    schedule_windows_respected: bool
    tactile_seated_cleanly: bool
    pos_control_passed: bool
    neg_control_blank_clean: bool
    spectral_residual_permille: int
    max_residual_permille: int
    precursor_overlap_permille: int
    max_overlap_permille: int
    signal_above_activity_thresh: bool


@dataclass(frozen=True)
class LedgerReceipt:
    """Hash-chained SHA-256 proof-carrying scientific memory receipt."""

    index: int
    run_id: str
    candidate_id: str
    outcome: str
    telemetry_sha256: str
    prev_receipt_sha256: str
    receipt_sha256: str


def execution_clean(t: PostRunTelemetry) -> bool:
    """Exact mirror of LabRehearsalLean.executionClean."""
    return (
        t.pre_gate_certified
        and t.acoustic_normal
        and t.schedule_windows_respected
        and t.tactile_seated_cleanly
        and t.pos_control_passed
        and t.neg_control_blank_clean
    )


def characterization_identifiable(t: PostRunTelemetry) -> bool:
    """Exact mirror of LabRehearsalLean.characterizationIdentifiable."""
    return (
        t.spectral_residual_permille <= t.max_residual_permille
        and t.precursor_overlap_permille <= t.max_overlap_permille
    )


def attribute_post_run_telemetry(t: PostRunTelemetry) -> AttributionOutcome:
    """Exact 4-way causal attribution mirroring LabRehearsalLean.attributeAssayOutcome."""
    if not execution_clean(t):
        return AttributionOutcome.EXECUTION_ARTIFACT
    if not characterization_identifiable(t):
        return AttributionOutcome.AMBIGUOUS_CHARACTERIZATION
    if t.signal_above_activity_thresh:
        return AttributionOutcome.CERTIFIED_POSITIVE
    return AttributionOutcome.CERTIFIED_NEGATIVE


def admit_to_scientific_memory(outcome: AttributionOutcome) -> bool:
    """Only CERTIFIED_POSITIVE and CERTIFIED_NEGATIVE may enter scientific memory."""
    return outcome in (
        AttributionOutcome.CERTIFIED_POSITIVE,
        AttributionOutcome.CERTIFIED_NEGATIVE,
    )


def _sha256_canonical(obj: dict[str, Any]) -> str:
    encoded = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class RawAppendLedger:
    """Baseline unguarded ledger appending every raw signal readout directly into memory."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, str]] = []

    def record(self, telemetry: PostRunTelemetry) -> str:
        label = (
            "POSITIVE" if telemetry.signal_above_activity_thresh else "NEGATIVE"
        )
        self.entries.append((telemetry.run_id, telemetry.candidate_id, label))
        return label


class CertifiedNegativeLedger:
    """Append-only hash-chained SHA-256 ledger admitting strictly certified results."""

    def __init__(self) -> None:
        self.receipts: list[LedgerReceipt] = []
        self.retry_queue: list[str] = []
        self.orthogonal_assay_queue: list[str] = []

    def process_telemetry(
        self,
        telemetry: PostRunTelemetry,
    ) -> tuple[AttributionOutcome, LedgerReceipt | None]:
        outcome = attribute_post_run_telemetry(telemetry)
        if outcome == AttributionOutcome.EXECUTION_ARTIFACT:
            self.retry_queue.append(telemetry.run_id)
            return outcome, None
        if outcome == AttributionOutcome.AMBIGUOUS_CHARACTERIZATION:
            self.orthogonal_assay_queue.append(telemetry.run_id)
            return outcome, None

        tel_hash = _sha256_canonical(asdict(telemetry))
        prev_hash = (
            self.receipts[-1].receipt_sha256 if self.receipts else GENESIS_PREV_HASH
        )
        idx = len(self.receipts)
        body = {
            "index": idx,
            "run_id": telemetry.run_id,
            "candidate_id": telemetry.candidate_id,
            "outcome": outcome.value,
            "telemetry_sha256": tel_hash,
            "prev_receipt_sha256": prev_hash,
        }
        receipt = LedgerReceipt(
            index=idx,
            run_id=telemetry.run_id,
            candidate_id=telemetry.candidate_id,
            outcome=outcome.value,
            telemetry_sha256=tel_hash,
            prev_receipt_sha256=prev_hash,
            receipt_sha256=_sha256_canonical(body),
        )
        self.receipts.append(receipt)
        return outcome, receipt

    def verify_chain_integrity(self) -> bool:
        """Verify SHA-256 hash links and confirm only certified outcomes exist in the ledger."""
        expected_prev = GENESIS_PREV_HASH
        for expected_idx, rec in enumerate(self.receipts):
            if rec.index != expected_idx or rec.prev_receipt_sha256 != expected_prev:
                return False
            if rec.outcome not in (
                AttributionOutcome.CERTIFIED_POSITIVE.value,
                AttributionOutcome.CERTIFIED_NEGATIVE.value,
            ):
                return False
            body = {
                "index": rec.index,
                "run_id": rec.run_id,
                "candidate_id": rec.candidate_id,
                "outcome": rec.outcome,
                "telemetry_sha256": rec.telemetry_sha256,
                "prev_receipt_sha256": rec.prev_receipt_sha256,
            }
            if _sha256_canonical(body) != rec.receipt_sha256:
                return False
            expected_prev = rec.receipt_sha256
        return True


def _generate_120_runs(seed: int = 2026) -> list[tuple[PostRunTelemetry, bool]]:
    """Generate 120 wet-lab runs paired with ground-truth scientific activity (True/False)."""
    rng = random.Random(seed)
    dataset: list[tuple[PostRunTelemetry, bool]] = []

    # 1. 42 clean true negatives (genuinely inactive compounds, clean robotics & spectra)
    for i in range(42):
        dataset.append(
            (
                PostRunTelemetry(
                    run_id=f"RUN-TN-{i + 1:03d}",
                    candidate_id=f"MOL-TN-{i + 1:03d}",
                    pre_gate_certified=True,
                    acoustic_normal=True,
                    schedule_windows_respected=True,
                    tactile_seated_cleanly=True,
                    pos_control_passed=True,
                    neg_control_blank_clean=True,
                    spectral_residual_permille=rng.randint(20, 85),
                    max_residual_permille=100,
                    precursor_overlap_permille=rng.randint(10, 90),
                    max_overlap_permille=120,
                    signal_above_activity_thresh=False,
                ),
                False,
            )
        )

    # 2. 28 clean true positives (genuinely active hits, clean robotics & spectra)
    for i in range(28):
        dataset.append(
            (
                PostRunTelemetry(
                    run_id=f"RUN-TP-{i + 1:03d}",
                    candidate_id=f"MOL-TP-{i + 1:03d}",
                    pre_gate_certified=True,
                    acoustic_normal=True,
                    schedule_windows_respected=True,
                    tactile_seated_cleanly=True,
                    pos_control_passed=True,
                    neg_control_blank_clean=True,
                    spectral_residual_permille=rng.randint(25, 90),
                    max_residual_permille=100,
                    precursor_overlap_permille=rng.randint(15, 95),
                    max_overlap_permille=120,
                    signal_above_activity_thresh=True,
                ),
                True,
            )
        )

    # 3. 26 execution-corrupted runs on TRUE ACTIVE compounds (e.g. air bubble, missed
    #    incubation window, tactile mis-seat, or dead positive control makes active compound
    #    read below threshold -> classic false-negative memory poisoning!)
    for i in range(26):
        fault_mode = i % 5
        dataset.append(
            (
                PostRunTelemetry(
                    run_id=f"RUN-FAULT-FN-{i + 1:03d}",
                    candidate_id=f"MOL-HIDDEN-HIT-{i + 1:03d}",
                    pre_gate_certified=True,
                    acoustic_normal=(fault_mode != 0),
                    schedule_windows_respected=(fault_mode != 1),
                    tactile_seated_cleanly=(fault_mode != 2),
                    pos_control_passed=(fault_mode != 3),
                    neg_control_blank_clean=(fault_mode != 4),
                    spectral_residual_permille=50,
                    max_residual_permille=100,
                    precursor_overlap_permille=40,
                    max_overlap_permille=120,
                    signal_above_activity_thresh=(fault_mode == 4),
                ),
                fault_mode != 4,
            )
        )

    # 4. 24 ambiguous characterization runs (A-Lab style unreacted precursor overlap or
    #    high Rietveld residual producing spurious apparent hits or unidentifiable nulls)
    for i in range(24):
        spurious_signal = i % 2 == 0
        dataset.append(
            (
                PostRunTelemetry(
                    run_id=f"RUN-AMBIG-{i + 1:03d}",
                    candidate_id=f"MOL-AMBIG-{i + 1:03d}",
                    pre_gate_certified=True,
                    acoustic_normal=True,
                    schedule_windows_respected=True,
                    tactile_seated_cleanly=True,
                    pos_control_passed=True,
                    neg_control_blank_clean=True,
                    spectral_residual_permille=165 if i % 3 == 0 else 75,
                    max_residual_permille=100,
                    precursor_overlap_permille=310 if i % 3 != 0 else 80,
                    max_overlap_permille=120,
                    signal_above_activity_thresh=spurious_signal,
                ),
                not spurious_signal,
            )
        )

    rng.shuffle(dataset)
    assert len(dataset) == 120
    return dataset


def run_benchmark(num_runs: int = 120, seed: int = 2026) -> dict[str, Any]:
    """Compare raw_append_ledger vs certified_negative_ledger across 120 noisy/clean runs."""
    dataset = _generate_120_runs(seed=seed)[:num_runs]
    truth_by_run = {tel.run_id: truth for tel, truth in dataset}

    raw_ledger = RawAppendLedger()
    cert_ledger = CertifiedNegativeLedger()

    outcome_counts: dict[str, int] = {o.value: 0 for o in AttributionOutcome}
    for tel, _ in dataset:
        raw_ledger.record(tel)
        out, _ = cert_ledger.process_telemetry(tel)
        outcome_counts[out.value] += 1

    # Compute memory poisoning and false-negative contamination on raw_append_ledger
    raw_poisoned = 0
    raw_false_neg = 0
    raw_neg_total = 0
    for run_id, _, label in raw_ledger.entries:
        truth = truth_by_run[run_id]
        claimed_pos = label == "POSITIVE"
        if claimed_pos != truth:
            raw_poisoned += 1
        if not claimed_pos:
            raw_neg_total += 1
            if truth is True:
                raw_false_neg += 1

    # Compute memory poisoning and false-negative contamination on certified_negative_ledger
    cert_poisoned = 0
    cert_false_neg = 0
    cert_neg_total = 0
    for rec in cert_ledger.receipts:
        truth = truth_by_run[rec.run_id]
        claimed_pos = rec.outcome == AttributionOutcome.CERTIFIED_POSITIVE.value
        if claimed_pos != truth:
            cert_poisoned += 1
        if not claimed_pos:
            cert_neg_total += 1
            if truth is True:
                cert_false_neg += 1

    return {
        "benchmark": "idea_11_certified_negative_ledger",
        "num_runs": len(dataset),
        "attribution_breakdown": outcome_counts,
        "raw_append_ledger": {
            "admitted_entries": len(raw_ledger.entries),
            "poisoned_entries": raw_poisoned,
            "memory_poisoning_rate": round(raw_poisoned / len(raw_ledger.entries), 4),
            "false_negative_entries": raw_false_neg,
            "false_negative_contamination_rate": round(raw_false_neg / raw_neg_total, 4),
        },
        "certified_negative_ledger": {
            "admitted_entries": len(cert_ledger.receipts),
            "routed_to_execution_retry": len(cert_ledger.retry_queue),
            "routed_to_orthogonal_assay": len(cert_ledger.orthogonal_assay_queue),
            "poisoned_entries": cert_poisoned,
            "memory_poisoning_rate": round(cert_poisoned / len(cert_ledger.receipts), 4),
            "false_negative_entries": cert_false_neg,
            "false_negative_contamination_rate": round(cert_false_neg / cert_neg_total, 4),
            "hash_chain_verified": cert_ledger.verify_chain_integrity(),
        },
    }


if __name__ == "__main__":
    import json as _json

    print(_json.dumps(run_benchmark(), indent=2))
