"""Bidirectional protocol IR adapters for `stepbot`, `twinstep`, and `YLM/lean` (`LabLean`)."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .core import (
    DEFAULT_DECK_SPEC,
    VOL_EPS,
    DeckSpec,
    _clean_vol,
    rehearse,
    to_tenths_exact,
    verify,
)

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
    "fixed_trash": "trash:1",
}

_COMMENT_SEAL_RE = re.compile(r"^seal\s+(\S+)$", re.IGNORECASE)
_COMMENT_THAW_RE = re.compile(r"^thaw\s+(\S+)$", re.IGNORECASE)
_COMMENT_INCUBATE_RE = re.compile(
    r"^incubate\s+(\S+)\s+at\s+([+-]?\d+(?:\.\d+)?)C\s+for\s+(\d+(?:\.\d+)?)s$",
    re.IGNORECASE,
)


def _normalize_loc(raw_loc: str, loc_map: Mapping[str, str] | None = None) -> str:
    merged = dict(DEFAULT_TWINSTEP_LOC_MAP)
    if loc_map:
        merged.update(loc_map)
    if raw_loc in merged:
        return merged[raw_loc]
    if raw_loc == "plate":
        return "plate"
    # Normalize Opentrons API 2.16 wire slots: `2:A1..A12` -> `res:1..12`, `3:A1..H12` -> `plate:A1..H12`
    m_wire_res = re.match(r"^(?:slot_)?2:[A-H](1[0-2]|[1-9])$", raw_loc)
    if m_wire_res:
        return f"res:{m_wire_res.group(1)}"
    m_wire_plate = re.match(r"^(?:slot_)?3:([A-H](?:1[0-2]|[1-9]))$", raw_loc)
    if m_wire_plate:
        return f"plate:{m_wire_plate.group(1)}"
    m_wire_tip = re.match(r"^(?:slot_)?1:([A-H](?:1[0-2]|[1-9]))$", raw_loc)
    if m_wire_tip:
        return f"tiprack:{m_wire_tip.group(1)}"
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


def _to_wire_well(loc: str) -> str:
    """Map `lab-rehearsal` container IDs onto Opentrons API 2.16 wire slot coordinates."""
    if loc == "plate":
        return "3:A1"
    m_res = re.match(r"^res:(1[0-2]|[1-9])$", loc)
    if m_res:
        return f"2:A{m_res.group(1)}"
    m_plate = re.match(r"^plate:([A-H](?:1[0-2]|[1-9]))$", loc)
    if m_plate:
        return f"3:{m_plate.group(1)}"
    if loc == "trash:1":
        return "trash"
    return loc


def _to_stepbot_alias_well(loc: str) -> str:
    if loc == "plate":
        return "plate:A1"
    m_res = re.match(r"^res:(1[0-2]|[1-9])$", loc)
    if m_res:
        return f"reservoir:A{m_res.group(1)}"
    return loc


def _vol_to_ul(vol: float, unit: str = "uL") -> float:
    if unit == "mL":
        return float(vol) * 1000.0
    if unit == "uL":
        return float(vol)
    raise ValueError(f"unsupported volume unit: {unit!r}")


def _extract_twinstep_loc(step: Mapping[str, Any], loc_map: Mapping[str, str] | None) -> str:
    if "well" in step and step["well"]:
        return _normalize_loc(str(step["well"]), loc_map)
    if "loc" in step and step["loc"]:
        return _normalize_loc(str(step["loc"]), loc_map)
    if "plate" in step and step["plate"]:
        raw_p = str(step["plate"])
        if raw_p == "plate":
            return "plate"
        if ":" in raw_p:
            return _normalize_loc(raw_p, loc_map)
        return _normalize_loc(f"{raw_p}:A1", loc_map)
    return "plate:A1"


def from_twinstep(
    protocol_or_steps: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    loc_map: Mapping[str, str] | None = None,
    tip_policy: str = "per_transfer",
) -> list[dict[str, Any]]:
    """Lower `twinstep` macro-steps (`transfer`, `mix`, `wash`, `incubate`, `seal`, `thaw`) into micro-steps.

    Optical `read` steps do not mutate liquid state and are skipped cleanly. Any mounted tip
    is dropped before `incubate`, `seal`, or `thaw` transitions.
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

    def drop_if_mounted() -> None:
        nonlocal tip_on, last_src, last_dst
        if tip_on:
            micro.append({"op": "drop_tip"})
            tip_on = False
        last_src, last_dst = None, None

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

        elif stype == "incubate":
            drop_if_mounted()
            loc = _extract_twinstep_loc(step, loc_map)
            temp_c = float(step.get("temp_c", step.get("temperature", step.get("temp", 37.0))))
            if "duration_s" in step:
                dur_s = float(step["duration_s"])
            elif "duration_sec" in step:
                dur_s = float(step["duration_sec"])
            elif "duration_min" in step:
                dur_s = float(step["duration_min"]) * 60.0
            elif "duration" in step:
                mult = 60.0 if str(step.get("unit", "s")).lower() in ("min", "minutes", "m") else 1.0
                dur_s = float(step["duration"]) * mult
            else:
                dur_s = 60.0
            micro.append({"op": "incubate", "loc": loc, "temp_c": temp_c, "duration_s": dur_s})

        elif stype in ("seal", "seal_well", "sealWell"):
            drop_if_mounted()
            loc = _extract_twinstep_loc(step, loc_map)
            micro.append({"op": "seal", "loc": loc})

        elif stype in ("thaw", "thaw_well", "thawWell"):
            drop_if_mounted()
            loc = _extract_twinstep_loc(step, loc_map)
            micro.append({"op": "thaw", "loc": loc})

        elif stype == "read":
            continue
        else:
            raise ValueError(f"unsupported twinstep step type: {stype!r}")

    if tip_on:
        micro.append({"op": "drop_tip"})
    return micro


def to_twinstep(steps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Lift `lab-rehearsal` micro-steps into `twinstep` macro-steps (`transfer`, `mix`, `incubate`, `seal`, `thaw`)."""
    out: list[dict[str, Any]] = []
    i = 0
    n = len(steps)
    while i < n:
        s = steps[i]
        op = s.get("op")
        if op in ("pick_up_tip", "drop_tip"):
            i += 1
            continue
        if op == "aspirate":
            src = str(s["loc"])
            asp_vol = float(s["vol"])
            if (
                i + 1 < n
                and steps[i + 1].get("op") == "dispense"
                and abs(asp_vol - float(steps[i + 1].get("vol", -1))) <= 1e-6
            ):
                out.append(
                    {
                        "type": "transfer",
                        "source": src,
                        "dest": str(steps[i + 1]["loc"]),
                        "volume": asp_vol,
                        "unit": "uL",
                    }
                )
                i += 2
                continue
            # Support multi-dispense sequences (`aspirate V` -> `dispense v1` .. `dispense vk`)
            j = i + 1
            disp_sum = 0.0
            sub_disps: list[tuple[str, float]] = []
            while j < n and steps[j].get("op") == "dispense":
                dv = float(steps[j]["vol"])
                sub_disps.append((str(steps[j]["loc"]), dv))
                disp_sum += dv
                j += 1
            if sub_disps and disp_sum <= asp_vol + 1e-6:
                for dst_loc, dv in sub_disps:
                    out.append(
                        {
                            "type": "transfer",
                            "source": src,
                            "dest": dst_loc,
                            "volume": dv,
                            "unit": "uL",
                        }
                    )
                i = j
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
        if op == "incubate":
            out.append(
                {
                    "type": "incubate",
                    "well": str(s["loc"]),
                    "temp_c": float(s["temp_c"]),
                    "duration_s": float(s["duration_s"]),
                }
            )
            i += 1
            continue
        if op == "seal":
            out.append({"type": "seal", "well": str(s["loc"])})
            i += 1
            continue
        if op == "thaw":
            out.append({"type": "thaw", "well": str(s["loc"])})
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


def _parse_pipette_model_bounds(model_str: str, explicit_channels: Any = None) -> tuple[float, float, int]:
    m = model_str.lower()
    pip_min, pip_max = DEFAULT_DECK_SPEC.pip_min, DEFAULT_DECK_SPEC.pip_max
    if "p20_" in m or m == "p20" or "p20" in m:
        pip_min, pip_max = 1.0, 20.0
    elif "p50_" in m or m == "p50":
        pip_min, pip_max = 5.0, 50.0
    elif "p300_" in m or m == "p300" or "p300" in m:
        pip_min, pip_max = 20.0, 300.0
    elif "p1000_" in m or m == "p1000" or "p1000" in m:
        pip_min, pip_max = 100.0, 1000.0

    channels = 8 if "multi" in m else 1
    if isinstance(explicit_channels, int) and not isinstance(explicit_channels, bool) and explicit_channels in (1, 8):
        channels = explicit_channels
    return pip_min, pip_max, channels


def _extract_stepbot_step_kind_and_cfg(raw_step: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    if "action" in raw_step:
        kind = str(raw_step["action"])
        cfg = {k: v for k, v in raw_step.items() if k != "action"}
        return kind, cfg
    if "type" in raw_step:
        kind = str(raw_step["type"])
        cfg = {k: v for k, v in raw_step.items() if k != "type"}
        return kind, cfg
    if len(raw_step) == 1:
        k, v = next(iter(raw_step.items()))
        if isinstance(v, Mapping):
            return str(k), dict(v)
        if k == "comment":
            return "comment", {"text": str(v)}
        if k in ("delay", "pause") and isinstance(v, (int, float)):
            return str(k), {"seconds": float(v)}
    return "", {}


def from_stepbot(
    spec_data: str | Path | Mapping[str, Any],
) -> tuple[list[dict[str, Any]], DeckSpec]:
    """Compile a `stepbot` YAML/dict specification (including `autolab-wire/v1` dicts) into `(micro_steps, DeckSpec)`."""
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

    pipette_specs: dict[str, tuple[float, float, int]] = {}
    pipettes = doc.get("pipettes", {})
    if isinstance(pipettes, Mapping) and pipettes:
        for mount, pentry in pipettes.items():
            if not isinstance(pentry, Mapping):
                continue
            raw_model = str(
                pentry.get("model")
                or pentry.get("instrument_name")
                or pentry.get("name")
                or "p300_single_gen2"
            )
            pipette_specs[str(mount)] = _parse_pipette_model_bounds(
                raw_model, pentry.get("channels")
            )

    if pipette_specs:
        default_mount = next(iter(pipette_specs))
        pip_min, pip_max, default_channels = pipette_specs[default_mount]
    else:
        default_mount = "left"
        pip_min, pip_max, default_channels = (
            DEFAULT_DECK_SPEC.pip_min,
            DEFAULT_DECK_SPEC.pip_max,
            1,
        )

    start_map: dict[str, tuple[float, str]] = {}
    diluents = set(DEFAULT_DECK_SPEC.diluents)
    liquids = doc.get("liquids", {})
    if isinstance(liquids, Mapping):
        for liq_name, lspec in liquids.items():
            if not isinstance(lspec, Mapping):
                continue
            wells_raw = lspec.get("wells", lspec.get("well", []))
            wells_list = [wells_raw] if isinstance(wells_raw, str) else list(wells_raw)
            vol = float(lspec.get("volume", 0.0))
            for w in wells_list:
                norm = _normalize_loc(str(w))
                start_map[norm] = (vol, str(liq_name))
            if str(liq_name).lower() in ("buffer", "water", "pbs", "diluent"):
                diluents.add(str(liq_name).lower())

    multi_mount = len(pipette_specs) > 1
    deck_spec = DeckSpec(
        well_max=DEFAULT_DECK_SPEC.well_max,
        res_max=DEFAULT_DECK_SPEC.res_max,
        pip_min=pip_min,
        pip_max=pip_max,
        channels=default_channels,
        pipettes=pipette_specs if multi_mount else {},
        start=start_map if start_map else dict(DEFAULT_DECK_SPEC.start),
        diluents=frozenset(diluents),
    )

    def _step_meta(cfg: Mapping[str, Any]) -> dict[str, Any]:
        mount = str(cfg.get("pipette", default_mount))
        _, _, ch = pipette_specs.get(mount, (pip_min, pip_max, default_channels))
        if isinstance(cfg.get("channels"), int) and not isinstance(cfg.get("channels"), bool):
            ch = int(cfg["channels"])
        meta: dict[str, Any] = {}
        if ch == 8:
            meta["channels"] = 8
        if multi_mount and mount in pipette_specs:
            meta["pipette"] = mount
        return meta

    micro: list[dict[str, Any]] = []
    tip_mounted = False
    tip_meta: dict[str, Any] = {}
    raw_steps_list = list(doc.get("steps", []))
    idx_s = 0
    while idx_s < len(raw_steps_list):
        raw_step = raw_steps_list[idx_s]
        if not isinstance(raw_step, Mapping):
            idx_s += 1
            continue
        kind, cfg = _extract_stepbot_step_kind_and_cfg(raw_step)

        if kind == "comment":
            text = str(cfg.get("text", cfg.get("message", ""))).strip()
            m_seal = _COMMENT_SEAL_RE.match(text)
            if m_seal:
                if tip_mounted:
                    micro.append({"op": "drop_tip", **tip_meta})
                    tip_mounted = False
                micro.append({"op": "seal", "loc": _normalize_loc(m_seal.group(1))})
                idx_s += 1
                continue
            m_thaw = _COMMENT_THAW_RE.match(text)
            if m_thaw:
                if tip_mounted:
                    micro.append({"op": "drop_tip", **tip_meta})
                    tip_mounted = False
                micro.append({"op": "thaw", "loc": _normalize_loc(m_thaw.group(1))})
                idx_s += 1
                continue
            m_inc = _COMMENT_INCUBATE_RE.match(text)
            if m_inc:
                if tip_mounted:
                    micro.append({"op": "drop_tip", **tip_meta})
                    tip_mounted = False
                inc_loc = _normalize_loc(m_inc.group(1))
                inc_temp = float(m_inc.group(2))
                inc_dur = float(m_inc.group(3))
                # Consume paired `delay` step immediately following an incubate annotation
                if idx_s + 1 < len(raw_steps_list) and isinstance(raw_steps_list[idx_s + 1], Mapping):
                    next_kind, _ = _extract_stepbot_step_kind_and_cfg(raw_steps_list[idx_s + 1])
                    if next_kind == "delay":
                        idx_s += 1
                micro.append(
                    {
                        "op": "incubate",
                        "loc": inc_loc,
                        "temp_c": inc_temp,
                        "duration_s": inc_dur,
                    }
                )
                idx_s += 1
                continue
            idx_s += 1
            continue

        if kind in ("delay", "pause"):
            idx_s += 1
            continue

        meta = _step_meta(cfg)

        if kind == "pick_up_tip":
            p_step: dict[str, Any] = {"op": "pick_up_tip", **meta}
            if "loc" in cfg and cfg["loc"]:
                p_step["loc"] = _normalize_loc(str(cfg["loc"]))
            micro.append(p_step)
            tip_mounted = True
            tip_meta = dict(meta)

        elif kind == "drop_tip":
            micro.append({"op": "drop_tip", **meta})
            tip_mounted = False
            tip_meta = {}

        elif kind == "incubate":
            if tip_mounted:
                micro.append({"op": "drop_tip", **tip_meta})
                tip_mounted = False
            w_raw = cfg.get("well", cfg.get("loc", cfg.get("wells", "plate:A1")))
            dur_s = float(cfg.get("duration_s", float(cfg.get("minutes", 0.0)) * 60.0 + float(cfg.get("seconds", 0.0))))
            micro.append(
                {
                    "op": "incubate",
                    "loc": _normalize_loc(str(w_raw)),
                    "temp_c": float(cfg.get("temp_c", cfg.get("temperature", 37.0))),
                    "duration_s": dur_s if dur_s > 0 else 60.0,
                }
            )

        elif kind in ("seal", "sealWell"):
            if tip_mounted:
                micro.append({"op": "drop_tip", **tip_meta})
                tip_mounted = False
            w_raw = cfg.get("well", cfg.get("loc", cfg.get("wells", "plate:A1")))
            micro.append({"op": "seal", "loc": _normalize_loc(str(w_raw))})

        elif kind in ("thaw", "thawWell"):
            if tip_mounted:
                micro.append({"op": "drop_tip", **tip_meta})
                tip_mounted = False
            w_raw = cfg.get("well", cfg.get("loc", cfg.get("wells", "plate:A1")))
            micro.append({"op": "thaw", "loc": _normalize_loc(str(w_raw))})

        elif kind == "transfer":
            raw_src = cfg.get("source", cfg.get("src"))
            raw_dst = cfg.get("dest", cfg.get("dst", cfg.get("destination")))
            srcs = [raw_src] if isinstance(raw_src, str) else list(raw_src)  # type: ignore[arg-type]
            dsts = [raw_dst] if isinstance(raw_dst, str) else list(raw_dst)  # type: ignore[arg-type]
            if len(srcs) == 1 and len(dsts) > 1:
                srcs = srcs * len(dsts)
            elif len(dsts) == 1 and len(srcs) > 1:
                dsts = dsts * len(srcs)
            vol = float(cfg["volume"])
            new_tip = str(cfg.get("new_tip", "once"))
            mix_after = cfg.get("mix_after")
            disp_top = str(cfg.get("dispense_position", "bottom")) == "top"

            if new_tip == "once":
                if tip_mounted:
                    micro.append({"op": "drop_tip", **tip_meta})
                micro.append({"op": "pick_up_tip", **meta})
                tip_mounted = False
            elif new_tip == "never":
                if not tip_mounted:
                    micro.append({"op": "pick_up_tip", **meta})
                    tip_mounted = True
                    tip_meta = dict(meta)
            elif new_tip == "always":
                if tip_mounted:
                    micro.append({"op": "drop_tip", **tip_meta})
                    tip_mounted = False

            for src, dst in zip(srcs, dsts):
                if new_tip == "always":
                    micro.append({"op": "pick_up_tip", **meta})
                nsrc = _normalize_loc(str(src))
                ndst = _normalize_loc(str(dst))
                micro.append({"op": "aspirate", "loc": nsrc, "vol": vol, **meta})
                d_step: dict[str, Any] = {"op": "dispense", "loc": ndst, "vol": vol, **meta}
                if disp_top:
                    d_step["air_dispense"] = True
                micro.append(d_step)
                if isinstance(mix_after, Mapping):
                    micro.append(
                        {
                            "op": "mix",
                            "loc": ndst,
                            "vol": float(mix_after["volume"]),
                            "reps": int(mix_after.get("reps", mix_after.get("repetitions", 1))),
                            **meta,
                        }
                    )
                if new_tip == "always":
                    micro.append({"op": "drop_tip", **meta})
            if new_tip == "once":
                micro.append({"op": "drop_tip", **meta})

        elif kind == "distribute":
            raw_src = cfg.get("source", cfg.get("src"))
            raw_dst = cfg.get("dest", cfg.get("dst", cfg.get("destination")))
            nsrc = _normalize_loc(str(raw_src))
            dsts = [raw_dst] if isinstance(raw_dst, str) else list(raw_dst)  # type: ignore[arg-type]
            vol = float(cfg["volume"])
            disp_vol = float(cfg.get("disposal_volume", 0.0) or 0.0)
            disp_top = str(cfg.get("dispense_position", "top")) == "top"
            new_tip = str(cfg.get("new_tip", "once"))
            if new_tip == "once":
                if tip_mounted:
                    micro.append({"op": "drop_tip", **tip_meta})
                micro.append({"op": "pick_up_tip", **meta})
                tip_mounted = False
            elif new_tip == "never" and not tip_mounted:
                micro.append({"op": "pick_up_tip", **meta})
                tip_mounted = True
                tip_meta = dict(meta)

            total_v = _clean_vol(vol * len(dsts) + disp_vol)
            if len(dsts) >= 2 and total_v <= pip_max + VOL_EPS:
                micro.append({"op": "aspirate", "loc": nsrc, "vol": total_v, **meta})
                for dst in dsts:
                    ndst = _normalize_loc(str(dst))
                    d_dist: dict[str, Any] = {"op": "dispense", "loc": ndst, "vol": vol, **meta}
                    if disp_top:
                        d_dist["air_dispense"] = True
                    micro.append(d_dist)
                if disp_vol > VOL_EPS:
                    micro.append({"op": "dispense", "loc": "trash:1", "vol": disp_vol, **meta})
            else:
                for dst in dsts:
                    ndst = _normalize_loc(str(dst))
                    micro.append({"op": "aspirate", "loc": nsrc, "vol": vol, **meta})
                    d_dist2: dict[str, Any] = {"op": "dispense", "loc": ndst, "vol": vol, **meta}
                    if disp_top:
                        d_dist2["air_dispense"] = True
                    micro.append(d_dist2)

            if new_tip == "once":
                micro.append({"op": "drop_tip", **meta})

        elif kind == "mix":
            raw_wells = cfg.get("wells", cfg.get("well"))
            wells = [raw_wells] if isinstance(raw_wells, str) else list(raw_wells)  # type: ignore[arg-type]
            vol = float(cfg["volume"])
            reps = int(cfg.get("reps", cfg.get("repetitions", cfg.get("cycles", 3))))
            new_tip = str(cfg.get("new_tip", "once"))
            if new_tip == "never":
                if not tip_mounted:
                    micro.append({"op": "pick_up_tip", **meta})
                    tip_mounted = True
                    tip_meta = dict(meta)
                for w in wells:
                    micro.append({"op": "mix", "loc": _normalize_loc(str(w)), "vol": vol, "reps": reps, **meta})
            elif new_tip == "once":
                if tip_mounted:
                    micro.append({"op": "drop_tip", **tip_meta})
                    tip_mounted = False
                micro.append({"op": "pick_up_tip", **meta})
                for w in wells:
                    micro.append({"op": "mix", "loc": _normalize_loc(str(w)), "vol": vol, "reps": reps, **meta})
                micro.append({"op": "drop_tip", **meta})
            else:
                if tip_mounted:
                    micro.append({"op": "drop_tip", **tip_meta})
                    tip_mounted = False
                for w in wells:
                    micro.append({"op": "pick_up_tip", **meta})
                    micro.append({"op": "mix", "loc": _normalize_loc(str(w)), "vol": vol, "reps": reps, **meta})
                    micro.append({"op": "drop_tip", **meta})

        elif kind == "discard":
            raw_src = cfg.get("source", cfg.get("src"))
            nsrc = _normalize_loc(str(raw_src))
            vol = float(cfg["volume"])
            new_tip = str(cfg.get("new_tip", "once"))
            if new_tip == "never":
                if not tip_mounted:
                    micro.append({"op": "pick_up_tip", **meta})
                    tip_mounted = True
                    tip_meta = dict(meta)
                micro.append({"op": "aspirate", "loc": nsrc, "vol": vol, **meta})
                micro.append({"op": "dispense", "loc": "trash:1", "vol": vol, **meta})
            else:
                if tip_mounted:
                    micro.append({"op": "drop_tip", **tip_meta})
                    tip_mounted = False
                micro.append({"op": "pick_up_tip", **meta})
                micro.append({"op": "aspirate", "loc": nsrc, "vol": vol, **meta})
                micro.append({"op": "dispense", "loc": "trash:1", "vol": vol, **meta})
                micro.append({"op": "drop_tip", **meta})

        elif kind == "dilution_series":
            nsrc = _normalize_loc(str(cfg.get("source", cfg.get("src"))))
            ndil = _normalize_loc(str(cfg["diluent"]))
            first_well = _normalize_loc(str(cfg["first_well"]))
            n_wells = int(cfg.get("n", cfg.get("count", 2)))
            factor = float(cfg.get("factor", 2.0))
            final_vol = float(cfg["final_volume"])
            mix_reps = int(cfg.get("mix_reps", 3))
            transfer_vol = round(final_vol / (factor - 1.0), 6)
            total_first = round(final_vol + transfer_vol, 6)
            wells = [_advance_well_across_row(first_well, k) for k in range(n_wells)]

            if tip_mounted:
                micro.append({"op": "drop_tip", **tip_meta})
                tip_mounted = False
            micro.append({"op": "pick_up_tip", **meta})
            for w in wells[1:]:
                micro.append({"op": "aspirate", "loc": ndil, "vol": final_vol, **meta})
                micro.append({"op": "dispense", "loc": w, "vol": final_vol, **meta})
            micro.append({"op": "drop_tip", **meta})

            micro.append({"op": "pick_up_tip", **meta})
            micro.append({"op": "aspirate", "loc": nsrc, "vol": total_first, **meta})
            micro.append({"op": "dispense", "loc": wells[0], "vol": total_first, **meta})
            for idx in range(n_wells - 1):
                micro.append({"op": "aspirate", "loc": wells[idx], "vol": transfer_vol, **meta})
                micro.append({"op": "dispense", "loc": wells[idx + 1], "vol": transfer_vol, **meta})
                micro.append(
                    {"op": "mix", "loc": wells[idx + 1], "vol": final_vol, "reps": mix_reps, **meta}
                )
            micro.append({"op": "aspirate", "loc": wells[-1], "vol": transfer_vol, **meta})
            micro.append({"op": "dispense", "loc": "trash:1", "vol": transfer_vol, **meta})
            micro.append({"op": "drop_tip", **meta})

        idx_s += 1

    if tip_mounted:
        micro.append({"op": "drop_tip", **tip_meta})

    return micro, deck_spec


def _expand_8ch_wire_ref(loc: str, wire_format: bool) -> str | list[str]:
    m_plate = re.match(r"^plate:A(1[0-2]|[1-9])$", loc)
    if m_plate:
        col = m_plate.group(1)
        prefix = "3" if wire_format else "plate"
        return [f"{prefix}:{r}{col}" for r in "ABCDEFGH"]
    return _to_wire_well(loc) if wire_format else _to_stepbot_alias_well(loc)


def _has_heterogeneous_plate_columns(spec: DeckSpec) -> bool:
    """Return True iff any 96-well plate column has non-uniform initial volume or liquid species."""
    for col in range(1, 13):
        entries = [spec.start.get(f"plate:{r}{col}", (0.0, "")) for r in "ABCDEFGH"]
        first_v, first_l = entries[0]
        for v, l in entries[1:]:
            if abs(float(v) - float(first_v)) > 1e-6 or str(l) != str(first_l):
                return True
    return False


def _coalesce_wire_block_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Coalesce sequential transfer+mix and homogeneous-volume transfers inside a tip block."""
    if len(actions) <= 1:
        return list(actions)

    # Pass 1: fold immediate `mix` on single destination well into preceding `transfer.mix_after`
    pass1: list[dict[str, Any]] = []
    idx = 0
    while idx < len(actions):
        cur = dict(actions[idx])
        if (
            cur.get("action") == "transfer"
            and "mix_after" not in cur
            and idx + 1 < len(actions)
            and actions[idx + 1].get("action") == "mix"
            and actions[idx + 1].get("pipette") == cur.get("pipette")
            and actions[idx + 1].get("wells") == cur.get("dest")
        ):
            nxt = actions[idx + 1]
            cur["mix_after"] = {
                "reps": int(nxt["reps"]),
                "volume": float(nxt["volume"]),
            }
            pass1.append(cur)
            idx += 2
            continue
        pass1.append(cur)
        idx += 1

    if len(pass1) <= 1:
        return pass1

    # Pass 2: fold consecutive `transfer` steps with identical pipette, volume & dispense_position
    pass2: list[dict[str, Any]] = []
    for act in pass1:
        if (
            pass2
            and pass2[-1].get("action") == "transfer"
            and act.get("action") == "transfer"
            and "mix_after" not in pass2[-1]
            and "mix_after" not in act
            and pass2[-1].get("pipette") == act.get("pipette")
            and abs(float(pass2[-1]["volume"]) - float(act["volume"])) <= 1e-6
            and pass2[-1].get("dispense_position", "bottom") == act.get("dispense_position", "bottom")
        ):
            prev = pass2[-1]
            p_src = prev["source"]
            p_dst = prev["dest"]
            a_src = act["source"]
            a_dst = act["dest"]
            p_srcs = [p_src] if isinstance(p_src, str) else list(p_src)
            p_dsts = [p_dst] if isinstance(p_dst, str) else list(p_dst)
            a_srcs = [a_src] if isinstance(a_src, str) else list(a_src)
            a_dsts = [a_dst] if isinstance(a_dst, str) else list(a_dst)
            if len(p_srcs) == 1 and len(p_dsts) > 1:
                p_srcs = p_srcs * len(p_dsts)
            elif len(p_dsts) == 1 and len(p_srcs) > 1:
                p_dsts = p_dsts * len(p_srcs)
            if len(a_srcs) == 1 and len(a_dsts) > 1:
                a_srcs = a_srcs * len(a_dsts)
            elif len(a_dsts) == 1 and len(a_srcs) > 1:
                a_dsts = a_dsts * len(a_srcs)
            if len(p_srcs) == len(p_dsts) and len(a_srcs) == len(a_dsts):
                merged_srcs = p_srcs + a_srcs
                merged_dsts = p_dsts + a_dsts
                prev["source"] = merged_srcs[0] if len(set(merged_srcs)) == 1 else merged_srcs
                prev["dest"] = merged_dsts[0] if len(merged_dsts) == 1 else merged_dsts
                continue
        pass2.append(dict(act))

    return pass2


def _well_for_row(loc: str, row_ch: str, fmt_well_fn: Any) -> str:
    m_plate = re.match(r"^plate:A(1[0-2]|[1-9])$", loc)
    if m_plate:
        return f"3:{row_ch}{m_plate.group(1)}"
    return fmt_well_fn(loc)


def to_stepbot(
    steps: Sequence[Mapping[str, Any]],
    *,
    name: str = "Exported lab-rehearsal protocol",
    spec: DeckSpec = DEFAULT_DECK_SPEC,
    wire_format: bool = False,
    pipette_model: str | None = None,
) -> dict[str, Any]:
    """Export `lab-rehearsal` micro-steps into a declarative `stepbot` protocol mapping.

    When `wire_format=True`, emits the Opentrons API `2.16` `autolab-wire/v1` dialect
    (`deck["1".."3"]`, `pipettes.right.instrument_name`, `steps[].action`) directly
    consumable by `stepbot.autolab_wire.compile_stage` and `lab-verify`.
    """
    mount = "right" if wire_format else "left"
    fmt_well = _to_wire_well if wire_format else _to_stepbot_alias_well

    pip_ops = [
        s
        for s in steps
        if isinstance(s, Mapping)
        and s.get("op") in ("pick_up_tip", "drop_tip", "aspirate", "dispense", "mix")
    ]
    has_8ch = any(int(s.get("channels", spec.channels)) == 8 for s in pip_ops)
    has_1ch = any(int(s.get("channels", spec.channels)) == 1 for s in pip_ops)

    if pipette_model is not None:
        resolved_model = pipette_model
    elif spec.pip_max <= 20.0 + VOL_EPS:
        resolved_model = "p20_multi_gen2" if (has_8ch and not has_1ch) else "p20_single_gen2"
    elif spec.pip_max > 300.0 + VOL_EPS:
        resolved_model = "p1000_single_gen2"
    else:
        resolved_model = "p300_multi_gen2" if (has_8ch and not has_1ch) else "p300_single_gen2"

    # Keep native 8-channel pipette when all steps are 8-channel and columns are homogeneous;
    # otherwise lower heterogeneous 8-channel columns onto per-row single-channel steps with
    # fresh tips (`new_tip="always"` / per-row `"once"`) so distinct rows never share one tip.
    use_native_8ch = bool(
        wire_format
        and "multi" in resolved_model
        and has_8ch
        and not has_1ch
        and not _has_heterogeneous_plate_columns(spec)
    )
    if wire_format and "multi" in resolved_model and not use_native_8ch:
        resolved_model = resolved_model.replace("multi", "single")

    if not wire_format:
        sb_steps: list[dict[str, Any]] = []
        i = 0
        n = len(steps)
        while i < n:
            s = steps[i]
            op = s.get("op")
            ch = int(s.get("channels", spec.channels))
            if op == "pick_up_tip":
                sb_steps.append({"pick_up_tip": {"pipette": mount}})
                i += 1
            elif op == "drop_tip":
                sb_steps.append({"drop_tip": {"pipette": mount}})
                i += 1
            elif op == "aspirate":
                asp_vol = float(s["vol"])
                src_ref = _expand_8ch_wire_ref(str(s["loc"]), False) if ch == 8 else fmt_well(str(s["loc"]))
                if (
                    i + 1 < n
                    and steps[i + 1].get("op") == "dispense"
                    and abs(asp_vol - float(steps[i + 1].get("vol", -1))) <= 1e-6
                ):
                    dst_raw = str(steps[i + 1]["loc"])
                    dst_ref = _expand_8ch_wire_ref(dst_raw, False) if ch == 8 else fmt_well(dst_raw)
                    if dst_raw == "trash:1":
                        sb_steps.append(
                            {"discard": {"pipette": mount, "source": src_ref, "volume": asp_vol}}
                        )
                    else:
                        sb_steps.append(
                            {
                                "transfer": {
                                    "pipette": mount,
                                    "source": src_ref,
                                    "dest": dst_ref,
                                    "volume": asp_vol,
                                    "new_tip": "never",
                                }
                            }
                        )
                    i += 2
                    continue
                # Multi-dispense (`aspirate V` -> `dispense v1..vk`)
                j = i + 1
                sub_disps: list[tuple[str, float]] = []
                disp_sum = 0.0
                while j < n and steps[j].get("op") == "dispense":
                    dv = float(steps[j]["vol"])
                    sub_disps.append((str(steps[j]["loc"]), dv))
                    disp_sum += dv
                    j += 1
                if sub_disps and disp_sum <= asp_vol + 1e-6:
                    first_dv = sub_disps[0][1]
                    if len(sub_disps) >= 2 and all(abs(dv - first_dv) <= 1e-6 for _, dv in sub_disps):
                        sb_steps.append(
                            {
                                "distribute": {
                                    "pipette": mount,
                                    "source": src_ref,
                                    "dest": [fmt_well(loc_d) for loc_d, _ in sub_disps],
                                    "volume": first_dv,
                                    "new_tip": "never",
                                    "disposal_volume": _clean_vol(max(0.0, asp_vol - disp_sum)),
                                }
                            }
                        )
                    else:
                        for loc_d, dv in sub_disps:
                            sb_steps.append(
                                {
                                    "transfer": {
                                        "pipette": mount,
                                        "source": src_ref,
                                        "dest": fmt_well(loc_d),
                                        "volume": dv,
                                        "new_tip": "never",
                                    }
                                }
                            )
                    i = j
                    continue
                # Multi-aspirate (`aspirate v1` + ... + `aspirate vk` -> `dispense V`)
                j = i
                asp_srcs: list[tuple[str, float]] = []
                asp_sum = 0.0
                while j < n and steps[j].get("op") == "aspirate":
                    av = float(steps[j]["vol"])
                    asp_srcs.append((str(steps[j]["loc"]), av))
                    asp_sum += av
                    j += 1
                if j < n and steps[j].get("op") == "dispense" and abs(asp_sum - float(steps[j]["vol"])) <= 1e-6:
                    dst_raw = str(steps[j]["loc"])
                    for loc_a, av in asp_srcs:
                        sb_steps.append(
                            {
                                "transfer": {
                                    "pipette": mount,
                                    "source": fmt_well(loc_a),
                                    "dest": fmt_well(dst_raw),
                                    "volume": av,
                                    "new_tip": "never",
                                }
                            }
                        )
                    i = j + 1
                    continue
                raise ValueError(f"cannot export step {i + 1} to stepbot: {s!r}")
            elif op == "mix":
                w_ref = _expand_8ch_wire_ref(str(s["loc"]), False) if ch == 8 else fmt_well(str(s["loc"]))
                sb_steps.append(
                    {
                        "mix": {
                            "pipette": mount,
                            "wells": w_ref,
                            "volume": float(s["vol"]),
                            "reps": int(s.get("reps", 1)),
                            "new_tip": "never",
                        }
                    }
                )
                i += 1
            elif op == "seal":
                sb_steps.append({"comment": {"text": f"seal {fmt_well(str(s['loc']))}"}})
                i += 1
            elif op == "thaw":
                sb_steps.append({"comment": {"text": f"thaw {fmt_well(str(s['loc']))}"}})
                i += 1
            elif op == "incubate":
                tc = float(s["temp_c"])
                ds = float(s["duration_s"])
                sb_steps.append(
                    {"comment": {"text": f"incubate {fmt_well(str(s['loc']))} at {tc:g}C for {ds:g}s"}}
                )
                sb_steps.append({"delay": {"seconds": ds}})
                i += 1
            else:
                raise ValueError(f"cannot export step {i + 1} to stepbot: {s!r}")

        liquids_out: dict[str, Any] = {}
        for loc, (vol, liq) in sorted(spec.start.items()):
            sb_well = fmt_well(loc)
            if liq in liquids_out:
                prev_w = liquids_out[liq]["wells"]
                w_list = [prev_w] if isinstance(prev_w, str) else list(prev_w)
                w_list.append(sb_well)
                liquids_out[liq]["wells"] = w_list
            else:
                liquids_out[liq] = {"wells": sb_well, "volume": float(vol)}

        tip_labware = (
            "opentrons_96_tiprack_20ul"
            if "p20" in resolved_model
            else (
                "opentrons_96_tiprack_1000ul"
                if "p1000" in resolved_model
                else "opentrons_96_tiprack_300ul"
            )
        )
        return {
            "metadata": {"name": name, "description": "Exported from lab-rehearsal"},
            "deck": {
                "plate": {"labware": "corning_96_wellplate_360ul_flat", "slot": 1},
                "reservoir": {"labware": "nest_12_reservoir_15ml", "slot": 2},
                "tips": {"labware": tip_labware, "slot": 3},
            },
            "pipettes": {mount: {"model": resolved_model, "tip_racks": ["tips"]}},
            "liquids": liquids_out,
            "steps": sb_steps,
        }

    # ---- Opentrons API 2.16 `autolab-wire/v1` compilation ----
    # Group micro-steps into tip-lifecycle blocks and standalone thermal/seal annotations
    segments: list[tuple[str, list[dict[str, Any]]]] = []
    cur_tip_ops: list[dict[str, Any]] | None = None
    for s in steps:
        op = s.get("op")
        if op == "pick_up_tip":
            cur_tip_ops = []
        elif op == "drop_tip":
            if cur_tip_ops is not None:
                segments.append(("tip_block", cur_tip_ops))
                cur_tip_ops = None
        elif op in ("incubate", "seal", "thaw"):
            if cur_tip_ops is not None and cur_tip_ops:
                segments.append(("tip_block", cur_tip_ops))
                cur_tip_ops = None
            segments.append(("thermal", [dict(s)]))
        else:
            if cur_tip_ops is None:
                cur_tip_ops = []
            cur_tip_ops.append(dict(s))
    if cur_tip_ops:
        segments.append(("tip_block", cur_tip_ops))

    wire_steps: list[dict[str, Any]] = []
    stepbot_tip_open = False

    def _lower_block_ops(
        ops_slice: Sequence[Mapping[str, Any]],
        *,
        row_override: str | None = None,
    ) -> list[dict[str, Any]]:
        actions_out: list[dict[str, Any]] = []
        idx_op = 0
        m_ops = len(ops_slice)
        while idx_op < m_ops:
            st = ops_slice[idx_op]
            op_name = st.get("op")
            ch_val = int(st.get("channels", spec.channels))

            def _ref(raw_l: str) -> str | list[str]:
                if row_override is not None:
                    return _well_for_row(raw_l, row_override, fmt_well)
                if ch_val == 8 and not use_native_8ch:
                    return _expand_8ch_wire_ref(raw_l, True)
                return fmt_well(raw_l)

            if op_name == "aspirate":
                asp_vol = float(st["vol"])
                src_ref = _ref(str(st["loc"]))
                if (
                    idx_op + 1 < m_ops
                    and ops_slice[idx_op + 1].get("op") == "dispense"
                    and abs(asp_vol - float(ops_slice[idx_op + 1].get("vol", -1))) <= 1e-6
                ):
                    air_flag = bool(ops_slice[idx_op + 1].get("air_dispense", False))
                    dst_raw = str(ops_slice[idx_op + 1]["loc"])
                    if dst_raw == "trash:1":
                        actions_out.append(
                            {
                                "action": "discard",
                                "pipette": mount,
                                "source": src_ref,
                                "volume": asp_vol,
                            }
                        )
                    else:
                        dst_ref = _ref(dst_raw)
                        t_act: dict[str, Any] = {
                            "action": "transfer",
                            "pipette": mount,
                            "source": src_ref,
                            "dest": dst_ref,
                            "volume": asp_vol,
                        }
                        if air_flag:
                            t_act["dispense_position"] = "top"
                        actions_out.append(t_act)
                    idx_op += 2
                    continue

                # Multi-dispense inside tip block
                j_op = idx_op + 1
                sub_disps_w: list[tuple[str, float, bool]] = []
                disp_sum = 0.0
                while j_op < m_ops and ops_slice[j_op].get("op") == "dispense":
                    dv = float(ops_slice[j_op]["vol"])
                    air_f = bool(ops_slice[j_op].get("air_dispense", False))
                    sub_disps_w.append((str(ops_slice[j_op]["loc"]), dv, air_f))
                    disp_sum += dv
                    j_op += 1
                if sub_disps_w and disp_sum <= asp_vol + 1e-6:
                    first_dv = sub_disps_w[0][1]
                    all_air = all(air_f for _, _, air_f in sub_disps_w)
                    any_air = any(air_f for _, _, air_f in sub_disps_w)
                    if (
                        len(sub_disps_w) >= 2
                        and (ch_val == 1 or use_native_8ch or row_override is not None)
                        and all(abs(dv - first_dv) <= 1e-6 for _, dv, _ in sub_disps_w)
                        and all(loc_d != "trash:1" for loc_d, _, _ in sub_disps_w)
                        and (all_air == any_air)
                    ):
                        dist_act: dict[str, Any] = {
                            "action": "distribute",
                            "pipette": mount,
                            "source": src_ref,
                            "dest": [_ref(loc_d) for loc_d, _, _ in sub_disps_w],
                            "volume": first_dv,
                            "disposal_volume": _clean_vol(max(0.0, asp_vol - disp_sum)),
                        }
                        if all_air:
                            dist_act["dispense_position"] = "top"
                        else:
                            dist_act["dispense_position"] = "bottom"
                        actions_out.append(dist_act)
                    else:
                        for loc_d, dv, air_f in sub_disps_w:
                            if loc_d == "trash:1":
                                actions_out.append(
                                    {
                                        "action": "discard",
                                        "pipette": mount,
                                        "source": src_ref,
                                        "volume": dv,
                                    }
                                )
                            else:
                                dst_ref = _ref(loc_d)
                                t_sub: dict[str, Any] = {
                                    "action": "transfer",
                                    "pipette": mount,
                                    "source": src_ref,
                                    "dest": dst_ref,
                                    "volume": dv,
                                }
                                if air_f:
                                    t_sub["dispense_position"] = "top"
                                actions_out.append(t_sub)
                    idx_op = j_op
                    continue

                # Multi-aspirate inside tip block
                j_op = idx_op
                asp_list: list[tuple[str, float]] = []
                asp_sum = 0.0
                while j_op < m_ops and ops_slice[j_op].get("op") == "aspirate":
                    av = float(ops_slice[j_op]["vol"])
                    asp_list.append((str(ops_slice[j_op]["loc"]), av))
                    asp_sum += av
                    j_op += 1
                if (
                    j_op < m_ops
                    and ops_slice[j_op].get("op") == "dispense"
                    and abs(asp_sum - float(ops_slice[j_op]["vol"])) <= 1e-6
                ):
                    dst_raw = str(ops_slice[j_op]["loc"])
                    air_f = bool(ops_slice[j_op].get("air_dispense", False))
                    for loc_a, av in asp_list:
                        if dst_raw == "trash:1":
                            actions_out.append(
                                {
                                    "action": "discard",
                                    "pipette": mount,
                                    "source": _ref(loc_a),
                                    "volume": av,
                                }
                            )
                        else:
                            t_ma: dict[str, Any] = {
                                "action": "transfer",
                                "pipette": mount,
                                "source": _ref(loc_a),
                                "dest": _ref(dst_raw),
                                "volume": av,
                            }
                            if air_f:
                                t_ma["dispense_position"] = "top"
                            actions_out.append(t_ma)
                    idx_op = j_op + 1
                    continue

            elif op_name == "mix":
                w_ref = _ref(str(st["loc"]))
                actions_out.append(
                    {
                        "action": "mix",
                        "pipette": mount,
                        "wells": w_ref,
                        "volume": float(st["vol"]),
                        "reps": int(st.get("reps", 1)),
                    }
                )
                idx_op += 1
                continue

            idx_op += 1
        return actions_out

    for seg_idx, (seg_kind, seg_ops) in enumerate(segments):
        if seg_kind == "thermal":
            ts = seg_ops[0]
            top = ts["op"]
            w_coord = fmt_well(str(ts["loc"]))
            if top == "seal":
                wire_steps.append({"action": "comment", "text": f"seal {w_coord}"})
            elif top == "thaw":
                wire_steps.append({"action": "comment", "text": f"thaw {w_coord}"})
            elif top == "incubate":
                tc = float(ts["temp_c"])
                ds = float(ts["duration_s"])
                wire_steps.append(
                    {"action": "comment", "text": f"incubate {w_coord} at {tc:g}C for {ds:g}s"}
                )
                wire_steps.append({"action": "delay", "seconds": ds})
            continue

        seg_is_8ch_lowered = (
            not use_native_8ch
            and any(int(s.get("channels", spec.channels)) == 8 for s in seg_ops)
        )
        has_later_tip_block = any(k == "tip_block" for k, _ in segments[seg_idx + 1 :])

        if seg_is_8ch_lowered:
            expanded_actions = _coalesce_wire_block_actions(_lower_block_ops(seg_ops))
            if not expanded_actions:
                continue
            if len(expanded_actions) == 1 and expanded_actions[0].get("action") in ("transfer", "mix", "discard"):
                act_8 = dict(expanded_actions[0])
                act_8["new_tip"] = "always"
                wire_steps.append(act_8)
                stepbot_tip_open = False
            else:
                for row_ch in "ABCDEFGH":
                    row_acts = _coalesce_wire_block_actions(
                        _lower_block_ops(seg_ops, row_override=row_ch)
                    )
                    if len(row_acts) == 1:
                        r_act = dict(row_acts[0])
                        r_act["new_tip"] = "once"
                        wire_steps.append(r_act)
                        stepbot_tip_open = False
                    else:
                        for r_idx, raw_ra in enumerate(row_acts):
                            r_act = dict(raw_ra)
                            r_act["new_tip"] = "once" if (r_idx == 0 and stepbot_tip_open) else "never"
                            wire_steps.append(r_act)
                        stepbot_tip_open = True
            continue

        block_actions = _lower_block_ops(seg_ops)
        if not block_actions:
            continue

        if has_later_tip_block and len(block_actions) > 1:
            block_actions = _coalesce_wire_block_actions(block_actions)

        if len(block_actions) == 1:
            act = dict(block_actions[0])
            is_minimal_single = (
                not stepbot_tip_open
                and act.get("action") == "transfer"
                and "dispense_position" not in act
                and "mix_after" not in act
                and not isinstance(act.get("dest"), list)
                and not isinstance(act.get("source"), list)
            )
            if not is_minimal_single:
                act["new_tip"] = "once"
            wire_steps.append(act)
            stepbot_tip_open = False
        else:
            for a_idx, raw_act in enumerate(block_actions):
                act = dict(raw_act)
                if a_idx == 0 and stepbot_tip_open:
                    act["new_tip"] = "once"
                else:
                    act["new_tip"] = "never"
                wire_steps.append(act)
            stepbot_tip_open = True

    tiprack_load_name = (
        "opentrons_96_tiprack_20ul"
        if "p20" in resolved_model
        else (
            "opentrons_96_tiprack_1000ul"
            if "p1000" in resolved_model
            else "opentrons_96_tiprack_300ul"
        )
    )
    wire_doc: dict[str, Any] = {
        "metadata": {
            "protocolName": name,
            "apiLevel": "2.16",
        },
        "deck": {
            "1": {"load_name": tiprack_load_name},
            "2": {"load_name": "nest_12_reservoir_15ml"},
            "3": {"load_name": "corning_96_wellplate_360ul_flat"},
        },
        "pipettes": {
            mount: {
                "instrument_name": resolved_model,
                "tip_racks": ["1"],
            }
        },
        "steps": wire_steps,
    }

    # Declare starting liquid stocks whenever multiple sources, non-default stocks, or pre-filled wells are used
    needs_explicit_liquids = (
        len(wire_steps) > 1
        or dict(spec.start) != dict(DEFAULT_DECK_SPEC.start)
        or any(st.get("source") not in (None, "2:A1") for st in wire_steps)
    )
    if needs_explicit_liquids and spec.start:
        wire_liquids: dict[str, Any] = {}
        for loc, (vol, liq) in sorted(spec.start.items()):
            if float(vol) <= VOL_EPS:
                continue
            w_coord = fmt_well(loc)
            if liq in wire_liquids and abs(float(wire_liquids[liq]["volume"]) - float(vol)) <= 1e-6:
                prev_w = wire_liquids[liq]["wells"]
                w_list = [prev_w] if isinstance(prev_w, str) else list(prev_w)
                w_list.append(w_coord)
                wire_liquids[liq]["wells"] = w_list
            elif liq in wire_liquids:
                wire_liquids[f"{liq}_{loc.replace(':', '_')}"] = {
                    "wells": w_coord,
                    "volume": float(vol),
                }
            else:
                wire_liquids[liq] = {"wells": w_coord, "volume": float(vol)}
        if wire_liquids:
            wire_doc["liquids"] = wire_liquids

    return wire_doc


def _quantize_tenths_list(
    raw_vols: Sequence[float],
    *,
    allow_sub_tenth_rounding: bool,
) -> list[int]:
    if not allow_sub_tenth_rounding:
        return [to_tenths_exact(v, "step volume") for v in raw_vols]
    out: list[int] = []
    err = 0.0
    for v in raw_vols:
        target = float(v) * 10.0 + err
        q = int(round(target))
        err = target - q
        out.append(q)
    return out


def to_lablean(
    steps: Sequence[Mapping[str, Any]],
    *,
    spec: DeckSpec = DEFAULT_DECK_SPEC,
    pipette_name: str = "P300",
    allow_sub_tenth_rounding: bool = False,
) -> dict[str, Any]:
    """Convert `lab-rehearsal` micro-steps and deck into a `LabLean` (`LabRehearsalLean`) JSON trace.

    By default (`allow_sub_tenth_rounding=False`), rejects sub-`0.1 uL` fractional volumes
    rather than accumulating per-step quantization drift, and lowers multi-dispense,
    multi-aspirate, `air_dispense`, `incubate`, `seal`, and `thaw` steps losslessly.
    """
    if allow_sub_tenth_rounding:
        pip_min_t = int(round(spec.pip_min * 10.0))
        pip_max_t = int(round(spec.pip_max * 10.0))
    else:
        pip_min_t = to_tenths_exact(spec.pip_min, "DeckSpec.pip_min")
        pip_max_t = to_tenths_exact(spec.pip_max, "DeckSpec.pip_max")

    pip_dict = {
        "name": pipette_name,
        "min_tenths": pip_min_t,
        "max_tenths": pip_max_t,
    }

    tenths_by_idx: dict[int, int] = {}
    err_by_stream: dict[str, float] = {"aspirate": 0.0, "dispense": 0.0, "mix": 0.0}
    for idx, st in enumerate(steps):
        sop = st.get("op")
        if sop in ("aspirate", "dispense", "mix"):
            v_f = float(st["vol"])
            if not allow_sub_tenth_rounding:
                tenths_by_idx[idx] = to_tenths_exact(v_f, "step volume")
            else:
                s_key = str(sop)
                target = v_f * 10.0 + err_by_stream[s_key]
                q = int(round(target))
                err_by_stream[s_key] = target - q
                tenths_by_idx[idx] = q

    ll_steps: list[dict[str, Any]] = []
    i = 0
    n = len(steps)
    while i < n:
        s = steps[i]
        op = s.get("op")
        if int(s.get("channels", spec.channels)) != 1:
            raise ValueError(
                f"step {i + 1} uses channels={s.get('channels', spec.channels)}; "
                "LabLean single-barrel trace requires channels=1"
            )
        if op == "pick_up_tip":
            ll_steps.append({"kind": "pickTip"})
            i += 1
        elif op == "drop_tip":
            if s.get("allow_wet_drop", False):
                raise ValueError("LabLean dropTip requires empty barrel (allow_wet_drop=False)")
            ll_steps.append({"kind": "dropTip"})
            i += 1
        elif (
            op == "aspirate"
            and i + 1 < n
            and steps[i + 1].get("op") == "dispense"
            and not bool(steps[i + 1].get("air_dispense", False))
            and tenths_by_idx[i] == tenths_by_idx[i + 1]
            and str(s["loc"]) != str(steps[i + 1]["loc"])
            and int(steps[i + 1].get("channels", spec.channels)) == 1
        ):
            ll_steps.append(
                {
                    "kind": "transfer",
                    "pipette": dict(pip_dict),
                    "src": str(s["loc"]),
                    "dst": str(steps[i + 1]["loc"]),
                    "vol_tenths": tenths_by_idx[i],
                }
            )
            i += 2
        elif op == "aspirate":
            ll_steps.append(
                {
                    "kind": "aspirate",
                    "pipette": dict(pip_dict),
                    "src": str(s["loc"]),
                    "vol_tenths": tenths_by_idx[i],
                }
            )
            i += 1
        elif op == "dispense":
            ll_steps.append(
                {
                    "kind": "dispense",
                    "pipette": dict(pip_dict),
                    "dst": str(s["loc"]),
                    "vol_tenths": tenths_by_idx[i],
                    "airDispense": bool(s.get("air_dispense", False)),
                }
            )
            i += 1
        elif op == "mix":
            ll_steps.append(
                {
                    "kind": "mix",
                    "pipette": dict(pip_dict),
                    "container": str(s["loc"]),
                    "vol_tenths": tenths_by_idx[i],
                    "times": int(s.get("reps", 1)),
                }
            )
            i += 1
        elif op == "incubate":
            ll_steps.append(
                {
                    "kind": "incubate",
                    "wellId": str(s["loc"]),
                    "tempC": int(round(float(s["temp_c"]))),
                    "durSec": int(round(float(s["duration_s"]))),
                }
            )
            i += 1
        elif op == "seal":
            ll_steps.append({"kind": "sealWell", "wellId": str(s["loc"])})
            i += 1
        elif op == "thaw":
            ll_steps.append({"kind": "thawWell", "wellId": str(s["loc"])})
            i += 1
        else:
            raise ValueError(f"cannot convert step {i + 1} to LabLean: {s!r}")

    reagents = sorted({liq for _, liq in spec.start.values()})
    return {
        "reagents": [
            {"name": r, "state": "roomTemp", "diluent": r.lower() in spec.diluents}
            for r in reagents
        ],
        "steps": ll_steps,
    }


def from_lablean(trace_doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Lower a `LabLean` step trace (`pickTip`, `dropTip`, `transfer`, `aspirate`, `dispense`, `mix`, `incubate`, `sealWell`, `thawWell`) to `lab-rehearsal`."""
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
        elif kind == "aspirate":
            vol = float(st["vol_tenths"]) / 10.0
            micro.append({"op": "aspirate", "loc": _normalize_loc(str(st["src"])), "vol": vol})
        elif kind == "dispense":
            vol = float(st["vol_tenths"]) / 10.0
            d_step: dict[str, Any] = {
                "op": "dispense",
                "loc": _normalize_loc(str(st["dst"])),
                "vol": vol,
            }
            if bool(st.get("airDispense", st.get("air_dispense", False))):
                d_step["air_dispense"] = True
            micro.append(d_step)
        elif kind == "mix":
            vol = float(st["vol_tenths"]) / 10.0
            w_key = st.get("container", st.get("wellId", st.get("loc")))
            micro.append(
                {
                    "op": "mix",
                    "loc": _normalize_loc(str(w_key)),
                    "vol": vol,
                    "reps": max(1, int(st.get("times", st.get("reps", 1)))),
                }
            )
        elif kind == "incubate":
            w_key = st.get("wellId", st.get("container", st.get("loc")))
            micro.append(
                {
                    "op": "incubate",
                    "loc": _normalize_loc(str(w_key)),
                    "temp_c": float(st.get("tempC", st.get("temp_c", 37.0))),
                    "duration_s": float(st.get("durSec", st.get("duration_s", 60.0))),
                }
            )
        elif kind in ("sealWell", "seal"):
            w_key = st.get("wellId", st.get("container", st.get("loc")))
            micro.append({"op": "seal", "loc": _normalize_loc(str(w_key))})
        elif kind in ("thawWell", "thaw"):
            w_key = st.get("wellId", st.get("container", st.get("loc")))
            micro.append({"op": "thaw", "loc": _normalize_loc(str(w_key))})
        else:
            raise ValueError(f"unsupported LabLean step kind: {kind!r}")
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
