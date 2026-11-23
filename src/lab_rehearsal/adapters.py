"""Bidirectional protocol IR adapters for `stepbot`, `twinstep`, and `YLM/lean` (`LabLean`)."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .core import DEFAULT_DECK_SPEC, DeckSpec, rehearse, verify

DEFAULT_TWINSTEP_LOC_MAP: dict[str, str] = {
    "reservoir:buffer": "res:1",
    "reservoir:pbs": "res:1",
    "reservoir:water": "res:1",
    "reservoir:diluent": "res:1",
    "reservoir:sample": "res:2",
    "reservoir:dye": "res:2",
    "reservoir:substrate": "res:3",
    "reservoir:stop": "res:4",
    "waste": "res:12",
    "trash": "trash:1",
}


def _normalize_loc(raw_loc: str, loc_map: Mapping[str, str] | None = None) -> str:
    merged = dict(DEFAULT_TWINSTEP_LOC_MAP)
    if loc_map:
        merged.update(loc_map)
    if raw_loc in merged:
        return merged[raw_loc]
    # Normalize `plate1:A1` -> `plate:A1`, `reservoir:A1`..`reservoir:A12` -> `res:1`..`res:12`
    m_plate = re.match(r"^plate\d*:([A-H](?:1[0-2]|[1-9]))$", raw_loc)
    if m_plate:
        return f"plate:{m_plate.group(1)}"
    m_res_well = re.match(r"^reservoir:[A-H](1[0-2]|[1-9])$", raw_loc)
    if m_res_well:
        return f"res:{m_res_well.group(1)}"
    m_res_col = re.match(r"^reservoir:(1[0-2]|[1-9])$", raw_loc)
    if m_res_col:
        return f"res:{m_res_col.group(1)}"
    return raw_loc


def _vol_to_ul(vol: float, unit: str = "uL") -> float:
    if unit == "mL":
        return float(vol) * 1000.0
    if unit == "uL":
        return float(vol)
    raise ValueError(f"unsupported volume unit: {unit!r}")


def from_twinstep(
    protocol_or_steps: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    loc_map: Mapping[str, str] | None = None,
    tip_policy: str = "per_transfer",
) -> list[dict[str, Any]]:
    """Lower `twinstep` macro-steps (`transfer`, `mix`, `wash`) into `lab-rehearsal` micro-steps.

    Non-pipetting steps (`incubate`, `read`) are ignored during liquid-handling lowering.
    `tip_policy="per_transfer"` wraps each transfer/mix block cleanly; `tip_policy="smart"`
    reuses tips across identical clean sources or downstream serial transfers.
    """
    if isinstance(protocol_or_steps, Mapping):
        raw_steps = protocol_or_steps.get("steps", [])
    else:
        raw_steps = protocol_or_steps
    if not isinstance(raw_steps, Sequence):
        raise ValueError("twinstep input must be a step list or dict with 'steps'")

    micro: list[dict[str, Any]] = []
    tip_on = False
    last_src: str | None = None
    last_dst: str | None = None

    def ensure_tip(fresh: bool) -> None:
        nonlocal tip_on
        if fresh and tip_on:
            micro.append({"op": "drop_tip"})
            tip_on = False
        if not tip_on:
            micro.append({"op": "pick_up_tip"})
            tip_on = True

    for step in raw_steps:
        if not isinstance(step, Mapping):
            raise ValueError(f"invalid twinstep step: {step!r}")
        stype = step.get("type")
        if stype == "transfer":
            src = _normalize_loc(str(step["source"]), loc_map)
            dst = _normalize_loc(str(step["dest"]), loc_map)
            vol = _vol_to_ul(float(step["volume"]), str(step.get("unit", "uL")))
            if tip_policy == "smart":
                can_reuse = tip_on and (src == last_src == "res:1" or src == last_dst)
                ensure_tip(fresh=not can_reuse)
            else:
                ensure_tip(fresh=True)
            micro.append({"op": "aspirate", "loc": src, "vol": vol})
            micro.append({"op": "dispense", "loc": dst, "vol": vol})
            last_src, last_dst = src, dst
            if tip_policy != "smart":
                micro.append({"op": "drop_tip"})
                tip_on = False

        elif stype == "mix":
            well = _normalize_loc(str(step["well"]), loc_map)
            vol = _vol_to_ul(float(step["volume"]), str(step.get("unit", "uL")))
            reps = int(step.get("cycles", step.get("reps", 1)))
            if tip_policy == "smart" and tip_on and well == last_dst:
                pass
            else:
                ensure_tip(fresh=True)
            micro.append({"op": "mix", "loc": well, "vol": vol, "reps": reps})
            last_dst = well
            if tip_policy != "smart":
                micro.append({"op": "drop_tip"})
                tip_on = False

        elif stype == "wash":
            target = step.get("well") or f"{step.get('plate', 'plate')}:A1"
            well = _normalize_loc(str(target), loc_map)
            vol = _vol_to_ul(float(step["volume"]), str(step.get("unit", "uL")))
            cycles = int(step.get("cycles", 1))
            for _ in range(max(1, cycles)):
                ensure_tip(fresh=True)
                micro.append({"op": "aspirate", "loc": "res:1", "vol": vol})
                micro.append({"op": "dispense", "loc": well, "vol": vol})
                micro.append({"op": "aspirate", "loc": well, "vol": vol})
                micro.append({"op": "dispense", "loc": "res:12", "vol": vol})
                micro.append({"op": "drop_tip"})
                tip_on = False
                last_src, last_dst = None, None

        elif stype in ("incubate", "read"):
            continue
        else:
            raise ValueError(f"unsupported twinstep step type: {stype!r}")

    if tip_on:
        micro.append({"op": "drop_tip"})
    return micro


def to_twinstep(steps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Lift `lab-rehearsal` micro-steps into `twinstep` `transfer` and `mix` steps."""
    out: list[dict[str, Any]] = []
    i = 0
    n = len(steps)
    while i < n:
        s = steps[i]
        op = s.get("op")
        if op in ("pick_up_tip", "drop_tip"):
            i += 1
            continue
        if (
            op == "aspirate"
            and i + 1 < n
            and steps[i + 1].get("op") == "dispense"
            and float(s.get("vol", 0)) == float(steps[i + 1].get("vol", -1))
        ):
            out.append(
                {
                    "type": "transfer",
                    "source": str(s["loc"]),
                    "dest": str(steps[i + 1]["loc"]),
                    "volume": float(s["vol"]),
                    "unit": "uL",
                }
            )
            i += 2
            continue
        if op == "mix":
            out.append(
                {
                    "type": "mix",
                    "well": str(s["loc"]),
                    "volume": float(s["vol"]),
                    "unit": "uL",
                    "cycles": int(s.get("reps", 1)),
                }
            )
            i += 1
            continue
        raise ValueError(f"cannot lift unpaired micro-step at index {i + 1}: {s!r}")
    return out


def _advance_well_across_row(first_well: str, offset: int) -> str:
    prefix, coord = first_well.split(":", 1)
    row = coord[0]
    col = int(coord[1:]) + offset
    if col > 12:
        raise ValueError(f"dilution_series exceeds column 12 from {first_well}")
    return f"{prefix}:{row}{col}"


def from_stepbot(
    spec_data: str | Path | Mapping[str, Any],
) -> tuple[list[dict[str, Any]], DeckSpec]:
    """Compile a `stepbot` YAML/dict specification into `(micro_steps, DeckSpec)`."""
    if isinstance(spec_data, (str, Path)):
        text = Path(spec_data).read_text(encoding="utf-8") if Path(str(spec_data)).exists() else str(spec_data)
        try:
            import yaml  # type: ignore[import-not-found]

            doc = yaml.safe_load(text)
        except ImportError:
            doc = json.loads(text)
    else:
        doc = dict(spec_data)

    if not isinstance(doc, Mapping):
        raise ValueError("stepbot specification must be a mapping")

    pip_min, pip_max = DEFAULT_DECK_SPEC.pip_min, DEFAULT_DECK_SPEC.pip_max
    pipettes = doc.get("pipettes", {})
    if isinstance(pipettes, Mapping) and pipettes:
        first_pip = next(iter(pipettes.values()))
        model = str(first_pip.get("model", "p300_single_gen2")).lower()
        if "p20_" in model or model == "p20":
            pip_min, pip_max = 1.0, 20.0
        elif "p300_" in model or model == "p300":
            pip_min, pip_max = 20.0, 300.0
        elif "p1000_" in model or model == "p1000":
            pip_min, pip_max = 100.0, 1000.0

    start_map: dict[str, tuple[float, str]] = {}
    diluents = set(DEFAULT_DECK_SPEC.diluents)
    liquids = doc.get("liquids", {})
    if isinstance(liquids, Mapping):
        for liq_name, lspec in liquids.items():
            if not isinstance(lspec, Mapping):
                continue
            wells_raw = lspec.get("wells", [])
            wells_list = [wells_raw] if isinstance(wells_raw, str) else list(wells_raw)
            vol = float(lspec.get("volume", 0.0))
            for w in wells_list:
                norm = _normalize_loc(str(w))
                start_map[norm] = (vol, str(liq_name))
            if str(liq_name).lower() in ("buffer", "water", "pbs", "diluent"):
                diluents.add(str(liq_name).lower())

    deck_spec = DeckSpec(
        well_max=DEFAULT_DECK_SPEC.well_max,
        res_max=DEFAULT_DECK_SPEC.res_max,
        pip_min=pip_min,
        pip_max=pip_max,
        start=start_map if start_map else dict(DEFAULT_DECK_SPEC.start),
        diluents=frozenset(diluents),
    )

    micro: list[dict[str, Any]] = []
    for raw_step in doc.get("steps", []):
        if not isinstance(raw_step, Mapping):
            continue
        if "comment" in raw_step or "delay" in raw_step or "pause" in raw_step:
            continue
        if "pick_up_tip" in raw_step:
            micro.append({"op": "pick_up_tip"})
        elif "drop_tip" in raw_step:
            micro.append({"op": "drop_tip"})
        elif "transfer" in raw_step:
            cfg = raw_step["transfer"]
            srcs = [cfg["source"]] if isinstance(cfg["source"], str) else list(cfg["source"])
            dsts = [cfg["dest"]] if isinstance(cfg["dest"], str) else list(cfg["dest"])
            vol = float(cfg["volume"])
            new_tip = str(cfg.get("new_tip", "once"))
            mix_after = cfg.get("mix_after")
            if new_tip == "once":
                micro.append({"op": "pick_up_tip"})
            for src, dst in zip(srcs, dsts):
                if new_tip == "always":
                    micro.append({"op": "pick_up_tip"})
                nsrc = _normalize_loc(str(src))
                ndst = _normalize_loc(str(dst))
                micro.append({"op": "aspirate", "loc": nsrc, "vol": vol})
                micro.append({"op": "dispense", "loc": ndst, "vol": vol})
                if isinstance(mix_after, Mapping):
                    micro.append(
                        {
                            "op": "mix",
                            "loc": ndst,
                            "vol": float(mix_after["volume"]),
                            "reps": int(mix_after["reps"]),
                        }
                    )
                if new_tip == "always":
                    micro.append({"op": "drop_tip"})
            if new_tip == "once":
                micro.append({"op": "drop_tip"})
        elif "distribute" in raw_step:
            cfg = raw_step["distribute"]
            nsrc = _normalize_loc(str(cfg["source"]))
            dsts = [cfg["dest"]] if isinstance(cfg["dest"], str) else list(cfg["dest"])
            vol = float(cfg["volume"])
            micro.append({"op": "pick_up_tip"})
            for dst in dsts:
                ndst = _normalize_loc(str(dst))
                micro.append({"op": "aspirate", "loc": nsrc, "vol": vol})
                micro.append({"op": "dispense", "loc": ndst, "vol": vol})
            micro.append({"op": "drop_tip"})
        elif "mix" in raw_step:
            cfg = raw_step["mix"]
            wells = [cfg["wells"]] if isinstance(cfg["wells"], str) else list(cfg["wells"])
            vol = float(cfg["volume"])
            reps = int(cfg.get("reps", 3))
            for w in wells:
                micro.append({"op": "pick_up_tip"})
                micro.append({"op": "mix", "loc": _normalize_loc(str(w)), "vol": vol, "reps": reps})
                micro.append({"op": "drop_tip"})
        elif "discard" in raw_step:
            cfg = raw_step["discard"]
            nsrc = _normalize_loc(str(cfg["source"]))
            vol = float(cfg["volume"])
            micro.append({"op": "pick_up_tip"})
            micro.append({"op": "aspirate", "loc": nsrc, "vol": vol})
            micro.append({"op": "dispense", "loc": "trash:1", "vol": vol})
            micro.append({"op": "drop_tip"})
        elif "dilution_series" in raw_step:
            cfg = raw_step["dilution_series"]
            nsrc = _normalize_loc(str(cfg["source"]))
            ndil = _normalize_loc(str(cfg["diluent"]))
            first_well = _normalize_loc(str(cfg["first_well"]))
            n_wells = int(cfg["n"])
            factor = float(cfg.get("factor", 2.0))
            final_vol = float(cfg["final_volume"])
            mix_reps = int(cfg.get("mix_reps", 3))
            transfer_vol = round(final_vol / (factor - 1.0), 6)
            total_first = round(final_vol + transfer_vol, 6)
            wells = [_advance_well_across_row(first_well, k) for k in range(n_wells)]

            micro.append({"op": "pick_up_tip"})
            for w in wells[1:]:
                micro.append({"op": "aspirate", "loc": ndil, "vol": final_vol})
                micro.append({"op": "dispense", "loc": w, "vol": final_vol})
            micro.append({"op": "drop_tip"})

            micro.append({"op": "pick_up_tip"})
            micro.append({"op": "aspirate", "loc": nsrc, "vol": total_first})
            micro.append({"op": "dispense", "loc": wells[0], "vol": total_first})
            for idx in range(n_wells - 1):
                micro.append({"op": "aspirate", "loc": wells[idx], "vol": transfer_vol})
                micro.append({"op": "dispense", "loc": wells[idx + 1], "vol": transfer_vol})
                micro.append(
                    {"op": "mix", "loc": wells[idx + 1], "vol": final_vol, "reps": mix_reps}
                )
            micro.append({"op": "aspirate", "loc": wells[-1], "vol": transfer_vol})
            micro.append({"op": "dispense", "loc": "trash:1", "vol": transfer_vol})
            micro.append({"op": "drop_tip"})

    return micro, deck_spec


def to_stepbot(
    steps: Sequence[Mapping[str, Any]],
    *,
    name: str = "Exported lab-rehearsal protocol",
    spec: DeckSpec = DEFAULT_DECK_SPEC,
) -> dict[str, Any]:
    """Export `lab-rehearsal` micro-steps into a declarative `stepbot` protocol mapping."""
    sb_steps: list[dict[str, Any]] = []
    i = 0
    n = len(steps)
    while i < n:
        s = steps[i]
        op = s.get("op")
        if op == "pick_up_tip":
            sb_steps.append({"pick_up_tip": {"pipette": "left"}})
            i += 1
        elif op == "drop_tip":
            sb_steps.append({"drop_tip": {"pipette": "left"}})
            i += 1
        elif (
            op == "aspirate"
            and i + 1 < n
            and steps[i + 1].get("op") == "dispense"
            and float(s.get("vol", 0)) == float(steps[i + 1].get("vol", -1))
        ):
            dst = str(steps[i + 1]["loc"])
            if dst in ("trash:1",):
                sb_steps.append(
                    {"discard": {"pipette": "left", "source": str(s["loc"]), "volume": float(s["vol"])}}
                )
            else:
                sb_steps.append(
                    {
                        "transfer": {
                            "pipette": "left",
                            "source": str(s["loc"]),
                            "dest": dst,
                            "volume": float(s["vol"]),
                            "new_tip": "never",
                        }
                    }
                )
            i += 2
        elif op == "mix":
            sb_steps.append(
                {
                    "mix": {
                        "pipette": "left",
                        "wells": str(s["loc"]),
                        "volume": float(s["vol"]),
                        "reps": int(s.get("reps", 1)),
                    }
                }
            )
            i += 1
        else:
            raise ValueError(f"cannot export step {i + 1} to stepbot: {s!r}")

    liquids_out: dict[str, Any] = {}
    for loc, (vol, liq) in sorted(spec.start.items()):
        res_m = re.match(r"^res:(\d+)$", loc)
        sb_well = f"reservoir:A{res_m.group(1)}" if res_m else loc
        liquids_out[liq] = {"wells": sb_well, "volume": float(vol)}

    return {
        "metadata": {"name": name, "description": "Exported from lab-rehearsal"},
        "deck": {
            "plate": {"labware": "corning_96_wellplate_360ul_flat", "slot": 1},
            "reservoir": {"labware": "nest_12_reservoir_15ml", "slot": 2},
            "tips": {"labware": "opentrons_96_tiprack_300ul", "slot": 3},
        },
        "pipettes": {"left": {"model": "p300_single_gen2", "tip_racks": ["tips"]}},
        "liquids": liquids_out,
        "steps": sb_steps,
    }


def to_lablean(
    steps: Sequence[Mapping[str, Any]],
    *,
    spec: DeckSpec = DEFAULT_DECK_SPEC,
    pipette_name: str = "P300",
) -> dict[str, Any]:
    """Convert `lab-rehearsal` steps and deck into a `LabLean` (`YLM/lean`) JSON trace."""
    ll_steps: list[dict[str, Any]] = []
    i = 0
    n = len(steps)
    while i < n:
        s = steps[i]
        op = s.get("op")
        if op == "pick_up_tip":
            ll_steps.append({"kind": "pickTip"})
            i += 1
        elif op == "drop_tip":
            ll_steps.append({"kind": "dropTip"})
            i += 1
        elif (
            op == "aspirate"
            and i + 1 < n
            and steps[i + 1].get("op") == "dispense"
            and float(s.get("vol", 0)) == float(steps[i + 1].get("vol", -1))
        ):
            vol_tenths = int(round(float(s["vol"]) * 10))
            ll_steps.append(
                {
                    "kind": "transfer",
                    "pipette": {
                        "name": pipette_name,
                        "min_tenths": int(round(spec.pip_min * 10)),
                        "max_tenths": int(round(spec.pip_max * 10)),
                    },
                    "src": str(s["loc"]),
                    "dst": str(steps[i + 1]["loc"]),
                    "vol_tenths": vol_tenths,
                }
            )
            i += 2
        elif op == "mix":
            vol_tenths = int(round(float(s["vol"]) * 10))
            ll_steps.append(
                {
                    "kind": "mix",
                    "pipette": {
                        "name": pipette_name,
                        "min_tenths": int(round(spec.pip_min * 10)),
                        "max_tenths": int(round(spec.pip_max * 10)),
                    },
                    "container": str(s["loc"]),
                    "vol_tenths": vol_tenths,
                    "times": int(s.get("reps", 1)),
                }
            )
            i += 1
        else:
            raise ValueError(f"cannot convert unpaired step {i + 1} to LabLean: {s!r}")

    reagents = sorted({liq for _, liq in spec.start.values()})
    return {
        "reagents": [
            {"name": r, "state": "roomTemp", "diluent": r.lower() in spec.diluents}
            for r in reagents
        ],
        "steps": ll_steps,
    }


def from_lablean(trace_doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Lower a `LabLean` step trace (`pickTip`, `dropTip`, `transfer`, `mix`) to `lab-rehearsal`."""
    micro: list[dict[str, Any]] = []
    for st in trace_doc.get("steps", []):
        kind = st.get("kind")
        if kind == "pickTip":
            micro.append({"op": "pick_up_tip"})
        elif kind == "dropTip":
            micro.append({"op": "drop_tip"})
        elif kind == "transfer":
            vol = float(st["vol_tenths"]) / 10.0
            micro.append({"op": "aspirate", "loc": _normalize_loc(str(st["src"])), "vol": vol})
            micro.append({"op": "dispense", "loc": _normalize_loc(str(st["dst"])), "vol": vol})
        elif kind == "mix":
            vol = float(st["vol_tenths"]) / 10.0
            micro.append(
                {
                    "op": "mix",
                    "loc": _normalize_loc(str(st["container"])),
                    "vol": vol,
                    "reps": max(1, int(st.get("times", 1))),
                }
            )
    return micro


def evaluate_twinstep_jsonl(
    jsonl_path: str | Path,
    *,
    spec: DeckSpec = DEFAULT_DECK_SPEC,
) -> dict[str, Any]:
    """Evaluate `verify()` precision and recall on a `twinstep` error-twin JSONL benchmark."""
    tp = fp = tn = fn = 0
    details: list[dict[str, Any]] = []
    with open(jsonl_path, "r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            item = json.loads(line)
            has_error = bool(item.get("is_corrupted", item.get("has_error", False)))
            steps = item["steps"]
            if steps and isinstance(steps[0], Mapping) and "type" in steps[0]:
                micro = from_twinstep(steps, tip_policy=str(item.get("tip_policy", "smart")))
            else:
                micro = list(steps)
            trace, end = rehearse(micro, spec=spec)
            approved, violations = verify(trace, end)
            flagged = not approved
            if has_error and flagged:
                tp += 1
            elif not has_error and flagged:
                fp += 1
            elif not has_error and not flagged:
                tn += 1
            else:
                fn += 1
            details.append(
                {
                    "id": item.get("id"),
                    "has_error": has_error,
                    "flagged": flagged,
                    "violations": [str(v) for v in violations],
                }
            )
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    return {
        "total": tp + fp + tn + fn,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "details": details,
    }
