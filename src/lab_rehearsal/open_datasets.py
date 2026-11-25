"""Open wet-lab protocol & embodied manipulation dataset converters (`2024-2026`).

Provides SPDX/eval-only catalog governance checks and deterministic offline converters that
lower external open-source protocol representations into `lab-rehearsal` micro-step lists and
calibrated `DeckSpec` configurations for 96-well digital-twin dry-run rehearsal:

- **Opentrons JSON v6/v7/v8 & Protocol Library (`protoBuilds`)**: `from_opentrons_json`
- **BioPlanner (`BioProt` EMNLP) & WLP structured steps**: `from_bioplanner`
- **FineBio (CVPR 2024 / IJCV 2025) atomic manipulation traces**: `from_finebio_trace`
- **BioProBench (ICML 2026 `ORD`/`ERR`/`GEN`) & `LAB-Bench` `ProtocolQA`**: `from_bioprobench`
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .adapters import from_lablean, from_stepbot, from_twinstep
from .core import (
    DEFAULT_DECK_SPEC,
    DEFAULT_DILUENTS,
    VOL_EPS,
    DeckSpec,
    RehearsalSession,
    _clean_vol,
    rehearse,
    validate_protocol_schema,
    verify,
)

DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[2] / "catalog" / "simulators.json"
VALID_INGESTION_PURPOSES = frozenset({"rehearsal_eval", "training", "redistribution"})

_WELL_COORD_RE = re.compile(r"^([A-H])(1[0-2]|[1-9])$")
_VOL_RE = re.compile(
    r"(?<![A-Za-z0-9_])(\d+(?:\.\d+)?)\s*(uL|ul|\u03bcl|\u00b5l|\u03bcL|\u00b5L|mL|ml)(?![A-Za-z])"
)
_TEMP_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*(?:\^\s*)?(?:\u00b0\s*)?C\b")
_DUR_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|min|hours?|hrs?|hr|h)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# 1. Catalog Governance & Eval-Only / SPDX Policy Gate
# ---------------------------------------------------------------------------


def load_dataset_catalog(catalog_path: Path | None = None) -> dict[str, Any]:
    """Load `catalog/simulators.json` and return the parsed governance manifest."""
    target = catalog_path if catalog_path is not None else DEFAULT_CATALOG_PATH
    data = json.loads(Path(target).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("simulators catalog root must be a JSON object")
    return data


def list_open_datasets(
    *,
    min_year: int | None = None,
    verified_only: bool = False,
    catalog_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return dataset entries filtered by publication/release year and license verification."""
    catalog = load_dataset_catalog(catalog_path)
    out: list[dict[str, Any]] = []
    for entry in catalog.get("datasets", []):
        if not isinstance(entry, dict):
            continue
        if min_year is not None and int(entry.get("year", 0)) < int(min_year):
            continue
        if verified_only and entry.get("license_status") not in (
            "verified_open",
            "verified_non_commercial",
        ):
            continue
        out.append(dict(entry))
    return out


def get_dataset_entry(
    dataset_id: str,
    *,
    catalog_path: Path | None = None,
) -> dict[str, Any]:
    """Look up a single dataset entry in `catalog/simulators.json` by `id`."""
    catalog = load_dataset_catalog(catalog_path)
    for entry in catalog.get("datasets", []):
        if isinstance(entry, dict) and entry.get("id") == dataset_id:
            return dict(entry)
    raise KeyError(f"unknown dataset id in catalog: {dataset_id!r}")


def check_dataset_ingestion_policy(
    dataset_id: str,
    *,
    purpose: str = "rehearsal_eval",
    require_commercial_ok: bool = False,
    catalog_path: Path | None = None,
) -> tuple[bool, list[str]]:
    """Default-deny governance check for dataset ingestion, training, and redistribution."""
    if purpose not in VALID_INGESTION_PURPOSES:
        return False, [f"invalid_purpose: {purpose!r} not in {sorted(VALID_INGESTION_PURPOSES)}"]

    try:
        entry = get_dataset_entry(dataset_id, catalog_path=catalog_path)
    except KeyError:
        return False, [f"unknown_dataset: {dataset_id!r} is not registered in simulators.json"]

    reasons: list[str] = []
    spdx = str(entry.get("spdx_license", "NOASSERTION"))
    status = str(entry.get("license_status", "unknown"))

    if purpose == "training" and bool(entry.get("eval_only", False)):
        reasons.append(
            f"eval_only_guard: dataset {dataset_id!r} is marked eval_only=true and cannot be used for training"
        )

    if purpose == "redistribution" and not bool(entry.get("redistribution_ok", False)):
        reasons.append(
            f"redistribution_denied: dataset {dataset_id!r} ({spdx}, {status}) forbids automated redistribution"
        )

    if require_commercial_ok and not bool(entry.get("commercial_use_ok", False)):
        reasons.append(
            f"non_commercial_license: dataset {dataset_id!r} ({spdx}) sets commercial_use_ok=false"
        )

    return len(reasons) == 0, reasons


# ---------------------------------------------------------------------------
# Shared Helpers
# ---------------------------------------------------------------------------


def _pipette_bounds_for_name(model_name: str) -> tuple[float, float, int]:
    """Map common Opentrons / benchtop pipette names to `(pip_min, pip_max, channels)`."""
    lc = model_name.lower()
    channels = 8 if ("multi" in lc or "_8" in lc or "8_channel" in lc or "_96" in lc) else 1
    if "p1000" in lc or "1000ul" in lc:
        if "_96" in lc or "flex" in lc:
            return 5.0, 1000.0, channels
        return 100.0, 1000.0, channels
    if "p300" in lc or "300ul" in lc or "blue_pipette" in lc:
        return 20.0, 300.0, channels
    if "p200" in lc or "200ul" in lc or "yellow_pipette" in lc:
        return 20.0, 200.0, channels
    if "p50" in lc or "50ul" in lc:
        return 5.0, 50.0, channels
    if "p20" in lc or "20ul" in lc or "red_pipette" in lc:
        return 1.0, 20.0, channels
    if "p10" in lc or "10ul" in lc:
        return 1.0, 10.0, channels
    return 20.0, 300.0, channels


def _normalize_plate_well(raw_well: str, fallback_idx: int = 0) -> str:
    w = raw_well.strip().upper()
    if _WELL_COORD_RE.match(w):
        return w
    rows = "ABCDEFGH"
    r = rows[fallback_idx % 8]
    c = (fallback_idx // 8) % 12 + 1
    return f"{r}{c}"


def _is_diluent_name(label: str) -> bool:
    lc = label.lower()
    return any(
        tok in lc
        for tok in ("water", "buffer", "pbs", "diluent", "media", "medium", "rpmi", "saline", "te")
    )


def _select_pipette_mount_for_vols(
    vols: Sequence[float],
) -> tuple[float, float, dict[str, tuple[float, float, int]]]:
    """Build single- or multi-pipette bounds covering the requested microliter volumes."""
    clean = [float(v) for v in vols if v > VOL_EPS]
    if not clean:
        return 20.0, 300.0, {}
    vmin, vmax = min(clean), max(clean)
    need_p20 = vmin < 20.0 - VOL_EPS
    need_p300 = any(20.0 - VOL_EPS <= v <= 300.0 + VOL_EPS for v in clean)
    need_p1000 = vmax > 300.0 + VOL_EPS

    pipettes: dict[str, tuple[float, float, int]] = {}
    if need_p20:
        pipettes["p20"] = (0.5, 20.0, 1)
    if need_p300 or (not need_p20 and not need_p1000):
        pipettes["p300"] = (20.0, 300.0, 1)
    if need_p1000:
        pipettes["p1000"] = (100.0, 1000.0, 1)

    if len(pipettes) == 1:
        only_min, only_max, _ = next(iter(pipettes.values()))
        return only_min, only_max, pipettes
    return 20.0, 300.0, pipettes


def _pick_mount_name(vol: float, pipettes: Mapping[str, tuple[float, float, int]]) -> str | None:
    if not pipettes:
        return None
    if len(pipettes) == 1:
        return next(iter(pipettes.keys()))
    if vol <= 20.0 + VOL_EPS and "p20" in pipettes:
        return "p20"
    if vol <= 300.0 + VOL_EPS and "p300" in pipettes:
        return "p300"
    if "p1000" in pipettes:
        return "p1000"
    return next(iter(pipettes.keys()))


# ---------------------------------------------------------------------------
# 2. Converter A: Opentrons JSON v6/v7/v8 & Opentrons/Protocols protoBuilds
# ---------------------------------------------------------------------------


def _from_opentrons_protobuild_ast(py_source: str) -> tuple[list[dict[str, Any]], DeckSpec]:
    """Statically extract simple liquid-handling calls from an Opentrons API v2 `.py` script."""
    tree = ast.parse(py_source)
    pipettes: dict[str, tuple[float, float, int]] = {}
    steps: list[dict[str, Any]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = node.value
            if isinstance(call.func, ast.Attribute) and call.func.attr == "load_instrument":
                if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
                    p_bounds = _pipette_bounds_for_name(call.args[0].value)
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            pipettes[target.id] = p_bounds

    well_idx = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not isinstance(call.func, ast.Attribute):
            continue
        method = call.func.attr
        caller = call.func.value.id if isinstance(call.func.value, ast.Name) else None
        pip_arg = {"pipette": caller} if caller and caller in pipettes else {}

        if method == "transfer" and call.args and isinstance(call.args[0], ast.Constant):
            raw_v = call.args[0].value
            if isinstance(raw_v, (int, float)) and float(raw_v) > 0:
                vol = round(float(raw_v), 1)
                dst = f"plate:{_normalize_plate_well('', well_idx)}"
                well_idx += 1
                steps.append({"op": "pick_up_tip", **pip_arg})
                steps.append({"op": "aspirate", "loc": "res:1", "vol": vol, **pip_arg})
                steps.append({"op": "dispense", "loc": dst, "vol": vol, **pip_arg})
                steps.append({"op": "drop_tip", **pip_arg})

    if not steps:
        raise ValueError("Opentrons protoBuild Python source contained no static literal transfer steps")

    p_min, p_max = (20.0, 300.0)
    if pipettes:
        first = next(iter(pipettes.values()))
        p_min, p_max = first[0], first[1]
    spec = DeckSpec(
        pip_min=p_min,
        pip_max=p_max,
        pipettes=pipettes,
        start={"res:1": (10000.0, "buffer"), "res:2": (2000.0, "sample")},
    )
    return steps, spec


def from_opentrons_json(
    doc: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], DeckSpec]:
    """Convert Opentrons Shared Protocol JSON (v6/v7/v8) or `protoBuilds` JSON into twin steps."""
    if not isinstance(doc, Mapping):
        raise ValueError("Opentrons JSON input must be a mapping/dict")

    if "content" in doc and isinstance(doc["content"], str) and "commands" not in doc:
        return _from_opentrons_protobuild_ast(doc["content"])

    commands = doc.get("commands")
    if not isinstance(commands, list):
        raise ValueError("Opentrons JSON protocol must contain a 'commands' list")

    labware_defs = doc.get("labwareDefinitions") if isinstance(doc.get("labwareDefinitions"), Mapping) else {}
    liquids_meta = doc.get("liquids") if isinstance(doc.get("liquids"), Mapping) else {}

    # 1. Discover pipettes (v6 top-level `pipettes` + v7 `loadPipette` commands)
    pipettes_map: dict[str, tuple[float, float, int]] = {}
    raw_pipettes = doc.get("pipettes")
    if isinstance(raw_pipettes, Mapping):
        for pid, pinfo in raw_pipettes.items():
            if isinstance(pinfo, Mapping):
                pname = str(pinfo.get("name", pinfo.get("pipetteName", "p300_single")))
                pipettes_map[str(pid)] = _pipette_bounds_for_name(pname)

    # 2. Discover labware (v6 top-level `labware` + v7 `loadLabware` commands)
    labware_info: dict[str, dict[str, Any]] = {}
    raw_labware = doc.get("labware")
    if isinstance(raw_labware, Mapping):
        for lid, lmeta in raw_labware.items():
            if isinstance(lmeta, Mapping):
                def_id = str(lmeta.get("definitionId", ""))
                d_obj = labware_defs.get(def_id, {}) if isinstance(labware_defs, Mapping) else {}
                params = d_obj.get("parameters", {}) if isinstance(d_obj, Mapping) else {}
                wells_def = d_obj.get("wells", {}) if isinstance(d_obj, Mapping) else {}
                a1_vol = (
                    float(wells_def["A1"]["totalLiquidVolume"])
                    if isinstance(wells_def.get("A1"), Mapping)
                    and "totalLiquidVolume" in wells_def["A1"]
                    else 300.0
                )
                labware_info[str(lid)] = {
                    "load_name": str(params.get("loadName", def_id)).lower(),
                    "display_name": str(lmeta.get("displayName", "")).lower(),
                    "is_tiprack": bool(params.get("isTiprack", False)) or "tiprack" in def_id.lower(),
                    "is_trash": params.get("format") == "trash" or "trash" in def_id.lower() or lid == "fixedTrash",
                    "well_max": a1_vol,
                }

    loaded_liquids: list[tuple[str, str, str, float]] = []  # (labware_id, well_name, liquid_name, vol)
    aspirated_labware: set[str] = set()
    dispensed_labware: set[str] = set()

    for cmd in commands:
        if not isinstance(cmd, Mapping):
            continue
        ctype = str(cmd.get("commandType", ""))
        params = cmd.get("params") if isinstance(cmd.get("params"), Mapping) else {}
        if ctype == "loadPipette":
            pid = str(params.get("pipetteId", "default"))
            pname = str(params.get("pipetteName", params.get("name", ""
            )))
            if pname:
                pipettes_map[pid] = _pipette_bounds_for_name(pname)
            elif pid not in pipettes_map:
                pipettes_map[pid] = (20.0, 300.0, 1)
        elif ctype == "loadLabware":
            lid = str(params.get("labwareId", ""))
            if lid and lid not in labware_info:
                load_name = str(params.get("loadName", "")).lower()
                disp_name = str(params.get("displayName", "")).lower()
                well_max = 300.0
                for d_obj in labware_defs.values():
                    if isinstance(d_obj, Mapping):
                        p_obj = d_obj.get("parameters", {})
                        if isinstance(p_obj, Mapping) and str(p_obj.get("loadName", "")).lower() == load_name:
                            w_obj = d_obj.get("wells", {})
                            if isinstance(w_obj, Mapping) and isinstance(w_obj.get("A1"), Mapping):
                                well_max = float(w_obj["A1"].get("totalLiquidVolume", 300.0))
                            break
                labware_info[lid] = {
                    "load_name": load_name,
                    "display_name": disp_name,
                    "is_tiprack": "tiprack" in load_name or "tip rack" in disp_name,
                    "is_trash": "trash" in load_name or "trash" in disp_name or lid == "fixedTrash",
                    "well_max": well_max,
                }
        elif ctype == "loadLiquid":
            lid = str(params.get("labwareId", ""))
            liq_id = str(params.get("liquidId", "reagent"))
            liq_entry = liquids_meta.get(liq_id, {}) if isinstance(liquids_meta, Mapping) else {}
            liq_name = str(liq_entry.get("displayName", liq_id)).strip().lower().replace(" ", "_") or "reagent"
            vol_by_well = params.get("volumeByWell")
            if isinstance(vol_by_well, Mapping):
                for wname, wvol in vol_by_well.items():
                    if isinstance(wvol, (int, float)) and float(wvol) > 0:
                        loaded_liquids.append((lid, str(wname), liq_name, float(wvol)))
        elif ctype in ("aspirate", "aspirateInPlace"):
            if params.get("labwareId"):
                aspirated_labware.add(str(params["labwareId"]))
        elif ctype in ("dispense", "dispenseInPlace"):
            if params.get("labwareId"):
                dispensed_labware.add(str(params["labwareId"]))

    # Classify non-tiprack, non-trash labwares into reservoir/source vs primary assay plate
    candidate_plates = [
        lid
        for lid, info in labware_info.items()
        if not info["is_tiprack"] and not info["is_trash"]
    ]
    explicit_reservoirs = {
        lid
        for lid in candidate_plates
        if any(
            tok in labware_info[lid]["load_name"] or tok in labware_info[lid]["display_name"]
            for tok in ("reservoir", "trough", "tuberack", "tube_rack")
        )
    }
    non_res_plates = [lid for lid in candidate_plates if lid not in explicit_reservoirs]
    source_only_plates: set[str] = set()
    if len(non_res_plates) > 1:
        for lid in non_res_plates:
            if (
                (lid in aspirated_labware and lid not in dispensed_labware)
                or "source" in lid.lower()
                or "source" in labware_info[lid]["display_name"]
            ):
                source_only_plates.add(lid)

    stock_labwares = explicit_reservoirs | source_only_plates
    res_slot_map: dict[tuple[str, str], str] = {}
    next_res_col = 1

    def map_container(labware_id: str, well_name: str) -> str:
        nonlocal next_res_col
        info = labware_info.get(labware_id, {})
        if info.get("is_trash") or labware_id == "fixedTrash" or "trash" in labware_id.lower():
            return "trash:1"
        if info.get("is_tiprack") or "tiprack" in labware_id.lower():
            return f"tiprack:{_normalize_plate_well(well_name)}"
        if labware_id in stock_labwares:
            key = (labware_id, well_name.strip().upper())
            if key not in res_slot_map:
                col_idx = min(next_res_col, 11)
                res_slot_map[key] = f"res:{col_idx}"
                next_res_col += 1
            return res_slot_map[key]
        return f"plate:{_normalize_plate_well(well_name)}"

    # Populate initial DeckSpec.start from loadLiquid commands
    start_map: dict[str, tuple[float, str]] = {}
    diluents_set: set[str] = set(DEFAULT_DILUENTS)
    used_source_plate_stock = bool(source_only_plates)

    for lid, wname, liq_name, wvol in loaded_liquids:
        mapped = map_container(lid, wname)
        if mapped.startswith(("res:", "plate:")):
            prev_v, prev_l = start_map.get(mapped, (0.0, liq_name))
            start_map[mapped] = (_clean_vol(prev_v + wvol), prev_l)
            if _is_diluent_name(liq_name):
                diluents_set.add(liq_name)

    # Determine well_max across destination plates
    dest_plate_maxes = [
        labware_info[lid]["well_max"]
        for lid in non_res_plates
        if lid not in source_only_plates and labware_info[lid]["well_max"] > 0
    ]
    well_max = max(dest_plate_maxes) if dest_plate_maxes else 300.0

    # Lower commands into micro-steps
    steps: list[dict[str, Any]] = []
    single_pip = len(pipettes_map) <= 1
    held_by_pip: dict[str, float] = {pid: 0.0 for pid in pipettes_map}
    last_loc_by_pip: dict[str, str] = {}
    wet_blowout_by_pip: dict[str, bool] = {}
    tip_on_by_pip: dict[str, bool] = {pid: False for pid in pipettes_map}
    module_temp_c: float | None = None
    plate_sealed = False

    for cmd in commands:
        if not isinstance(cmd, Mapping):
            continue
        ctype = str(cmd.get("commandType", ""))
        params = cmd.get("params") if isinstance(cmd.get("params"), Mapping) else {}
        pid = str(params.get("pipetteId", next(iter(pipettes_map), "default")))
        p_bounds = pipettes_map.get(pid, (20.0, 300.0, 1))
        ch_kw: dict[str, Any] = {"channels": 8} if p_bounds[2] == 8 else {}
        pip_kw: dict[str, Any] = {} if single_pip else {"pipette": pid}

        if ctype == "pickUpTip":
            lid = str(params.get("labwareId", "tipRackId"))
            wname = str(params.get("wellName", "A1"))
            tip_loc = map_container(lid, wname)
            if ch_kw and not tip_loc.startswith("tiprack:A"):
                m_col = re.search(r"(1[0-2]|[1-9])$", tip_loc)
                tip_loc = f"tiprack:A{m_col.group(1) if m_col else '1'}"
            steps.append({"op": "pick_up_tip", "loc": tip_loc, **ch_kw, **pip_kw})
            tip_on_by_pip[pid] = True
            held_by_pip[pid] = 0.0
            wet_blowout_by_pip[pid] = False

        elif ctype in ("aspirate", "aspirateInPlace"):
            lid = str(params.get("labwareId", ""))
            wname = str(params.get("wellName", "A1"))
            loc = map_container(lid, wname) if lid else last_loc_by_pip.get(pid, "res:1")
            if ch_kw and loc.startswith("plate:") and not loc.startswith("plate:A"):
                m_col = re.search(r"(1[0-2]|[1-9])$", loc)
                loc = f"plate:A{m_col.group(1) if m_col else '1'}"
            vol = _clean_vol(float(params.get("volume", 0.0)))
            if not loaded_liquids and loc.startswith("res:") and loc not in start_map:
                liq_tag = "buffer" if loc == "res:1" else f"reagent_{loc.split(':')[1]}"
                start_map[loc] = (10000.0, liq_tag)
            steps.append({"op": "aspirate", "loc": loc, "vol": vol, **ch_kw, **pip_kw})
            held_by_pip[pid] = _clean_vol(held_by_pip.get(pid, 0.0) + vol)
            last_loc_by_pip[pid] = loc

        elif ctype in ("dispense", "dispenseInPlace"):
            lid = str(params.get("labwareId", ""))
            wname = str(params.get("wellName", "A1"))
            loc = map_container(lid, wname) if lid else last_loc_by_pip.get(pid, "plate:A1")
            if ch_kw and loc.startswith("plate:") and not loc.startswith("plate:A"):
                m_col = re.search(r"(1[0-2]|[1-9])$", loc)
                loc = f"plate:A{m_col.group(1) if m_col else '1'}"
            vol = _clean_vol(float(params.get("volume", 0.0)))
            well_loc = params.get("wellLocation") if isinstance(params.get("wellLocation"), Mapping) else {}
            offset = well_loc.get("offset") if isinstance(well_loc.get("offset"), Mapping) else {}
            air_flag = (
                well_loc.get("origin") == "top"
                or float(offset.get("z", 0.0) or 0.0) >= 5.0
            )
            step_d: dict[str, Any] = {"op": "dispense", "loc": loc, "vol": vol, **ch_kw, **pip_kw}
            if air_flag:
                step_d["air_dispense"] = True
            steps.append(step_d)
            held_by_pip[pid] = _clean_vol(max(0.0, held_by_pip.get(pid, 0.0) - vol))
            last_loc_by_pip[pid] = loc

        elif ctype in ("blowout", "blowoutInPlace"):
            lid = str(params.get("labwareId", ""))
            wname = str(params.get("wellName", "A1"))
            loc = map_container(lid, wname) if lid else last_loc_by_pip.get(pid, "trash:1")
            if ch_kw and loc.startswith("plate:") and not loc.startswith("plate:A"):
                m_col = re.search(r"(1[0-2]|[1-9])$", loc)
                loc = f"plate:A{m_col.group(1) if m_col else '1'}"
            rem = held_by_pip.get(pid, 0.0)
            if rem > VOL_EPS:
                if steps and steps[-1].get("op") == "dispense" and steps[-1].get("loc") == loc:
                    steps[-1]["vol"] = _clean_vol(float(steps[-1]["vol"]) + rem)
                    held_by_pip[pid] = 0.0
                elif rem >= p_bounds[0] - VOL_EPS:
                    steps.append({"op": "dispense", "loc": loc, "vol": rem, **ch_kw, **pip_kw})
                    held_by_pip[pid] = 0.0
                else:
                    wet_blowout_by_pip[pid] = True

        elif ctype in ("dropTip", "dropTipInPlace"):
            drop_step: dict[str, Any] = {"op": "drop_tip", **ch_kw, **pip_kw}
            if wet_blowout_by_pip.get(pid, False):
                drop_step["allow_wet_drop"] = True
            steps.append(drop_step)
            tip_on_by_pip[pid] = False
            held_by_pip[pid] = 0.0
            wet_blowout_by_pip[pid] = False

        elif ctype in (
            "temperatureModule/setTargetTemperature",
            "heaterShaker/setTargetTemperature",
            "thermocycler/setTargetBlockTemperature",
        ):
            raw_t = params.get("celsius", params.get("temperature"))
            if isinstance(raw_t, (int, float)):
                module_temp_c = min(98.0, max(4.0, float(raw_t)))

        elif ctype == "thermocycler/closeLid" and not plate_sealed:
            steps.append({"op": "seal", "loc": "plate"})
            plate_sealed = True

        elif ctype in ("waitForDuration", "delay"):
            secs = float(params.get("seconds", 0.0) or 0.0) + 60.0 * float(
                params.get("minutes", 0.0) or 0.0
            )
            if secs > 0.0 and module_temp_c is not None and not any(tip_on_by_pip.values()):
                if module_temp_c >= 80.0 and not plate_sealed:
                    steps.append({"op": "seal", "loc": "plate"})
                    plate_sealed = True
                steps.append(
                    {
                        "op": "incubate",
                        "loc": "plate",
                        "temp_c": module_temp_c,
                        "duration_s": secs,
                    }
                )

    if not start_map:
        start_map = dict(DEFAULT_DECK_SPEC.start)

    if pipettes_map:
        first_bounds = next(iter(pipettes_map.values()))
        pip_min, pip_max, channels = first_bounds
    else:
        pip_min, pip_max, channels = 20.0, 300.0, 1

    spec = DeckSpec(
        well_max=float(well_max),
        pip_min=float(pip_min),
        pip_max=float(pip_max),
        channels=int(channels),
        pipettes={} if single_pip else pipettes_map,
        res_dead_vol=0.0 if used_source_plate_stock else DEFAULT_DECK_SPEC.res_dead_vol,
        plate_dead_vol=0.0 if used_source_plate_stock else DEFAULT_DECK_SPEC.plate_dead_vol,
        start=start_map,
        diluents=frozenset(diluents_set),
    )
    return steps, spec


# ---------------------------------------------------------------------------
# 3. Converter B: BioPlanner (`BioProt` EMNLP) & WLP Structured Protocols
# ---------------------------------------------------------------------------


def _parse_volume_ul_text(raw: Any) -> float | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return _clean_vol(float(raw))
    if not isinstance(raw, str):
        return None
    m = _VOL_RE.search(raw)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).lower()
    if unit == "ml":
        val *= 1000.0
    return round(val, 1)


def _parse_duration_s_text(raw: Any, default_s: float = 60.0) -> float:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return max(1.0, float(raw))
    if isinstance(raw, str):
        m = _DUR_RE.search(raw)
        if m:
            val = float(m.group(1))
            unit = m.group(2).lower()
            if unit.startswith("m"):
                return val * 60.0
            if unit.startswith("h"):
                return val * 3600.0
            return max(1.0, val)
    return default_s


def _ast_const_value(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant):
        if isinstance(node.operand.value, (int, float)):
            return -node.operand.value
    if isinstance(node, ast.Tuple):
        return tuple(_ast_const_value(e) for e in node.elts)
    if isinstance(node, ast.List):
        return [_ast_const_value(e) for e in node.elts]
    if isinstance(node, ast.Dict):
        out: dict[Any, Any] = {}
        for k, v in zip(node.keys, node.values):
            if k is not None:
                out[_ast_const_value(k)] = _ast_const_value(v)
        return out
    return None


def _from_bioplanner_pseudocode(record: Mapping[str, Any]) -> tuple[list[dict[str, Any]], DeckSpec]:
    pseudo = str(record.get("edited_pseudocode") or record.get("generated_pseudocode") or "").strip()
    steps_text = str(record.get("steps") or "")

    reagent_vols: list[tuple[str, str, float]] = []  # (dest_well, reagent_label, vol_ul)
    incubations: list[tuple[str, float, float, bool]] = []  # (dest_well, temp_c, dur_s, needs_seal)

    if pseudo:
        try:
            tree = ast.parse(pseudo)
        except SyntaxError:
            tree = None
        if tree is not None:
            well_idx = 0
            for stmt in tree.body:
                if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
                    continue
                call = stmt.value
                fname = call.func.id if isinstance(call.func, ast.Name) else "step"
                dest_well = f"plate:{_normalize_plate_well('', well_idx)}"
                added_in_call = False
                kw_map = {kw.arg: _ast_const_value(kw.value) for kw in call.keywords if kw.arg}

                for k_name, k_val in kw_map.items():
                    if k_name in ("cycling_params", "run_params", "thermal_params"):
                        continue
                    if isinstance(k_val, dict):
                        for r_key, r_desc in k_val.items():
                            if str(r_key).lower() in ("cycles", "temp", "temperature", "duration", "rpm"):
                                continue
                            vol_ul = _parse_volume_ul_text(r_desc)
                            if vol_ul is not None and 0.0 < vol_ul <= 1000.0:
                                label = re.sub(r"[^a-z0-9_]+", "_", str(r_key).lower()).strip("_") or "reagent"
                                reagent_vols.append((dest_well, label, vol_ul))
                                added_in_call = True

                cyc = kw_map.get("cycling_params")
                if isinstance(cyc, dict):
                    init_t = cyc.get("initial")
                    if isinstance(init_t, tuple) and len(init_t) >= 2 and isinstance(init_t[0], (int, float)):
                        temp_c = min(98.0, max(4.0, float(init_t[0])))
                        mult = 60.0 if len(init_t) >= 3 and str(init_t[2]).lower().startswith("m") else 1.0
                        dur_s = max(1.0, float(init_t[1]) * mult)
                        incubations.append((dest_well, temp_c, dur_s, temp_c >= 80.0))
                        added_in_call = True
                elif "incubat" in fname.lower():
                    temp_c = float(kw_map.get("temp_c", kw_map.get("temperature", 37.0)) or 37.0)
                    temp_c = min(98.0, max(4.0, temp_c))
                    dur_s = _parse_duration_s_text(kw_map.get("duration", 60.0))
                    incubations.append((dest_well, temp_c, dur_s, temp_c >= 80.0))
                    added_in_call = True

                if added_in_call:
                    well_idx += 1

    # Fallback to numbered protocol `steps` text if pseudocode had no literal microliter volumes
    if not reagent_vols and steps_text:
        for idx, m in enumerate(_VOL_RE.finditer(steps_text)):
            raw_v = float(m.group(1))
            if m.group(2).lower() == "ml":
                raw_v *= 1000.0
            if 0.5 <= raw_v <= 300.0:
                reagent_vols.append(("plate:A1", f"reagent_{idx + 1}", round(raw_v, 1)))
            if len(reagent_vols) >= 8:
                break

    if not reagent_vols and not incubations:
        raise ValueError("BioPlanner record contained no convertible liquid-handling or thermal steps")

    reagent_to_res: dict[str, str] = {}
    start_map: dict[str, tuple[float, str]] = {}
    diluents_set: set[str] = set(DEFAULT_DILUENTS)

    for _, r_label, vol_ul in reagent_vols:
        if r_label not in reagent_to_res:
            col = min(len(reagent_to_res) + 1, 11)
            reagent_to_res[r_label] = f"res:{col}"
        res_loc = reagent_to_res[r_label]
        prev_v, _ = start_map.get(res_loc, (1000.0, r_label))
        start_map[res_loc] = (_clean_vol(prev_v + max(vol_ul * 10.0, 200.0)), r_label)
        if _is_diluent_name(r_label):
            diluents_set.add(r_label)

    all_vols = [v for _, _, v in reagent_vols]
    pip_min, pip_max, pipettes = _select_pipette_mount_for_vols(all_vols)
    multi_pip = len(pipettes) > 1

    inc_by_well: dict[str, list[tuple[float, float, bool]]] = {}
    for w, t, d, s in incubations:
        inc_by_well.setdefault(w, []).append((t, d, s))

    ordered_wells: list[str] = []
    for w, _, _ in reagent_vols:
        if w not in ordered_wells:
            ordered_wells.append(w)
    for w in inc_by_well:
        if w not in ordered_wells:
            ordered_wells.append(w)

    steps: list[dict[str, Any]] = []
    sealed_wells: set[str] = set()
    well_running_vol: dict[str, float] = {}

    for well in ordered_wells:
        for w, r_label, vol_ul in reagent_vols:
            if w != well:
                continue
            src = reagent_to_res[r_label]
            mount = _pick_mount_name(vol_ul, pipettes) if multi_pip else None
            p_kw = {"pipette": mount} if mount else {}
            steps.append({"op": "pick_up_tip", **p_kw})
            steps.append({"op": "aspirate", "loc": src, "vol": vol_ul, **p_kw})
            disp_step: dict[str, Any] = {"op": "dispense", "loc": well, "vol": vol_ul, **p_kw}
            if well_running_vol.get(well, 0.0) > VOL_EPS:
                disp_step["air_dispense"] = True
            steps.append(disp_step)
            steps.append({"op": "drop_tip", **p_kw})
            well_running_vol[well] = _clean_vol(well_running_vol.get(well, 0.0) + vol_ul)

        for temp_c, dur_s, needs_seal in inc_by_well.get(well, ()):
            if needs_seal and well not in sealed_wells:
                steps.append({"op": "seal", "loc": well})
                sealed_wells.add(well)
            steps.append(
                {
                    "op": "incubate",
                    "loc": well,
                    "temp_c": temp_c,
                    "duration_s": dur_s,
                }
            )

    well_max = max(300.0, max(well_running_vol.values(), default=0.0))
    spec = DeckSpec(
        well_max=float(well_max),
        pip_min=float(pip_min),
        pip_max=float(pip_max),
        pipettes=pipettes if multi_pip else {},
        start=start_map or dict(DEFAULT_DECK_SPEC.start),
        diluents=frozenset(diluents_set),
    )
    return steps, spec


def from_bioplanner(
    record: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], DeckSpec]:
    """Convert BioPlanner (`BioProt`) JSON or structured WLP action steps into twin micro-steps."""
    if isinstance(record, Mapping) and (
        "generated_pseudocode" in record
        or "edited_pseudocode" in record
        or ("steps" in record and isinstance(record["steps"], str))
    ):
        return _from_bioplanner_pseudocode(record)

    raw_actions: Sequence[Any]
    if isinstance(record, Mapping):
        raw_actions = (
            record.get("protocol_steps")
            or record.get("actions")
            or record.get("steps")
            or []
        )
    elif isinstance(record, Sequence) and not isinstance(record, (str, bytes)):
        raw_actions = record
    else:
        raise ValueError("BioPlanner/WLP input must be a BioProt dict or structured action sequence")

    reagent_to_res: dict[str, str] = {}
    start_map: dict[str, tuple[float, str]] = {}
    diluents_set: set[str] = set(DEFAULT_DILUENTS)
    frozen_locs: set[str] = set()
    thawed_seen: set[str] = set()
    vols_seen: list[float] = []

    def resolve_loc(raw_loc: Any, *, is_source: bool, reagent_hint: str = "reagent") -> str:
        s = str(raw_loc or "").strip()
        if s == "plate" or s.startswith(("plate:", "res:", "trash:")):
            return s
        if _WELL_COORD_RE.match(s.upper()):
            return f"plate:{s.upper()}"
        if "trash" in s.lower() or "waste" in s.lower():
            return "res:12"
        if is_source:
            tag = re.sub(r"[^a-z0-9_]+", "_", (s or reagent_hint).lower()).strip("_") or "reagent"
            if tag not in reagent_to_res:
                col = min(len(reagent_to_res) + 1, 11)
                reagent_to_res[tag] = f"res:{col}"
            return reagent_to_res[tag]
        return "plate:A1"

    # First pass: collect stock volumes, pipette bounds, and frozen containers
    for act in raw_actions:
        if not isinstance(act, Mapping):
            raise ValueError(f"structured WLP step must be a dict, got {act!r}")
        kind = str(act.get("action", act.get("op", ""))).lower().strip()
        v = _parse_volume_ul_text(act.get("volume", act.get("vol")))
        if v is not None and v > 0:
            vols_seen.append(v)
        if kind == "thaw":
            tloc = resolve_loc(act.get("target", act.get("loc", "res:2")), is_source="res" in str(act.get("loc", "")))
            frozen_locs.add(tloc)
            thawed_seen.add(tloc)
        if bool(act.get("frozen", False)):
            floc = resolve_loc(act.get("source", act.get("loc", "res:2")), is_source=True)
            frozen_locs.add(floc)

    pip_min, pip_max, pipettes = _select_pipette_mount_for_vols(vols_seen)
    multi_pip = len(pipettes) > 1
    steps: list[dict[str, Any]] = []
    well_vols: dict[str, float] = {}

    for act in raw_actions:
        kind = str(act.get("action", act.get("op", ""))).lower().strip()
        reagent_name = re.sub(
            r"[^a-z0-9_]+", "_", str(act.get("reagent", "buffer")).lower()
        ).strip("_") or "buffer"

        if kind in ("add", "pipette", "transfer", "dispense"):
            vol = _parse_volume_ul_text(act.get("volume", act.get("vol", 100.0))) or 100.0
            src = resolve_loc(act.get("source", reagent_name), is_source=True, reagent_hint=reagent_name)
            dst = resolve_loc(act.get("destination", act.get("dest", act.get("loc", "plate:A1"))), is_source=False)
            if src.startswith("res:"):
                prev_v, prev_l = start_map.get(src, (1000.0, reagent_name))
                start_map[src] = (_clean_vol(prev_v + max(vol * 5.0, 500.0)), prev_l)
                if _is_diluent_name(prev_l):
                    diluents_set.add(prev_l)
            mount = _pick_mount_name(vol, pipettes) if multi_pip else None
            p_kw = {"pipette": mount} if mount else {}
            steps.append({"op": "pick_up_tip", **p_kw})
            steps.append({"op": "aspirate", "loc": src, "vol": vol, **p_kw})
            d_step: dict[str, Any] = {"op": "dispense", "loc": dst, "vol": vol, **p_kw}
            if bool(act.get("air_dispense", False)) or well_vols.get(dst, 0.0) > VOL_EPS:
                d_step["air_dispense"] = True
            steps.append(d_step)
            if bool(act.get("mix_after", False)):
                mix_v = min(vol, well_vols.get(dst, 0.0) + vol)
                steps.append({"op": "mix", "loc": dst, "vol": mix_v, "reps": int(act.get("reps", 3)), **p_kw})
            steps.append({"op": "drop_tip", **p_kw})
            well_vols[dst] = _clean_vol(well_vols.get(dst, 0.0) + vol)

        elif kind in ("mix", "vortex", "resuspend"):
            dst = resolve_loc(act.get("destination", act.get("loc", "plate:A1")), is_source=False)
            vol = _parse_volume_ul_text(act.get("volume", act.get("vol", 50.0))) or 50.0
            reps = max(1, min(100, int(act.get("reps", act.get("cycles", 3)))))
            mount = _pick_mount_name(vol, pipettes) if multi_pip else None
            p_kw = {"pipette": mount} if mount else {}
            steps.append({"op": "pick_up_tip", **p_kw})
            steps.append({"op": "mix", "loc": dst, "vol": vol, "reps": reps, **p_kw})
            steps.append({"op": "drop_tip", **p_kw})

        elif kind == "discard":
            src = resolve_loc(act.get("source", act.get("loc", "plate:A1")), is_source=False)
            vol = _parse_volume_ul_text(act.get("volume", act.get("vol", 100.0))) or 100.0
            mount = _pick_mount_name(vol, pipettes) if multi_pip else None
            p_kw = {"pipette": mount} if mount else {}
            steps.append({"op": "pick_up_tip", **p_kw})
            steps.append({"op": "aspirate", "loc": src, "vol": vol, **p_kw})
            steps.append({"op": "dispense", "loc": "res:12", "vol": vol, **p_kw})
            steps.append({"op": "drop_tip", **p_kw})
            well_vols[src] = _clean_vol(max(0.0, well_vols.get(src, 0.0) - vol))

        elif kind in ("seal", "cover"):
            loc = resolve_loc(act.get("target", act.get("loc", "plate")), is_source=False)
            steps.append({"op": "seal", "loc": loc})

        elif kind == "thaw":
            loc = resolve_loc(act.get("target", act.get("loc", "res:2")), is_source="res" in str(act.get("loc", "")))
            steps.append({"op": "thaw", "loc": loc})

        elif kind in ("incubate", "thermocycle", "pcr"):
            loc = resolve_loc(act.get("target", act.get("loc", "plate")), is_source=False)
            temp_c = float(act.get("temp_c", act.get("temperature", 37.0)))
            dur_s = _parse_duration_s_text(act.get("duration_s", act.get("duration", 60.0)))
            steps.append({"op": "incubate", "loc": loc, "temp_c": temp_c, "duration_s": dur_s})

        else:
            raise ValueError(f"unsupported BioPlanner/WLP action: {kind!r}")

    if not start_map:
        start_map = dict(DEFAULT_DECK_SPEC.start)
    for floc in frozen_locs:
        if floc.startswith("res:") and floc not in start_map:
            start_map[floc] = (2000.0, "frozen_stock")

    spec = DeckSpec(
        pip_min=float(pip_min),
        pip_max=float(pip_max),
        pipettes=pipettes if multi_pip else {},
        start=start_map,
        diluents=frozenset(diluents_set),
        frozen_locs=frozenset(frozen_locs),
    )
    return steps, spec


# ---------------------------------------------------------------------------
# 4. Converter C: FineBio (CVPR 2024 / IJCV 2025) Atomic Operation Traces
# ---------------------------------------------------------------------------

_FINEBIO_PIPETTES = {
    "blue_pipette": ("blue", (20.0, 300.0, 1), 100.0),
    "yellow_pipette": ("yellow", (20.0, 200.0, 1), 50.0),
    "red_pipette": ("red", (1.0, 20.0, 1), 10.0),
    "8_channel_pipette": ("multi8", (20.0, 300.0, 8), 100.0),
}

_FINEBIO_STOCK_VESSELS: dict[str, tuple[str, float, str]] = {
    "50ml_tube": ("res:1", 10000.0, "buffer"),
    "15ml_tube": ("res:2", 2000.0, "sample"),
    "micro_tube": ("res:3", 1500.0, "reagent_micro"),
    "tube_without_lid": ("res:4", 1500.0, "lysis_reagent"),
    "tube_with_spin_column": ("plate:B1", 0.0, ""),
    "spin_column": ("plate:B1", 0.0, ""),
}


def from_finebio_trace(
    trace: Mapping[str, Any] | Sequence[Any],
    *,
    video_id: str | None = None,
    default_vol_ul: float | None = None,
) -> tuple[list[dict[str, Any]], DeckSpec]:
    """Convert FineBio hierarchical `verb-manipulated-affected` atomic operation traces into twin steps.

    Supports official `annotation_all.json` payloads (`{"database": {"P03_02_01": ...}}`),
    single-take annotation dicts (`{"annotations": [...]}`), and compact lists of atomic
    operation strings or segment dicts.
    """
    raw_items: Sequence[Any]
    if isinstance(trace, Mapping):
        if "database" in trace and isinstance(trace["database"], Mapping):
            db = trace["database"]
            if not db:
                raise ValueError("FineBio database is empty")
            chosen_id = video_id if video_id is not None else sorted(db.keys())[0]
            if chosen_id not in db:
                raise KeyError(f"video_id {chosen_id!r} not found in FineBio database")
            take_obj = db[chosen_id]
            raw_items = take_obj.get("annotations", []) if isinstance(take_obj, Mapping) else []
        elif "annotations" in trace and isinstance(trace["annotations"], Sequence):
            raw_items = trace["annotations"]
        else:
            raise ValueError("FineBio mapping must contain 'database' or 'annotations'")
    elif isinstance(trace, Sequence) and not isinstance(trace, (str, bytes)):
        raw_items = trace
    else:
        raise ValueError("FineBio trace must be a dict or sequence of atomic operations")

    parsed_ops: list[dict[str, Any]] = []
    used_pipettes: dict[str, tuple[float, float, int]] = {}
    default_vols_by_pip: dict[str, float] = {}

    for item in raw_items:
        if isinstance(item, str):
            label = item.strip()
            parts = label.split("-")
            verb = parts[0] if parts else ""
            manip = parts[1] if len(parts) >= 2 else "none"
            aff = parts[2] if len(parts) >= 3 else "none"
            vol_override = None
            well_override = None
            seg = None
        elif isinstance(item, Mapping):
            label = str(item.get("atomic_operation_label", "")).strip()
            parts = label.split("-") if label else []
            verb = str(item.get("verb_label", parts[0] if parts else "")).strip()
            manip = str(item.get("manipulated_label", parts[1] if len(parts) >= 2 else "none")).strip()
            aff = str(item.get("affected_label", parts[2] if len(parts) >= 3 else "none")).strip()
            vol_override = item.get("volume_ul")
            well_override = item.get("well")
            seg = item.get("segment")
        else:
            raise ValueError(f"invalid FineBio annotation entry: {item!r}")

        for obj_name in (manip, aff):
            if obj_name in _FINEBIO_PIPETTES:
                short_name, bounds, dvol = _FINEBIO_PIPETTES[obj_name]
                used_pipettes[short_name] = bounds
                default_vols_by_pip[short_name] = (
                    float(default_vol_ul) if default_vol_ul is not None else dvol
                )

        parsed_ops.append(
            {
                "verb": verb,
                "manip": manip,
                "aff": aff,
                "vol": float(vol_override) if isinstance(vol_override, (int, float)) else None,
                "well": str(well_override) if well_override else None,
                "seg": seg,
            }
        )

    multi_pip = len(used_pipettes) > 1
    active_pip = next(iter(used_pipettes.keys()), "blue")
    plunger_pressed = False
    inserted_vessel: str | None = None
    inserted_well_override: str | None = None
    in_vessel_cycles = 0
    dispensed_in_vessel = False
    held_vol = 0.0
    dest_well_counter = 0
    plate_filled_wells: set[str] = set()
    plate_sealed = False
    pcr_loaded = False

    start_map: dict[str, tuple[float, str]] = {
        "res:1": (10000.0, "buffer"),
        "res:2": (2000.0, "sample"),
    }
    steps: list[dict[str, Any]] = []

    def pip_kwargs(p_short: str) -> dict[str, Any]:
        kw: dict[str, Any] = {}
        bounds = used_pipettes.get(p_short, (20.0, 300.0, 1))
        if bounds[2] == 8:
            kw["channels"] = 8
        if multi_pip:
            kw["pipette"] = p_short
        return kw

    def map_vessel(vessel: str, well_ov: str | None, for_dispense: bool, p_short: str) -> str:
        nonlocal dest_well_counter
        is_8ch = used_pipettes.get(p_short, (20.0, 300.0, 1))[2] == 8
        if well_ov:
            if well_ov.startswith(("plate:", "res:", "trash:")):
                return well_ov
            return f"plate:{_normalize_plate_well(well_ov)}"
        if vessel == "trash_can":
            return "trash:1"
        if vessel in ("cell_culture_plate", "8_tube_stripes"):
            if is_8ch:
                col = (dest_well_counter % 12) + 1
                if for_dispense:
                    dest_well_counter += 1
                return f"plate:A{col}"
            w = f"plate:{_normalize_plate_well('', dest_well_counter)}"
            if for_dispense:
                dest_well_counter += 1
            return w
        if vessel in _FINEBIO_STOCK_VESSELS:
            loc, init_v, init_l = _FINEBIO_STOCK_VESSELS[vessel]
            if for_dispense and vessel == "micro_tube":
                w = f"plate:{_normalize_plate_well('', dest_well_counter)}"
                dest_well_counter += 1
                return w
            if init_v > 0 and loc not in start_map:
                start_map[loc] = (init_v, init_l)
            return loc
        return "plate:A1"

    for op_info in parsed_ops:
        verb = op_info["verb"]
        manip = op_info["manip"]
        aff = op_info["aff"]
        vol_ov = op_info["vol"]
        well_ov = op_info["well"]

        if manip in _FINEBIO_PIPETTES:
            active_pip = _FINEBIO_PIPETTES[manip][0]
        nom_vol = vol_ov if vol_ov is not None else default_vols_by_pip.get(active_pip, 100.0)
        p_kw = pip_kwargs(active_pip)

        # 1. Tip attachment (`insert-*_pipette-*_tip`)
        if verb == "insert" and manip in _FINEBIO_PIPETTES and aff.endswith("_tip"):
            steps.append({"op": "pick_up_tip", **p_kw})
            held_vol = 0.0
            plunger_pressed = False

        # 2. Tip ejection (`eject-*_pipette-*_tip` or `detach-*_pipette-*_tip`)
        elif verb in ("eject", "detach") and manip in _FINEBIO_PIPETTES and aff.endswith("_tip"):
            steps.append({"op": "drop_tip", **p_kw})
            held_vol = 0.0
            plunger_pressed = False
            inserted_vessel = None

        # 3. Pipette insertion into vessel
        elif verb == "insert" and manip in _FINEBIO_PIPETTES and not aff.endswith("_tip"):
            inserted_vessel = aff
            inserted_well_override = well_ov
            in_vessel_cycles = 0
            dispensed_in_vessel = False

        # 4. Plunger press
        elif verb == "press" and manip in _FINEBIO_PIPETTES:
            if inserted_vessel is not None and held_vol > VOL_EPS:
                dst = map_vessel(inserted_vessel, inserted_well_override or well_ov, True, active_pip)
                d_step: dict[str, Any] = {"op": "dispense", "loc": dst, "vol": held_vol, **p_kw}
                steps.append(d_step)
                if dst.startswith("plate:"):
                    plate_filled_wells.add(dst)
                held_vol = 0.0
                dispensed_in_vessel = True
            plunger_pressed = True

        # 5. Plunger release
        elif verb == "release" and manip in _FINEBIO_PIPETTES:
            if inserted_vessel is not None and plunger_pressed:
                if dispensed_in_vessel:
                    dispensed_in_vessel = False
                else:
                    in_vessel_cycles += 1
            plunger_pressed = False

        # 6. Pipette withdrawal from vessel (`detach-*_pipette-<vessel>`)
        elif verb == "detach" and manip in _FINEBIO_PIPETTES and not aff.endswith("_tip"):
            vessel = inserted_vessel or aff
            if in_vessel_cycles == 1 and held_vol <= VOL_EPS:
                src = map_vessel(vessel, inserted_well_override or well_ov, False, active_pip)
                steps.append({"op": "aspirate", "loc": src, "vol": nom_vol, **p_kw})
                held_vol = nom_vol
            elif in_vessel_cycles >= 2:
                loc = map_vessel(vessel, inserted_well_override or well_ov, False, active_pip)
                steps.append(
                    {"op": "mix", "loc": loc, "vol": nom_vol, "reps": in_vessel_cycles, **p_kw}
                )
            inserted_vessel = None
            inserted_well_override = None
            in_vessel_cycles = 0
            dispensed_in_vessel = False

        # 7. Plate sealing & PCR thermal cycling
        elif verb == "put" and aff == "pcr_machine":
            pcr_loaded = True
        elif verb == "press" and manip == "pcr_machine" and pcr_loaded:
            if not plate_sealed:
                steps.append({"op": "seal", "loc": "plate"})
                plate_sealed = True
            steps.append({"op": "incubate", "loc": "plate", "temp_c": 95.0, "duration_s": 180.0})
            pcr_loaded = False

    if used_pipettes:
        first_b = next(iter(used_pipettes.values()))
        p_min, p_max, p_ch = first_b
    else:
        p_min, p_max, p_ch = 20.0, 300.0, 1

    spec = DeckSpec(
        pip_min=float(p_min),
        pip_max=float(p_max),
        channels=int(p_ch),
        pipettes=used_pipettes if multi_pip else {},
        start=start_map,
    )
    return steps, spec


# ---------------------------------------------------------------------------
# 5. Converter D: BioProBench (`ORD` / `ERR` / `GEN`) & ProtocolQA (`LAB-Bench`)
# ---------------------------------------------------------------------------


def _parse_procedural_sentences(
    sentences: Sequence[str],
    *,
    scale_macro_volumes: bool = True,
) -> tuple[list[dict[str, Any]], DeckSpec]:
    """Lower natural-language wet-lab procedural sentences into deterministic twin micro-steps."""
    joined_lower = " ".join(sentences).lower()
    has_thaw_anywhere = bool(re.search(r"\bthaw(?:ed|ing)?\b", joined_lower))

    frozen_locs: set[str] = {"res:2"} if has_thaw_anywhere else set()
    start_map: dict[str, tuple[float, str]] = {
        "res:1": (10000.0, "buffer"),
        "res:2": (2000.0, "sample"),
        "res:3": (3000.0, "reagent_a"),
        "res:4": (3000.0, "stop_solution"),
    }

    steps: list[dict[str, Any]] = []
    vols_seen: list[float] = []
    filled_wells: set[str] = set()
    well_vol_a1 = 0.0

    def normalize_sentence_vol(raw_text: str, default_ul: float = 100.0) -> float:
        m = _VOL_RE.search(raw_text)
        if not m:
            return default_ul
        val = float(m.group(1))
        unit = m.group(2).lower()
        if unit == "ml":
            val *= 1000.0
        # Preserve 96-well overfill defects (301..999 uL in uL units) so ERR catches `no_overfill`,
        # while scaling multi-mL 15 mL centrifuge-tube steps at 1:50 onto a 300 uL 96-well plate.
        if scale_macro_volumes and unit == "ml" and val > 300.0:
            val = max(20.0, min(250.0, round(val / 50.0, 1)))
        return round(val, 1)

    for raw_s in sentences:
        s = raw_s.strip()
        if not s:
            continue
        lc = s.lower()

        # Explicit thaw step
        if re.match(r"^(?:slowly\s+|gently\s+)?thaw\b", lc) or "thaw frozen" in lc:
            steps.append({"op": "thaw", "loc": "res:2"})
            continue

        # Explicit plate sealing step
        if re.search(r"\b(?:seal|apply\s+(?:adhesive\s+)?seal|cover\s+with\s+optical\s+film)\b", lc):
            steps.append({"op": "seal", "loc": "plate:A1"})
            continue

        # Supernatant removal / discard step (leaves ~20 uL cell pellet when suspension is present)
        if re.search(r"\b(?:discard|remove|aspirate)\s+(?:the\s+)?supernatant\b", lc):
            if well_vol_a1 >= 40.0:
                vol = round(well_vol_a1 - 20.0, 1)
            else:
                vol = normalize_sentence_vol(s, default_ul=60.0)
            vols_seen.append(vol)
            steps.append({"op": "pick_up_tip"})
            steps.append({"op": "aspirate", "loc": "plate:A1", "vol": vol})
            steps.append({"op": "dispense", "loc": "res:12", "vol": vol})
            steps.append({"op": "drop_tip"})
            well_vol_a1 = max(0.0, round(well_vol_a1 - vol, 1))
            if "resuspend" in lc:
                r_vol = normalize_sentence_vol(lc.split("resuspend", 1)[1], default_ul=60.0)
                vols_seen.append(r_vol)
                steps.append({"op": "pick_up_tip"})
                steps.append({"op": "aspirate", "loc": "res:3", "vol": r_vol})
                steps.append(
                    {
                        "op": "dispense",
                        "loc": "plate:A1",
                        "vol": r_vol,
                        "air_dispense": True,
                    }
                )
                steps.append({"op": "drop_tip"})
                steps.append({"op": "pick_up_tip"})
                steps.append({"op": "mix", "loc": "plate:A1", "vol": min(r_vol, 50.0), "reps": 3})
                steps.append({"op": "drop_tip"})
                filled_wells.add("plate:A1")
                well_vol_a1 = round(well_vol_a1 + r_vol, 1)
            continue

        # Wash step
        if re.match(r"^wash\b", lc):
            vol = normalize_sentence_vol(s, default_ul=100.0)
            vols_seen.append(vol)
            steps.append({"op": "pick_up_tip"})
            steps.append({"op": "aspirate", "loc": "res:1", "vol": vol})
            d_step: dict[str, Any] = {"op": "dispense", "loc": "plate:A1", "vol": vol}
            if "plate:A1" in filled_wells:
                d_step["air_dispense"] = True
            steps.append(d_step)
            steps.append({"op": "drop_tip"})
            filled_wells.add("plate:A1")
            well_vol_a1 = round(well_vol_a1 + vol, 1)
            continue

        # Pure incubation / maintenance / thermal hold step
        if re.search(r"\b(?:incubate|maintain|denature|heat|anneal)\b", lc) and not re.search(
            r"\b(?:add|transfer|pipette|dispense)\b", lc
        ):
            m_t = _TEMP_RE.search(s)
            if m_t:
                temp_c = float(m_t.group(1))
            elif "room temperature" in lc or "rt" in lc.split():
                temp_c = 25.0
            elif "ice" in lc:
                temp_c = 4.0
            else:
                temp_c = 37.0
            dur_s = _parse_duration_s_text(s, default_s=300.0)
            steps.append(
                {
                    "op": "incubate",
                    "loc": "plate:A1",
                    "temp_c": temp_c,
                    "duration_s": dur_s,
                }
            )
            continue

        # Reagent / sample / stop-solution addition or transfer
        if re.search(r"\b(?:add|transfer|pipette|dispense|aliquot|seed|plate|stop|prepare)\b", lc) or _VOL_RE.search(s):
            vol = normalize_sentence_vol(s, default_ul=100.0)
            if "final volume of" in lc and vol > well_vol_a1 + 20.0:
                vol = round(vol - well_vol_a1, 1)
            vols_seen.append(vol)
            if any(tok in lc for tok in ("sample", "cell", "lysate", "template", "stock", "thawed")):
                src = "res:2"
            elif "stop" in lc:
                src = "res:4"
            elif any(tok in lc for tok in ("ack", "substrate", "primer", "inhibitor")):
                src = "res:3"
            else:
                src = "res:1"
            need_mix = "mix" in lc or "resuspend" in lc
            steps.append({"op": "pick_up_tip"})
            steps.append({"op": "aspirate", "loc": src, "vol": vol})
            d_add: dict[str, Any] = {"op": "dispense", "loc": "plate:A1", "vol": vol}
            if "plate:A1" in filled_wells and not need_mix:
                d_add["air_dispense"] = True
            steps.append(d_add)
            steps.append({"op": "drop_tip"})
            if need_mix:
                steps.append({"op": "pick_up_tip"})
                steps.append({"op": "mix", "loc": "plate:A1", "vol": min(vol, 100.0), "reps": 3})
                steps.append({"op": "drop_tip"})
            filled_wells.add("plate:A1")
            well_vol_a1 = round(well_vol_a1 + vol, 1)

    if not steps:
        raise ValueError("BioProBench/ProtocolQA record yielded zero convertible steps")

    # Keep standard P300 [20, 300] bounds unless valid micro-volumes exist without sub-min corruption
    pip_min, pip_max = 20.0, 300.0
    spec = DeckSpec(
        pip_min=pip_min,
        pip_max=pip_max,
        start=start_map,
        frozen_locs=frozenset(frozen_locs),
    )
    return steps, spec


def from_bioprobench(
    record: Mapping[str, Any],
    *,
    variant: str = "correct",
    scale_macro_volumes: bool = True,
) -> tuple[list[dict[str, Any]], DeckSpec]:
    """Convert BioProBench (`ORD` / `ERR` / `GEN`) or `LAB-Bench` `ProtocolQA` records into twin steps.

    Parameters
    ----------
    record:
        A BioProBench task dict (`correct_steps`/`wrong_steps` for `ORD`;
        `corrected_text`/`corrupted_text` for `ERR`; `steps`/`protocol` for `GEN`/`ProtocolQA`).
    variant:
        `"correct"` / `"corrected"` to extract ground-truth valid steps, or
        `"wrong"` / `"corrupted"` to extract perturbed steps for negative-control rehearsal.
    """
    if not isinstance(record, Mapping):
        raise ValueError("BioProBench input must be a mapping/dict")

    # Case 1: BioProBench ORD (`correct_steps` vs `wrong_steps`)
    if "correct_steps" in record or "wrong_steps" in record:
        use_wrong = variant in ("wrong", "corrupted")
        key = "wrong_steps" if use_wrong else "correct_steps"
        raw_list = record.get(key)
        if not isinstance(raw_list, Sequence) or isinstance(raw_list, (str, bytes)):
            raise ValueError(f"BioProBench ORD record missing list {key!r}")
        return _parse_procedural_sentences(
            [str(x) for x in raw_list], scale_macro_volumes=scale_macro_volumes
        )

    # Case 2: BioProBench ERR (`corrected_text` vs `corrupted_text`)
    if "corrected_text" in record or "corrupted_text" in record:
        use_corrupted = variant in ("wrong", "corrupted")
        body = str(record.get("corrupted_text" if use_corrupted else "corrected_text", ""))
        ctx = record.get("context") if isinstance(record.get("context"), Mapping) else {}
        sentences: list[str] = []
        prior = str(ctx.get("prior_step", "")).strip()
        if prior and any(k in prior.lower() for k in ("thaw", "seal", "add", "wash")):
            sentences.append(prior)
        sentences.extend(part.strip() for part in re.split(r"(?<=[.;])\s+|\n+", body) if part.strip())
        return _parse_procedural_sentences(sentences, scale_macro_volumes=scale_macro_volumes)

    # Case 3: ProtocolQA / GEN (`protocol` or `steps` list/text)
    raw_proto = record.get("protocol", record.get("steps"))
    if isinstance(raw_proto, str):
        lines = [ln.strip() for ln in raw_proto.splitlines() if ln.strip()]
        return _parse_procedural_sentences(lines, scale_macro_volumes=scale_macro_volumes)
    if isinstance(raw_proto, Sequence) and not isinstance(raw_proto, (str, bytes)):
        if raw_proto and isinstance(raw_proto[0], Mapping):
            return from_bioplanner(raw_proto)
        return _parse_procedural_sentences(
            [str(x) for x in raw_proto], scale_macro_volumes=scale_macro_volumes
        )

    raise ValueError("unrecognized BioProBench / ProtocolQA record structure")


# ---------------------------------------------------------------------------
# 6. Unified Auto-Detecting Dispatcher & End-to-End Dry-Run Rehearsal Helper
# ---------------------------------------------------------------------------


def convert_open_protocol(
    payload: Any,
    *,
    fmt: str = "auto",
    variant: str = "correct",
) -> tuple[list[dict[str, Any]], DeckSpec]:
    """Auto-detect and convert an external open-dataset protocol into `(steps, DeckSpec)`."""
    norm_fmt = fmt.lower().strip()

    if norm_fmt in ("opentrons_json", "opentrons", "opentrons_protobuild"):
        return from_opentrons_json(payload)
    if norm_fmt in ("bioplanner", "bioprot", "wlp"):
        return from_bioplanner(payload)
    if norm_fmt in ("finebio", "finebio_trace"):
        return from_finebio_trace(payload)
    if norm_fmt in ("bioprobench", "protocolqa", "lab_bench"):
        return from_bioprobench(payload, variant=variant)
    if norm_fmt == "twinstep":
        return from_twinstep(payload, tip_policy="smart"), DEFAULT_DECK_SPEC
    if norm_fmt == "lablean":
        return from_lablean(payload), DEFAULT_DECK_SPEC
    if norm_fmt == "stepbot":
        return from_stepbot(payload)
    if norm_fmt == "native":
        if not isinstance(payload, list):
            raise ValueError("native protocol must be a list of step dicts")
        return list(payload), DEFAULT_DECK_SPEC

    if norm_fmt != "auto":
        raise ValueError(f"unsupported open protocol format: {fmt!r}")

    # Auto-detection heuristics
    if isinstance(payload, Mapping):
        if (
            "$otSharedSchema" in payload
            or "commands" in payload
            or ("content" in payload and "def run(" in str(payload.get("content", "")))
        ):
            return from_opentrons_json(payload)
        if "database" in payload or "annotations" in payload:
            return from_finebio_trace(payload)
        if (
            "correct_steps" in payload
            or "wrong_steps" in payload
            or "corrected_text" in payload
            or "corrupted_text" in payload
        ):
            return from_bioprobench(payload, variant=variant)
        if (
            "generated_pseudocode" in payload
            or "edited_pseudocode" in payload
            or "protocol_steps" in payload
            or "actions" in payload
        ):
            return from_bioplanner(payload)
        if "pipettes" in payload or "deck" in payload:
            return from_stepbot(payload)
        if isinstance(payload.get("steps"), list) and payload["steps"]:
            s0 = payload["steps"][0]
            if isinstance(s0, Mapping) and "kind" in s0:
                return from_lablean(payload), DEFAULT_DECK_SPEC
            if isinstance(s0, Mapping) and "type" in s0:
                return from_twinstep(payload, tip_policy="smart"), DEFAULT_DECK_SPEC
            if isinstance(s0, str):
                return from_bioprobench(payload, variant=variant)
        if isinstance(payload.get("steps"), str):
            return from_bioplanner(payload)

    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        items = list(payload)
        if not items:
            return [], DEFAULT_DECK_SPEC
        first = items[0]
        if isinstance(first, str):
            if "-" in first and any(
                first.startswith(v + "-")
                for v in ("take", "put", "press", "release", "eject", "open", "close", "insert", "detach", "shake")
            ):
                return from_finebio_trace(items)
            return _parse_procedural_sentences([str(x) for x in items])
        if isinstance(first, Mapping):
            if "atomic_operation_label" in first or "verb_label" in first:
                return from_finebio_trace(items)
            if "action" in first:
                return from_bioplanner(items)
            if "type" in first and "op" not in first:
                return from_twinstep(items, tip_policy="smart"), DEFAULT_DECK_SPEC
            if "op" in first:
                return [dict(x) for x in items], DEFAULT_DECK_SPEC

    raise ValueError("unable to auto-detect open protocol format from payload")


def rehearse_open_protocol(
    payload: Any,
    *,
    dataset_id: str | None = None,
    fmt: str = "auto",
    variant: str = "correct",
    purpose: str = "rehearsal_eval",
    require_commercial_ok: bool = False,
    session: RehearsalSession | None = None,
    protocol_name: str = "open_protocol.json",
    catalog_path: Path | None = None,
) -> dict[str, Any]:
    """Validate dataset governance policy, convert protocol, and run 96-well twin verification."""
    if dataset_id is not None:
        policy_ok, policy_reasons = check_dataset_ingestion_policy(
            dataset_id,
            purpose=purpose,
            require_commercial_ok=require_commercial_ok,
            catalog_path=catalog_path,
        )
        if not policy_ok:
            return {
                "dataset_id": dataset_id,
                "policy_approved": False,
                "policy_reasons": policy_reasons,
                "approved": False,
                "schema_errors": [],
                "steps": [],
                "violations": [],
            }
    else:
        policy_reasons = []

    steps, spec = convert_open_protocol(payload, fmt=fmt, variant=variant)
    schema_errors = validate_protocol_schema(steps)

    if session is not None:
        prev_spec = session.spec
        session.spec = spec
        try:
            eval_res = session.evaluate_protocol(protocol_name, steps)
        finally:
            session.spec = prev_spec
        return {
            "dataset_id": dataset_id,
            "policy_approved": True,
            "policy_reasons": policy_reasons,
            "approved": bool(eval_res["approved"]) and not schema_errors,
            "schema_errors": schema_errors,
            "steps": steps,
            "spec": spec,
            "violations": list(eval_res["violations"]),
            "end": eval_res["end"],
            "hash": eval_res["hash"],
            "run_id": eval_res["run_id"],
        }

    trace, end = rehearse(steps, spec=spec)
    approved, violations = verify(trace, end)
    return {
        "dataset_id": dataset_id,
        "policy_approved": True,
        "policy_reasons": policy_reasons,
        "approved": bool(approved) and not schema_errors,
        "schema_errors": schema_errors,
        "steps": steps,
        "spec": spec,
        "trace": trace,
        "end": end,
        "violations": violations,
    }
