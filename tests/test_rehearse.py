"""Comprehensive regression and unit tests for `lab-rehearsal` (Issues #1..#22)."""

from __future__ import annotations

import json
import math
from pathlib import Path
import subprocess
import sys

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from lab_rehearsal import (  # noqa: E402
    DEFAULT_DECK_SPEC,
    DeckSpec,
    MemoryStore,
    RehearsalSession,
    TIPRACK_COORDS,
    evaluate_twinstep_jsonl,
    extract_violation_rule,
    from_lablean,
    from_stepbot,
    from_twinstep,
    new_deck,
    protocol_hash,
    rehearse,
    rehearse_monte_carlo,
    run_detailed,
    steps_hash,
    to_lablean,
    to_stepbot,
    to_twinstep,
    validate_protocol_schema,
    validate_run_record_schema,
    verify,
)


def _rules(violations):
    return [v["rule"] for v in violations]


# ---- Issue #1: Mix and wet-contact dispense contamination ----
def test_issue_1_mix_cross_contamination_rejected_and_propagated():
    proto_path = REPO_ROOT / "demo" / "protocols" / "broken_mix_contamination.json"
    steps = json.loads(proto_path.read_text(encoding="utf-8"))
    trace, end = rehearse(steps)
    approved, violations = verify(trace, end)
    assert not approved
    assert _rules(violations) == ["no_tip_reuse"]
    assert "tip carrying ['sample'] reused in res:1 containing ['buffer']" in str(violations[0])
    # Mix must also propagate carryover into simulated deck state
    assert "sample" in end["deck"]["res:1"]["liquids"]


def test_issue_1_submerged_dispense_contamination_vs_air_dispense():
    # Touch sample in res:2, dispense all to plate:A1, aspirate buffer? No:
    # Aspirate 200 uL sample, dispense 100 uL into res:1 (stock buffer) -> rejected!
    dirty_stock_dispense = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:2", "vol": 200},
        {"op": "dispense", "loc": "res:1", "vol": 100},
        {"op": "dispense", "loc": "plate:A1", "vol": 100},
        {"op": "drop_tip"},
    ]
    trace, end = rehearse(dirty_stock_dispense)
    approved, violations = verify(trace, end)
    assert not approved
    assert "no_tip_reuse" in _rules(violations)

    # Multi-dispense where first well holds foreign reagent ("toxin"), tainting tip exterior:
    spec = DeckSpec(
        start={
            "res:1": (10000.0, "buffer"),
            "plate:A1": (50.0, "toxin"),
            "plate:A2": (50.0, "buffer"),
        }
    )
    submerged_steps = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 200},
        {"op": "dispense", "loc": "plate:A1", "vol": 100},
        {"op": "dispense", "loc": "plate:A2", "vol": 100},
        {"op": "drop_tip"},
    ]
    t_sub, e_sub = rehearse(submerged_steps, spec=spec)
    ok_sub, v_sub = verify(t_sub, e_sub)
    assert not ok_sub
    assert "no_tip_reuse" in _rules(v_sub)

    # With explicit air_dispense=True on both dispenses, tip never contacts A1's toxin:
    air_steps = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 200},
        {"op": "dispense", "loc": "plate:A1", "vol": 100, "air_dispense": True},
        {"op": "dispense", "loc": "plate:A2", "vol": 100, "air_dispense": True},
        {"op": "drop_tip"},
    ]
    t_air, e_air = rehearse(air_steps, spec=spec)
    ok_air, v_air = verify(t_air, e_air)
    assert ok_air, v_air


# ---- Issue #2: Quantitative concentration monotonicity ----
def test_issue_2_good_serial_dilution_passes_while_reverse_dilution_rejected():
    good_path = REPO_ROOT / "demo" / "protocols" / "good_serial_dilution.json"
    good_steps = json.loads(good_path.read_text(encoding="utf-8"))
    t_good, e_good = rehearse(good_steps)
    ok_good, v_good = verify(t_good, e_good)
    assert ok_good, v_good

    # B1 -> ... -> H1 -> B1 (re-entering 1:2 concentrated B1 with 1:128 diluted tip from H1)
    back_up_row = good_steps[:-1] + [
        {"op": "mix", "loc": "plate:B1", "vol": 50, "reps": 2},
        {"op": "drop_tip"},
    ]
    t_rev, e_rev = rehearse(back_up_row)
    ok_rev, v_rev = verify(t_rev, e_rev)
    assert not ok_rev
    assert "no_tip_reuse" in _rules(v_rev)
    assert "higher-concentration" in str(v_rev[0])

    # Dipping a high-concentration tip (B1 = 1:2) directly into pre-diluted H1 (1:128) without serial transfer
    jump_down = (
        good_steps[:-1]
        + [
            {"op": "drop_tip"},
            {"op": "pick_up_tip"},
            {"op": "mix", "loc": "plate:B1", "vol": 50, "reps": 2},
            {"op": "mix", "loc": "plate:H1", "vol": 50, "reps": 2},
            {"op": "drop_tip"},
        ]
    )[1:]  # slice off trailing drop_tip duplicate from good_steps[:-1] + drop_tip -> wait: good_steps[:-1] has no drop_tip!
    # Build cleanly from good_steps (which already ends with drop_tip):
    jump_down_clean = good_steps + [
        {"op": "pick_up_tip"},
        {"op": "mix", "loc": "plate:B1", "vol": 50, "reps": 2},
        {"op": "mix", "loc": "plate:H1", "vol": 50, "reps": 2},
        {"op": "drop_tip"},
    ]
    t_jump, e_jump = rehearse(jump_down_clean)
    ok_jump, v_jump = verify(t_jump, e_jump)
    assert not ok_jump
    assert "no_tip_reuse" in _rules(v_jump)
    assert "higher concentration" in str(v_jump[0])


# ---- Issue #3: Combined pipette barrel capacity during mix ----
def test_issue_3_mix_checks_combined_pipette_barrel_capacity():
    steps = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 250},
        {"op": "mix", "loc": "res:1", "vol": 100, "reps": 3},
        {"op": "dispense", "loc": "plate:A1", "vol": 250},
        {"op": "drop_tip"},
    ]
    trace, end = rehearse(steps)
    approved, violations = verify(trace, end)
    assert not approved
    assert "volume_in_range" in _rules(violations)
    assert any("pipette would hold 350 uL > 300 uL" in str(v) for v in violations)


# ---- Issue #4: drop_tip without tip or with undispensed liquid ----
def test_issue_4_drop_tip_without_tip_and_wet_drop():
    # Redundant drop_tip without mounted tip
    trace1, end1 = rehearse([{"op": "drop_tip"}])
    ok1, v1 = verify(trace1, end1)
    assert not ok1
    assert _rules(v1) == ["tip_present"]
    assert "drop_tip without a tip" in str(v1[0])

    # Wet drop_tip discarding aspirated liquid silently
    wet_steps = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 100},
        {"op": "drop_tip"},
    ]
    trace2, end2 = rehearse(wet_steps)
    ok2, v2 = verify(trace2, end2)
    assert not ok2
    assert "tip_present" in _rules(v2)
    assert "undispensed liquid" in str(v2[0])

    # Explicit allow_wet_drop=True permits intentional blow-out/discard with tip
    allowed_wet = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 100},
        {"op": "drop_tip", "allow_wet_drop": True},
    ]
    trace3, end3 = rehearse(allowed_wet)
    ok3, v3 = verify(trace3, end3)
    assert ok3, v3


# ---- Issue #5: Validate `reps` on `mix` and forbid on non-`mix` ----
@pytest.mark.parametrize("bad_reps", [0, -5, "three", 1.5, True, 101])
def test_issue_5_invalid_mix_reps_rejected(bad_reps):
    steps = [
        {"op": "pick_up_tip"},
        {"op": "mix", "loc": "res:1", "vol": 100, "reps": bad_reps},
        {"op": "drop_tip"},
    ]
    trace, end = rehearse(steps)
    ok, violations = verify(trace, end)
    assert not ok
    assert "schema_valid" in _rules(violations)


def test_issue_5_reps_forbidden_on_non_mix():
    steps = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 100, "reps": 3},
        {"op": "dispense", "loc": "plate:A1", "vol": 100},
        {"op": "drop_tip"},
    ]
    trace, end = rehearse(steps)
    ok, violations = verify(trace, end)
    assert not ok
    assert "schema_valid" in _rules(violations)


# ---- Issue #6: Guard vol against null, NaN, Inf, bool, string ----
@pytest.mark.parametrize("bad_vol", [None, float("nan"), float("inf"), float("-inf"), "100uL", True])
def test_issue_6_malformed_vol_does_not_crash_or_poison_nan(bad_vol):
    steps = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": bad_vol},
        {"op": "aspirate", "loc": "res:1", "vol": 200},
        {"op": "dispense", "loc": "plate:A1", "vol": 200},
        {"op": "drop_tip"},
    ]
    trace, end = rehearse(steps)
    ok, violations = verify(trace, end)
    assert not ok
    assert "schema_valid" in _rules(violations)
    # Subsequent container volumes must remain finite, never NaN!
    assert math.isfinite(end["deck"]["res:1"]["vol"])
    assert math.isfinite(end["deck"]["plate:A1"]["vol"])


# ---- Issue #7: Exact decimal / epsilon tolerance without float drift ----
def test_issue_7_decimal_aliquots_summing_to_well_max_and_exact_depletion():
    # Classic IEEE-754 drift in sequential += loop: 10 * 27.3 + 27.0 == 300.00000000000006 > 300.0!
    aliquots = [27.3] * 10 + [27.0]
    naive_acc = 0.0
    for v in aliquots:
        naive_acc += v
    assert naive_acc > 300.0
    spec = DeckSpec(
        plate_dead_vol=0.0,
        res_dead_vol=0.0,
        start={"res:1": (300.0, "buffer")},
    )
    steps = [{"op": "pick_up_tip"}]
    for v in aliquots:
        steps.append({"op": "aspirate", "loc": "res:1", "vol": v})
        steps.append({"op": "dispense", "loc": "plate:A1", "vol": v})
    steps.append({"op": "drop_tip"})

    trace, end = rehearse(steps, spec=spec)
    ok, violations = verify(trace, end)
    assert ok, violations
    assert end["plate"]["plate:A1"] == 300.0
    assert end["deck"]["res:1"]["vol"] == 0.0


# ---- Issue #8: Zero-volume failed dispense does not contaminate empty well ----
def test_issue_8_zero_volume_failed_dispense_keeps_empty_well_clean():
    steps = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:2", "vol": 100},
        {"op": "dispense", "loc": "plate:A1", "vol": 100},
        # Pipette is now empty (0 uL), tries to dispense 100 uL into empty plate:B1:
        {"op": "dispense", "loc": "plate:B1", "vol": 100},
        {"op": "drop_tip"},
    ]
    trace, end = rehearse(steps)
    ok, violations = verify(trace, end)
    assert not ok
    assert end["deck"]["plate:B1"]["vol"] == 0.0
    assert end["deck"]["plate:B1"]["liquids"] == set()
    for k, w in end["deck"].items():
        if isinstance(k, str) and not k.startswith("_"):
            assert (w["vol"] == 0.0) == (len(w["liquids"]) == 0)


# ---- Issue #9: Canonical protocol_hash includes deck_spec, start, rules_version ----
def test_issue_9_protocol_hash_changes_with_deck_and_warns_on_env_drift(tmp_path):
    steps = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 50},
        {"op": "dispense", "loc": "plate:A1", "vol": 50},
        {"op": "drop_tip"},
    ]
    spec_p300 = DeckSpec(pip_min=20.0, pip_max=300.0)
    spec_p20 = DeckSpec(pip_min=2.0, pip_max=20.0)
    h1 = protocol_hash(steps, spec_p300)
    h2 = protocol_hash(steps, spec_p20)
    assert len(h1) == 64 and len(h2) == 64
    assert h1 != h2
    assert steps_hash(steps) == steps_hash(steps)

    mem_file = tmp_path / "runs.jsonl"
    s1 = RehearsalSession(memory_path=mem_file, spec=spec_p300)
    r1 = s1.evaluate_protocol("p.json", steps)
    assert r1["approved"] is True

    s2 = RehearsalSession(memory_path=mem_file, spec=spec_p20)
    r2 = s2.evaluate_protocol("p.json", steps)
    assert "different deck/environment config" in r2["recall"]
    assert r2["approved"] is False


# ---- Issue #10: Structured Violation dicts + legacy recall fallback ----
def test_issue_10_structured_violations_and_legacy_string_recall(tmp_path):
    mem_file = tmp_path / "legacy_runs.jsonl"
    legacy_lines = [
        json.dumps({"run": 1, "hash": "abc123", "passed": False, "violations": ["short"]}),
        json.dumps({"run": 2, "hash": "abc123", "passed": False, "violations": ["step 2 enough_liquid: oops"]}),
        "{corrupted partial json line",
    ]
    mem_file.write_text("\n".join(legacy_lines) + "\n", encoding="utf-8")
    store = MemoryStore(mem_file)
    msg = store.recall("abc123")
    assert "WARNING: failed before in run(s) [1, 2]" in msg
    assert "enough_liquid" in msg
    assert store.corrupted_lines == 1
    assert extract_violation_rule("") == "unknown_rule"


# ---- Issue #11: Running CLI / demo never deletes or dirties tracked runs.jsonl ----
def test_issue_11_demo_cli_leaves_tracked_memory_untouched(tmp_path):
    tracked_mem = REPO_ROOT / "demo" / "memory" / "runs.jsonl"
    before_bytes = tracked_mem.read_bytes()
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "demo" / "rehearse.py"), "--demo"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "######## pass 2 ########" in proc.stdout
    assert tracked_mem.read_bytes() == before_bytes


# ---- Issue #12: Empty protocol `[]` and non-list JSON root ----
def test_issue_12_empty_protocol_and_wrapped_dict_root():
    t_empty, e_empty = rehearse([])
    ok_empty, v_empty = verify(t_empty, e_empty)
    assert not ok_empty
    assert len(v_empty) == 1
    assert v_empty[0]["rule"] == "empty_protocol"
    assert "at least 1 step" in str(v_empty[0])

    t_dict, e_dict = rehearse({"steps": [{"op": "pick_up_tip"}]})
    ok_dict, v_dict = verify(t_dict, e_dict)
    assert not ok_dict
    assert len(v_dict) == 1
    assert v_dict[0]["rule"] == "schema_valid"


# ---- Issue #13: Output parity and CLI exit codes ----
def test_issue_13_demo_output_parity_and_cli_exit_code(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "demo" / "rehearse.py"), "--demo"],
        capture_output=True,
        text=True,
        check=True,
    )
    expected = (REPO_ROOT / "demo" / "output.txt").read_text(encoding="utf-8")
    assert proc.stdout.strip() == expected.strip()

    # Broken protocol invoked directly via CLI must exit with code 1
    bad_proto = REPO_ROOT / "demo" / "protocols" / "broken_overfill.json"
    bad_proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "demo" / "rehearse.py"),
            str(bad_proto),
            "--memory",
            str(tmp_path / "cli_runs.jsonl"),
        ],
        capture_output=True,
        text=True,
    )
    assert bad_proc.returncode == 1


# ---- Issue #14: Coverage of known_step, tip_present, all_tips_dropped, 97th tip, barrel overflow ----
def test_issue_14_all_rule_branches_covered():
    # 1. known_step: unknown op and out-of-bounds well coordinates
    t1, e1 = rehearse(
        [
            {"op": "vortex", "loc": "plate:A1", "vol": 50},
            {"op": "aspirate", "loc": "plate:I1", "vol": 50},
            {"op": "aspirate", "loc": "plate:A13", "vol": 50},
        ]
    )
    ok1, v1 = verify(t1, e1)
    assert not ok1
    assert _rules(v1) == ["known_step", "known_step", "known_step"]

    # 2. tip_present: liquid op without tip & double pickup
    t2, e2 = rehearse(
        [
            {"op": "aspirate", "loc": "res:1", "vol": 50},
            {"op": "pick_up_tip"},
            {"op": "pick_up_tip"},
            {"op": "drop_tip"},
        ]
    )
    ok2, v2 = verify(t2, e2)
    assert not ok2
    assert "aspirate without a tip" in str(v2[0])
    assert "tip already attached" in str(v2[1])

    # 3. tip_present: 97th tip exhaustion
    exhaust_steps = []
    for _ in range(97):
        exhaust_steps.append({"op": "pick_up_tip"})
        exhaust_steps.append({"op": "drop_tip"})
    t3, e3 = rehearse(exhaust_steps)
    ok3, v3 = verify(t3, e3)
    assert not ok3
    assert any("tip rack empty" in str(v) for v in v3)

    # 4. volume_in_range: multi-aspirate barrel overflow (> 300 uL)
    t4, e4 = rehearse(
        [
            {"op": "pick_up_tip"},
            {"op": "aspirate", "loc": "res:1", "vol": 200},
            {"op": "aspirate", "loc": "res:1", "vol": 150},
            {"op": "dispense", "loc": "res:12", "vol": 300},
            {"op": "dispense", "loc": "res:12", "vol": 50},
            {"op": "drop_tip"},
        ]
    )
    ok4, v4 = verify(t4, e4)
    assert not ok4
    assert any("pipette would hold 350 uL > 300 uL" in str(v) for v in v4)

    # 5. all_tips_dropped: tip left attached at end
    t5, e5 = rehearse([{"op": "pick_up_tip"}])
    ok5, v5 = verify(t5, e5)
    assert not ok5
    assert _rules(v5) == ["all_tips_dropped"]


# ---- Issue #15: Strict JSON Schemas for protocol & run records ----
def test_issue_15_strict_json_schemas_reject_misspelled_keys():
    proto_schema = json.loads((REPO_ROOT / "schema" / "protocol.schema.json").read_text(encoding="utf-8"))
    rec_schema = json.loads((REPO_ROOT / "schema" / "run_record.schema.json").read_text(encoding="utf-8"))

    good_steps = json.loads((REPO_ROOT / "demo" / "protocols" / "good_serial_dilution.json").read_text(encoding="utf-8"))
    jsonschema.validate(good_steps, proto_schema)
    assert validate_protocol_schema(good_steps) == []

    misspelled = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "volume": 100},
        {"op": "drop_tip", "loc": "plate:A1"},
    ]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(misspelled, proto_schema)
    t, e = rehearse(misspelled)
    ok, violations = verify(t, e)
    assert not ok
    assert _rules(violations)[:2] == ["schema_valid", "schema_valid"]

    for line in (REPO_ROOT / "demo" / "memory" / "runs.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            jsonschema.validate(rec, rec_schema)
            assert validate_run_record_schema(rec) == []


# ---- Issue #16: Dedicated waste containers block aspirate/mix ----
def test_issue_16_waste_container_blocks_aspirate_and_mix():
    steps = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 100},
        {"op": "dispense", "loc": "res:12", "vol": 100},
        {"op": "aspirate", "loc": "res:12", "vol": 50},
        {"op": "dispense", "loc": "plate:A1", "vol": 50},
        {"op": "drop_tip"},
    ]
    trace, end = rehearse(steps)
    ok, violations = verify(trace, end)
    assert not ok
    assert "no_aspirate_from_waste" in _rules(violations)


# ---- Issue #17 & #18: UTF-8 indexed MemoryStore, runtime hardware faults, fast-deny ----
def test_issue_17_18_runtime_faults_and_fast_deny_policy(tmp_path):
    mem_file = tmp_path / "runs.jsonl"
    steps = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 100},
        {"op": "dispense", "loc": "plate:A1", "vol": 100},
        {"op": "drop_tip"},
    ]

    def clogged_tip_executor(_steps):
        return False, ["LLD pressure spike: clogged tip on channel 1"]

    session = RehearsalSession(memory_path=mem_file, fast_deny_known_failures=True)
    res1 = session.evaluate_protocol("assay_utf8.json", steps, executor=clogged_tip_executor)
    assert res1["approved"] is True
    assert res1["run_passed"] is False
    assert "clogged tip" in res1["run"]

    # Second run recalls distinct runtime hardware failure and fast-denies
    res2 = session.evaluate_protocol("assay_utf8.json", steps)
    assert "runtime hardware failure" in res2["recall"]
    assert res2["fast_denied"] is True
    assert res2["approved"] is False

    msg, ok, errs = run_detailed(steps, True, executor=clogged_tip_executor)
    assert ok is False and len(errs) == 1 and "failed" in msg


# ---- Issue #19: Individual tiprack:A1..H12 coordinates and multi-protocol persistence ----
def test_issue_19_addressable_tiprack_and_persistent_deck_session(tmp_path):
    assert len(TIPRACK_COORDS) == 96
    assert TIPRACK_COORDS[0] == "tiprack:A1" and TIPRACK_COORDS[-1] == "tiprack:H12"

    # Picking up same specific coordinate twice fails on empty slot
    steps = [
        {"op": "pick_up_tip", "loc": "tiprack:B3"},
        {"op": "drop_tip"},
        {"op": "pick_up_tip", "loc": "tiprack:B3"},
        {"op": "drop_tip"},
    ]
    t, e = rehearse(steps)
    ok, v = verify(t, e)
    assert not ok
    assert any("tip slot tiprack:B3 is empty" in str(x) for x in v)

    # Double pickup preserves existing contaminated touched state!
    double_pick_dirty = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:2", "vol": 100},
        {"op": "dispense", "loc": "plate:A1", "vol": 100},
        {"op": "pick_up_tip"},  # fails, must NOT reset touched!
        {"op": "aspirate", "loc": "res:1", "vol": 100},  # must ALSO catch no_tip_reuse!
        {"op": "dispense", "loc": "plate:B1", "vol": 100},
        {"op": "drop_tip"},
    ]
    td, ed = rehearse(double_pick_dirty)
    okd, vd = verify(td, ed)
    assert not okd
    assert "tip_present" in _rules(vd)
    assert "no_tip_reuse" in _rules(vd)

    # Persistent deck across sequential protocols
    sess = RehearsalSession(memory_path=tmp_path / "batch.jsonl", persist_deck=True)
    p1 = [{"op": "pick_up_tip", "loc": "tiprack:A1"}, {"op": "drop_tip"}]
    r1 = sess.evaluate_protocol("p1.json", p1)
    assert r1["approved"] is True
    assert sess.deck["_tiprack"]["tiprack:A1"] is False
    r2 = sess.evaluate_protocol("p2.json", p1)
    assert r2["approved"] is False
    sess.close()


# ---- Issue #20: Bidirectional adapters for stepbot, twinstep, LabLean ----
def test_issue_20_bidirectional_ir_adapters_and_twinstep_benchmark():
    # 1. stepbot dilution_series -> lab-rehearsal -> stepbot
    stepbot_doc = {
        "metadata": {"name": "Dilution"},
        "pipettes": {"left": {"model": "p300_single_gen2", "tip_racks": ["tips"]}},
        "liquids": {
            "buffer": {"wells": "reservoir:A1", "volume": 10000},
            "dye": {"wells": "reservoir:A2", "volume": 2000},
        },
        "steps": [
            {
                "dilution_series": {
                    "source": "reservoir:A2",
                    "diluent": "reservoir:A1",
                    "first_well": "plate:A1",
                    "n": 4,
                    "factor": 2,
                    "final_volume": 100,
                    "mix_reps": 3,
                }
            }
        ],
    }
    micro, spec = from_stepbot(stepbot_doc)
    t, e = rehearse(micro, spec=spec)
    ok, v = verify(t, e)
    assert ok, v
    exported_sb = to_stepbot(micro, spec=spec)
    assert len(exported_sb["steps"]) > 0

    # 2. twinstep round-trip & JSONL benchmark evaluation
    ts_steps = [
        {"type": "transfer", "source": "reservoir:buffer", "dest": "plate1:A1", "volume": 150, "unit": "uL"},
        {"type": "mix", "well": "plate1:A1", "volume": 100, "unit": "uL", "cycles": 3},
    ]
    ts_micro = from_twinstep(ts_steps, tip_policy="smart")
    lifted = to_twinstep(ts_micro)
    assert len(lifted) == 2 and lifted[0]["type"] == "transfer" and lifted[1]["type"] == "mix"

    metrics = evaluate_twinstep_jsonl(REPO_ROOT / "tests" / "fixtures" / "twinstep_twins.jsonl")
    assert metrics["precision"] == 1.0
    assert metrics["recall"] == 1.0
    assert metrics["tp"] == 4 and metrics["tn"] == 2

    # 3. LabLean round-trip
    ll_doc = to_lablean(ts_micro)
    back_micro = from_lablean(ll_doc)
    tb, eb = rehearse(back_micro)
    okb, vb = verify(tb, eb)
    assert okb, vb


# ---- Issue #21: Dead-volume enforcement and Monte Carlo pipetting inaccuracy ----
def test_issue_21_dead_volume_and_monte_carlo_simulation():
    # Reservoir holding 600 uL with 500 uL dead volume has only 100 uL usable:
    spec = DeckSpec(res_dead_vol=500.0, start={"res:1": (600.0, "buffer")})
    underflow_steps = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 200},
        {"op": "dispense", "loc": "plate:A1", "vol": 200},
        {"op": "drop_tip"},
    ]
    t, e = rehearse(underflow_steps, spec=spec)
    ok, v = verify(t, e)
    assert not ok
    assert "enough_liquid" in _rules(v)
    assert "dead volume" in str(v[0])

    # Near-capacity dispense (299 uL into 300 uL well) passes deterministically
    # but shows positive overfill risk under positive ISO 8655 bias in Monte Carlo!
    near_max_steps = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 299},
        {"op": "dispense", "loc": "plate:A1", "vol": 299},
        {"op": "drop_tip"},
    ]
    mc1 = rehearse_monte_carlo(near_max_steps, n_rollouts=80, seed=123, spec=DEFAULT_DECK_SPEC)
    mc2 = rehearse_monte_carlo(near_max_steps, n_rollouts=80, seed=123, spec=DEFAULT_DECK_SPEC)
    assert mc1 == mc2  # strictly deterministic given seed
    assert mc1["deterministic_approved"] is True
    assert mc1["overfill_rate"] > 0.0


# ---- Issue #22: Catalog SPDX & license governance validation ----
def test_issue_22_catalog_spdx_and_unlicensed_guards():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import check_catalog

    errs = check_catalog.validate_catalog(REPO_ROOT, online=False)
    assert errs == []

    catalog = json.loads((REPO_ROOT / "catalog" / "simulators.json").read_text(encoding="utf-8"))
    by_id = {d["id"]: d for d in catalog["datasets"]}
    for unlicensed_id in ("autobio", "chemistry3d", "labimus"):
        assert by_id[unlicensed_id]["commercial_use_ok"] is False
        assert by_id[unlicensed_id]["redistribution_ok"] is False
