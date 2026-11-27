"""Auto-Lab 15-repository wire node (`autolab-wire/v1`) for `lab-rehearsal` (Layer L4).

Executes stateful 96-well digital-twin liquid-handling & thermal simulation, default-deny
safety verification (`no_tip_reuse`, `no_overfill`, `enough_liquid`, `volume_in_range`,
`tip_present`, `thawed_before_pipetting`, `unsealed_before_pipetting`,
`high_temp_requires_seal`, `all_tips_dropped`), round-trip compiled-protocol verification,
sub-linear structural episodic recall, and `lab-attrib` `EXECUTION_ARTIFACT` retry
reconciliation before compiling approved multi-step trajectories into a declarative
`stepbot` Opentrons API `2.16` protocol dictionary.
"""

from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
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
    VOL_EPS,
    DeckSpec,
    RehearsalSession,
    _clean_vol,
    _is_finite_number,
    is_tenth_ul_exact,
    protocol_hash,
    protocol_skeleton_hash,
    rehearse,
    to_tenths_exact,
    verify,
)

_TRACKED_DEMO_LEDGER = (REPO_DIR / "demo" / "memory" / "runs.jsonl").resolve()


def _parse_requested_volume_ul(packet: Mapping[str, Any]) -> tuple[bool, bool, float | None, Any]:
    """Extract and validate `parsed_spans.volume_ul` (or top-level `volume_ul`).

    Returns `(has_explicit, is_valid_positive, finite_value_or_none, raw_value)`.
    """
    parsed_spans = packet.get("parsed_spans")
    if isinstance(parsed_spans, Mapping) and "volume_ul" in parsed_spans:
        raw_val = parsed_spans["volume_ul"]
        has_explicit = True
    elif "volume_ul" in packet:
        raw_val = packet["volume_ul"]
        has_explicit = True
    else:
        raw_val = 100.0
        has_explicit = False

    if isinstance(raw_val, bool) or not isinstance(raw_val, (int, float)):
        return has_explicit, False, None, raw_val

    fval = float(raw_val)
    if not math.isfinite(fval):
        return has_explicit, False, None, raw_val

    return has_explicit, (fval > 0.0), fval, raw_val


def _build_default_transfer_steps(
    volume_ul: float,
    spec: DeckSpec,
) -> tuple[list[dict[str, Any]] | None, float, str | None]:
    """Build exact single-well transfer step(s) for `volume_ul`, never silently capping.

    - Sub-minimum volumes (`0 < volume_ul < pip_min`) emit a 1-stroke transfer of `volume_ul`
      so `verify()` reports `volume_in_range` unless a compatible pipette is selected.
    - Volumes `<= min(well_max, pip_max)` emit a single transfer of exact `volume_ul`.
    - Volumes `pip_max < volume_ul <= well_max` split into exact multi-stroke transfers into
      `plate:A1` summing to `volume_ul` when each stroke is `>= pip_min`.
    - Volumes exceeding single-well capacity (`well_max`), usable source stock, or stroke
      minimum bounds return `(None, 0.0, reason)` so `process_packet` halts with
      `requested_volume_not_rehearsable`.
    """
    vol = _clean_vol(float(volume_ul))
    if vol > spec.well_max + VOL_EPS:
        return (
            None,
            0.0,
            (
                f"requested volume_ul {volume_ul:g} uL exceeds single-well capacity "
                f"{spec.well_max:g} uL without explicit multi-well rehearsal_steps"
            ),
        )

    src_init = float(spec.start.get("res:1", (0.0, ""))[0])
    src_usable = max(0.0, _clean_vol(src_init - spec.res_dead_vol))
    if vol > src_usable + VOL_EPS:
        return (
            None,
            0.0,
            (
                f"requested volume_ul {volume_ul:g} uL exceeds usable source stock "
                f"{src_usable:g} uL in res:1"
            ),
        )

    if vol <= spec.pip_max + VOL_EPS:
        steps: list[dict[str, Any]] = [
            {"op": "pick_up_tip"},
            {"op": "aspirate", "loc": "res:1", "vol": vol},
            {"op": "dispense", "loc": "plate:A1", "vol": vol},
            {"op": "drop_tip"},
        ]
        return steps, vol, None

    n_strokes = int(math.ceil((vol - VOL_EPS) / spec.pip_max))
    if is_tenth_ul_exact(vol):
        total_t = to_tenths_exact(vol, "volume_ul")
        q_t, r_t = divmod(total_t, n_strokes)
        strokes = [(q_t + 1) / 10.0] * r_t + [q_t / 10.0] * (n_strokes - r_t)
    else:
        base_s = round(vol / n_strokes, 6)
        strokes = [base_s] * (n_strokes - 1)
        strokes.append(_clean_vol(vol - sum(strokes)))

    if min(strokes) < spec.pip_min - VOL_EPS:
        return (
            None,
            0.0,
            (
                f"requested volume_ul {volume_ul:g} uL splits into {n_strokes} strokes "
                f"{strokes} below pipette minimum {spec.pip_min:g} uL"
            ),
        )

    multi_steps: list[dict[str, Any]] = [{"op": "pick_up_tip"}]
    for idx, sv in enumerate(strokes):
        multi_steps.append({"op": "aspirate", "loc": "res:1", "vol": sv})
        disp_step: dict[str, Any] = {"op": "dispense", "loc": "plate:A1", "vol": sv}
        if idx > 0:
            disp_step["air_dispense"] = True
        multi_steps.append(disp_step)
    multi_steps.append({"op": "drop_tip"})
    return multi_steps, _clean_vol(sum(strokes)), None


def _sum_dispensed_volume_ul(steps: Any, spec: DeckSpec) -> float:
    if not isinstance(steps, list):
        return 0.0
    total = 0.0
    for s in steps:
        if isinstance(s, Mapping) and s.get("op") == "dispense" and _is_finite_number(s.get("vol")):
            ch = int(s.get("channels", spec.channels)) if isinstance(s.get("channels", spec.channels), int) else 1
            mult = 8 if ch == 8 and str(s.get("loc", "")).startswith("plate") else 1
            total += float(s["vol"]) * mult
    return _clean_vol(total)


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
        if p_ch == 8:
            pipette_model = "p20_multi_gen2" if p_max <= 20.0 else "p300_multi_gen2"
        else:
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
        dead_v = spec.res_dead_vol if loc.startswith("res") else spec.plate_dead_vol
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


def _verify_compiled_wire_roundtrip(
    protocol_yaml_dict: Mapping[str, Any],
    spec: DeckSpec,
    orig_end: Mapping[str, Any],
    expected_single_well_total: float | None = None,
) -> str | None:
    """Re-rehearse `protocol_yaml_dict` via `from_stepbot` on `spec` and check parity."""
    try:
        rt_steps, rt_spec = from_stepbot(protocol_yaml_dict)
    except Exception as exc:  # pragma: no cover - defensive fail-closed guard
        return f"compiled stepbot dict failed round-trip parsing: {exc}"

    rehearse_spec = replace(
        spec,
        pip_min=rt_spec.pip_min,
        pip_max=rt_spec.pip_max,
        channels=rt_spec.channels,
        pipettes=rt_spec.pipettes,
    )
    rt_trace, rt_end = rehearse(rt_steps, spec=rehearse_spec)
    rt_approved, rt_violations = verify(rt_trace, rt_end)
    if not rt_approved or rt_violations:
        v_str = "; ".join(str(v) for v in rt_violations[:3])
        return f"compiled protocol failed re-rehearsal verification ({v_str})"

    orig_tips = int(orig_end.get("tips_used", 0))
    rt_tips = int(rt_end.get("tips_used", -1))
    if rt_tips != orig_tips:
        return f"compiled protocol tip count {rt_tips} differs from rehearsed tips_used {orig_tips}"

    orig_deck: Mapping[str, Any] = orig_end.get("deck", {})
    rt_deck: Mapping[str, Any] = rt_end.get("deck", {})
    plate_keys = sorted(
        {
            k
            for k in set(orig_deck.keys()) | set(rt_deck.keys())
            if isinstance(k, str) and (k.startswith("plate:") or k.startswith("plate_"))
        }
    )
    for pk in plate_keys:
        ov = float(orig_deck.get(pk, {}).get("vol", 0.0))
        rv = float(rt_deck.get(pk, {}).get("vol", 0.0))
        if abs(ov - rv) > 0.1 + 1e-6:
            return (
                f"compiled protocol well {pk} final volume {rv:g} uL differs from "
                f"rehearsed {ov:g} uL (> 0.1 uL)"
            )

    if expected_single_well_total is not None:
        rt_disp = _sum_dispensed_volume_ul(rt_steps, rehearse_spec)
        if abs(rt_disp - expected_single_well_total) > 0.1 + 1e-6:
            return (
                f"compiled protocol total dispensed volume {rt_disp:g} uL differs from "
                f"requested {expected_single_well_total:g} uL"
            )

    return None


def find_lean_checker_binary(repo_dir: Path | None = None) -> Path | None:
    """Locate built `lab-rehearsal-check` executable in `lean/.lake/build/bin` if available."""
    root = repo_dir if repo_dir is not None else REPO_DIR
    bin_dir = root / "lean" / ".lake" / "build" / "bin"
    for cand in ("lab-rehearsal-check.exe", "lab-rehearsal-check"):
        p = bin_dir / cand
        if p.is_file():
            return p
    return None


def _search_elan_tool(tool_base: str, home_dir: Path | None = None) -> str | None:
    """Locate `tool_base` (`lean` or `lake`) across PATH and `~/.elan` on Linux, macOS, and Windows."""
    found = shutil.which(tool_base)
    if found:
        return found
    home = home_dir if home_dir is not None else Path.home()
    elan_root = home / ".elan"
    candidates: list[Path] = [
        elan_root / "bin" / tool_base,
        elan_root / "bin" / f"{tool_base}.exe",
        elan_root / "toolchains" / "leanprover--lean4---v4.36.0" / "bin" / tool_base,
        elan_root / "toolchains" / "leanprover--lean4---v4.36.0" / "bin" / f"{tool_base}.exe",
    ]
    toolchains_dir = elan_root / "toolchains"
    if toolchains_dir.is_dir():
        for tc in sorted(toolchains_dir.iterdir(), reverse=True):
            candidates.append(tc / "bin" / tool_base)
            candidates.append(tc / "bin" / f"{tool_base}.exe")
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return None


def find_lean_executable(home_dir: Path | None = None) -> str | None:
    """Locate `lean` across PATH and `~/.elan` on Linux, macOS, and Windows."""
    return _search_elan_tool("lean", home_dir=home_dir)


def find_lake_executable(home_dir: Path | None = None) -> str | None:
    """Locate `lake` across PATH and `~/.elan` on Linux, macOS, and Windows."""
    return _search_elan_tool("lake", home_dir=home_dir)


def build_lean_case_payload(
    case_id: str,
    steps: list[dict[str, Any]],
    spec: DeckSpec = DEFAULT_DECK_SPEC,
    *,
    proto_hash_int: int = 1,
) -> dict[str, Any]:
    """Serialize `(steps, spec)` into the exact tenth-uL JSON payload consumed by `lean/Main.lean`."""
    ll_doc = to_lablean(steps, spec=spec)
    pip_min_t = to_tenths_exact(spec.pip_min, "DeckSpec.pip_min")
    pip_max_t = to_tenths_exact(spec.pip_max, "DeckSpec.pip_max")

    well_ids: dict[str, int] = {}
    wells_json: list[dict[str, Any]] = []
    diluents_lc = {d.lower() for d in spec.diluents}
    next_wid = 1

    def _register_well(
        loc_key: str,
        cap_ul: float,
        dead_ul: float,
        *,
        is_waste: bool,
        is_stock: bool,
    ) -> None:
        nonlocal next_wid
        if loc_key in well_ids:
            return
        wid = next_wid
        next_wid += 1
        well_ids[loc_key] = wid
        iv, il = spec.start.get(loc_key, (0.0, None))
        vt = to_tenths_exact(iv, f"{loc_key} start volume")
        has_buf = bool(il and vt > 0 and str(il).lower() in diluents_lc)
        has_samp = bool(il and vt > 0 and str(il).lower() not in diluents_lc)
        wells_json.append(
            {
                "wellId": wid,
                "vol": vt,
                "cap": to_tenths_exact(cap_ul, f"{loc_key} cap"),
                "deadVol": 0 if is_waste else to_tenths_exact(dead_ul, f"{loc_key} dead_vol"),
                "soluteAmt": vt if has_samp else 0,
                "hasBuffer": has_buf,
                "hasSample": has_samp,
                "frozen": loc_key in spec.frozen_locs,
                "sealed": loc_key in spec.sealed_locs,
                "isWaste": is_waste,
                "isReagentStock": is_stock,
            }
        )

    for r in "ABCDEFGH":
        for c in range(1, 13):
            k = f"plate:{r}{c}"
            _register_well(
                k,
                spec.well_max,
                spec.plate_dead_vol,
                is_waste=k in spec.waste_locs,
                is_stock=False,
            )
    for c in range(1, 13):
        k = f"res:{c}"
        is_w = k in spec.waste_locs
        _register_well(
            k,
            spec.res_max,
            spec.res_dead_vol,
            is_waste=is_w,
            is_stock=not is_w,
        )
    _register_well("trash:1", spec.trash_max, 0.0, is_waste=True, is_stock=False)

    steps_json: list[dict[str, Any]] = []
    for st in ll_doc["steps"]:
        kind = st["kind"]
        if kind in ("pickTip", "dropTip"):
            steps_json.append({"kind": kind})
        elif kind == "aspirate":
            steps_json.append(
                {
                    "kind": "aspirate",
                    "srcId": well_ids.get(str(st["src"]), 999999),
                    "vol": int(st["vol_tenths"]),
                }
            )
        elif kind == "dispense":
            steps_json.append(
                {
                    "kind": "dispense",
                    "dstId": well_ids.get(str(st["dst"]), 999999),
                    "vol": int(st["vol_tenths"]),
                    "airDispense": bool(st.get("airDispense", False)),
                }
            )
        elif kind == "transfer":
            steps_json.append(
                {
                    "kind": "transfer",
                    "srcId": well_ids.get(str(st["src"]), 999999),
                    "dstId": well_ids.get(str(st["dst"]), 999999),
                    "vol": int(st["vol_tenths"]),
                }
            )
        elif kind == "mix":
            steps_json.append(
                {
                    "kind": "mix",
                    "wellId": well_ids.get(str(st["container"]), 999999),
                    "vol": int(st["vol_tenths"]),
                    "reps": int(st["times"]),
                }
            )
        elif kind == "incubate":
            steps_json.append(
                {
                    "kind": "incubate",
                    "wellId": well_ids.get(str(st["wellId"]), 999999),
                    "tempC": int(st["tempC"]),
                    "durSec": int(st["durSec"]),
                }
            )
        elif kind == "sealWell":
            steps_json.append(
                {
                    "kind": "sealWell",
                    "wellId": well_ids.get(str(st["wellId"]), 999999),
                }
            )
        elif kind == "thawWell":
            steps_json.append(
                {
                    "kind": "thawWell",
                    "wellId": well_ids.get(str(st["wellId"]), 999999),
                }
            )

    return {
        "id": case_id,
        "protoHash": int(proto_hash_int),
        "pipMin": pip_min_t,
        "pipMax": pip_max_t,
        "failedHashes": [],
        "wells": wells_json,
        "steps": steps_json,
    }


def run_lean_checker_cases(
    cases: list[dict[str, Any]],
    *,
    auto_build: bool = True,
) -> list[dict[str, Any]] | None:
    """Execute the compiled Lean 4 `lab-rehearsal-check` binary on `cases`."""
    exe = find_lean_checker_binary()
    if exe is None and auto_build:
        lake_bin = find_lake_executable()
        if lake_bin is not None:
            proc_b = subprocess.run(
                [lake_bin, "build", "lab-rehearsal-check"],
                cwd=str(REPO_DIR / "lean"),
                capture_output=True,
                text=True,
            )
            if proc_b.returncode == 0:
                exe = find_lean_checker_binary()
    if exe is None:
        return None

    with tempfile.TemporaryDirectory(prefix="lab_rehearsal_lean_") as tmp_dir:
        in_file = Path(tmp_dir) / "cases.json"
        in_file.write_text(
            json.dumps({"cases": cases}, separators=(",", ":")),
            encoding="utf-8",
        )
        proc = subprocess.run(
            [str(exe), str(in_file)],
            cwd=str(REPO_DIR / "lean"),
            capture_output=True,
            text=True,
            check=True,
        )
        parsed = json.loads(proc.stdout)
        return list(parsed["results"])


def _emit_early_halt(
    out: dict[str, Any],
    *,
    protocol_name: str,
    spec: DeckSpec,
    deck_epoch: int,
    rule: str,
    message: str,
    requested_volume_ul: float | None,
    rehearsed_volume_ul: float,
) -> dict[str, Any]:
    out["halted"] = True
    out["halt_reason"] = "L4_REHEARSAL_BLOCK"
    violations_list = [{"step": 0, "rule": rule, "message": message}]
    minted_bt = _mint_wire_bottom_trace(
        protocol_name, spec, deck_epoch, ["L4_REHEARSAL_BLOCK"]
    )
    cand_hash = protocol_hash([], spec)
    skel_hash = protocol_skeleton_hash([], spec)
    out["rehearsal"] = {
        "passed": False,
        "violations": violations_list,
        "protocol_yaml_dict": None,
        "protocol_hash": cand_hash,
        "skeleton_hash": skel_hash,
        "episodic_recall": "no earlier runs of this protocol",
        "tips_used": 0,
        "run_id": 0,
        "run_uuid": "0" * 32,
        "fast_denied": False,
        "retry_count": 0,
        "attribution_reconciled": False,
        "tenths_grid_exact": False,
        "lean_verified": False,
        "requested_volume_ul": requested_volume_ul,
        "rehearsed_volume_ul": rehearsed_volume_ul,
        "similar_runs": [],
    }
    summary = f"Blocked 96-well protocol rehearsal (1 violation(s): {rule})."
    out["trace"].append(
        {
            "repo": REPO_ID,
            "layer": LAYER,
            "status": "BLOCK",
            "summary": summary,
        }
    )
    out["bottom_reasoning_traces"].append(
        {
            "repo": REPO_ID,
            "invariant": "L4_REHEARSAL_BLOCK",
            "deck_epoch": deck_epoch,
            "tenths_grid_exact": False,
            "lean_verified": False,
            "lablean_steps": 0,
            "skeleton_hash": skel_hash,
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


def process_packet(
    packet: dict[str, Any],
    *,
    memory_path: str | os.PathLike[str] | None = None,
    fast_deny_known_failures: bool | None = None,
    max_artifact_retries: int = 2,
    run_lean_check: bool | None = None,
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

    raw_mem_path = memory_path if memory_path is not None else out.get("rehearsal_memory_path")
    resolved_mem_path: Path | None = None
    if raw_mem_path is not None:
        resolved_mem_path = Path(raw_mem_path)
        if resolved_mem_path.resolve() == _TRACKED_DEMO_LEDGER:
            raise ValueError("Refusing to mutate tracked demo/memory/runs.jsonl from autolab_wire")

    protocol_name = str(
        out.get("protocol_name", "Auto-Lab L4 Rehearsed 96-Well Transfer")
    )
    live_deck_epoch = int(
        out.get(
            "deck_epoch",
            out.get("stepbot_execution", {}).get("deck_epoch", 1)
            if isinstance(out.get("stepbot_execution"), Mapping)
            else 1,
        )
    )

    has_explicit_vol, is_valid_vol, finite_vol, raw_vol_val = _parse_requested_volume_ul(out)
    raw_steps = out.get("rehearsal_steps")

    if raw_steps is None or has_explicit_vol:
        if not is_valid_vol or finite_vol is None:
            early_spec, _ = _resolve_wire_deck_spec(out, None)
            return _emit_early_halt(
                out,
                protocol_name=protocol_name,
                spec=early_spec,
                deck_epoch=live_deck_epoch,
                rule="invalid_requested_volume",
                message=(
                    f"requested volume_ul {raw_vol_val!r} must be a finite positive number (> 0 uL)"
                ),
                requested_volume_ul=finite_vol,
                rehearsed_volume_ul=0.0,
            )

    if raw_steps is None:
        assert finite_vol is not None
        active_spec, pipette_model = _resolve_wire_deck_spec(out, None)
        built_steps, rehearsed_volume_ul, build_err = _build_default_transfer_steps(
            finite_vol, active_spec
        )
        if built_steps is None:
            return _emit_early_halt(
                out,
                protocol_name=protocol_name,
                spec=active_spec,
                deck_epoch=live_deck_epoch,
                rule="requested_volume_not_rehearsable",
                message=build_err or f"requested volume_ul {finite_vol:g} uL is not rehearsable",
                requested_volume_ul=finite_vol,
                rehearsed_volume_ul=0.0,
            )
        steps_to_run: Any = built_steps
        requested_volume_ul: float = finite_vol
        nominal_rehearsal_vol = rehearsed_volume_ul
    else:
        try:
            steps_to_run, adapter_spec = _normalize_rehearsal_steps(raw_steps)
        except ValueError as exc:
            early_spec, _ = _resolve_wire_deck_spec(out, None)
            return _emit_early_halt(
                out,
                protocol_name=protocol_name,
                spec=early_spec,
                deck_epoch=live_deck_epoch,
                rule="schema_valid",
                message=f"invalid rehearsal_steps payload: {exc}",
                requested_volume_ul=finite_vol,
                rehearsed_volume_ul=0.0,
            )
        active_spec, pipette_model = _resolve_wire_deck_spec(out, adapter_spec)
        rehearsed_volume_ul = _sum_dispensed_volume_ul(steps_to_run, active_spec)
        requested_volume_ul = (
            float(finite_vol)
            if (has_explicit_vol and finite_vol is not None)
            else rehearsed_volume_ul
        )
        nominal_rehearsal_vol = requested_volume_ul

    eff_fast_deny = (
        bool(out.get("fast_deny_known_failures", False))
        if fast_deny_known_failures is None
        else bool(fast_deny_known_failures)
    )
    eff_max_retries = int(out.get("max_artifact_retries", max_artifact_retries))
    eff_run_lean = (
        bool(out.get("run_lean_check", False))
        if run_lean_check is None
        else bool(run_lean_check)
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

    lean_verified = False
    lean_mismatch_err: str | None = None
    if eff_run_lean and tenths_grid_exact and isinstance(steps_to_run, list):
        try:
            case_payload = build_lean_case_payload("wire_packet", steps_to_run, active_spec)
            lean_res = run_lean_checker_cases([case_payload], auto_build=True)
            if lean_res is not None and len(lean_res) == 1:
                if bool(lean_res[0].get("ok", False)):
                    lean_verified = True
                else:
                    lean_mismatch_err = (
                        f"Lean checkRehearsal rejected packet: "
                        f"{lean_res[0].get('rule')} ({lean_res[0].get('detail')})"
                    )
        except Exception as exc:
            lean_mismatch_err = f"Lean checker invocation error: {exc}"

    active_invariants = (
        ["checkRehearsal_preserves_capacity"]
        if lean_verified
        else (
            ["lean_model_compatible"]
            if tenths_grid_exact
            else ["python_float_rehearsal_unquantized"]
        )
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
    if lean_mismatch_err and not violations_list:
        violations_list.append(
            {"step": 0, "rule": "lean_check_rejected", "message": lean_mismatch_err}
        )
    approved = bool(eval_res["approved"]) and not violations_list

    protocol_yaml_dict: dict[str, Any] | None = None
    if not approved:
        out["halted"] = True
        out["halt_reason"] = "L4_REHEARSAL_BLOCK"
    else:
        try:
            candidate_yaml = to_stepbot(
                steps_to_run,
                name=protocol_name,
                spec=active_spec,
                wire_format=True,
                pipette_model=pipette_model,
            )
            rt_err = _verify_compiled_wire_roundtrip(
                candidate_yaml,
                active_spec,
                eval_res["end"],
                expected_single_well_total=requested_volume_ul if raw_steps is None else None,
            )
        except Exception as exc:
            candidate_yaml = None
            rt_err = f"stepbot wire compilation failed: {exc}"

        if rt_err is not None:
            out["halted"] = True
            out["halt_reason"] = "L4_REHEARSAL_BLOCK"
            protocol_yaml_dict = None
            violations_list.append(
                {
                    "step": 0,
                    "rule": "compiled_protocol_mismatch",
                    "message": rt_err,
                }
            )
        else:
            protocol_yaml_dict = candidate_yaml

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
        "lean_verified": lean_verified,
        "requested_volume_ul": requested_volume_ul,
        "rehearsed_volume_ul": rehearsed_volume_ul,
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
            if lean_verified
            else (
                "lean_model_compatible"
                if tenths_grid_exact
                else "python_float_rehearsal_unquantized"
            )
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
            "lean_verified": lean_verified,
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
