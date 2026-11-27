"""Regression, formal parity, physics, 8-channel, and 10k-episode memory tests for Issues #24..#29."""

from __future__ import annotations

import hashlib
import json
import hashlib
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
for _p in (str(SRC_DIR), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from autolab_wire import (  # noqa: E402
    build_lean_case_payload,
    find_lake_executable,
    find_lean_checker_binary,
    find_lean_executable,
    run_lean_checker_cases,
)

from community_extensions.calm_twin_calibrator import (  # noqa: E402
    CalmAdaptiveTwin,
    compute_thermal_exposure,
)
from community_extensions.certified_negative_ledger import (  # noqa: E402
    AttributionOutcome,
    CertifiedNegativeLedger,
    PostRunTelemetry,
)
from lab_rehearsal import (  # noqa: E402
    DEFAULT_DECK_SPEC,
    DeckSpec,
    MemoryStore,
    RehearsalSession,
    compute_structural_signature,
    from_lablean,
    from_stepbot,
    from_twinstep,
    rehearse,
    to_lablean,
    to_stepbot,
    to_twinstep,
    validate_protocol_schema,
    validate_run_record_schema,
    verify,
)


def _rules(violations) -> list[str]:
    return [v["rule"] for v in violations]


# ==============================================================================
# Issue #24: Thermal physics, volatile evaporation, solute up-concentration & IR
# ==============================================================================
def test_issue_24_lablean_unsealed_90c_rejected_and_sealed_approved() -> None:
    ll_unsealed = {
        "steps": [
            {"kind": "pickTip"},
            {"kind": "transfer", "src": "res:1", "dst": "plate:A1", "vol_tenths": 600},
            {"kind": "dropTip"},
            {"kind": "incubate", "wellId": "plate:A1", "tempC": 90, "durSec": 600},
        ]
    }
    micro = from_lablean(ll_unsealed)
    assert len(micro) == 5
    assert micro[-1] == {"op": "incubate", "loc": "plate:A1", "temp_c": 90.0, "duration_s": 600.0}
    trace, end = rehearse(micro)
    approved, violations = verify(trace, end)
    assert approved is False
    assert "high_temp_requires_seal" in _rules(violations)

    # Sealing before 90 C incubation passes cleanly and round-trips through LabLean & twinstep
    ll_sealed = {
        "steps": [
            {"kind": "thawWell", "wellId": "res:1"},
            {"kind": "pickTip"},
            {"kind": "transfer", "src": "res:1", "dst": "plate:A1", "vol_tenths": 600},
            {"kind": "dropTip"},
            {"kind": "sealWell", "wellId": "plate:A1"},
            {"kind": "incubate", "wellId": "plate:A1", "tempC": 90, "durSec": 600},
        ]
    }
    spec_frozen = DeckSpec(frozen_locs=frozenset({"res:1"}))
    micro_s = from_lablean(ll_sealed)
    t_s, e_s = rehearse(micro_s, spec=spec_frozen)
    ok_s, v_s = verify(t_s, e_s)
    assert ok_s is True, v_s

    # Round-trip across LabLean, twinstep, and protocol JSON Schema
    proto_schema = json.loads((REPO_ROOT / "schema" / "protocol.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(micro_s, proto_schema)
    assert validate_protocol_schema(micro_s) == []
    assert from_lablean(to_lablean(micro_s, spec=spec_frozen)) == micro_s
    assert from_twinstep(to_twinstep(micro_s), tip_policy="smart") == micro_s


def test_issue_24_frozen_and_sealed_pipetting_interlocks() -> None:
    spec = DeckSpec(frozen_locs=frozenset({"res:2"}))
    # Pipetting from frozen res:2 without thaw fails thawed_before_pipetting
    bad_frozen = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:2", "vol": 50.0},
        {"op": "dispense", "loc": "plate:A1", "vol": 50.0},
        {"op": "drop_tip"},
    ]
    t1, e1 = rehearse(bad_frozen, spec=spec)
    ok1, v1 = verify(t1, e1)
    assert not ok1 and "thawed_before_pipetting" in _rules(v1)

    # Pipetting through sealed plate:A1 fails unsealed_before_pipetting
    bad_sealed = [
        {"op": "seal", "loc": "plate:A1"},
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 50.0},
        {"op": "dispense", "loc": "plate:A1", "vol": 50.0},
        {"op": "drop_tip"},
    ]
    t2, e2 = rehearse(bad_sealed)
    ok2, v2 = verify(t2, e2)
    assert not ok2 and "unsealed_before_pipetting" in _rules(v2)


def test_issue_24_volatile_evaporation_underflow_and_solute_upconcentration() -> None:
    # 50 uL ethanol_70pct (evap_coeff=0.42) + 10 uL non-volatile sample = 60 uL in plate:A1
    # Open incubation at 72 C for 25 min (1500 s) loses 0.42 * 97.2222 = 40.8333 uL!
    spec = DeckSpec(
        pip_min=10.0,
        start={
            "res:1": (5000.0, "ethanol_70pct"),
            "res:2": (2000.0, "sample"),
        },
        diluents=frozenset({"ethanol_70pct", "buffer"}),
    )
    exposure = compute_thermal_exposure(72.0, 25.0)
    expected_loss = 0.42 * exposure
    assert abs(expected_loss - 40.833333333) < 1e-5

    steps_underflow = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 50.0},
        {"op": "dispense", "loc": "plate:A1", "vol": 50.0},
        {"op": "drop_tip"},
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:2", "vol": 10.0},
        {"op": "dispense", "loc": "plate:A1", "vol": 10.0},
        {"op": "drop_tip"},
        # Open-well incubation at 72 C (< 80 C) boils off 40.83 uL -> 19.17 uL left (14.17 uL usable)
        {"op": "incubate", "loc": "plate:A1", "temp_c": 72.0, "duration_s": 1500.0},
        # Post-incubation 50 uL aspirate underflows remaining 19.17 uL!
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "plate:A1", "vol": 50.0},
        {"op": "dispense", "loc": "plate:B1", "vol": 50.0},
        {"op": "drop_tip"},
    ]
    trace, end = rehearse(steps_underflow, spec=spec)
    ok, violations = verify(trace, end)
    assert ok is False
    assert "enough_liquid" in _rules(violations)

    # Verify >3x solute up-concentration right after incubation (before post-incubation aspirate)
    t_inc, e_inc = rehearse(steps_underflow[:9], spec=spec)
    ok_inc, v_inc = verify(t_inc, e_inc)
    assert ok_inc is True, v_inc
    well_a1 = e_inc["deck"]["plate:A1"]
    assert abs(well_a1["vol"] - (60.0 - expected_loss)) < 1e-5
    # Non-volatile sample mass is conserved at 10.0 while total volume shrank to 19.17 uL (>3.1x)!
    assert abs(well_a1["amounts"]["sample"] - 10.0) < 1e-6
    up_conc = well_a1["amounts"]["sample"] / well_a1["vol"]
    init_conc = 10.0 / 60.0
    assert abs(up_conc / init_conc - (60.0 / well_a1["vol"])) < 1e-6
    assert up_conc / init_conc > 3.1

    # Re-entering evaporated 3.13x plate:A1 with a tip at initial 1x (10/60) concentration in plate:A2
    # is caught by no_tip_reuse!
    steps_conc_trap = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 50.0},
        {"op": "dispense", "loc": "plate:A1", "vol": 50.0},
        {"op": "aspirate", "loc": "res:1", "vol": 50.0},
        {"op": "dispense", "loc": "plate:A2", "vol": 50.0},
        {"op": "drop_tip"},
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:2", "vol": 10.0},
        {"op": "dispense", "loc": "plate:A1", "vol": 10.0},
        {"op": "drop_tip"},
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:2", "vol": 10.0},
        {"op": "dispense", "loc": "plate:A2", "vol": 10.0},
        {"op": "drop_tip"},
        {"op": "seal", "loc": "plate:A2"},  # keep A2 at 1x while A1 evaporates open!
        {"op": "incubate", "loc": "plate:A1", "temp_c": 72.0, "duration_s": 1500.0},
    ]
    # Use an unsealed A2 control prepared AFTER A1's incubation so A2 is open at 1x:
    steps_conc_trap = steps_underflow[:9] + [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 50.0},
        {"op": "dispense", "loc": "plate:A2", "vol": 50.0},
        {"op": "drop_tip"},
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:2", "vol": 10.0},
        {"op": "dispense", "loc": "plate:A2", "vol": 10.0},
        # Tip is now at 1x (0.1667) from A2; mixing evaporated A1 (0.5217 = 3.13x) violates no_tip_reuse!
        {"op": "mix", "loc": "plate:A1", "vol": 10.0, "reps": 2},
        {"op": "drop_tip"},
    ]
    t_trap, e_trap = rehearse(steps_conc_trap, spec=spec)
    ok_trap, v_trap = verify(t_trap, e_trap)
    assert not ok_trap
    assert "no_tip_reuse" in _rules(v_trap)


# ==============================================================================
# Issue #25: 8-channel manifold (p20_multi / p300_multi) tracking
# ==============================================================================
def test_issue_25_eight_channel_trough_drawdown_and_row_tracking() -> None:
    stepbot_multi = {
        "pipettes": {"left": {"model": "p300_multi_gen2", "tip_racks": ["tips"]}},
        "liquids": {"buffer": {"wells": "reservoir:A1", "volume": 2000.0}},
        "steps": [
            {
                "transfer": {
                    "source": "reservoir:A1",
                    "dest": "plate:A1",
                    "volume": 200.0,
                    "new_tip": "always",
                }
            }
        ],
    }
    micro, spec = from_stepbot(stepbot_multi)
    trace, end = rehearse(micro, spec=spec)
    approved, violations = verify(trace, end)

    assert approved is False
    assert "enough_liquid" in _rules(violations)
    assert end["tips_used"] == 8
    assert end["deck"]["res:1"]["vol"] == 400.0
    assert end["plate"] == {f"plate:{r}1": 200.0 for r in "ABCDEFGH"}


def test_issue_25_rack_exhaustion_partial_column_and_row_e_overfill(tmp_path) -> None:
    # 1. 13-column (104 > 96 tips) exhaustion
    steps_13_cols = []
    for _ in range(13):
        steps_13_cols.append({"op": "pick_up_tip", "channels": 8})
        steps_13_cols.append({"op": "drop_tip", "channels": 8})
    t13, e13 = rehearse(steps_13_cols)
    ok13, v13 = verify(t13, e13)
    assert not ok13
    assert e13["tips_used"] == 96
    assert any("tip rack empty" in str(v) for v in v13)

    # 2. Partial-column rejection when tiprack:D1 was consumed by a prior 1-channel run
    sess = RehearsalSession(memory_path=tmp_path / "persisted.jsonl", persist_deck=True)
    r_single = sess.evaluate_protocol(
        "take_d1.json",
        [{"op": "pick_up_tip", "loc": "tiprack:D1"}, {"op": "drop_tip"}],
    )
    assert r_single["approved"] is True
    r_partial = sess.evaluate_protocol(
        "col1_8ch.json",
        [
            {"op": "pick_up_tip", "loc": "tiprack:A1", "channels": 8},
            {"op": "drop_tip", "channels": 8},
        ],
    )
    assert r_partial["approved"] is False
    assert any("tiprack:D1" in str(v) for v in r_partial["violations"])
    sess.close()

    # 3. Row E pre-filled well overfill (plate:E1 has 150 uL + 200 uL = 350 uL > 300 uL)
    spec_row_e = DeckSpec(
        start={
            "res:1": (10000.0, "buffer"),
            "plate:E1": (150.0, "buffer"),
        }
    )
    row_e_steps = [
        {"op": "pick_up_tip", "channels": 8},
        {"op": "aspirate", "loc": "res:1", "vol": 200.0, "channels": 8},
        {"op": "dispense", "loc": "plate:A1", "vol": 200.0, "channels": 8},
        {"op": "drop_tip", "channels": 8},
    ]
    te, ee = rehearse(row_e_steps, spec=spec_row_e)
    oke, ve = verify(te, ee)
    assert not oke
    assert _rules(ve) == ["no_overfill"]
    assert "plate:E1 would hold 350 uL > 300 uL" in str(ve[0])


# ==============================================================================
# Issue #26: Tenth-uL conservation & Python <-> LabRehearsalLean differential parity
# ==============================================================================
class _LeanRefRunner:
    """Exact integer tenth-uL reference mirror of `LabRehearsalLean.checkRehearsal`."""

    CONC_TOL_PERMILLE = 1

    @classmethod
    def check_rehearsal(cls, steps: list[dict], spec: DeckSpec = DEFAULT_DECK_SPEC) -> bool:
        ll_doc = to_lablean(steps, spec=spec)
        pip_min = int(round(spec.pip_min * 10))
        pip_max = int(round(spec.pip_max * 10))
        if not (0 < pip_min <= pip_max):
            return False

        # Build deck wells in tenths of uL
        wells: dict[str, dict] = {}
        diluents_lc = {d.lower() for d in spec.diluents}
        for r in "ABCDEFGH":
            for c in range(1, 13):
                k = f"plate:{r}{c}"
                iv, il = spec.start.get(k, (0.0, None))
                vt = int(round(iv * 10))
                has_buf = bool(il and vt > 0 and il.lower() in diluents_lc)
                has_samp = bool(il and vt > 0 and il.lower() not in diluents_lc)
                wells[k] = {
                    "vol": vt,
                    "cap": int(round(spec.well_max * 10)),
                    "deadVol": int(round(spec.plate_dead_vol * 10)),
                    "soluteAmt": vt if has_samp else 0,
                    "hasBuffer": has_buf,
                    "hasSample": has_samp,
                    "frozen": k in spec.frozen_locs,
                    "sealed": k in spec.sealed_locs,
                    "isWaste": k in spec.waste_locs,
                    "isReagentStock": False,
                }
        for c in range(1, 13):
            k = f"res:{c}"
            iv, il = spec.start.get(k, (0.0, None))
            vt = int(round(iv * 10))
            is_w = k in spec.waste_locs
            has_buf = bool(il and vt > 0 and il.lower() in diluents_lc)
            has_samp = bool(il and vt > 0 and il.lower() not in diluents_lc)
            wells[k] = {
                "vol": vt,
                "cap": int(round(spec.res_max * 10)),
                "deadVol": 0 if is_w else int(round(spec.res_dead_vol * 10)),
                "soluteAmt": vt if has_samp else 0,
                "hasBuffer": has_buf,
                "hasSample": has_samp,
                "frozen": k in spec.frozen_locs,
                "sealed": k in spec.sealed_locs,
                "isWaste": is_w,
                "isReagentStock": not is_w,
            }
        wells["trash:1"] = {
            "vol": 0,
            "cap": int(round(spec.trash_max * 10)),
            "deadVol": 0,
            "soluteAmt": 0,
            "hasBuffer": False,
            "hasSample": False,
            "frozen": False,
            "sealed": False,
            "isWaste": True,
            "isReagentStock": False,
        }

        tip_state = "noTip"
        pv = ps = lv = ls = 0
        snaps: list[tuple[int, int]] = []
        tb = ts = False

        def may_enter(w: dict) -> bool:
            if tip_state == "noTip":
                return False
            if tip_state == "cleanTip":
                return True
            if (ts and not w["hasSample"]) or (tb and not w["hasBuffer"]):
                return False
            for sv, ss in snaps:
                if ss > 0 and sv > 0 and w["vol"] > 0:
                    if w["soluteAmt"] * sv * 1000 > ss * w["vol"] * (1000 + cls.CONC_TOL_PERMILLE):
                        return False
            if ls > 0 and lv > 0:
                if w["vol"] == 0:
                    return False
                if ls * w["vol"] * 1000 > w["soluteAmt"] * lv * (1000 + cls.CONC_TOL_PERMILLE):
                    return False
            return True

        def may_dispense(t_st: str, p_sol: int, t_b: bool, t_s: bool, w: dict, air: bool) -> bool:
            if t_st == "noTip":
                return False
            if t_st == "cleanTip" or air or w["isWaste"]:
                return True
            if w["isReagentStock"]:
                return (not t_s or w["hasSample"]) and (not t_b or w["hasBuffer"])
            if w["vol"] == 0:
                return True
            return not (t_s and p_sol == 0 and not w["hasSample"])

        for st in ll_doc["steps"]:
            kind = st["kind"]
            if kind == "pickTip":
                if tip_state != "noTip":
                    return False
                tip_state = "cleanTip"
                pv = ps = lv = ls = 0
                snaps = []
                tb = ts = False
            elif kind == "dropTip":
                if tip_state == "noTip" or pv != 0:
                    return False
                tip_state = "noTip"
                pv = ps = lv = ls = 0
                snaps = []
                tb = ts = False
            elif kind == "aspirate":
                w = wells.get(st["src"])
                v = st["vol_tenths"]
                if w is None or not (pip_min <= v <= pip_max) or pv + v > pip_max:
                    return False
                if w["frozen"] or w["sealed"] or w["isWaste"] or not may_enter(w):
                    return False
                if w["deadVol"] + v > w["vol"]:
                    return False
                ms = 0 if w["vol"] == 0 else (w["soluteAmt"] * v) // w["vol"]
                orig_v, orig_s, orig_b, orig_sp = w["vol"], w["soluteAmt"], w["hasBuffer"], w["hasSample"]
                if w["vol"] - v == 0:
                    w["vol"] = w["soluteAmt"] = 0
                    w["hasBuffer"] = w["hasSample"] = False
                else:
                    w["vol"] -= v
                    w["soluteAmt"] -= ms
                if orig_v > 0:
                    snaps.append((orig_v, orig_s))
                pv += v
                ps += ms
                lv, ls = orig_v, orig_s
                tb = tb or orig_b
                ts = ts or orig_sp
                tip_state = "usedTip"
            elif kind == "dispense":
                w = wells.get(st["dst"])
                v = st["vol_tenths"]
                air = bool(st.get("airDispense", False))
                if w is None or not (pip_min <= v <= pip_max) or tip_state == "noTip" or v > pv:
                    return False
                if w["frozen"] or w["sealed"] or not may_dispense(tip_state, ps, tb, ts, w, air):
                    return False
                if w["vol"] + v > w["cap"]:
                    return False
                gs = 0 if pv == 0 else (ps * v) // pv
                pip_b = (v > gs) or (tb and not ts)
                pip_s = gs > 0
                w["vol"] += v
                w["soluteAmt"] += gs
                w["hasBuffer"] = w["hasBuffer"] or pip_b or (not air and tb)
                w["hasSample"] = w["hasSample"] or pip_s or (not air and ts)
                pv -= v
                ps -= gs
                if not air:
                    lv, ls = w["vol"], w["soluteAmt"]
                    snaps.append((w["vol"], w["soluteAmt"]))
                    tb = tb or w["hasBuffer"]
                    ts = ts or w["hasSample"]
                tip_state = "usedTip"
            elif kind == "transfer":
                src = wells.get(st["src"])
                dst = wells.get(st["dst"])
                v = st["vol_tenths"]
                if src is None or dst is None or st["src"] == st["dst"] or pv != 0:
                    return False
                if not (pip_min <= v <= pip_max) or src["frozen"] or dst["frozen"] or src["sealed"] or dst["sealed"]:
                    return False
                if src["isWaste"] or not may_enter(src):
                    return False
                ms = 0 if src["vol"] == 0 else (src["soluteAmt"] * v) // src["vol"]
                if not may_dispense("usedTip", ps + ms, tb or src["hasBuffer"], ts or src["hasSample"], dst, False):
                    return False
                if src["deadVol"] + v > src["vol"] or dst["vol"] + v > dst["cap"]:
                    return False
                orig_v, orig_s, orig_b, orig_sp = src["vol"], src["soluteAmt"], src["hasBuffer"], src["hasSample"]
                if src["vol"] - v == 0:
                    src["vol"] = src["soluteAmt"] = 0
                    src["hasBuffer"] = src["hasSample"] = False
                else:
                    src["vol"] -= v
                    src["soluteAmt"] -= ms
                dst["vol"] += v
                dst["soluteAmt"] += ms
                dst["hasBuffer"] = dst["hasBuffer"] or orig_b or tb
                dst["hasSample"] = dst["hasSample"] or orig_sp or ts
                if orig_v > 0:
                    snaps.append((orig_v, orig_s))
                snaps.append((dst["vol"], dst["soluteAmt"]))
                lv, ls = dst["vol"], dst["soluteAmt"]
                tb = tb or orig_b or dst["hasBuffer"]
                ts = ts or orig_sp or dst["hasSample"]
                tip_state = "usedTip"
            elif kind == "mix":
                w = wells.get(st["container"])
                v = st["vol_tenths"]
                reps = st["times"]
                if w is None or reps < 1 or not (pip_min <= v <= pip_max) or pv + v > pip_max:
                    return False
                if w["frozen"] or w["sealed"] or w["isWaste"] or not may_enter(w):
                    return False
                if w["deadVol"] + v > w["vol"]:
                    return False
                w["hasBuffer"] = w["hasBuffer"] or tb
                w["hasSample"] = w["hasSample"] or ts
                lv, ls = w["vol"], w["soluteAmt"]
                snaps.append((w["vol"], w["soluteAmt"]))
                tb = tb or w["hasBuffer"]
                ts = ts or w["hasSample"]
                tip_state = "usedTip"
            elif kind == "incubate":
                w = wells.get(st["wellId"])
                tc, ds = st["tempC"], st["durSec"]
                if w is None or ds <= 0 or not (4 <= tc <= 98) or w["frozen"]:
                    return False
                if tc >= 80 and not w["sealed"]:
                    return False
            elif kind == "sealWell":
                w = wells.get(st["wellId"])
                if w is None or w["sealed"]:
                    return False
                w["sealed"] = True
            elif kind == "thawWell":
                w = wells.get(st["wellId"])
                if w is None or not w["frozen"]:
                    return False
                w["frozen"] = False

        return tip_state == "noTip"


def test_issue_26_sub_tenth_quantization_guard_and_multi_dispense() -> None:
    # 1. Sub-0.1 uL fractional volume (27.26 uL) is rejected by default in to_lablean()
    #    and in DeckSpec(strict_tenths=True) instead of silently inverting capacity!
    steps_27_26 = [{"op": "pick_up_tip"}]
    for _ in range(11):
        steps_27_26.append({"op": "aspirate", "loc": "res:1", "vol": 27.26})
        steps_27_26.append({"op": "dispense", "loc": "plate:A1", "vol": 27.26})
    steps_27_26.append({"op": "drop_tip"})

    with pytest.raises(ValueError, match="0.1 uL"):
        to_lablean(steps_27_26)

    t_strict, e_strict = rehearse(steps_27_26, spec=DeckSpec(strict_tenths=True))
    ok_strict, v_strict = verify(t_strict, e_strict)
    assert not ok_strict
    assert "tenth_ul_grid" in _rules(v_strict)

    # Error-feedback rounding when explicitly opted in preserves exact cumulative capacity verdicts:
    ll_opt = to_lablean(steps_27_26, allow_sub_tenth_rounding=True)
    assert sum(s["vol_tenths"] for s in ll_opt["steps"] if s["kind"] == "transfer") == 2999  # <= 3000!

    steps_33_34 = [{"op": "pick_up_tip"}]
    for _ in range(9):
        steps_33_34.append({"op": "aspirate", "loc": "res:1", "vol": 33.34})
        steps_33_34.append({"op": "dispense", "loc": "plate:A1", "vol": 33.34})
    steps_33_34.append({"op": "drop_tip"})
    ll_over = to_lablean(steps_33_34, allow_sub_tenth_rounding=True)
    assert sum(s["vol_tenths"] for s in ll_over["steps"] if s["kind"] == "transfer") == 3001  # > 3000!

    # 2. Multi-dispense lowers to primitive aspirate/dispense without ValueError and round-trips
    multi_disp = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 200.0},
        {"op": "dispense", "loc": "plate:A1", "vol": 100.0, "air_dispense": True},
        {"op": "dispense", "loc": "plate:B1", "vol": 100.0, "air_dispense": True},
        {"op": "drop_tip"},
    ]
    ll_multi = to_lablean(multi_disp)
    assert [s["kind"] for s in ll_multi["steps"]] == [
        "pickTip",
        "aspirate",
        "dispense",
        "dispense",
        "dropTip",
    ]
    assert from_lablean(ll_multi) == multi_disp


def _build_differential_parity_cases() -> list[tuple[str, list[dict], DeckSpec, bool | None]]:
    """Build 8 hand boundary cases + 40 seeded random trajectories (48 >= 47 cases)."""
    good_steps = json.loads(
        (REPO_ROOT / "demo" / "protocols" / "good_serial_dilution.json").read_text(encoding="utf-8")
    )
    reverse_steps = good_steps[:-1] + [
        {"op": "mix", "loc": "plate:B1", "vol": 50.0, "reps": 2},
        {"op": "drop_tip"},
    ]
    jump_down_steps = good_steps + [
        {"op": "pick_up_tip"},
        {"op": "mix", "loc": "plate:B1", "vol": 50.0, "reps": 2},
        {"op": "mix", "loc": "plate:H1", "vol": 50.0, "reps": 2},
        {"op": "drop_tip"},
    ]

    boundary_300_pass = [{"op": "pick_up_tip"}]
    for v in [27.3] * 10 + [27.0]:
        boundary_300_pass.append({"op": "aspirate", "loc": "res:1", "vol": v})
        boundary_300_pass.append({"op": "dispense", "loc": "plate:A1", "vol": v})
    boundary_300_pass.append({"op": "drop_tip"})

    boundary_300_1_fail = boundary_300_pass[:-2] + [
        {"op": "dispense", "loc": "plate:A1", "vol": 27.1},
        {"op": "drop_tip"},
    ]
    # Pair last aspirate to 27.1 as well so only no_overfill triggers:
    boundary_300_1_fail[-3] = {"op": "aspirate", "loc": "res:1", "vol": 27.1}

    overfill_301_in_300_fail = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 200.0},
        {"op": "dispense", "loc": "plate:A1", "vol": 200.0},
        {"op": "aspirate", "loc": "res:1", "vol": 101.0},
        {"op": "dispense", "loc": "plate:A1", "vol": 101.0},
        {"op": "drop_tip"},
    ]

    multi_air_ok = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 200.0},
        {"op": "dispense", "loc": "plate:A1", "vol": 100.0, "air_dispense": True},
        {"op": "dispense", "loc": "plate:A2", "vol": 100.0, "air_dispense": True},
        {"op": "drop_tip"},
    ]
    multi_sub_fail = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 200.0},
        {"op": "dispense", "loc": "plate:A1", "vol": 100.0},
        {"op": "dispense", "loc": "plate:A2", "vol": 100.0},
        {"op": "drop_tip"},
    ]
    spec_prefill = DeckSpec(
        start={
            "res:1": (10000.0, "buffer"),
            "plate:A1": (50.0, "sample"),
            "plate:A2": (50.0, "buffer"),
        }
    )

    cases: list[tuple[str, list[dict], DeckSpec, bool | None]] = [
        ("good_serial_dilution", good_steps, DEFAULT_DECK_SPEC, True),
        ("reverse_serial_dilution", reverse_steps, DEFAULT_DECK_SPEC, False),
        ("jump_down_dilution", jump_down_steps, DEFAULT_DECK_SPEC, False),
        ("boundary_300_0_pass", boundary_300_pass, DEFAULT_DECK_SPEC, True),
        ("boundary_300_1_fail", boundary_300_1_fail, DEFAULT_DECK_SPEC, False),
        ("overfill_301_in_300_fail", overfill_301_in_300_fail, DEFAULT_DECK_SPEC, False),
        ("multi_disp_air_ok", multi_air_ok, spec_prefill, True),
        ("multi_disp_sub_fail", multi_sub_fail, spec_prefill, False),
    ]

    # Add 40 seeded random tenth-uL trajectories covering aspirate/dispense/mix/seal/incubate
    rng = random.Random(20261124)
    wells_pool = ["res:1", "res:2", "plate:A1", "plate:B1", "plate:C1", "res:12"]
    for idx in range(40):
        st_list: list[dict] = [{"op": "pick_up_tip"}]
        n_ops = rng.randint(1, 5)
        for _ in range(n_ops):
            choice = rng.choice(["transfer", "multi_disp", "mix"])
            v = round(rng.randint(180, 2200) / 10.0, 1)
            if choice == "transfer":
                s_loc = rng.choice(["res:1", "res:2", "plate:A1"])
                d_loc = rng.choice(["plate:A1", "plate:B1", "plate:C1", "res:12"])
                if s_loc == d_loc:
                    d_loc = "plate:B1" if s_loc != "plate:B1" else "plate:C1"
                st_list.append({"op": "aspirate", "loc": s_loc, "vol": v})
                st_list.append({"op": "dispense", "loc": d_loc, "vol": v})
            elif choice == "multi_disp":
                v_half = round((rng.randint(100, 1400) // 2) / 10.0, 1)
                air = rng.choice([True, False])
                st_list.append({"op": "aspirate", "loc": "res:1", "vol": round(2 * v_half, 1)})
                st_list.append({"op": "dispense", "loc": "plate:A1", "vol": v_half, "air_dispense": air})
                st_list.append({"op": "dispense", "loc": "plate:B1", "vol": v_half, "air_dispense": air})
            else:
                w_loc = rng.choice(wells_pool)
                st_list.append({"op": "mix", "loc": w_loc, "vol": v, "reps": rng.randint(1, 4)})
        if rng.random() < 0.85:
            st_list.append({"op": "drop_tip"})
        if rng.random() < 0.3:
            if rng.random() < 0.5:
                st_list.append({"op": "seal", "loc": "plate:A1"})
            st_list.append(
                {
                    "op": "incubate",
                    "loc": "plate:A1",
                    "temp_c": float(rng.choice([37, 65, 90])),
                    "duration_s": 120.0,
                }
            )
        cases.append((f"rand_{idx}", st_list, DEFAULT_DECK_SPEC, None))

    return cases


def test_issue_26_differential_parity_python_vs_labrehearsallean() -> None:
    """Fallback pure-Python reference check when Lean is absent (runs in all Python environments)."""
    cases = _build_differential_parity_cases()
    assert len(cases) >= 47
    for label, steps_c, spec_c, expected_ok in cases:
        t_py, e_py = rehearse(steps_c, spec=spec_c)
        ok_py, v_py = verify(t_py, e_py)
        ok_lean_ref = _LeanRefRunner.check_rehearsal(steps_c, spec=spec_c)
        if expected_ok is not None:
            assert ok_py == expected_ok, f"{label}: Python expected {expected_ok}, got {ok_py} ({v_py})"
        assert ok_lean_ref == ok_py, (
            f"Fallback ref mismatch on {label}: Python={ok_py} ({v_py}) vs _LeanRefRunner={ok_lean_ref}"
        )


def test_issue_35_real_lean_differential_parity_and_axiom_audit() -> None:
    """Run >= 47 cases against compiled Lean `LabRehearsalLean.checkRehearsal` and audit theorem axioms."""
    import os

    require_lean = os.environ.get("REQUIRE_REAL_LEAN", "") == "1"
    lean_bin = find_lean_executable()
    lake_bin = find_lake_executable()
    if lean_bin is None and lake_bin is None and find_lean_checker_binary() is None:
        if require_lean:
            pytest.fail("REQUIRE_REAL_LEAN=1 is set, but Lean 4 toolchain (lean/lake) was not found")
        pytest.skip(
            "Lean 4 toolchain (lean/lake) not installed; skipping real Lean checkRehearsal differential test"
        )

    cases = _build_differential_parity_cases()
    assert len(cases) >= 47

    lean_payloads = [
        build_lean_case_payload(label, steps_c, spec_c, proto_hash_int=idx + 1)
        for idx, (label, steps_c, spec_c, _) in enumerate(cases)
    ]
    lean_results = run_lean_checker_cases(lean_payloads, auto_build=True)
    if lean_results is None:
        if require_lean:
            pytest.fail("REQUIRE_REAL_LEAN=1 is set, but lab-rehearsal-check could not be built or run")
        pytest.skip(
            "Lean 4 binary lab-rehearsal-check unavailable and lake could not build it; skipping real Lean check"
        )

    assert len(lean_results) == len(cases)
    by_id = {str(r["id"]): r for r in lean_results}
    mismatches: list[str] = []
    for label, steps_c, spec_c, expected_ok in cases:
        t_py, e_py = rehearse(steps_c, spec=spec_c)
        ok_py, v_py = verify(t_py, e_py)
        r_lean = by_id[label]
        ok_lean = bool(r_lean["ok"])
        if ok_lean != ok_py:
            mismatches.append(
                f"{label}: Python={ok_py} ({v_py}) vs Lean={ok_lean} "
                f"(rule={r_lean.get('rule')}, detail={r_lean.get('detail')})"
            )
        if expected_ok is not None:
            assert ok_lean == expected_ok, f"{label}: Lean expected {expected_ok}, got {r_lean}"
            assert ok_py == expected_ok, f"{label}: Python expected {expected_ok}, got {ok_py} ({v_py})"

    assert mismatches == [], f"Real Lean differential mismatches ({len(mismatches)}): {mismatches}"
    print(
        f"[real-lean-parity] Verified {len(cases)} cases against LabRehearsalLean.checkRehearsal (0 mismatches)"
    )

    # Mutation sensitivity check: relaxing Python `no_overfill` to allow 301 uL in a 300 uL well
    # immediately causes a verdict mismatch against real Lean `checkRehearsal`!
    import lab_rehearsal.core as core_mod

    overfill_case = next(c for c in cases if c[0] == "overfill_301_in_300_fail")
    def _mutated_no_overfill(f):
        if f.get("op") == "dispense" and float(f.get("dest_after", 0.0)) <= 301.0:
            return None
        return core_mod.no_overfill(f)

    _mutated_no_overfill.__name__ = "no_overfill"
    orig_rules = list(core_mod.STEP_RULES)
    try:
        core_mod.STEP_RULES = [
            _mutated_no_overfill if fn.__name__ == "no_overfill" else fn
            for fn in orig_rules
        ]
        t_mut, e_mut = rehearse(overfill_case[1], spec=overfill_case[2])
        ok_py_mutated, _ = verify(t_mut, e_mut)
    finally:
        core_mod.STEP_RULES = orig_rules

    lean_overfill = by_id["overfill_301_in_300_fail"]
    assert ok_py_mutated is True
    assert lean_overfill["ok"] is False
    assert lean_overfill["rule"] == "no_well_overfill"
    assert ok_py_mutated != bool(lean_overfill["ok"])

    # `#print axioms` audit over all theorems in `lean/LabRehearsalLean.lean` (zero sorryAx/custom axioms)
    if lean_bin is not None and lake_bin is not None:
        lean_src = (REPO_ROOT / "lean" / "LabRehearsalLean.lean").read_text(encoding="utf-8")
        theorem_names = re.findall(r"^(?:@\[[^\]]+\]\s+)?theorem\s+([A-Za-z0-9_']+)", lean_src, flags=re.M)
        assert len(theorem_names) >= 29
        allowed_axioms = {"propext", "Quot.sound", "Classical.choice"}
        with tempfile.TemporaryDirectory(prefix="lean_axiom_audit_") as tmp_dir:
            audit_file = Path(tmp_dir) / "AxiomAudit.lean"
            lines = ["import LabRehearsalLean"] + [
                f"#print axioms LabRehearsalLean.{thm}" for thm in theorem_names
            ]
            audit_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            proc = subprocess.run(
                [lake_bin, "env", lean_bin, str(audit_file)],
                cwd=str(REPO_ROOT / "lean"),
                capture_output=True,
                text=True,
            )
            assert proc.returncode == 0, proc.stderr + proc.stdout
            assert "warning:" not in proc.stderr and "warning:" not in proc.stdout
            assert "sorryAx" not in proc.stdout and "sorryAx" not in proc.stderr
            for out_line in proc.stdout.splitlines():
                if "depends on axioms:" in out_line:
                    bracket = out_line.split("[", 1)[1].rsplit("]", 1)[0]
                    used = {a.strip() for a in bracket.split(",") if a.strip()}
                    assert used <= allowed_axioms, f"Unexpected axiom in {out_line}"


def test_issue_35_cross_platform_elan_discovery_and_missing_lean_skip(monkeypatch, tmp_path) -> None:
    """Verify POSIX `~/.elan/bin/lean` discovery (no `.exe`) and clean fallback when absent."""
    import autolab_wire

    monkeypatch.setattr(autolab_wire.shutil, "which", lambda _name: None)

    # 1. Empty home directory -> returns None cleanly
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    assert find_lean_executable(home_dir=empty_home) is None
    assert find_lake_executable(home_dir=empty_home) is None
    assert find_lean_checker_binary(repo_dir=empty_home) is None

    # 2. POSIX layout (`~/.elan/bin/lean` and `~/.elan/bin/lake` without `.exe`) is discovered
    posix_home = tmp_path / "posix_home"
    elan_bin = posix_home / ".elan" / "bin"
    elan_bin.mkdir(parents=True)
    posix_lean = elan_bin / "lean"
    posix_lake = elan_bin / "lake"
    posix_lean.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    posix_lake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    assert find_lean_executable(home_dir=posix_home) == str(posix_lean)
    assert find_lake_executable(home_dir=posix_home) == str(posix_lake)


# ==============================================================================
# Issue #28: CertifiedNegativeLedger EXECUTION_ARTIFACT retry vs permanent safety deny
# ==============================================================================
def test_issue_28_execution_artifact_retry_succeeds_while_safety_failure_fast_denies(tmp_path) -> None:
    clean_steps = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 100.0},
        {"op": "dispense", "loc": "plate:A1", "vol": 100.0},
        {"op": "drop_tip"},
    ]
    mem_path = tmp_path / "runs.jsonl"
    session = RehearsalSession(
        memory_path=mem_path,
        fast_deny_known_failures=True,
        max_artifact_retries=2,
    )
    ledger = CertifiedNegativeLedger(memory_store=session.memory)

    # Run 1: passes static L4 verification, but transient acoustic bubble fault occurs on robot
    r1 = session.evaluate_protocol(
        "candidate_01.json",
        clean_steps,
        executor=lambda _: (False, ["acoustic sensor spike: air bubble on channel 1"]),
    )
    assert r1["approved"] is True and r1["run_passed"] is False

    # Post-run CertifiedNegativeLedger classifies as EXECUTION_ARTIFACT & reconciles MemoryStore
    outcome1, rec1 = ledger.process_telemetry(
        PostRunTelemetry(
            run_id=str(r1["run_id"]),
            candidate_id="MOL-01",
            pre_gate_certified=True,
            acoustic_normal=False,
            schedule_windows_respected=True,
            tactile_seated_cleanly=True,
            pos_control_passed=True,
            neg_control_blank_clean=True,
            spectral_residual_permille=40,
            max_residual_permille=100,
            precursor_overlap_permille=30,
            max_overlap_permille=120,
            signal_above_activity_thresh=False,
        )
    )
    assert outcome1 == AttributionOutcome.EXECUTION_ARTIFACT
    assert rec1 is None
    assert ledger.retry_queue == ["1"]

    # Clean retry Run 2 is PERMITTED (not deadlocked!) and succeeds on clean hardware
    r2 = session.evaluate_protocol("candidate_01.json", clean_steps)
    assert r2["fast_denied"] is False
    assert r2["approved"] is True
    assert r2["run_passed"] is True
    assert r2["retry_count"] == 1

    outcome2, rec2 = ledger.process_telemetry(
        PostRunTelemetry(
            run_id=r2["run_uuid"],
            candidate_id="MOL-01",
            pre_gate_certified=True,
            acoustic_normal=True,
            schedule_windows_respected=True,
            tactile_seated_cleanly=True,
            pos_control_passed=True,
            neg_control_blank_clean=True,
            spectral_residual_permille=35,
            max_residual_permille=100,
            precursor_overlap_permille=25,
            max_overlap_permille=120,
            signal_above_activity_thresh=True,
        )
    )
    assert outcome2 == AttributionOutcome.CERTIFIED_POSITIVE
    assert rec2 is not None
    recall_after = session.memory.recall(r2["hash"])
    assert "CERTIFIED_POSITIVE" in recall_after
    assert "recovered after 1 prior transient fault" in recall_after

    # Exceeding max_artifact_retries=2 (3 consecutive EXECUTION_ARTIFACT runs) escalates to fast-deny!
    alt_steps = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 120.0},
        {"op": "dispense", "loc": "plate:B2", "vol": 120.0},
        {"op": "drop_tip"},
    ]
    for attempt in range(3):
        ra = session.evaluate_protocol(
            "flaky_hw.json",
            alt_steps,
            executor=lambda _: (False, ["stepper stall"]),
        )
        assert ra["fast_denied"] is False
        session.memory.reconcile_attribution(ra["run_uuid"], "EXECUTION_ARTIFACT")
    r_fourth = session.evaluate_protocol("flaky_hw.json", alt_steps)
    assert r_fourth["fast_denied"] is True
    assert r_fourth["approved"] is False

    # Deterministic static safety failure (no_overfill) is fast-denied IMMEDIATELY (0 retries)!
    overfill = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 200.0},
        {"op": "dispense", "loc": "plate:C1", "vol": 200.0},
        {"op": "aspirate", "loc": "res:1", "vol": 200.0},
        {"op": "dispense", "loc": "plate:C1", "vol": 200.0},
        {"op": "drop_tip"},
    ]
    ro1 = session.evaluate_protocol("bad_static.json", overfill)
    assert ro1["approved"] is False and ro1["fast_denied"] is False
    ro2 = session.evaluate_protocol("bad_static.json", overfill)
    assert ro2["fast_denied"] is True and ro2["approved"] is False


# ==============================================================================
# Issue #29: BottomReasoningTrace persistence & 10,000-episode sub-linear recall
# ==============================================================================
def test_issue_29_column_shifted_and_volume_perturbed_structural_recall(tmp_path) -> None:
    col1_overfill = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:1", "vol": 200.0},
        {"op": "dispense", "loc": "plate:A1", "vol": 200.0},
        {"op": "aspirate", "loc": "res:1", "vol": 200.0},
        {"op": "dispense", "loc": "plate:A1", "vol": 200.0},
        {"op": "drop_tip"},
    ]
    col2_overfill = [
        {**s, "loc": "plate:A2"} if s.get("loc") == "plate:A1" else dict(s)
        for s in col1_overfill
    ]
    vol_perturbed_overfill = [
        {**s, "vol": 200.5} if "vol" in s else dict(s)
        for s in col2_overfill
    ]

    sess = RehearsalSession(memory_path=tmp_path / "runs.jsonl")
    r1 = sess.evaluate_protocol("col1.json", col1_overfill, deck_epoch=2)
    assert r1["approved"] is False

    r2 = sess.evaluate_protocol("col2.json", col2_overfill, deck_epoch=2)
    assert "WARNING: structurally similar protocol failed before in run(s) [1]" in r2["recall"]
    assert "no_overfill" in r2["recall"]

    r3 = sess.evaluate_protocol("col2_200_5.json", vol_perturbed_overfill, deck_epoch=3)
    assert "WARNING: structurally similar protocol failed before in run(s) [1, 2]" in r3["recall"]
    assert "stale deck_epoch" in r3["recall"]

    # CalmAdaptiveTwin state persistence round-trip across process restart
    state_file = tmp_path / "calm_state.json"
    twin1 = CalmAdaptiveTwin(stale_days=14, state_path=state_file)
    rec = twin1.register_sop("SOP-ETH-01", "ethanol_70pct", validated_day=3, deck_epoch=4)
    assert rec.bottom_trace is not None

    twin2 = CalmAdaptiveTwin(stale_days=14, state_path=state_file)
    dec = twin2.check_rusty_sop_gate("SOP-ETH-01", current_day=5, live_deck_epoch=4)
    assert dec.is_rusty is False and dec.allowed_full_scale is True
    assert dec.refreshed_trace == rec.bottom_trace


def test_issue_29_sublinear_10000_episodes_benchmark(tmp_path) -> None:
    mem_file = tmp_path / "synthetic_10k.jsonl"
    rng = random.Random(10000)

    # Synthesize 10,000 diverse episodes on disk
    ops_vocab = ["aspirate", "dispense", "mix", "incubate", "seal", "thaw"]
    vols_vocab = [15.0, 30.0, 80.0, 120.0, 180.0, 260.0]
    wells_vocab = [f"plate:{r}{c}" for r in "ABCDEFGH" for c in range(1, 13)]

    lines: list[str] = []
    target_twin_steps = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:2", "vol": 180.0},
        {"op": "dispense", "loc": "plate:D4", "vol": 180.0},
        {"op": "mix", "loc": "plate:D4", "vol": 90.0, "reps": 3},
        {"op": "aspirate", "loc": "res:2", "vol": 180.0},
        {"op": "dispense", "loc": "plate:D4", "vol": 180.0},
        {"op": "drop_tip"},
        {"op": "incubate", "loc": "plate:D4", "temp_c": 65.0, "duration_s": 600.0},
    ]

    for run_no in range(1, 10001):
        if run_no == 4321:
            proto_steps = target_twin_steps
            passed = False
            viols = [{"step": 6, "rule": "no_overfill", "message": "plate:D4 > 300 uL"}]
        else:
            # Generate diverse multi-step protocol structures
            n_len = rng.randint(4, 14)
            proto_steps = [{"op": "pick_up_tip"}]
            for _ in range(n_len):
                kind = rng.choice(ops_vocab)
                w = rng.choice(wells_vocab)
                v = rng.choice(vols_vocab)
                if kind == "aspirate":
                    src = rng.choice(["res:1", "res:2", "res:3", w])
                    proto_steps.append({"op": "aspirate", "loc": src, "vol": v})
                elif kind == "dispense":
                    proto_steps.append(
                        {"op": "dispense", "loc": w, "vol": v, "air_dispense": rng.choice([True, False])}
                    )
                elif kind == "mix":
                    proto_steps.append({"op": "mix", "loc": w, "vol": v, "reps": 2})
                elif kind == "incubate":
                    proto_steps.append(
                        {
                            "op": "incubate",
                            "loc": w,
                            "temp_c": rng.choice([22.0, 42.0, 90.0]),
                            "duration_s": 120.0,
                        }
                    )
                else:
                    proto_steps.append({"op": kind, "loc": w})
            proto_steps.append({"op": "drop_tip"})
            passed = True
            viols = []

        skel_h, shingles = compute_structural_signature(proto_steps)
        h_hex = hashlib.sha256(f"syn_{run_no}".encode("utf-8")).hexdigest()
        rec = {
            "run": run_no,
            "run_uuid": f"uuid_{run_no:08d}",
            "timestamp_utc": "2026-11-24T00:00:00Z",
            "protocol": f"syn_{run_no}.json",
            "hash": h_hex,
            "steps_hash": h_hex,
            "env_hash": "4" * 64,
            "skeleton_hash": skel_h,
            "shingles": shingles,
            "deck_epoch": 1 if run_no == 4321 else 2,
            "rules_version": "2.0.0",
            "verify_passed": passed,
            "run_passed": True if passed else None,
            "passed": passed,
            "violations": viols,
            "run_errors": [],
        }
        lines.append(json.dumps(rec))

    mem_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    store = MemoryStore(mem_file)
    assert len(store.records()) == 10000

    # 1. Exact shifted + volume-perturbed twin (plate:D4 -> plate:G11, 180.0 -> 180.5 uL)
    shifted_query = [
        {"op": "pick_up_tip"},
        {"op": "aspirate", "loc": "res:2", "vol": 180.5},
        {"op": "dispense", "loc": "plate:G11", "vol": 180.5},
        {"op": "mix", "loc": "plate:G11", "vol": 90.5, "reps": 3},
        {"op": "aspirate", "loc": "res:2", "vol": 180.5},
        {"op": "dispense", "loc": "plate:G11", "vol": 180.5},
        {"op": "drop_tip"},
        {"op": "incubate", "loc": "plate:G11", "temp_c": 65.0, "duration_s": 600.0},
    ]
    t0 = time.perf_counter()
    matches = store.recall_similar(shifted_query, top_k=5, live_deck_epoch=2)
    elapsed_s = time.perf_counter() - t0
    assert elapsed_s < 0.25
    assert matches and matches[0]["run"] == 4321
    assert matches[0]["exact_skeleton"] is True
    assert matches[0]["deck_epoch_stale"] is True
    # Sub-linear candidate inspection: < 5% (< 500) of 10,000 episodes inspected!
    assert store.last_candidates_inspected < 500

    # 2. Structural near-miss query (extra seal step at end -> different skeleton_hash, high shingle Jaccard)
    near_miss_query = shifted_query[:-1] + [
        {"op": "seal", "loc": "plate:G11"},
        shifted_query[-1],
    ]
    nm_matches = store.recall_similar(near_miss_query, top_k=5, min_jaccard=0.5)
    assert any(m["run"] == 4321 for m in nm_matches)
    assert store.last_candidates_inspected < 500

    # 3. Short hash prefix lookup uses 4-hex radix bucket (< 5 records inspected vs 10,000!)
    target_prefix = hashlib.sha256(b"syn_4321").hexdigest()[:12]
    msg = store.recall(target_prefix)
    assert "4321" in msg and "no_overfill" in msg
    assert store.last_candidates_inspected < 10
