"""Unit tests for the `lab-rehearsal` Auto-Lab wire node (`autolab_wire.py`)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import sys

import jsonschema

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import autolab_wire  # noqa: E402
from autolab_wire import LAYER, REPO_ID, process_packet  # noqa: E402
from lab_rehearsal import from_stepbot, validate_run_record_schema  # noqa: E402

CJK_RE = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")


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
    assert (
        res["bottom_reasoning_traces"][-1]["invariant"]
        == "checkRehearsal_preserves_capacity"
    )

    # Stepbot transfer volume caps at 200.0 uL when upstream parsed volume > 200.0 uL
    res_capped = process_packet({"parsed_spans": {"volume_ul": 275.0}})
    assert res_capped["halted"] is False
    assert res_capped["rehearsal"]["protocol_yaml_dict"]["steps"][0]["volume"] == 200.0


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

    # 3. Full 43-step 8-well good_serial_dilution.json preserves all 16 transfers and 7 mixes
    good_steps = json.loads(
        (REPO_ROOT / "demo" / "protocols" / "good_serial_dilution.json").read_text(encoding="utf-8")
    )
    pkt_good = process_packet({"rehearsal_steps": good_steps})
    assert pkt_good["halted"] is False
    yaml_good = pkt_good["rehearsal"]["protocol_yaml_dict"]
    assert sum(1 for s in yaml_good["steps"] if s["action"] == "transfer") == 16
    assert sum(1 for s in yaml_good["steps"] if s["action"] == "mix") == 7
    assert "sample" in yaml_good["liquids"]
    assert yaml_good["liquids"]["sample"]["wells"] == "2:A2"


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


def test_wire_and_docs_pure_english_integrity() -> None:
    for path in (
        Path(autolab_wire.__file__).resolve(),
        Path(__file__).resolve(),
        REPO_ROOT / "README.md",
        REPO_ROOT / "docs" / "ARCHITECTURE_AND_HISTORY.md",
    ):
        text = path.read_text(encoding="utf-8")
        assert CJK_RE.search(text) is None, f"Non-English CJK character found in {path}"
