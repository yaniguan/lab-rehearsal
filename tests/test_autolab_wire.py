"""Unit tests for the `lab-rehearsal` Auto-Lab wire node (`autolab_wire.py`)."""

from __future__ import annotations

import copy
from dataclasses import replace
import importlib.util
import io
import json
import math
from pathlib import Path
import re
import sys
from typing import Any

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import autolab_wire  # noqa: E402
from autolab_wire import LAYER, REPO_ID, process_packet  # noqa: E402
from lab_rehearsal import (  # noqa: E402
    DEFAULT_DECK_SPEC,
    from_stepbot,
    rehearse,
    validate_run_record_schema,
    verify,
)

CJK_RE = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")


def _load_sibling_stepbot_wire() -> Any | None:
    sb_dir = REPO_ROOT.parent / "stepbot"
    sb_wire_path = sb_dir / "autolab_wire.py"
    if not sb_wire_path.is_file():
        return None
    saved_ce = {
        k: v
        for k, v in sys.modules.items()
        if k == "community_extensions" or k.startswith("community_extensions.")
    }
    saved_path = list(sys.path)
    try:
        for k in saved_ce:
            sys.modules.pop(k, None)
        sys.path.insert(0, str(sb_dir))
        sys.path.insert(0, str(sb_dir / "src"))
        spec = importlib.util.spec_from_file_location("stepbot_autolab_wire_ext", sb_wire_path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k in list(sys.modules.keys()):
            if k == "community_extensions" or k.startswith("community_extensions."):
                sys.modules.pop(k, None)
        sys.modules.update(saved_ce)
        sys.path[:] = saved_path
        if str(sb_dir / "src") not in sys.path:
            sys.path.append(str(sb_dir / "src"))


def test_wire_metadata_and_cwd_independent_rehearsal(monkeypatch, tmp_path) -> None:
    assert REPO_ID == "lab-rehearsal"
    assert LAYER == "L4"

    tracked_ledger = REPO_ROOT / "demo" / "memory" / "runs.jsonl"
    before_bytes = tracked_ledger.read_bytes()

    monkeypatch.chdir(tmp_path)
    original = {"parsed_spans": {"volume_ul": 150.0}}
    res = process_packet(original)

    # Original packet must not be mutated
    assert "rehearsal" not in original
    assert tracked_ledger.read_bytes() == before_bytes

    assert res["schema_version"] == "autolab-wire/v1"
    assert res["halted"] is False
    assert res["halt_reason"] is None
    assert res["rehearsal"]["passed"] is True
    assert res["rehearsal"]["violations"] == []
    assert res["rehearsal"]["tips_used"] == 1
    assert res["rehearsal"]["tenths_grid_exact"] is True
    assert res["rehearsal"]["lean_verified"] is False
    assert res["rehearsal"]["requested_volume_ul"] == 150.0
    assert res["rehearsal"]["rehearsed_volume_ul"] == 150.0

    yaml_dict = res["rehearsal"]["protocol_yaml_dict"]
    assert yaml_dict is not None
    assert yaml_dict["metadata"]["apiLevel"] == "2.16"
    assert yaml_dict["deck"] == {
        "1": {"load_name": "opentrons_96_tiprack_300ul"},
        "2": {"load_name": "nest_12_reservoir_15ml"},
        "3": {"load_name": "corning_96_wellplate_360ul_flat"},
    }
    assert yaml_dict["pipettes"] == {
        "right": {"instrument_name": "p300_single_gen2", "tip_racks": ["1"]}
    }
    assert yaml_dict["steps"] == [
        {
            "action": "transfer",
            "pipette": "right",
            "source": "2:A1",
            "dest": "3:A1",
            "volume": 150.0,
        }
    ]

    assert res["trace"][-1]["repo"] == "lab-rehearsal"
    assert res["trace"][-1]["layer"] == "L4"
    assert res["trace"][-1]["status"] == "PASS"
    assert res["bottom_reasoning_traces"][-1]["invariant"] == "lean_model_compatible"


def test_issue_32_no_silent_volume_cap_and_non_finite_rejection() -> None:
    # 1. Rehearsable > 200 uL request (250 uL <= 300 uL well & P300 cap) compiles exact 250 uL
    res_250 = process_packet({"parsed_spans": {"volume_ul": 250.0}})
    assert res_250["halted"] is False
    assert res_250["rehearsal"]["passed"] is True
    assert res_250["rehearsal"]["requested_volume_ul"] == 250.0
    assert res_250["rehearsal"]["rehearsed_volume_ul"] == 250.0
    compiled_250 = res_250["rehearsal"]["protocol_yaml_dict"]["steps"]
    assert sum(float(s["volume"]) for s in compiled_250 if "volume" in s) == pytest.approx(250.0)

    # Multi-stroke request on P20 (50 uL = 16.7 + 16.7 + 16.6 uL into 300 uL well) sums to exact 50 uL
    res_p20_multi = process_packet(
        {"parsed_spans": {"volume_ul": 50.0}, "pipette_model": "p20_single_gen2"}
    )
    assert res_p20_multi["halted"] is False
    assert res_p20_multi["rehearsal"]["requested_volume_ul"] == 50.0
    assert res_p20_multi["rehearsal"]["rehearsed_volume_ul"] == 50.0
    rt_steps_50, rt_spec_50 = from_stepbot(res_p20_multi["rehearsal"]["protocol_yaml_dict"])
    _, end_50 = rehearse(rt_steps_50, spec=rt_spec_50)
    assert end_50["plate"]["plate:A1"] == pytest.approx(50.0)

    # 2. Every V in {250, 500, 5000, 1e9} is either approved with exact total V or halted with named rule
    for req_v in (250.0, 500.0, 5000.0, 1e9):
        pkt = process_packet({"parsed_spans": {"volume_ul": req_v}})
        assert pkt["rehearsal"]["requested_volume_ul"] == req_v
        if pkt["halted"]:
            assert pkt["halt_reason"] == "L4_REHEARSAL_BLOCK"
            assert pkt["rehearsal"]["passed"] is False
            assert pkt["rehearsal"]["protocol_yaml_dict"] is None
            rules = [v["rule"] for v in pkt["rehearsal"]["violations"]]
            assert "requested_volume_not_rehearsable" in rules
        else:
            rt_s, _ = from_stepbot(pkt["rehearsal"]["protocol_yaml_dict"])
            disp_total = sum(float(s["vol"]) for s in rt_s if s["op"] == "dispense")
            assert disp_total == pytest.approx(req_v)

    assert process_packet({"parsed_spans": {"volume_ul": 500.0}})["halted"] is True
    assert process_packet({"parsed_spans": {"volume_ul": 5000.0}})["halted"] is True
    assert process_packet({"parsed_spans": {"volume_ul": 1e9}})["halted"] is True

    # 3. Non-finite, non-positive, None, string, and bool inputs halt cleanly without exception
    for bad_v in (float("inf"), float("-inf"), float("nan"), "abc", None, True, False, 0, -15.0):
        bad_res = process_packet({"parsed_spans": {"volume_ul": bad_v}})
        assert bad_res["halted"] is True
        assert bad_res["halt_reason"] == "L4_REHEARSAL_BLOCK"
        assert bad_res["rehearsal"]["passed"] is False
        assert bad_res["rehearsal"]["protocol_yaml_dict"] is None
        bad_rules = [v["rule"] for v in bad_res["rehearsal"]["violations"]]
        assert "invalid_requested_volume" in bad_rules
        # Strict JSON serializability on all emitted wire blocks (no NaN/Infinity leaked)
        json.dumps(bad_res["rehearsal"], allow_nan=False)
        json.dumps(bad_res["trace"], allow_nan=False)
        json.dumps(bad_res["bottom_reasoning_traces"], allow_nan=False)


def test_dirty_tip_reuse_blocks_with_l4_rehearsal_block() -> None:
    dirty_tip_steps = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:2", "vol": 100.0},
        {"op": "dispense", "loc": "plate:A1", "vol": 100.0},
        {"op": "aspirate", "loc": "res:1", "vol": 100.0},
        {"op": "dispense", "loc": "plate:B1", "vol": 100.0},
        {"op": "drop_tip"},
    ]
    res = process_packet({"rehearsal_steps": dirty_tip_steps})

    assert res["halted"] is True
    assert res["halt_reason"] == "L4_REHEARSAL_BLOCK"
    assert res["rehearsal"]["passed"] is False
    assert res["rehearsal"]["protocol_yaml_dict"] is None
    rules = [v["rule"] for v in res["rehearsal"]["violations"]]
    assert "no_tip_reuse" in rules
    assert res["trace"][-1]["status"] == "BLOCK"

    # Must remain cleanly deepcopyable and JSON-serializable for downstream wire nodes
    copied = copy.deepcopy(res)
    serialized = json.dumps(copied)
    assert "L4_REHEARSAL_BLOCK" in serialized


def test_well_overfill_blocks_with_l4_rehearsal_block() -> None:
    overfill_steps = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 200.0},
        {"op": "dispense", "loc": "plate:A1", "vol": 200.0},
        {"op": "aspirate", "loc": "res:1", "vol": 200.0},
        {"op": "dispense", "loc": "plate:A1", "vol": 200.0},
        {"op": "drop_tip"},
    ]
    res = process_packet({"rehearsal_steps": overfill_steps})

    assert res["halted"] is True
    assert res["halt_reason"] == "L4_REHEARSAL_BLOCK"
    assert res["rehearsal"]["passed"] is False
    assert res["rehearsal"]["protocol_yaml_dict"] is None
    rules = [v["rule"] for v in res["rehearsal"]["violations"]]
    assert "no_overfill" in rules
    assert res["trace"][-1]["status"] == "BLOCK"


def test_halted_packet_passes_through_unchanged() -> None:
    incoming = {
        "schema_version": "autolab-wire/v1",
        "halted": True,
        "halt_reason": "L5_LABSHIFT_WINDOW_BLOCK",
        "trace": [{"repo": "labshift", "layer": "L5", "status": "BLOCK", "summary": "window"}],
        "bottom_reasoning_traces": [],
    }
    res = process_packet(incoming)

    assert res == incoming
    assert res is not incoming
    assert "rehearsal" not in res
    assert len(res["trace"]) == 1


def test_issue_27_sub_min_volume_and_multistep_wire_compilation() -> None:
    # 1. Sub-minimum 10.0 uL on default P300 blocks honestly instead of emitting unrehearsed 10 uL!
    pkt_low_p300 = process_packet({"parsed_spans": {"volume_ul": 10.0}})
    assert pkt_low_p300["halted"] is True
    assert pkt_low_p300["halt_reason"] == "L4_REHEARSAL_BLOCK"
    assert pkt_low_p300["rehearsal"]["protocol_yaml_dict"] is None
    assert pkt_low_p300["rehearsal"]["violations"][0]["rule"] == "volume_in_range"

    # Selecting P20 allows 10.0 uL cleanly and binds both tiprack and pipette to 20 uL hardware
    pkt_low_p20 = process_packet(
        {"parsed_spans": {"volume_ul": 10.0}, "pipette_model": "p20_single_gen2"}
    )
    assert pkt_low_p20["halted"] is False
    yaml_p20 = pkt_low_p20["rehearsal"]["protocol_yaml_dict"]
    assert yaml_p20["deck"]["1"]["load_name"] == "opentrons_96_tiprack_20ul"
    assert yaml_p20["pipettes"]["right"]["instrument_name"] == "p20_single_gen2"
    assert yaml_p20["steps"] == [
        {
            "action": "transfer",
            "pipette": "right",
            "source": "2:A1",
            "dest": "3:A1",
            "volume": 10.0,
        }
    ]

    # 2. Multi-step transfer + mix + seal + incubate trajectory is preserved in protocol_yaml_dict
    multi_steps = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 150.0},
        {"op": "dispense", "loc": "plate:B2", "vol": 150.0},
        {"op": "mix", "loc": "plate:B2", "vol": 100.0, "reps": 3},
        {"op": "drop_tip"},
        {"op": "seal", "loc": "plate:B2"},
        {"op": "incubate", "loc": "plate:B2", "temp_c": 85.0, "duration_s": 300.0},
    ]
    pkt_multi = process_packet({"rehearsal_steps": multi_steps})
    assert pkt_multi["halted"] is False
    yaml_multi = pkt_multi["rehearsal"]["protocol_yaml_dict"]
    assert yaml_multi is not None
    actions = [s["action"] for s in yaml_multi["steps"]]
    assert actions == ["transfer", "mix", "comment", "comment", "delay"]
    assert yaml_multi["steps"][0]["dest"] == "3:B2"
    assert yaml_multi["steps"][0]["volume"] == 150.0
    assert yaml_multi["steps"][1]["wells"] == "3:B2"
    assert yaml_multi["steps"][1]["volume"] == 100.0
    assert yaml_multi["steps"][1]["reps"] == 3
    assert yaml_multi["steps"][4] == {"action": "delay", "seconds": 300.0}

    # Round-trip back via from_stepbot preserves every operation including seal & incubate!
    rt_micro, _ = from_stepbot(yaml_multi)
    assert rt_micro == multi_steps

    # 3. Full 43-step 8-well good_serial_dilution.json preserves all 16 well transfers, 7 mixes, and 2 tips
    good_steps = json.loads(
        (REPO_ROOT / "demo" / "protocols" / "good_serial_dilution.json").read_text(encoding="utf-8")
    )
    pkt_good = process_packet({"rehearsal_steps": good_steps})
    assert pkt_good["halted"] is False
    assert pkt_good["rehearsal"]["tips_used"] == 2
    yaml_good = pkt_good["rehearsal"]["protocol_yaml_dict"]
    transfer_dest_count = sum(
        len(s["dest"]) if isinstance(s["dest"], list) else 1
        for s in yaml_good["steps"]
        if s["action"] == "transfer"
    )
    assert transfer_dest_count == 16
    assert sum(1 for s in yaml_good["steps"] if s["action"] == "mix") == 7
    assert "sample" in yaml_good["liquids"]
    assert yaml_good["liquids"]["sample"]["wells"] == "2:A2"


def test_issue_33_compiled_wire_roundtrip_parity_and_8ch_sample_columns(monkeypatch) -> None:
    # 1. 8-channel column move A1..H1 -> A2..H2 with 8 distinct samples never shares 1 tip across rows
    start_8 = {f"plate:{row}1": [100.0, f"sample_{row}"] for row in "ABCDEFGH"}
    steps_8 = [
        {"op": "pick_up_tip", "channels": 8},
        {"op": "aspirate", "loc": "plate:A1", "vol": 50.0, "channels": 8},
        {"op": "dispense", "loc": "plate:A2", "vol": 50.0, "channels": 8},
        {"op": "drop_tip", "channels": 8},
    ]
    res_8 = process_packet(
        {
            "rehearsal_steps": steps_8,
            "pipette_model": "p300_multi_gen2",
            "deck_spec": {"start": start_8, "plate_dead_vol": 5.0},
        }
    )
    assert res_8["halted"] is False, res_8["rehearsal"]["violations"]
    assert res_8["rehearsal"]["tips_used"] == 8
    yaml_8 = res_8["rehearsal"]["protocol_yaml_dict"]
    assert yaml_8 is not None
    rt_steps_8, rt_spec_8 = from_stepbot(yaml_8)
    spec_8_check = replace(
        DEFAULT_DECK_SPEC,
        start={f"plate:{r}1": (100.0, f"sample_{r}") for r in "ABCDEFGH"},
        plate_dead_vol=5.0,
        pip_min=rt_spec_8.pip_min,
        pip_max=rt_spec_8.pip_max,
        channels=rt_spec_8.channels,
        pipettes=rt_spec_8.pipettes,
    )
    t8, e8 = rehearse(rt_steps_8, spec=spec_8_check)
    ok8, v8 = verify(t8, e8)
    assert ok8 is True and v8 == []
    assert e8["tips_used"] == 8
    for row in "ABCDEFGH":
        assert e8["plate"][f"plate:{row}2"] == pytest.approx(50.0)

    # 2. All passing demo protocols round-trip with identical verdict, per-well volumes, and tip count (2)
    demo_dir = REPO_ROOT / "demo" / "protocols"
    passing_demos = 0
    for proto_file in sorted(demo_dir.glob("*.json")):
        steps_demo = json.loads(proto_file.read_text(encoding="utf-8"))
        orig_t, orig_e = rehearse(steps_demo)
        orig_ok, _ = verify(orig_t, orig_e)
        pkt_demo = process_packet({"rehearsal_steps": steps_demo})
        assert pkt_demo["rehearsal"]["passed"] is orig_ok
        if orig_ok:
            passing_demos += 1
            yaml_demo = pkt_demo["rehearsal"]["protocol_yaml_dict"]
            assert yaml_demo is not None
            rt_s, rt_sp = from_stepbot(yaml_demo)
            rt_t, rt_e = rehearse(rt_s, spec=rt_sp)
            rt_ok, rt_v = verify(rt_t, rt_e)
            assert rt_ok is True and rt_v == []
            assert rt_e["tips_used"] == orig_e["tips_used"]
            if proto_file.name == "good_serial_dilution.json":
                assert rt_e["tips_used"] == 2
            for wk, wv in orig_e["plate"].items():
                assert rt_e["plate"][wk] == pytest.approx(wv, abs=0.1)
    assert passing_demos >= 1

    # 3. Any drifted compiled dict (e.g. wrong tip count or cross-contamination) is blocked with compiled_protocol_mismatch
    drifted_dict = {
        "metadata": {"protocolName": "Drifted", "apiLevel": "2.16"},
        "deck": {
            "1": {"load_name": "opentrons_96_tiprack_300ul"},
            "2": {"load_name": "nest_12_reservoir_15ml"},
            "3": {"load_name": "corning_96_wellplate_360ul_flat"},
        },
        "pipettes": {"right": {"instrument_name": "p300_single_gen2", "tip_racks": ["1"]}},
        "steps": [
            {
                "action": "transfer",
                "pipette": "right",
                "source": ["3:A1", "3:B1", "3:C1", "3:D1", "3:E1", "3:F1", "3:G1", "3:H1"],
                "dest": ["3:A2", "3:B2", "3:C2", "3:D2", "3:E2", "3:F2", "3:G2", "3:H2"],
                "volume": 50.0,
                "new_tip": "once",
            }
        ],
    }
    monkeypatch.setattr(autolab_wire, "to_stepbot", lambda *a, **kw: copy.deepcopy(drifted_dict))
    blocked_drift = process_packet(
        {
            "rehearsal_steps": steps_8,
            "pipette_model": "p300_multi_gen2",
            "deck_spec": {"start": start_8, "plate_dead_vol": 5.0},
        }
    )
    assert blocked_drift["halted"] is True
    assert blocked_drift["halt_reason"] == "L4_REHEARSAL_BLOCK"
    assert blocked_drift["rehearsal"]["protocol_yaml_dict"] is None
    assert any(
        v["rule"] == "compiled_protocol_mismatch"
        for v in blocked_drift["rehearsal"]["violations"]
    )


def test_issue_33_stepbot_compile_stage_and_opentrons_simulate_parity() -> None:
    sb_wire = _load_sibling_stepbot_wire()
    if sb_wire is None:
        pytest.skip("Sibling stepbot repository not available for cross-repo compile_stage check")

    good_steps = json.loads(
        (REPO_ROOT / "demo" / "protocols" / "good_serial_dilution.json").read_text(encoding="utf-8")
    )
    start_8 = {f"plate:{row}1": [100.0, f"sample_{row}"] for row in "ABCDEFGH"}
    steps_8 = [
        {"op": "pick_up_tip", "channels": 8},
        {"op": "aspirate", "loc": "plate:A1", "vol": 50.0, "channels": 8},
        {"op": "dispense", "loc": "plate:A2", "vol": 50.0, "channels": 8},
        {"op": "drop_tip", "channels": 8},
    ]
    approved_packets = [
        ("single_150", process_packet({"parsed_spans": {"volume_ul": 150.0}}), 1),
        ("single_250", process_packet({"parsed_spans": {"volume_ul": 250.0}}), 1),
        ("serial_dilution", process_packet({"rehearsal_steps": good_steps}), 2),
        (
            "multi8_samples",
            process_packet(
                {
                    "rehearsal_steps": steps_8,
                    "pipette_model": "p300_multi_gen2",
                    "deck_spec": {"start": start_8, "plate_dead_vol": 5.0},
                }
            ),
            8,
        ),
    ]
    compiled_bundles: list[tuple[str, dict[str, Any], int]] = []
    for label, pkt, expected_tips in approved_packets:
        assert pkt["halted"] is False, f"{label} unexpectedly halted: {pkt['rehearsal']['violations']}"
        sb_out = sb_wire.compile_stage(pkt)
        assert sb_out["halted"] is False
        assert sb_out["stepbot_compiled"]["manifest"]["total_tips_used"] == expected_tips
        compiled_bundles.append((label, sb_out, expected_tips))

    ot_sim = pytest.importorskip(
        "opentrons.simulate",
        reason="opentrons (<9) not installed in current Python environment; skipping CPU simulate tip check",
    )
    for label, sb_out, expected_tips in compiled_bundles:
        py_code = sb_out["stepbot_compiled"]["python_code"]
        runlog, _ = ot_sim.simulate(io.StringIO(py_code))
        sim_pickups = sum(
            1 for entry in runlog if "Picking up tip" in str(entry.get("payload", {}).get("text", ""))
        )
        assert sim_pickups == expected_tips, f"{label}: opentrons.simulate picked up {sim_pickups} != {expected_tips}"


def test_issue_28_persistent_wire_memory_and_artifact_retry(tmp_path) -> None:
    mem_file = tmp_path / "campaign_runs.jsonl"
    rec_schema = json.loads(
        (REPO_ROOT / "schema" / "run_record.schema.json").read_text(encoding="utf-8")
    )

    # Packet 1: clean rehearsal passes
    p1 = process_packet(
        {"parsed_spans": {"volume_ul": 120.0}, "fast_deny_known_failures": True},
        memory_path=mem_file,
    )
    assert p1["halted"] is False
    assert p1["rehearsal"]["episodic_recall"] == "no earlier runs of this protocol"
    run1_uuid = p1["rehearsal"]["run_uuid"]

    # Packet 2: post-run lab-attrib classifies Run 1 as transient EXECUTION_ARTIFACT -> retry allowed!
    p2 = process_packet(
        {
            "parsed_spans": {"volume_ul": 120.0},
            "fast_deny_known_failures": True,
            "attribution": {"run_uuid": run1_uuid, "outcome": "EXECUTION_ARTIFACT"},
        },
        memory_path=mem_file,
    )
    assert p2["halted"] is False
    assert p2["rehearsal"]["attribution_reconciled"] is True
    assert p2["rehearsal"]["fast_denied"] is False
    assert p2["rehearsal"]["retry_count"] == 1
    assert "EXECUTION_ARTIFACT" in p2["rehearsal"]["episodic_recall"]

    # Packet 3: Run 2 certified positive -> recall reflects certified recovery
    p3 = process_packet(
        {
            "parsed_spans": {"volume_ul": 120.0},
            "fast_deny_known_failures": True,
            "attribution": {
                "run_uuid": p2["rehearsal"]["run_uuid"],
                "outcome": "CERTIFIED_POSITIVE",
                "ledger_entry_sha256": "a" * 64,
            },
        },
        memory_path=mem_file,
    )
    assert p3["halted"] is False
    assert "recovered after 1 prior transient fault" in p3["rehearsal"]["episodic_recall"]

    lines = [ln for ln in mem_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 3
    for ln in lines:
        rec = json.loads(ln)
        jsonschema.validate(rec, rec_schema)
        assert validate_run_record_schema(rec) == []


def test_issue_35_wire_honest_invariant_and_real_lean_check() -> None:
    # Without run_lean_check=True, wire node labels tenth-uL packet honestly as `lean_model_compatible`
    res_default = process_packet({"parsed_spans": {"volume_ul": 120.0}})
    assert res_default["halted"] is False
    assert res_default["rehearsal"]["lean_verified"] is False
    assert res_default["bottom_reasoning_traces"][-1]["invariant"] == "lean_model_compatible"

    # Off-grid float volume (e.g. 120.05 uL) gets `python_float_rehearsal_unquantized`
    off_grid_steps = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 120.05},
        {"op": "dispense", "loc": "plate:A1", "vol": 120.05},
        {"op": "drop_tip"},
    ]
    res_unquant = process_packet({"rehearsal_steps": off_grid_steps})
    assert res_unquant["halted"] is False
    assert res_unquant["rehearsal"]["tenths_grid_exact"] is False
    assert res_unquant["rehearsal"]["lean_verified"] is False
    assert res_unquant["bottom_reasoning_traces"][-1]["invariant"] == "python_float_rehearsal_unquantized"

    # With run_lean_check=True, compiled Lean binary verifies the packet and upgrades invariant
    if (
        autolab_wire.find_lean_checker_binary() is None
        and autolab_wire.find_lake_executable() is None
    ):
        pytest.skip("Lean 4 toolchain not installed; skipping real Lean wire packet verification")

    res_lean = process_packet({"parsed_spans": {"volume_ul": 120.0}, "run_lean_check": True})
    assert res_lean["halted"] is False
    assert res_lean["rehearsal"]["lean_verified"] is True
    assert res_lean["bottom_reasoning_traces"][-1]["lean_verified"] is True
    assert res_lean["bottom_reasoning_traces"][-1]["invariant"] == "checkRehearsal_preserves_capacity"


def test_wire_and_docs_pure_english_integrity() -> None:
    for path in (
        Path(autolab_wire.__file__).resolve(),
        Path(__file__).resolve(),
        REPO_ROOT / "README.md",
        REPO_ROOT / "docs" / "ARCHITECTURE_AND_HISTORY.md",
    ):
        text = path.read_text(encoding="utf-8")
        assert CJK_RE.search(text) is None, f"Non-English CJK character found in {path}"
