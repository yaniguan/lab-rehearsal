"""Auto-Lab 15-repository wire node (`autolab-wire/v1`) for `lab-rehearsal` (Layer L4).

Executes stateful 96-well digital-twin liquid-handling & thermal simulation, default-deny
safety verification (`no_tip_reuse`, `no_overfill`, `enough_liquid`, `volume_in_range`,
`tip_present`, `thawed_before_pipetting`, `unsealed_before_pipetting`,
`high_temp_requires_seal`, `all_tips_dropped`), sub-linear structural episodic recall, and
`lab-attrib` `EXECUTION_ARTIFACT` retry reconciliation before compiling approved multi-step
trajectories into a declarative `stepbot` Opentrons API `2.16` protocol dictionary.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping

REPO_ID = "lab-rehearsal"
LAYER = "L4"
WIRE_SCHEMA_VERSION = "autolab-wire/v1"

REPO_DIR = Path(__file__).resolve().parent
SRC_DIR = REPO_DIR / "src"
for _candidate in (str(SRC_DIR), str(REPO_DIR)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from lab_rehearsal.adapters import (  # noqa: E402
    _parse_pipette_model_bounds,
    from_lablean,
    from_stepbot,
    from_twinstep,
    to_lablean,
    to_stepbot,
)
from lab_rehearsal.core import (  # noqa: E402
    DEFAULT_DECK_SPEC,
    DEFAULT_EVAP_COEFFS,
    VALID_ATTRIBUTION_OUTCOMES,
    DeckSpec,
    RehearsalSession,
    protocol_hash,
)

_TRACKED_DEMO_LEDGER = (REPO_DIR / "demo" / "memory" / "runs.jsonl").resolve()


def _build_default_transfer_steps(volume_ul: float) -> tuple[list[dict[str, Any]], float]:
    """Build a single-well transfer sequence capping nominal well volume at `200.0 uL`.

    Does NOT silently clamp sub-minimum volumes upward so out-of-range P300 requests
    (e.g. `10.0 uL < 20.0 uL`) are honestly caught by `verify()` unless a P20 pipette
    is configured.
    """
    rehearsal_vol = min(float(volume_ul), 200.0)
    steps: list[dict[str, Any]] = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": rehearsal_vol},
        {"op": "dispense", "loc": "plate:A1", "vol": rehearsal_vol},
        {"op": "drop_tip"},
    ]
    return steps, rehearsal_vol


def _normalize_rehearsal_steps(raw_steps: Any) -> tuple[Any, DeckSpec | None]:
    """Adapt optional macro-step inputs (`twinstep`, `LabLean`, `stepbot`) into micro-steps."""
    if (
        isinstance(raw_steps, list)
        and raw_steps
        and isinstance(raw_steps[0], Mapping)
        and "type" in raw_steps[0]
        and "op" not in raw_steps[0]
    ):
        return from_twinstep(raw_steps, tip_policy="smart"), None
    if (
        isinstance(raw_steps, Mapping)
        and isinstance(raw_steps.get("steps"), list)
        and raw_steps["steps"]
        and isinstance(raw_steps["steps"][0], Mapping)
        and "kind" in raw_steps["steps"][0]
    ):
        return from_lablean(raw_steps), None
    if (
        isinstance(raw_steps, Mapping)
        and isinstance(raw_steps.get("steps"), list)
        and ("pipettes" in raw_steps or "deck" in raw_steps)
    ):
        micro, sb_spec = from_stepbot(raw_steps)
        return micro, sb_spec
    return raw_steps, None


def _resolve_wire_deck_spec(
    packet: Mapping[str, Any],
    adapter_spec: DeckSpec | None,
) -> tuple[DeckSpec, str]:
    base = adapter_spec if adapter_spec is not None else DEFAULT_DECK_SPEC
    pipette_model = str(packet.get("pipette_model", "")).strip()
    if pipette_model:
        p_min, p_max, p_ch = _parse_pipette_model_bounds(pipette_model)
    else:
        p_min, p_max, p_ch = base.pip_min, base.pip_max, base.channels
        pipette_model = "p20_single_gen2" if p_max <= 20.0 else "p300_single_gen2"

    raw_overrides = packet.get("deck_spec")
    ov = raw_overrides if isinstance(raw_overrides, Mapping) else {}

    start_map = dict(base.start)
    if isinstance(ov.get("start"), Mapping):
        start_map = {
            str(k): (float(v[0]), str(v[1]))
            for k, v in ov["start"].items()
            if isinstance(v, (list, tuple)) and len(v) == 2
        }

    evap_coeffs = dict(base.evap_coeffs)
    if isinstance(ov.get("evap_coeffs"), Mapping):
        evap_coeffs.update({str(k): float(v) for k, v in ov["evap_coeffs"].items()})

    frozen_locs = set(base.frozen_locs)
    if isinstance(ov.get("frozen_locs"), (list, set, tuple)):
        frozen_locs.update(str(x) for x in ov["frozen_locs"])

    sealed_locs = set(base.sealed_locs)
    if isinstance(ov.get("sealed_locs"), (list, set, tuple)):
        sealed_locs.update(str(x) for x in ov["sealed_locs"])

    spec = DeckSpec(
        well_max=float(ov.get("well_max", base.well_max)),
        res_max=float(ov.get("res_max", base.res_max)),
        trash_max=float(ov.get("trash_max", base.trash_max)),
        pip_min=float(ov.get("pip_min", p_min)),
        pip_max=float(ov.get("pip_max", p_max)),
        tip_rack=int(ov.get("tip_rack", base.tip_rack)),
        plate_dead_vol=float(ov.get("plate_dead_vol", base.plate_dead_vol)),
        res_dead_vol=float(ov.get("res_dead_vol", base.res_dead_vol)),
        start=start_map,
        diluents=base.diluents,
        waste_locs=base.waste_locs,
        channels=int(ov.get("channels", p_ch)),
        pipettes=base.pipettes,
        frozen_locs=frozenset(frozen_locs),
        sealed_locs=frozenset(sealed_locs),
        evap_coeffs=evap_coeffs,
        strict_tenths=bool(packet.get("strict_tenths", ov.get("strict_tenths", base.strict_tenths))),
    )
    return spec, pipette_model


def _mint_wire_bottom_trace(
    protocol_name: str,
    spec: DeckSpec,
    deck_epoch: int,
    active_invariants: list[str],
) -> dict[str, Any]:
    merged_evap = {**DEFAULT_EVAP_COEFFS, **{str(k): float(v) for k, v in spec.evap_coeffs.items()}}
    liq_cals: dict[str, dict[str, float]] = {}
    primary_liq = "buffer"
    for loc, (_, liq_name) in sorted(spec.start.items()):
        if not liq_name:
            continue
        if primary_liq == "buffer" and liq_name != "buffer":
            primary_liq = liq_name
        dead_v = spec.res_dead_vol if loc.startswith("res:") else spec.plate_dead_vol
        liq_cals[str(liq_name)] = {
            "dead_vol_ul": round(float(dead_v), 4),
            "evap_coeff": round(float(merged_evap.get(str(liq_name), 0.0)), 5),
        }

    core_payload = {
        "sop_id": protocol_name,
        "liquid_name": primary_liq,
        "deck_epoch": int(deck_epoch),
        "calibrated_dead_vol_ul": round(float(spec.plate_dead_vol), 4),
        "calibrated_evap_coeff": round(float(merged_evap.get(primary_liq, 0.0)), 5),
        "liquid_calibrations": liq_cals,
        "active_invariants": list(active_invariants),
    }
    digest = hashlib.sha256(
        json.dumps(core_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {**core_payload, "trace_sha256": digest}


def _reconcile_incoming_attribution(
    session: RehearsalSession,
    packet: Mapping[str, Any],
    candidate_hash: str,
) -> bool:
    attr = packet.get("attribution")
    if not isinstance(attr, Mapping):
        return False
    outcome = attr.get("outcome")
    if not isinstance(outcome, str) or outcome not in VALID_ATTRIBUTION_OUTCOMES:
        return False

    receipt_sha = attr.get("ledger_entry_sha256") or attr.get("receipt_sha256")
    if not isinstance(receipt_sha, str) or len(receipt_sha) != 64:
        receipt_sha = None

    target_ref: int | str | None = attr.get("run_uuid") or attr.get("rehearsal_run_id") or attr.get("run_id")
    if target_ref is not None:
        if session.memory.reconcile_attribution(target_ref, outcome, receipt_sha256=receipt_sha):
            return True

    # Fallback: reconcile most recent rehearsal record matching `candidate_hash`
    session.memory._ensure_loaded()
    prior = session.memory._by_hash.get(candidate_hash, [])
    if prior:
        last_run_id = int(prior[-1]["run"])
        return session.memory.reconcile_attribution(
            last_run_id, outcome, receipt_sha256=receipt_sha
        )
    return False


def process_packet(
    packet: dict[str, Any],
    *,
    memory_path: str | os.PathLike[str] | None = None,
    fast_deny_known_failures: bool | None = None,
    max_artifact_retries: int = 2,
) -> dict[str, Any]:
    """Run L4 96-well digital-twin rehearsal and verification on an `autolab-wire/v1` packet."""
    out: dict[str, Any] = copy.deepcopy(packet)
    out.setdefault("schema_version", WIRE_SCHEMA_VERSION)
    out.setdefault("halted", False)
    out.setdefault("halt_reason", None)
    out.setdefault("trace", [])
    out.setdefault("bottom_reasoning_traces", [])

    if bool(out["halted"]):
        return out

    parsed_spans = out.get("parsed_spans")
    spans_map = parsed_spans if isinstance(parsed_spans, Mapping) else {}
    volume_ul = float(spans_map.get("volume_ul", 100.0))

    protocol_name = str(
        out.get("protocol_name", "Auto-Lab L4 Rehearsed 96-Well Transfer")
    )
    raw_steps = out.get("rehearsal_steps")
    if raw_steps is None:
        steps_to_run, nominal_rehearsal_vol = _build_default_transfer_steps(volume_ul)
        adapter_spec = None
    else:
        steps_to_run, adapter_spec = _normalize_rehearsal_steps(raw_steps)
        nominal_rehearsal_vol = volume_ul

    active_spec, pipette_model = _resolve_wire_deck_spec(out, adapter_spec)

    raw_mem_path = memory_path if memory_path is not None else out.get("rehearsal_memory_path")
    resolved_mem_path: Path | None = None
    if raw_mem_path is not None:
        resolved_mem_path = Path(raw_mem_path)
        if resolved_mem_path.resolve() == _TRACKED_DEMO_LEDGER:
            raise ValueError("Refusing to mutate tracked demo/memory/runs.jsonl from autolab_wire")

    eff_fast_deny = (
        bool(out.get("fast_deny_known_failures", False))
        if fast_deny_known_failures is None
        else bool(fast_deny_known_failures)
    )
    eff_max_retries = int(out.get("max_artifact_retries", max_artifact_retries))

    live_deck_epoch = int(
        out.get(
            "deck_epoch",
            out.get("stepbot_execution", {}).get("deck_epoch", 1)
            if isinstance(out.get("stepbot_execution"), Mapping)
            else 1,
        )
    )

    tenths_grid_exact = False
    lablean_step_count = 0
    if isinstance(steps_to_run, list):
        try:
            ll_doc = to_lablean(steps_to_run, spec=active_spec)
            tenths_grid_exact = True
            lablean_step_count = len(ll_doc["steps"])
        except ValueError:
            tenths_grid_exact = False

    active_invariants = (
        ["checkRehearsal_preserves_capacity"]
        if tenths_grid_exact
        else ["python_float_rehearsal_unquantized"]
    )
    minted_bt = _mint_wire_bottom_trace(
        protocol_name, active_spec, live_deck_epoch, active_invariants
    )

    session = RehearsalSession(
        memory_path=resolved_mem_path,
        spec=active_spec,
        fast_deny_known_failures=eff_fast_deny,
        max_artifact_retries=eff_max_retries,
    )
    try:
        cand_hash = protocol_hash(steps_to_run, active_spec)
        attribution_reconciled = _reconcile_incoming_attribution(session, out, cand_hash)
        eval_res = session.evaluate_protocol(
            protocol_name,
            steps_to_run,
            deck_epoch=live_deck_epoch,
            bottom_trace=minted_bt,
        )
        similar_matches = session.memory.recall_similar(
            steps_to_run,
            active_spec,
            top_k=3,
            live_deck_epoch=live_deck_epoch,
        )
    finally:
        session.close()

    violations_list: list[dict[str, Any]] = [
        {
            "step": int(v.get("step", 0)),
            "rule": str(v.get("rule", "unknown_rule")),
            "message": str(v.get("message", str(v))),
        }
        for v in eval_res["violations"]
    ]
    approved = bool(eval_res["approved"]) and not violations_list

    protocol_yaml_dict: dict[str, Any] | None = None
    if not approved:
        out["halted"] = True
        out["halt_reason"] = "L4_REHEARSAL_BLOCK"
    else:
        protocol_yaml_dict = to_stepbot(
            steps_to_run,
            name=protocol_name,
            spec=active_spec,
            wire_format=True,
            pipette_model=pipette_model,
        )

    out["rehearsal"] = {
        "passed": not bool(out["halted"]),
        "violations": violations_list,
        "protocol_yaml_dict": protocol_yaml_dict,
        "protocol_hash": str(eval_res["hash"]),
        "skeleton_hash": str(eval_res["skeleton_hash"]),
        "episodic_recall": str(eval_res["recall"]),
        "tips_used": int(eval_res["end"].get("tips_used", 0)),
        "run_id": int(eval_res["run_id"]),
        "run_uuid": str(eval_res["run_uuid"]),
        "fast_denied": bool(eval_res.get("fast_denied", False)),
        "retry_count": int(eval_res.get("retry_count", 0)),
        "attribution_reconciled": attribution_reconciled,
        "tenths_grid_exact": tenths_grid_exact,
        "similar_runs": [
            {"run": m["run"], "jaccard": m["jaccard"], "passed": m["passed"]}
            for m in similar_matches
        ],
    }

    if out["halted"]:
        rules_triggered = ", ".join(v["rule"] for v in violations_list) or "unknown_rule"
        summary = (
            f"Blocked 96-well protocol rehearsal ({len(violations_list)} violation(s): "
            f"{rules_triggered})."
        )
        final_invariant = "L4_REHEARSAL_BLOCK"
    else:
        assert protocol_yaml_dict is not None
        sb_steps = protocol_yaml_dict["steps"]
        first_vol = next(
            (float(s["volume"]) for s in sb_steps if "volume" in s),
            nominal_rehearsal_vol,
        )
        summary = (
            f"Rehearsed 96-well protocol ({len(sb_steps)} compiled step(s), "
            f"0 violations, lead volume={first_vol:g} uL)."
        )
        final_invariant = (
            "checkRehearsal_preserves_capacity"
            if tenths_grid_exact
            else "python_float_rehearsal_unquantized"
        )

    out["trace"].append(
        {
            "repo": REPO_ID,
            "layer": LAYER,
            "status": "BLOCK" if out["halted"] else "PASS",
            "summary": summary,
        }
    )
    out["bottom_reasoning_traces"].append(
        {
            "repo": REPO_ID,
            "invariant": final_invariant,
            "deck_epoch": live_deck_epoch,
            "tenths_grid_exact": tenths_grid_exact,
            "lablean_steps": lablean_step_count,
            "skeleton_hash": str(eval_res["skeleton_hash"]),
            "trace_sha256": minted_bt["trace_sha256"],
            "first_principles": (
                "Every accepted 96-well pipetting trajectory conserves total deck liquid mass, "
                "enforces 300 uL well capacity and dead-volume reserves, models open-well thermal "
                "evaporation, blocks cross-reagent tip reuse, and drops all mounted tips before "
                "hardware compilation."
            ),
        }
    )
    return out


if __name__ == "__main__":
    demo_packet = process_packet({"parsed_spans": {"volume_ul": 120.0}})
    print(json.dumps(demo_packet, indent=2))
