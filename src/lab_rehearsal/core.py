"""Core digital-twin state machine, default-deny verification gate, and episodic memory."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import tempfile
from typing import Any, Callable, Mapping, Sequence
import uuid

RULES_VERSION = "2.0.0"
VOL_EPS = 1e-9
CONC_TOL = 1e-3  # 0.1% concentration rounding tolerance (aligned with LabLean concTolPermille=1)

WELL_MAX, RES_MAX = 300.0, 15000.0
PIP_MIN, PIP_MAX = 20.0, 300.0
TIP_RACK = 96
PLATE_DEAD_VOL = 5.0
RES_DEAD_VOL = 500.0

START: dict[str, tuple[float, str]] = {
    "res:1": (10000.0, "buffer"),
    "res:2": (2000.0, "sample"),
}
DEFAULT_DILUENTS: frozenset[str] = frozenset({"buffer", "water", "pbs", "diluent"})
DEFAULT_WASTE_LOCS: frozenset[str] = frozenset({"res:12", "trash:1"})

DEFAULT_EVAP_COEFFS: dict[str, float] = {
    "ethanol_70pct": 0.42,
    "whole_serum": 0.16,
    "dmso_90pct": 0.14,
    "glycerol_50pct": 0.06,
    "peg_400": 0.05,
    "acetone": 0.65,
    "methanol": 0.55,
    "acetonitrile": 0.48,
}
VOLATILE_CARRIERS: frozenset[str] = frozenset(
    {
        "buffer",
        "water",
        "pbs",
        "diluent",
        "ethanol_70pct",
        "dmso_90pct",
        "glycerol_50pct",
        "peg_400",
        "acetone",
        "methanol",
        "acetonitrile",
    }
)
VALID_ATTRIBUTION_OUTCOMES: frozenset[str] = frozenset(
    {
        "EXECUTION_ARTIFACT",
        "AMBIGUOUS_CHARACTERIZATION",
        "CERTIFIED_POSITIVE",
        "CERTIFIED_NEGATIVE",
    }
)

LIQUID_OPS: frozenset[str] = frozenset({"aspirate", "dispense", "mix"})
THERMAL_STATE_OPS: frozenset[str] = frozenset({"incubate", "seal", "thaw"})
KNOWN_OPS: frozenset[str] = LIQUID_OPS | {"pick_up_tip", "drop_tip"} | THERMAL_STATE_OPS

ALLOWED_KEYS_BY_OP: dict[str, frozenset[str]] = {
    "pick_up_tip": frozenset({"op", "loc", "channels", "pipette"}),
    "drop_tip": frozenset({"op", "allow_wet_drop", "channels", "pipette"}),
    "aspirate": frozenset({"op", "loc", "vol", "channels", "pipette"}),
    "dispense": frozenset({"op", "loc", "vol", "air_dispense", "channels", "pipette"}),
    "mix": frozenset({"op", "loc", "vol", "reps", "channels", "pipette"}),
    "incubate": frozenset({"op", "loc", "temp_c", "duration_s"}),
    "seal": frozenset({"op", "loc"}),
    "thaw": frozenset({"op", "loc"}),
}
REQUIRED_KEYS_BY_OP: dict[str, frozenset[str]] = {
    "pick_up_tip": frozenset({"op"}),
    "drop_tip": frozenset({"op"}),
    "aspirate": frozenset({"op", "loc", "vol"}),
    "dispense": frozenset({"op", "loc", "vol"}),
    "mix": frozenset({"op", "loc", "vol"}),
    "incubate": frozenset({"op", "loc", "temp_c", "duration_s"}),
    "seal": frozenset({"op", "loc"}),
    "thaw": frozenset({"op", "loc"}),
}

TIPRACK_COORDS: tuple[str, ...] = tuple(
    f"tiprack:{row}{col}" for row in "ABCDEFGH" for col in range(1, 13)
)
_TIPRACK_RE = re.compile(r"^tiprack(?:_[A-Za-z0-9_]+)?:[A-H](?:1[0-2]|[1-9])$")
_TIPRACK_ROW_A_RE = re.compile(r"^(tiprack(?:_[A-Za-z0-9_]+)?):A(1[0-2]|[1-9])$")
_PLATE_PREFIX_RE = re.compile(r"^plate(?:_[A-Za-z0-9_]+)?$")
_PLATE_ROW_A_RE = re.compile(r"^(plate(?:_[A-Za-z0-9_]+)?):A(1[0-2]|[1-9])$")
_LIQUID_LOC_SCHEMA_RE = re.compile(
    r"^(plate(?:_[A-Za-z0-9_]+)?:[A-H](?:1[0-2]|[1-9])|res:(?:1[0-2]|[1-9])|res_[A-Za-z0-9_]+:[A-H](?:1[0-2]|[1-9])|trash:1)$"
)
_THERMAL_LOC_SCHEMA_RE = re.compile(
    r"^(plate(?:_[A-Za-z0-9_]+)?:[A-H](?:1[0-2]|[1-9])|res:(?:1[0-2]|[1-9])|res_[A-Za-z0-9_]+:[A-H](?:1[0-2]|[1-9])|plate(?:_[A-Za-z0-9_]+)?)$"
)
_MINHASH_SEEDS: tuple[int, ...] = tuple(0x9E3779B1 * (i + 1) + 0x85EBCA77 for i in range(18))


def _clean_vol(val: float) -> float:
    if abs(val) <= VOL_EPS:
        return 0.0
    return round(float(val), 9)


def _is_finite_number(val: Any) -> bool:
    return not isinstance(val, bool) and isinstance(val, (int, float)) and math.isfinite(float(val))


def _is_strict_int(val: Any) -> bool:
    return not isinstance(val, bool) and isinstance(val, int)


def is_tenth_ul_exact(val: float, tol: float = 1e-6) -> bool:
    """Return True iff `val` lies on the exact integer 0.1 uL tenth-microliter grid."""
    if not _is_finite_number(val):
        return False
    scaled = float(val) * 10.0
    return abs(scaled - round(scaled)) <= tol


def to_tenths_exact(val: float, label: str = "volume") -> int:
    """Convert `val` (uL) to integer 0.1 uL units, rejecting sub-0.1 uL fractions."""
    if not is_tenth_ul_exact(val):
        raise ValueError(
            f"{label} {val!r} uL is not an exact multiple of 0.1 uL (LabLean tenth-uL grid)"
        )
    return int(round(float(val) * 10.0))


def compute_thermal_exposure(temp_c: float, open_duration_min: float) -> float:
    """Effective thermal evaporation exposure unit for heated open-well operations."""
    thermal_factor = 1.0 + max(0.0, float(temp_c) - 20.0) / 18.0
    return max(0.0, float(open_duration_min)) * thermal_factor


class Violation(dict):
    """Structured violation dict (`step`, `rule`, `message`) with human-readable `__str__`."""

    def __init__(self, step: int, rule: str, message: str) -> None:
        super().__init__(step=int(step), rule=str(rule), message=str(message))

    def __str__(self) -> str:
        if self["step"] > 0:
            return f"step {self['step']} {self['rule']}: {self['message']}"
        return f"protocol {self['rule']}: {self['message']}"


@dataclass(frozen=True)
class DeckSpec:
    """Immutable physical configuration of the simulated liquid-handling deck."""

    well_max: float = WELL_MAX
    res_max: float = RES_MAX
    trash_max: float = 100000.0
    pip_min: float = PIP_MIN
    pip_max: float = PIP_MAX
    tip_rack: int = TIP_RACK
    plate_dead_vol: float = PLATE_DEAD_VOL
    res_dead_vol: float = RES_DEAD_VOL
    start: Mapping[str, tuple[float, str]] = field(default_factory=lambda: dict(START))
    diluents: frozenset[str] = DEFAULT_DILUENTS
    waste_locs: frozenset[str] = DEFAULT_WASTE_LOCS
    channels: int = 1
    pipettes: Mapping[str, tuple[float, float, int]] = field(default_factory=dict)
    frozen_locs: frozenset[str] = frozenset()
    sealed_locs: frozenset[str] = frozenset()
    evap_coeffs: Mapping[str, float] = field(default_factory=dict)
    strict_tenths: bool = False
    containers: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    tipracks: tuple[str, ...] = ()
    require_declared_start: bool = False

    def to_canonical_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "well_max": float(self.well_max),
            "res_max": float(self.res_max),
            "trash_max": float(self.trash_max),
            "pip_min": float(self.pip_min),
            "pip_max": float(self.pip_max),
            "tip_rack": int(self.tip_rack),
            "plate_dead_vol": float(self.plate_dead_vol),
            "res_dead_vol": float(self.res_dead_vol),
            "start": {k: [float(v[0]), str(v[1])] for k, v in sorted(self.start.items())},
            "diluents": sorted(self.diluents),
            "waste_locs": sorted(self.waste_locs),
        }
        if int(self.channels) != 1:
            out["channels"] = int(self.channels)
        if self.pipettes:
            out["pipettes"] = {
                str(k): [float(v[0]), float(v[1]), int(v[2])]
                for k, v in sorted(self.pipettes.items())
            }
        if self.frozen_locs:
            out["frozen_locs"] = sorted(self.frozen_locs)
        if self.sealed_locs:
            out["sealed_locs"] = sorted(self.sealed_locs)
        if self.evap_coeffs:
            out["evap_coeffs"] = {str(k): float(v) for k, v in sorted(self.evap_coeffs.items())}
        if self.strict_tenths:
            out["strict_tenths"] = True
        if self.containers:
            out["containers"] = {
                str(cp): {
                    "well_max": float(cinfo.get("well_max", self.well_max)),
                    "dead_vol": float(cinfo.get("dead_vol", 0.0)),
                    "role": str(cinfo.get("role", "well")),
                    "wells": [str(w) for w in cinfo.get("wells", ())],
                }
                for cp, cinfo in sorted(self.containers.items())
            }
        if self.tipracks:
            out["tipracks"] = [str(tp) for tp in self.tipracks]
        if self.require_declared_start:
            out["require_declared_start"] = True
        return out


DEFAULT_DECK_SPEC = DeckSpec()


def new_deck(spec: DeckSpec = DEFAULT_DECK_SPEC) -> dict[str, Any]:
    """Initialize a fresh virtual deck with plates, reservoirs, waste, and tip rack(s)."""
    deck: dict[str, Any] = {}
    if "plate" not in spec.containers:
        for row in "ABCDEFGH":
            for col in range(1, 13):
                loc = f"plate:{row}{col}"
                init_vol, init_liq = spec.start.get(loc, (0.0, None))
                v = _clean_vol(float(init_vol))
                deck[loc] = {
                    "vol": v,
                    "max": float(spec.well_max),
                    "dead_vol": float(spec.plate_dead_vol),
                    "role": "waste" if loc in spec.waste_locs else "well",
                    "liquids": {init_liq} if (init_liq and v > VOL_EPS) else set(),
                    "amounts": {init_liq: v} if (init_liq and v > VOL_EPS) else {},
                    "frozen": loc in spec.frozen_locs or "plate" in spec.frozen_locs,
                    "sealed": loc in spec.sealed_locs or "plate" in spec.sealed_locs,
                    "volume_known": (loc in spec.start) if spec.require_declared_start else True,
                }
    if "res" not in spec.containers:
        for col in range(1, 13):
            loc = f"res:{col}"
            init_vol, init_liq = spec.start.get(loc, (0.0, None))
            v = _clean_vol(float(init_vol))
            if loc in spec.waste_locs:
                role = "waste"
            elif spec.require_declared_start and v <= VOL_EPS:
                role = "well"
            else:
                role = "reagent"
            deck[loc] = {
                "vol": v,
                "max": float(spec.res_max),
                "dead_vol": 0.0 if role != "reagent" else float(spec.res_dead_vol),
                "role": role,
                "liquids": {init_liq} if (init_liq and v > VOL_EPS) else set(),
                "amounts": {init_liq: v} if (init_liq and v > VOL_EPS) else {},
                "frozen": loc in spec.frozen_locs,
                "sealed": loc in spec.sealed_locs,
                "volume_known": (loc in spec.start) if spec.require_declared_start else True,
            }
    for prefix, cinfo in sorted(spec.containers.items()):
        w_max = float(cinfo.get("well_max", spec.well_max))
        d_vol = float(cinfo.get("dead_vol", 0.0))
        base_role = str(cinfo.get("role", "well"))
        wells_seq = cinfo.get("wells", ())
        for wname in wells_seq:
            loc = f"{prefix}:{wname}"
            init_vol, init_liq = spec.start.get(loc, (0.0, None))
            v = _clean_vol(float(init_vol))
            role = "waste" if loc in spec.waste_locs else base_role
            deck[loc] = {
                "vol": v,
                "max": w_max,
                "dead_vol": 0.0 if role == "waste" else d_vol,
                "role": role,
                "liquids": {init_liq} if (init_liq and v > VOL_EPS) else set(),
                "amounts": {init_liq: v} if (init_liq and v > VOL_EPS) else {},
                "frozen": loc in spec.frozen_locs or prefix in spec.frozen_locs,
                "sealed": loc in spec.sealed_locs or prefix in spec.sealed_locs,
                "volume_known": (loc in spec.start) if spec.require_declared_start else True,
            }
    for wloc in sorted(spec.waste_locs):
        if wloc not in deck:
            deck[wloc] = {
                "vol": 0.0,
                "max": float(spec.trash_max),
                "dead_vol": 0.0,
                "role": "waste",
                "liquids": set(),
                "amounts": {},
                "frozen": wloc in spec.frozen_locs,
                "sealed": wloc in spec.sealed_locs,
                "volume_known": True,
            }
    tiprack: dict[str, bool] = {}
    for idx, coord in enumerate(TIPRACK_COORDS):
        tiprack[coord] = idx < spec.tip_rack
    for tp in spec.tipracks:
        if tp == "tiprack":
            continue
        for row in "ABCDEFGH":
            for col in range(1, 13):
                tiprack[f"{tp}:{row}{col}"] = True
    deck["_tiprack"] = tiprack
    deck["_spec"] = spec
    return deck


def _conc_map(well: Mapping[str, Any]) -> dict[str, float]:
    vol = float(well.get("vol", 0.0))
    if vol <= VOL_EPS:
        return {}
    amounts: Mapping[str, float] = well.get("amounts", {})
    out = {k: float(a) / vol for k, a in amounts.items() if a > VOL_EPS}
    if not out and well.get("liquids"):
        n = len(well["liquids"])
        out = {k: 1.0 / n for k in well["liquids"]}
    return out


def steps_hash(steps: Any) -> str:
    """Return full 64-hex SHA-256 of canonicalized protocol steps."""
    text = json.dumps(steps, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def env_hash(
    spec: DeckSpec = DEFAULT_DECK_SPEC,
    rules_version: str = RULES_VERSION,
) -> str:
    """Return full 64-hex SHA-256 of deck configuration, initial stock, and rule-set version."""
    envelope = {
        "deck_spec": spec.to_canonical_dict(),
        "rules_version": str(rules_version),
    }
    text = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def protocol_hash(
    steps: Any,
    spec: DeckSpec = DEFAULT_DECK_SPEC,
    rules_version: str = RULES_VERSION,
) -> str:
    """Return full 64-hex SHA-256 of the complete canonical rehearsal envelope."""
    envelope = {
        "steps": steps,
        "deck_spec": spec.to_canonical_dict(),
        "rules_version": str(rules_version),
    }
    text = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _vol_regime_bucket(vol: float, spec: DeckSpec) -> str:
    if vol < spec.pip_min - VOL_EPS:
        return "sub_min"
    if vol > spec.pip_max + VOL_EPS:
        return "over_pip_max"
    if vol < 50.0:
        return "v_20_50"
    if vol < 150.0:
        return "v_50_150"
    if vol <= 300.0 + VOL_EPS:
        return "v_150_300"
    return "v_gt_300"


def compute_structural_signature(
    steps: Any,
    spec: DeckSpec = DEFAULT_DECK_SPEC,
) -> tuple[str, list[str]]:
    """Compute coordinate-normalized `(skeleton_hash, shingle_list)` for sub-linear recall."""
    if not isinstance(steps, list) or not steps:
        return steps_hash(steps), []

    diluents_lc = {d.lower() for d in spec.diluents}
    loc_canon: dict[str, str] = {}
    role_counts: dict[str, int] = {}
    tokens: list[str] = []

    for raw_step in steps:
        if not isinstance(raw_step, Mapping):
            tokens.append("malformed_step")
            continue
        op = str(raw_step.get("op", "unknown"))
        raw_ch = raw_step.get("channels", spec.channels)
        ch = int(raw_ch) if _is_strict_int(raw_ch) else 1

        if op in ("pick_up_tip", "drop_tip"):
            c_role = "tiprack" if op == "pick_up_tip" else "mount"
            loc_rel = "explicit" if (op == "pick_up_tip" and "loc" in raw_step) else "auto"
        else:
            loc = str(raw_step.get("loc", ""))
            if loc in spec.waste_locs or loc.startswith("trash:"):
                c_role = "waste"
            elif loc.startswith("res:"):
                init = spec.start.get(loc)
                if not init or float(init[0]) <= VOL_EPS or not init[1]:
                    c_role = "res_empty"
                elif str(init[1]).lower() in diluents_lc:
                    c_role = "res_diluent"
                else:
                    c_role = "res_solute"
            elif loc.startswith("plate:") or loc == "plate":
                init = spec.start.get(loc)
                if init and float(init[0]) > VOL_EPS:
                    c_role = "plate_prefilled"
                else:
                    c_role = "plate_well"
            else:
                c_role = "unknown_loc"

            if loc not in loc_canon:
                idx = role_counts.get(c_role, 0)
                role_counts[c_role] = idx + 1
                loc_canon[loc] = f"{c_role}#{idx}"
            loc_rel = loc_canon[loc]

        if op in LIQUID_OPS and _is_finite_number(raw_step.get("vol")):
            v_bucket = _vol_regime_bucket(float(raw_step["vol"]), spec)
        elif op == "incubate" and _is_finite_number(raw_step.get("temp_c")):
            tc = float(raw_step["temp_c"])
            v_bucket = "temp_80p" if tc >= 80.0 else ("temp_elev" if tc >= 30.0 else "temp_amb")
        else:
            v_bucket = "na"

        air_suffix = "|air" if raw_step.get("air_dispense") else ""
        tokens.append(f"{op}|{c_role}|{loc_rel}|{v_bucket}|ch{ch}{air_suffix}")

    skel_text = json.dumps(tokens, separators=(",", ":"))
    skel_hash = hashlib.sha256(skel_text.encode("utf-8")).hexdigest()

    shingle_set: set[str] = set()
    if len(tokens) >= 3:
        for i in range(len(tokens) - 2):
            gram = f"{tokens[i]}>>{tokens[i + 1]}>>{tokens[i + 2]}".encode("utf-8")
            shingle_set.add(hashlib.blake2b(gram, digest_size=8).hexdigest())
    else:
        gram = ">>".join(tokens).encode("utf-8")
        shingle_set.add(hashlib.blake2b(gram, digest_size=8).hexdigest())

    return skel_hash, sorted(shingle_set)


def protocol_skeleton_hash(steps: Any, spec: DeckSpec = DEFAULT_DECK_SPEC) -> str:
    """Return 64-hex SHA-256 of the coordinate-normalized step skeleton."""
    skel, _ = compute_structural_signature(steps, spec)
    return skel


def _minhash_lsh_bands(shingles: Sequence[str]) -> tuple[str, ...]:
    """Compute 6 LSH band keys (3 rows/band = 18 MinHash permutations) over 16-hex shingles."""
    if not shingles:
        return ()
    int_vals = [int(s, 16) for s in shingles]
    sig: list[int] = []
    mask = 0xFFFFFFFFFFFFFFFF
    for seed in _MINHASH_SEEDS:
        m = min(((v ^ seed) * 0xBF58476D1CE4E5B9) & mask for v in int_vals)
        sig.append(m)
    bands: list[str] = []
    for b in range(6):
        r0, r1, r2 = sig[3 * b], sig[3 * b + 1], sig[3 * b + 2]
        bands.append(f"b{b}:{r0:016x}:{r1:016x}:{r2:016x}")
    return tuple(bands)


def extract_violation_rule(entry: Any) -> str:
    """Extract rule identifier from structured Violation dict or legacy free-text string safely."""
    if isinstance(entry, Mapping):
        rule = entry.get("rule")
        if isinstance(rule, str) and rule.strip():
            return rule.strip()
        msg = entry.get("message", "")
        if isinstance(msg, str) and msg.strip():
            entry = msg
        else:
            return "unknown_rule"
    if isinstance(entry, str):
        tokens = entry.strip().split()
        if len(tokens) >= 3 and tokens[0] == "step":
            return tokens[2].rstrip(":")
        if len(tokens) >= 2 and tokens[0] == "protocol":
            return tokens[1].rstrip(":")
        for tok in tokens:
            if tok.endswith(":") and len(tok) > 1:
                return tok.rstrip(":")
        return tokens[0].rstrip(":") if tokens else "unknown_rule"
    return "unknown_rule"


class MemoryStore:
    """UTF-8, corruption-resilient, sub-linearly indexed episodic run ledger."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._loaded = False
        self._records: list[dict[str, Any]] = []
        self._by_hash: dict[str, list[dict[str, Any]]] = {}
        self._by_hash_prefix4: dict[str, list[dict[str, Any]]] = {}
        self._by_hash_prefix1: dict[str, list[dict[str, Any]]] = {}
        self._by_steps_hash: dict[str, list[dict[str, Any]]] = {}
        self._by_skeleton_hash: dict[str, list[int]] = {}
        self._by_shingle: dict[str, list[int]] = {}
        self._by_lsh_band: dict[str, list[int]] = {}
        self._shingle_sets: list[frozenset[str]] = []
        self._by_run_id: dict[int, dict[str, Any]] = {}
        self._by_run_uuid: dict[str, dict[str, Any]] = {}
        self._corrupted_lines: int = 0
        self.last_candidates_inspected: int = 0

    def _clear_indices(self) -> None:
        self._records.clear()
        self._by_hash.clear()
        self._by_hash_prefix4.clear()
        self._by_hash_prefix1.clear()
        self._by_steps_hash.clear()
        self._by_skeleton_hash.clear()
        self._by_shingle.clear()
        self._by_lsh_band.clear()
        self._shingle_sets.clear()
        self._by_run_id.clear()
        self._by_run_uuid.clear()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._clear_indices()
        self._corrupted_lines = 0
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    self._corrupted_lines += 1
                    continue
                if not isinstance(rec, dict):
                    self._corrupted_lines += 1
                    continue
                self._index_record(rec)

    def _index_record(self, rec: dict[str, Any]) -> None:
        idx = len(self._records)
        self._records.append(rec)
        if _is_strict_int(rec.get("run")):
            self._by_run_id[int(rec["run"])] = rec
        ruuid = rec.get("run_uuid")
        if isinstance(ruuid, str) and ruuid:
            self._by_run_uuid[ruuid] = rec

        h = str(rec.get("hash", ""))
        if h:
            self._by_hash.setdefault(h, []).append(rec)
            self._by_hash_prefix1.setdefault(h[:1], []).append(rec)
            if len(h) >= 4:
                self._by_hash_prefix4.setdefault(h[:4], []).append(rec)

        sh = str(rec.get("steps_hash", ""))
        if sh:
            self._by_steps_hash.setdefault(sh, []).append(rec)

        skel = rec.get("skeleton_hash")
        if isinstance(skel, str) and skel:
            self._by_skeleton_hash.setdefault(skel, []).append(idx)

        raw_shingles = rec.get("shingles")
        if isinstance(raw_shingles, list) and raw_shingles:
            sh_list = [str(x) for x in raw_shingles if isinstance(x, str)]
            sh_fset = frozenset(sh_list)
            self._shingle_sets.append(sh_fset)
            for s_tok in sh_fset:
                self._by_shingle.setdefault(s_tok, []).append(idx)
            for band_key in _minhash_lsh_bands(sh_list):
                self._by_lsh_band.setdefault(band_key, []).append(idx)
        else:
            self._shingle_sets.append(frozenset())

    def _flush_all_atomic(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as fh:
            for rec in self._records:
                fh.write(json.dumps(rec, ensure_ascii=True) + "\n")
            fh.flush()
        os.replace(tmp_path, self.path)

    @property
    def corrupted_lines(self) -> int:
        self._ensure_loaded()
        return self._corrupted_lines

    def records(self) -> list[dict[str, Any]]:
        self._ensure_loaded()
        return list(self._records)

    def reset(self) -> None:
        if self.path.exists():
            self.path.unlink()
        self._loaded = True
        self._clear_indices()
        self._corrupted_lines = 0

    def _lookup_by_hash_or_prefix(self, h: str) -> list[dict[str, Any]]:
        exact = self._by_hash.get(h)
        if exact:
            self.last_candidates_inspected = len(exact)
            return list(exact)
        if len(h) < 64 and h:
            if len(h) >= 4:
                bucket = self._by_hash_prefix4.get(h[:4], ())
            else:
                bucket = self._by_hash_prefix1.get(h[:1], ())
            self.last_candidates_inspected = len(bucket)
            return [r for r in bucket if str(r.get("hash", "")).startswith(h)]
        self.last_candidates_inspected = 0
        return []

    def recall_similar(
        self,
        steps: Any,
        spec: DeckSpec = DEFAULT_DECK_SPEC,
        *,
        top_k: int = 5,
        min_jaccard: float = 0.5,
        live_deck_epoch: int | None = None,
    ) -> list[dict[str, Any]]:
        """Sub-linear structural protocol similarity lookup via skeleton hash, LSH bands, and prefix filtering."""
        self._ensure_loaded()
        skel_hash, shingles = compute_structural_signature(steps, spec)
        return self._query_similar_from_signature(
            skel_hash,
            shingles,
            top_k=top_k,
            min_jaccard=min_jaccard,
            live_deck_epoch=live_deck_epoch,
        )

    def _query_similar_from_signature(
        self,
        skel_hash: str,
        shingles: Sequence[str],
        *,
        exclude_hash: str | None = None,
        top_k: int = 5,
        min_jaccard: float = 0.5,
        live_deck_epoch: int | None = None,
    ) -> list[dict[str, Any]]:
        candidate_indices: set[int] = set()

        # 1. O(1) exact coordinate-normalized skeleton hash bucket
        for idx in self._by_skeleton_hash.get(skel_hash, ()):
            candidate_indices.add(idx)

        # 2. Sub-linear LSH 3-row bands + Bayardo prefix-filtered rarest shingle postings
        q_set = frozenset(shingles)
        if q_set:
            for band_key in _minhash_lsh_bands(shingles):
                postings = self._by_lsh_band.get(band_key, ())
                if len(postings) <= max(64, len(self._records) // 25):
                    candidate_indices.update(postings)

            # Prefix filtering on rarest shingle posting lists
            m = len(q_set)
            req_overlap = max(1, math.ceil(min_jaccard * m))
            prefix_len = max(1, m - req_overlap + 1)
            sorted_shingles = sorted(q_set, key=lambda s: len(self._by_shingle.get(s, ())))
            max_posting_cap = max(64, len(self._records) // 25)
            for s_tok in sorted_shingles[:prefix_len]:
                plist = self._by_shingle.get(s_tok, ())
                if len(plist) <= max_posting_cap:
                    candidate_indices.update(plist)

        self.last_candidates_inspected = len(candidate_indices)
        scored: list[tuple[float, int, dict[str, Any]]] = []
        for idx in candidate_indices:
            rec = self._records[idx]
            if exclude_hash and rec.get("hash") == exclude_hash:
                continue
            if rec.get("skeleton_hash") == skel_hash:
                jaccard = 1.0
            else:
                r_set = self._shingle_sets[idx]
                if not q_set or not r_set:
                    continue
                inter = len(q_set & r_set)
                if inter == 0:
                    continue
                jaccard = inter / len(q_set | r_set)
            if jaccard < min_jaccard:
                continue

            bt = rec.get("bottom_trace")
            rec_epoch = (
                bt.get("deck_epoch")
                if isinstance(bt, Mapping)
                else rec.get("deck_epoch")
            )
            epoch_stale = (
                live_deck_epoch is not None
                and isinstance(rec_epoch, int)
                and rec_epoch != live_deck_epoch
            )
            scored.append(
                (
                    jaccard,
                    int(rec.get("run", 0)),
                    {
                        "run": int(rec.get("run", 0)),
                        "run_uuid": rec.get("run_uuid"),
                        "protocol": rec.get("protocol"),
                        "jaccard": round(jaccard, 4),
                        "exact_skeleton": rec.get("skeleton_hash") == skel_hash,
                        "passed": bool(rec.get("passed", False)),
                        "verify_passed": bool(rec.get("verify_passed", rec.get("passed", False))),
                        "attribution_outcome": rec.get("attribution_outcome"),
                        "violations": list(rec.get("violations", [])),
                        "bottom_trace": bt,
                        "deck_epoch": rec_epoch,
                        "deck_epoch_stale": epoch_stale,
                    },
                )
            )

        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in scored[: max(1, top_k)]]

    def recall(
        self,
        h: str,
        *,
        s_hash: str | None = None,
        e_hash: str | None = None,
        skeleton_hash: str | None = None,
        shingles: Sequence[str] | None = None,
        live_deck_epoch: int | None = None,
    ) -> str:
        """Return warning/status summary for matching or structurally isomorphic prior runs."""
        self._ensure_loaded()
        past = self._lookup_by_hash_or_prefix(h)

        env_drift_runs: list[int] = []
        if s_hash:
            for r in self._by_steps_hash.get(s_hash, ()):
                if e_hash and r.get("env_hash") and r.get("env_hash") != e_hash:
                    env_drift_runs.append(int(r.get("run", 0)))

        if not past:
            if env_drift_runs:
                return (
                    f"WARNING: identical steps ran in run(s) {env_drift_runs} "
                    "under a different deck/environment config"
                )
            if skeleton_hash:
                sim = self._query_similar_from_signature(
                    skeleton_hash,
                    shingles or (),
                    exclude_hash=h,
                    top_k=5,
                    min_jaccard=0.6,
                    live_deck_epoch=live_deck_epoch,
                )
                sim_failed = [m for m in sim if not m["passed"]]
                stale_epochs = [m["run"] for m in sim if m["deck_epoch_stale"]]
                if sim_failed:
                    sim_runs = sorted({m["run"] for m in sim_failed})
                    rules = sorted(
                        {
                            extract_violation_rule(v)
                            for m in sim_failed
                            for v in m.get("violations", [])
                        }
                    )
                    rule_str = ", ".join(rules) if rules else "runtime_fault"
                    epoch_note = (
                        f"; stale deck_epoch in run(s) {sorted(set(stale_epochs))}"
                        if stale_epochs
                        else ""
                    )
                    return (
                        f"WARNING: structurally similar protocol failed before in run(s) "
                        f"{sim_runs} ({rule_str}){epoch_note}"
                    )
                if stale_epochs:
                    return (
                        f"WARNING: structurally similar protocol in run(s) "
                        f"{sorted(set(stale_epochs))} has stale deck_epoch vs {live_deck_epoch}"
                    )
            return "no earlier runs of this protocol"

        verify_failed = [
            r for r in past if not r.get("verify_passed", r.get("passed", False))
        ]
        run_failed = [
            r
            for r in past
            if r.get("verify_passed", r.get("passed", False))
            and (
                r.get("run_passed") is False
                or r.get("attribution_outcome") in ("EXECUTION_ARTIFACT", "AMBIGUOUS_CHARACTERIZATION")
            )
            and not r.get("passed", False)
        ]
        run_succeeded = [
            r
            for r in past
            if r.get("verify_passed", r.get("passed", False)) and r.get("passed", False)
        ]

        drift_suffix = (
            f"; env changed vs run(s) {env_drift_runs}" if env_drift_runs else ""
        )

        # If the latest run succeeded after earlier transient hardware/attribution faults, surface recovery!
        if run_succeeded and past[-1].get("passed", False) and run_failed and not verify_failed:
            pass_runs = [r["run"] for r in run_succeeded]
            cert_outcomes = [
                str(r["attribution_outcome"])
                for r in run_succeeded
                if r.get("attribution_outcome") in ("CERTIFIED_POSITIVE", "CERTIFIED_NEGATIVE")
            ]
            cert_prefix = f"{cert_outcomes[-1]}, " if cert_outcomes else ""
            fault_runs = [r["run"] for r in run_failed]
            return (
                f"passed in latest run(s) {pass_runs} "
                f"({cert_prefix}recovered after {len(fault_runs)} prior transient fault(s) "
                f"in run(s) {fault_runs}){drift_suffix}"
            )

        if verify_failed:
            rules = sorted(
                {
                    extract_violation_rule(v)
                    for r in verify_failed
                    for v in r.get("violations", [])
                }
            )
            rule_str = ", ".join(rules) if rules else "unknown_rule"
            msg = f"WARNING: failed before in run(s) {[r['run'] for r in verify_failed]} ({rule_str})"
            if run_failed:
                msg += f"; runtime fault in run(s) {[r['run'] for r in run_failed]}"
            return msg + drift_suffix
        if run_failed:
            errs = sorted(
                {str(e) for r in run_failed for e in r.get("run_errors", []) if e}
            )
            artifacts = [
                r["run"] for r in run_failed if r.get("attribution_outcome") == "EXECUTION_ARTIFACT"
            ]
            detail = f" ({', '.join(errs)})" if errs else ""
            if artifacts and len(artifacts) == len(run_failed):
                return (
                    f"WARNING: transient EXECUTION_ARTIFACT in run(s) {artifacts}"
                    f"{detail} (retryable runtime hardware failure){drift_suffix}"
                )
            return (
                f"WARNING: runtime hardware failure in run(s) {[r['run'] for r in run_failed]}"
                f"{detail}{drift_suffix}"
            )
        return f"passed before in run(s) {[r['run'] for r in past]}{drift_suffix}"

    def has_deterministic_verify_failure(self, h: str) -> bool:
        """Return True iff any prior non-fast-deny rehearsal of `h` failed static safety verification."""
        self._ensure_loaded()
        for r in self._by_hash.get(h, ()):
            if not r.get("verify_passed", r.get("passed", True)):
                v_rules = {extract_violation_rule(v) for v in r.get("violations", [])}
                if v_rules != {"known_prior_failure"}:
                    return True
        return False

    def consecutive_execution_artifacts(self, h: str) -> int:
        """Count trailing consecutive `EXECUTION_ARTIFACT` runs since the last clean pass."""
        self._ensure_loaded()
        count = 0
        for r in reversed(self._by_hash.get(h, ())):
            if r.get("passed", False):
                break
            if r.get("attribution_outcome") == "EXECUTION_ARTIFACT":
                count += 1
            else:
                break
        return count

    def should_fast_deny(self, h: str, *, max_artifact_retries: int = 2) -> tuple[bool, str]:
        """Decide whether `h` should be fast-denied, permitting bounded `EXECUTION_ARTIFACT` retries."""
        self._ensure_loaded()
        past = self._by_hash.get(h, ())
        if not past:
            return False, ""
        if self.has_deterministic_verify_failure(h):
            return True, "deterministic_verify_failure"

        latest = past[-1]
        if latest.get("passed", False):
            return False, ""

        outcome = latest.get("attribution_outcome")
        if outcome == "EXECUTION_ARTIFACT":
            consec = self.consecutive_execution_artifacts(h)
            if consec > max_artifact_retries:
                return True, f"exceeded max_artifact_retries ({consec} > {max_artifact_retries})"
            return False, ""

        if outcome == "AMBIGUOUS_CHARACTERIZATION":
            return True, "quarantined_ambiguous_characterization"

        # Unreconciled runtime hardware failure (`attribution_outcome is None` and `passed is False`)
        return True, "unreconciled_runtime_failure"

    def has_known_failure(self, h: str, *, max_artifact_retries: int = 2) -> bool:
        deny, _ = self.should_fast_deny(h, max_artifact_retries=max_artifact_retries)
        return deny

    def reconcile_attribution(
        self,
        run_id_or_uuid: int | str,
        attribution_outcome: str,
        *,
        receipt_sha256: str | None = None,
    ) -> bool:
        """Bind a post-run `lab-attrib` / `CertifiedNegativeLedger` outcome to an episodic record."""
        self._ensure_loaded()
        outcome_str = str(attribution_outcome)
        if outcome_str not in VALID_ATTRIBUTION_OUTCOMES:
            raise ValueError(
                f"invalid attribution_outcome {outcome_str!r}; expected one of {sorted(VALID_ATTRIBUTION_OUTCOMES)}"
            )
        if receipt_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", str(receipt_sha256)):
            raise ValueError(f"receipt_sha256 must be 64-hex lowercase SHA-256, got {receipt_sha256!r}")

        target: dict[str, Any] | None = None
        if isinstance(run_id_or_uuid, int) and not isinstance(run_id_or_uuid, bool):
            target = self._by_run_id.get(run_id_or_uuid)
        elif isinstance(run_id_or_uuid, str):
            target = self._by_run_uuid.get(run_id_or_uuid)
            if target is None and run_id_or_uuid.isdigit():
                target = self._by_run_id.get(int(run_id_or_uuid))

        if target is None:
            return False

        target["attribution_outcome"] = outcome_str
        if receipt_sha256 is not None:
            target["certified_receipt_sha256"] = str(receipt_sha256)

        if target.get("verify_passed", False):
            if outcome_str == "EXECUTION_ARTIFACT":
                target["run_passed"] = False
                target["passed"] = False
            elif outcome_str == "AMBIGUOUS_CHARACTERIZATION":
                target["passed"] = False
            elif outcome_str in ("CERTIFIED_POSITIVE", "CERTIFIED_NEGATIVE"):
                target["run_passed"] = True
                target["passed"] = True

        if self.path.exists() or self._records:
            self._flush_all_atomic()
        return True

    def append(
        self,
        protocol: str,
        h: str,
        verify_passed: bool,
        violations: Sequence[Mapping[str, Any] | str],
        *,
        s_hash: str | None = None,
        e_hash: str | None = None,
        skeleton_hash: str | None = None,
        shingles: Sequence[str] | None = None,
        deck_epoch: int | None = None,
        bottom_trace: Mapping[str, Any] | None = None,
        attribution_outcome: str | None = None,
        retry_count: int | None = None,
        retry_of_run_uuid: str | None = None,
        certified_receipt_sha256: str | None = None,
        rules_version: str = RULES_VERSION,
        run_passed: bool | None = None,
        run_errors: Sequence[str] = (),
    ) -> int:
        self._ensure_loaded()
        max_id = max((int(r.get("run", 0)) for r in self._records), default=0)
        run_id = max_id + 1
        structured_violations: list[dict[str, Any]] = []
        for v in violations:
            if isinstance(v, Mapping):
                structured_violations.append(
                    {
                        "step": int(v.get("step", 0)),
                        "rule": str(v.get("rule", extract_violation_rule(v))),
                        "message": str(v.get("message", "")),
                    }
                )
            else:
                structured_violations.append(
                    {
                        "step": 0,
                        "rule": extract_violation_rule(v),
                        "message": str(v),
                    }
                )
        effective_run_passed: bool | None
        if not verify_passed:
            effective_run_passed = None
        else:
            effective_run_passed = True if run_passed is None else bool(run_passed)
        overall_passed = bool(verify_passed) and (effective_run_passed is not False)
        if attribution_outcome in ("EXECUTION_ARTIFACT", "AMBIGUOUS_CHARACTERIZATION"):
            overall_passed = False

        record: dict[str, Any] = {
            "run": run_id,
            "run_uuid": uuid.uuid4().hex,
            "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "protocol": str(protocol),
            "hash": str(h),
            "steps_hash": str(s_hash or h),
            "env_hash": str(e_hash or env_hash(rules_version=rules_version)),
            "rules_version": str(rules_version),
            "verify_passed": bool(verify_passed),
            "run_passed": effective_run_passed,
            "passed": overall_passed,
            "violations": structured_violations,
            "run_errors": [str(e) for e in run_errors],
        }
        if skeleton_hash is not None:
            record["skeleton_hash"] = str(skeleton_hash)
        if shingles is not None:
            record["shingles"] = [str(s) for s in shingles]
        if deck_epoch is not None:
            record["deck_epoch"] = int(deck_epoch)
        if bottom_trace is not None:
            record["bottom_trace"] = dict(bottom_trace)
        if attribution_outcome is not None:
            record["attribution_outcome"] = str(attribution_outcome)
        if retry_count is not None:
            record["retry_count"] = int(retry_count)
        if retry_of_run_uuid is not None:
            record["retry_of_run_uuid"] = str(retry_of_run_uuid)
        if certified_receipt_sha256 is not None:
            record["certified_receipt_sha256"] = str(certified_receipt_sha256)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(record, ensure_ascii=True) + "\n"
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(serialized)
            fh.flush()
        self._index_record(record)
        return run_id


def _validate_step_schema(step: dict[str, Any]) -> str | None:
    """Validate allowed/required keys and parameter types for a step dictionary."""
    op = step.get("op")
    if not isinstance(op, str) or op not in KNOWN_OPS:
        return None  # Handled by `known_step`

    allowed = ALLOWED_KEYS_BY_OP[op]
    required = REQUIRED_KEYS_BY_OP[op]
    extra = sorted(set(step.keys()) - allowed)
    if extra:
        return f"unexpected key(s) {extra} for op '{op}'"
    missing = sorted(required - set(step.keys()))
    if missing:
        return f"missing required key(s) {missing} for op '{op}'"

    if "channels" in step:
        ch = step["channels"]
        if not _is_strict_int(ch) or ch not in (1, 8):
            return f"'channels' must be integer 1 or 8, got {ch!r}"

    if "pipette" in step:
        pip = step["pipette"]
        if not isinstance(pip, str) or not pip.strip():
            return f"'pipette' must be a non-empty string, got {pip!r}"

    if op in LIQUID_OPS:
        if not isinstance(step.get("loc"), str) or not step["loc"].strip():
            return f"'loc' must be a non-empty string for op '{op}'"
        raw_vol = step.get("vol")
        if not _is_finite_number(raw_vol):
            return f"invalid volume {raw_vol!r} (must be a finite number)"

    if op in THERMAL_STATE_OPS:
        if not isinstance(step.get("loc"), str) or not step["loc"].strip():
            return f"'loc' must be a non-empty string for op '{op}'"

    if op == "incubate":
        raw_temp = step.get("temp_c")
        raw_dur = step.get("duration_s")
        if not _is_finite_number(raw_temp):
            return f"invalid temp_c {raw_temp!r} (must be a finite number)"
        if not _is_finite_number(raw_dur):
            return f"invalid duration_s {raw_dur!r} (must be a finite number)"

    if op == "pick_up_tip" and "loc" in step:
        loc = step["loc"]
        if not isinstance(loc, str) or not _TIPRACK_RE.match(loc):
            return f"invalid tiprack coordinate {loc!r} (expected tiprack:A1..H12)"

    if op == "drop_tip" and "allow_wet_drop" in step:
        if not isinstance(step["allow_wet_drop"], bool):
            return f"'allow_wet_drop' must be a boolean, got {step['allow_wet_drop']!r}"

    if op == "dispense" and "air_dispense" in step:
        if not isinstance(step["air_dispense"], bool):
            return f"'air_dispense' must be a boolean, got {step['air_dispense']!r}"

    if op == "mix" and "reps" in step:
        reps = step["reps"]
        if not _is_strict_int(reps) or not (1 <= reps <= 100):
            return f"invalid mix reps {reps!r} (must be integer in 1..100)"

    return None


def _resolve_step_pipette(step: Mapping[str, Any], spec: DeckSpec) -> tuple[float, float, int]:
    """Return `(pip_min, pip_max, channels)` active for `step`."""
    pip_min, pip_max, channels = float(spec.pip_min), float(spec.pip_max), int(spec.channels)
    mount = step.get("pipette")
    if isinstance(mount, str) and mount in spec.pipettes:
        p_cfg = spec.pipettes[mount]
        pip_min, pip_max, channels = float(p_cfg[0]), float(p_cfg[1]), int(p_cfg[2])
    if "channels" in step and _is_strict_int(step["channels"]):
        channels = int(step["channels"])
    return pip_min, pip_max, channels


def _fresh_nozzle() -> dict[str, Any]:
    return {
        "content": 0.0,
        "pip_amounts": {},
        "touched": set(),
        "touched_snapshots": [],
        "last_conc": {},
        "had_unknown_aspirate": False,
    }


def _apply_open_well_evaporation(
    well: dict[str, Any],
    temp_c: float,
    duration_s: float,
    spec: DeckSpec,
) -> float:
    """Apply open-well volatile solvent loss while preserving non-volatile solute mass."""
    if well.get("sealed", False) or well.get("frozen", False):
        return 0.0
    if well["vol"] <= VOL_EPS or duration_s <= 0.0:
        return 0.0

    evap_map = dict(DEFAULT_EVAP_COEFFS)
    for k, v in spec.evap_coeffs.items():
        evap_map[str(k)] = float(v)

    evap_keys = [k for k in well["liquids"] if evap_map.get(k, 0.0) > 0.0]
    if not evap_keys:
        return 0.0

    w_sum = sum(float(well["amounts"].get(k, 0.0)) for k in evap_keys)
    if w_sum > VOL_EPS:
        eff_coeff = (
            sum(float(well["amounts"].get(k, 0.0)) * evap_map[k] for k in evap_keys) / w_sum
        )
    else:
        eff_coeff = max(evap_map[k] for k in evap_keys)

    exposure = compute_thermal_exposure(temp_c, duration_s / 60.0)
    raw_loss = eff_coeff * exposure
    actual_loss = _clean_vol(min(well["vol"], max(0.0, raw_loss)))
    if actual_loss <= VOL_EPS:
        return 0.0

    new_vol = _clean_vol(well["vol"] - actual_loss)
    if new_vol <= VOL_EPS:
        lost = well["vol"]
        well["vol"] = 0.0
        well["amounts"].clear()
        well["liquids"].clear()
        return lost

    diluents_lc = {d.lower() for d in spec.diluents}
    carrier_keys = [
        k
        for k in list(well["amounts"].keys())
        if k.lower() in VOLATILE_CARRIERS or k.lower() in diluents_lc
    ]
    non_volatile_keys = [k for k in well["amounts"] if k not in carrier_keys]

    if carrier_keys:
        c_total = sum(float(well["amounts"][k]) for k in carrier_keys)
        if c_total > VOL_EPS:
            c_deduct = min(actual_loss, c_total)
            frac_rem = max(0.0, 1.0 - c_deduct / c_total)
            for ck in carrier_keys:
                rem = _clean_vol(float(well["amounts"][ck]) * frac_rem)
                if rem > VOL_EPS:
                    well["amounts"][ck] = rem
                elif not non_volatile_keys and ck == carrier_keys[0]:
                    well["amounts"][ck] = new_vol
                else:
                    well["amounts"].pop(ck, None)

    well["vol"] = new_vol
    well["liquids"] = set(well["amounts"].keys())
    return actual_loss


def rehearse(
    steps: Any,
    *,
    spec: DeckSpec = DEFAULT_DECK_SPEC,
    deck: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Simulate every step without aborting early; record facts for `verify()`."""
    active_deck = deck if deck is not None else new_deck(spec)
    active_spec: DeckSpec = active_deck.get("_spec", spec)
    tiprack: dict[str, bool] = active_deck.setdefault(
        "_tiprack", {c: (idx < active_spec.tip_rack) for idx, c in enumerate(TIPRACK_COORDS)}
    )
    initial_tips_left = sum(1 for v in tiprack.values() if v)

    if not isinstance(steps, list):
        end_summary = {
            "tip_on": False,
            "tips_used": 0,
            "tips_remaining": initial_tips_left,
            "plate": {},
            "deck": active_deck,
            "protocol_error": f"protocol root must be a JSON list of step dicts, got {type(steps).__name__}",
        }
        return [], end_summary

    mounts: dict[str, dict[str, Any]] = {
        "_default": {
            "tip": False,
            "mounted_channels": 0,
            "nozzles": [_fresh_nozzle() for _ in range(8)],
        }
    }
    all_tiprack_prefixes: tuple[str, ...] = ("tiprack",) + tuple(
        tp for tp in active_spec.tipracks if tp != "tiprack"
    )
    all_tiprack_coords: tuple[str, ...] = tuple(
        f"{tp}:{row}{col}"
        for tp in all_tiprack_prefixes
        for row in "ABCDEFGH"
        for col in range(1, 13)
    )
    trace: list[dict[str, Any]] = []

    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            any_t = any(bool(ms["tip"]) for ms in mounts.values())
            trace.append(
                {
                    "i": i,
                    "op": None,
                    "loc": None,
                    "vol": 0.0,
                    "tip": any_t,
                    "known": False,
                    "schema_error": f"step must be an object/dict, got {type(step).__name__}",
                    "spec": active_spec,
                }
            )
            continue

        raw_pip_mount = step.get("pipette")
        mount_key = (
            raw_pip_mount.strip()
            if isinstance(raw_pip_mount, str) and raw_pip_mount.strip()
            else "_default"
        )
        mstate = mounts.setdefault(
            mount_key,
            {
                "tip": False,
                "mounted_channels": 0,
                "nozzles": [_fresh_nozzle() for _ in range(8)],
            },
        )
        tip = bool(mstate["tip"])
        mounted_channels = int(mstate["mounted_channels"])
        nozzles: list[dict[str, Any]] = mstate["nozzles"]

        op = step.get("op")
        loc = step.get("loc")
        schema_err = _validate_step_schema(step)
        pip_min, pip_max, step_channels = _resolve_step_pipette(step, active_spec)

        raw_vol = step.get("vol", 0.0)
        if _is_finite_number(raw_vol):
            vol = _clean_vol(float(raw_vol))
        else:
            vol = 0.0
            if op in LIQUID_OPS and schema_err is None:
                schema_err = f"invalid volume {raw_vol!r} (must be a finite number)"

        is_known_op = isinstance(op, str) and op in KNOWN_OPS
        is_known_loc = True
        well: dict[str, Any] | None = None
        if op in LIQUID_OPS:
            well = active_deck.get(loc) if isinstance(loc, str) and not loc.startswith("_") else None
            is_known_loc = well is not None
        elif op in THERMAL_STATE_OPS:
            if isinstance(loc, str) and _PLATE_PREFIX_RE.match(loc):
                is_known_loc = any(
                    isinstance(k, str) and k.startswith(f"{loc}:") for k in active_deck
                )
            else:
                well = active_deck.get(loc) if isinstance(loc, str) and not loc.startswith("_") else None
                is_known_loc = well is not None
                if well is not None and well.get("role") == "waste" and loc == "trash:1":
                    is_known_loc = False
        elif op == "pick_up_tip" and loc is not None:
            is_known_loc = isinstance(loc, str) and loc in tiprack

        if (
            schema_err is None
            and step_channels == 8
            and op in LIQUID_OPS
            and isinstance(loc, str)
            and (loc.startswith("plate:") or loc.startswith("plate_"))
        ):
            m_plate_a = _PLATE_ROW_A_RE.match(loc)
            if not m_plate_a:
                schema_err = (
                    f"8-channel {op} on 96-well plate must target row A (plate:A1..A12), got {loc!r}"
                )
            else:
                p_pref, p_col = m_plate_a.group(1), int(m_plate_a.group(2))
                if any(f"{p_pref}:{r_ch}{p_col}" not in active_deck for r_ch in "ABCDEFGH"):
                    schema_err = (
                        f"8-channel {op} requires 8 rows A..H on {p_pref} column {p_col}"
                    )

        known = is_known_op and is_known_loc
        tips_left = sum(1 for v in tiprack.values() if v)

        fact: dict[str, Any] = {
            "i": i,
            "op": op,
            "loc": loc,
            "vol": vol,
            "tip": tip,
            "channels": step_channels,
            "mounted_channels": mounted_channels,
            "pip_min": pip_min,
            "pip_max": pip_max,
            "known": known,
            "schema_error": schema_err,
            "spec": active_spec,
        }
        trace.append(fact)

        if not known or schema_err is not None:
            continue

        if op == "pick_up_tip":
            fact["tips_left"] = tips_left
            if step_channels == 1:
                if loc is not None:
                    slot_has_tip = bool(tiprack.get(loc, False))
                    fact["tip_slot"] = loc
                    fact["slot_has_tip"] = slot_has_tip
                    if not tip and slot_has_tip:
                        tiprack[loc] = False
                        mstate["tip"] = True
                        mstate["mounted_channels"] = 1
                        mstate["nozzles"] = [_fresh_nozzle() for _ in range(8)]
                else:
                    next_slot = next((k for k in all_tiprack_coords if tiprack.get(k, False)), None)
                    fact["tip_slot"] = next_slot
                    fact["slot_has_tip"] = next_slot is not None
                    if not tip and next_slot is not None:
                        tiprack[next_slot] = False
                        mstate["tip"] = True
                        mstate["mounted_channels"] = 1
                        mstate["nozzles"] = [_fresh_nozzle() for _ in range(8)]
            else:
                # 8-channel manifold pickup across rows A..H of a single column on target tiprack
                if loc is not None:
                    m_row_a = _TIPRACK_ROW_A_RE.match(loc)
                    if not m_row_a:
                        fact["tip_slot"] = loc
                        fact["slot_has_tip"] = False
                        fact["bad_8ch_tip_row"] = True
                    else:
                        tr_pref = m_row_a.group(1)
                        col = int(m_row_a.group(2))
                        col_slots = [f"{tr_pref}:{r}{col}" for r in "ABCDEFGH"]
                        missing = [s for s in col_slots if not tiprack.get(s, False)]
                        slot_has_tip = len(missing) == 0
                        fact["tip_slot"] = loc
                        fact["slot_has_tip"] = slot_has_tip
                        fact["missing_tip_slots"] = missing
                        if not tip and slot_has_tip:
                            for s_coord in col_slots:
                                tiprack[s_coord] = False
                            mstate["tip"] = True
                            mstate["mounted_channels"] = 8
                            mstate["nozzles"] = [_fresh_nozzle() for _ in range(8)]
                else:
                    chosen_pref: str | None = None
                    chosen_col: int | None = None
                    for tr_pref in all_tiprack_prefixes:
                        for col in range(1, 13):
                            if all(tiprack.get(f"{tr_pref}:{r}{col}", False) for r in "ABCDEFGH"):
                                chosen_pref = tr_pref
                                chosen_col = col
                                break
                        if chosen_col is not None:
                            break
                    if chosen_pref is not None and chosen_col is not None:
                        fact["tip_slot"] = f"{chosen_pref}:A{chosen_col}"
                        fact["slot_has_tip"] = True
                        if not tip:
                            for r in "ABCDEFGH":
                                tiprack[f"{chosen_pref}:{r}{chosen_col}"] = False
                            mstate["tip"] = True
                            mstate["mounted_channels"] = 8
                            mstate["nozzles"] = [_fresh_nozzle() for _ in range(8)]
                    else:
                        fact["tip_slot"] = None
                        fact["slot_has_tip"] = False
                        fact["no_full_column"] = True

        elif op == "drop_tip":
            active_n = max(1, mounted_channels)
            max_in_pip = max(float(nozzles[idx]["content"]) for idx in range(active_n))
            fact["in_pipette"] = max_in_pip
            fact["allow_wet_drop"] = bool(step.get("allow_wet_drop", False))
            mstate["tip"] = False
            mstate["mounted_channels"] = 0
            mstate["nozzles"] = [_fresh_nozzle() for _ in range(8)]

        elif op in THERMAL_STATE_OPS:
            target_wells: list[tuple[str, dict[str, Any]]]
            if isinstance(loc, str) and _PLATE_PREFIX_RE.match(loc):
                target_wells = [
                    (k, v)
                    for k, v in active_deck.items()
                    if isinstance(k, str) and k.startswith(f"{loc}:")
                ]
            else:
                assert well is not None
                target_wells = [(str(loc), well)]

            if op == "seal":
                any_already_sealed = any(w.get("sealed", False) for _, w in target_wells)
                fact["was_sealed"] = any_already_sealed
                for _, w in target_wells:
                    w["sealed"] = True

            elif op == "thaw":
                all_frozen = all(w.get("frozen", False) for _, w in target_wells)
                fact["was_frozen"] = all_frozen
                for _, w in target_wells:
                    w["frozen"] = False

            elif op == "incubate":
                temp_c = float(step["temp_c"])
                duration_s = float(step["duration_s"])
                any_frozen = any(w.get("frozen", False) for _, w in target_wells)
                all_sealed = all(w.get("sealed", False) for _, w in target_wells)
                fact.update(
                    temp_c=temp_c,
                    duration_s=duration_s,
                    frozen=any_frozen,
                    sealed=all_sealed,
                )
                total_evap = 0.0
                for _, w in target_wells:
                    total_evap = _clean_vol(
                        total_evap + _apply_open_well_evaporation(w, temp_c, duration_s, active_spec)
                    )
                fact["evap_loss"] = total_evap
                if len(target_wells) == 1:
                    fact["vol_after"] = target_wells[0][1]["vol"]

        elif step_channels == 1:
            assert well is not None
            n0 = nozzles[0]
            if tip and mounted_channels not in (0, 1):
                fact["channel_mismatch"] = (mounted_channels, 1)
            target_conc = _conc_map(well)
            fact.update(
                available=well["vol"],
                dead_vol=well["dead_vol"],
                role=well["role"],
                frozen=bool(well.get("frozen", False)),
                sealed=bool(well.get("sealed", False)),
                in_pipette=n0["content"],
                touched=set(n0["touched"]),
                liquids=set(well["liquids"]),
                pipette_liquids=set(n0["pip_amounts"].keys()),
                touched_snapshots=[dict(s) for s in n0["touched_snapshots"]],
                last_conc=dict(n0["last_conc"]),
                target_conc=dict(target_conc),
            )
            if op in ("aspirate", "mix") and well["role"] != "waste" and not bool(well.get("volume_known", True)):
                fact["start_volume_unknown"] = True
                if op == "aspirate":
                    n0["had_unknown_aspirate"] = True

            if op == "aspirate":
                fact["pipette_after"] = _clean_vol(n0["content"] + vol)
                take = _clean_vol(min(max(vol, 0.0), well["vol"]))
                if take > VOL_EPS and well["vol"] > VOL_EPS:
                    frac = take / well["vol"]
                    for r_name, r_amt in list(well["amounts"].items()):
                        moved = _clean_vol(r_amt * frac)
                        n0["pip_amounts"][r_name] = _clean_vol(
                            n0["pip_amounts"].get(r_name, 0.0) + moved
                        )
                        rem = _clean_vol(r_amt - moved)
                        if rem > VOL_EPS:
                            well["amounts"][r_name] = rem
                        else:
                            well["amounts"].pop(r_name, None)
                    well["vol"] = _clean_vol(well["vol"] - take)
                    n0["content"] = _clean_vol(n0["content"] + take)
                    if well["vol"] <= VOL_EPS:
                        well["vol"] = 0.0
                        well["amounts"].clear()
                        well["liquids"].clear()
                    else:
                        well["liquids"] = set(well["amounts"].keys())
                if target_conc:
                    n0["touched"] |= set(target_conc.keys())
                    n0["touched_snapshots"].append(dict(target_conc))
                    n0["last_conc"] = dict(target_conc)

            elif op == "dispense":
                air_dispense = bool(step.get("air_dispense", False))
                fact["air_dispense"] = air_dispense
                if n0.get("had_unknown_aspirate") and n0["content"] <= VOL_EPS:
                    fact["dispense_after_unknown_aspirate"] = True
                give = _clean_vol(min(max(vol, 0.0), n0["content"]))
                fact["give"] = give
                if give > VOL_EPS and n0["content"] > VOL_EPS:
                    frac = give / n0["content"]
                    for r_name, r_amt in list(n0["pip_amounts"].items()):
                        moved = _clean_vol(r_amt * frac)
                        well["amounts"][r_name] = _clean_vol(
                            well["amounts"].get(r_name, 0.0) + moved
                        )
                        rem = _clean_vol(r_amt - moved)
                        if rem > VOL_EPS:
                            n0["pip_amounts"][r_name] = rem
                        else:
                            n0["pip_amounts"].pop(r_name, None)
                    n0["content"] = _clean_vol(n0["content"] - give)
                    if n0["content"] <= VOL_EPS:
                        n0["content"] = 0.0
                        n0["pip_amounts"].clear()
                    well["vol"] = _clean_vol(well["vol"] + give)
                    well["volume_known"] = True
                    well["liquids"] = set(well["amounts"].keys())
                    if not air_dispense:
                        well["liquids"] |= n0["touched"]
                        n0["touched"] |= well["liquids"]
                        post_conc = _conc_map(well)
                        if post_conc:
                            n0["touched_snapshots"].append(post_conc)
                            n0["last_conc"] = dict(post_conc)
                else:
                    if well["vol"] > VOL_EPS and not air_dispense:
                        n0["touched"] |= well["liquids"]
                fact.update(dest_after=well["vol"], max=well["max"])

            elif op == "mix":
                reps = int(step.get("reps", 1))
                fact["reps"] = reps
                fact["pipette_after"] = _clean_vol(n0["content"] + vol)
                if well["vol"] > VOL_EPS and vol > VOL_EPS:
                    well["liquids"] |= n0["touched"]
                    n0["touched"] |= well["liquids"]
                    post_conc = _conc_map(well)
                    if post_conc:
                        n0["touched_snapshots"].append(post_conc)
                        n0["last_conc"] = dict(post_conc)

        else:
            # 8-channel manifold liquid operation across rows A..H or shared trough
            assert isinstance(loc, str)
            if tip and mounted_channels not in (0, 8):
                fact["channel_mismatch"] = (mounted_channels, 8)

            shared_trough = not (loc.startswith("plate:") or loc.startswith("plate_"))
            fact["shared_trough"] = shared_trough
            nozzle_facts: list[dict[str, Any]] = []

            if shared_trough:
                assert well is not None
                target_conc = _conc_map(well)
                total_req_vol = _clean_vol(8.0 * vol)
                unk_trough = (
                    op in ("aspirate", "mix")
                    and well["role"] != "waste"
                    and not bool(well.get("volume_known", True))
                )
                if unk_trough:
                    fact["start_volume_unknown"] = True
                    if op == "aspirate":
                        for idx in range(8):
                            nozzles[idx]["had_unknown_aspirate"] = True
                fact.update(
                    available=well["vol"],
                    dead_vol=well["dead_vol"],
                    role=well["role"],
                    frozen=bool(well.get("frozen", False)),
                    sealed=bool(well.get("sealed", False)),
                    shared_total_vol=total_req_vol,
                    in_pipette=min(float(nozzles[k]["content"]) for k in range(8)),
                    pipette_after=max(_clean_vol(nozzles[k]["content"] + vol) for k in range(8)),
                )
                for idx in range(8):
                    nz = nozzles[idx]
                    nf_s: dict[str, Any] = {
                        "nozzle": idx,
                        "op": op,
                        "loc": loc,
                        "vol": vol,
                        "tip": tip,
                        "spec": active_spec,
                        "pip_min": pip_min,
                        "pip_max": pip_max,
                        "available": well["vol"],
                        "dead_vol": well["dead_vol"],
                        "role": well["role"],
                        "frozen": bool(well.get("frozen", False)),
                        "sealed": bool(well.get("sealed", False)),
                        "in_pipette": nz["content"],
                        "pipette_after": _clean_vol(nz["content"] + vol) if op in ("aspirate", "mix") else nz["content"],
                        "air_dispense": bool(step.get("air_dispense", False)),
                        "touched": set(nz["touched"]),
                        "liquids": set(well["liquids"]),
                        "pipette_liquids": set(nz["pip_amounts"].keys()),
                        "touched_snapshots": [dict(s) for s in nz["touched_snapshots"]],
                        "last_conc": dict(nz["last_conc"]),
                        "target_conc": dict(target_conc),
                    }
                    if unk_trough:
                        nf_s["start_volume_unknown"] = True
                    if op == "dispense" and nz.get("had_unknown_aspirate") and nz["content"] <= VOL_EPS:
                        nf_s["dispense_after_unknown_aspirate"] = True
                    nozzle_facts.append(nf_s)

                if op == "aspirate":
                    take_total = _clean_vol(min(max(total_req_vol, 0.0), well["vol"]))
                    take_each = _clean_vol(take_total / 8.0)
                    if take_total > VOL_EPS and well["vol"] > VOL_EPS:
                        frac = take_total / well["vol"]
                        for r_name, r_amt in list(well["amounts"].items()):
                            moved_total = _clean_vol(r_amt * frac)
                            moved_each = _clean_vol(moved_total / 8.0)
                            for idx in range(8):
                                nz = nozzles[idx]
                                nz["pip_amounts"][r_name] = _clean_vol(
                                    nz["pip_amounts"].get(r_name, 0.0) + moved_each
                                )
                            rem = _clean_vol(r_amt - moved_total)
                            if rem > VOL_EPS:
                                well["amounts"][r_name] = rem
                            else:
                                well["amounts"].pop(r_name, None)
                        well["vol"] = _clean_vol(well["vol"] - take_total)
                        for idx in range(8):
                            nozzles[idx]["content"] = _clean_vol(nozzles[idx]["content"] + take_each)
                        if well["vol"] <= VOL_EPS:
                            well["vol"] = 0.0
                            well["amounts"].clear()
                            well["liquids"].clear()
                        else:
                            well["liquids"] = set(well["amounts"].keys())
                    if target_conc:
                        for idx in range(8):
                            nz = nozzles[idx]
                            nz["touched"] |= set(target_conc.keys())
                            nz["touched_snapshots"].append(dict(target_conc))
                            nz["last_conc"] = dict(target_conc)

                elif op == "dispense":
                    air_dispense = bool(step.get("air_dispense", False))
                    fact["air_dispense"] = air_dispense
                    for idx in range(8):
                        nz = nozzles[idx]
                        give = _clean_vol(min(max(vol, 0.0), nz["content"]))
                        if give > VOL_EPS and nz["content"] > VOL_EPS:
                            frac = give / nz["content"]
                            for r_name, r_amt in list(nz["pip_amounts"].items()):
                                moved = _clean_vol(r_amt * frac)
                                well["amounts"][r_name] = _clean_vol(
                                    well["amounts"].get(r_name, 0.0) + moved
                                )
                                rem = _clean_vol(r_amt - moved)
                                if rem > VOL_EPS:
                                    nz["pip_amounts"][r_name] = rem
                                else:
                                    nz["pip_amounts"].pop(r_name, None)
                            nz["content"] = _clean_vol(nz["content"] - give)
                            if nz["content"] <= VOL_EPS:
                                nz["content"] = 0.0
                                nz["pip_amounts"].clear()
                            well["vol"] = _clean_vol(well["vol"] + give)
                            well["volume_known"] = True
                            well["liquids"] = set(well["amounts"].keys())
                            if not air_dispense:
                                well["liquids"] |= nz["touched"]
                                nz["touched"] |= well["liquids"]
                                post_conc = _conc_map(well)
                                if post_conc:
                                    nz["touched_snapshots"].append(post_conc)
                                    nz["last_conc"] = dict(post_conc)
                    fact.update(dest_after=well["vol"], max=well["max"])
                    for nf in nozzle_facts:
                        nf["dest_after"] = well["vol"]
                        nf["max"] = well["max"]

                elif op == "mix":
                    reps = int(step.get("reps", 1))
                    fact["reps"] = reps
                    if well["vol"] > VOL_EPS and vol > VOL_EPS:
                        for idx in range(8):
                            well["liquids"] |= nozzles[idx]["touched"]
                        post_conc = _conc_map(well)
                        for idx in range(8):
                            nozzles[idx]["touched"] |= well["liquids"]
                            if post_conc:
                                nozzles[idx]["touched_snapshots"].append(dict(post_conc))
                                nozzles[idx]["last_conc"] = dict(post_conc)

            else:
                # 8-row plate column operation: {plate_prefix}:A{col}..H{col}
                m_pr = _PLATE_ROW_A_RE.match(loc)
                assert m_pr is not None
                p_pref = m_pr.group(1)
                col = int(m_pr.group(2))
                air_dispense = bool(step.get("air_dispense", False))
                for idx, row_ch in enumerate("ABCDEFGH"):
                    r_loc = f"{p_pref}:{row_ch}{col}"
                    r_well = active_deck[r_loc]
                    nz = nozzles[idx]
                    r_conc = _conc_map(r_well)
                    nf: dict[str, Any] = {
                        "nozzle": idx,
                        "op": op,
                        "loc": r_loc,
                        "vol": vol,
                        "tip": tip,
                        "spec": active_spec,
                        "pip_min": pip_min,
                        "pip_max": pip_max,
                        "available": r_well["vol"],
                        "dead_vol": r_well["dead_vol"],
                        "role": r_well["role"],
                        "frozen": bool(r_well.get("frozen", False)),
                        "sealed": bool(r_well.get("sealed", False)),
                        "in_pipette": nz["content"],
                        "pipette_after": _clean_vol(nz["content"] + vol) if op in ("aspirate", "mix") else nz["content"],
                        "air_dispense": air_dispense,
                        "touched": set(nz["touched"]),
                        "liquids": set(r_well["liquids"]),
                        "pipette_liquids": set(nz["pip_amounts"].keys()),
                        "touched_snapshots": [dict(s) for s in nz["touched_snapshots"]],
                        "last_conc": dict(nz["last_conc"]),
                        "target_conc": dict(r_conc),
                    }
                    if (
                        op in ("aspirate", "mix")
                        and r_well["role"] != "waste"
                        and not bool(r_well.get("volume_known", True))
                    ):
                        nf["start_volume_unknown"] = True
                        if op == "aspirate":
                            nz["had_unknown_aspirate"] = True
                    if op == "dispense" and nz.get("had_unknown_aspirate") and nz["content"] <= VOL_EPS:
                        nf["dispense_after_unknown_aspirate"] = True

                    if op == "aspirate":
                        take = _clean_vol(min(max(vol, 0.0), r_well["vol"]))
                        if take > VOL_EPS and r_well["vol"] > VOL_EPS:
                            frac = take / r_well["vol"]
                            for r_name, r_amt in list(r_well["amounts"].items()):
                                moved = _clean_vol(r_amt * frac)
                                nz["pip_amounts"][r_name] = _clean_vol(
                                    nz["pip_amounts"].get(r_name, 0.0) + moved
                                )
                                rem = _clean_vol(r_amt - moved)
                                if rem > VOL_EPS:
                                    r_well["amounts"][r_name] = rem
                                else:
                                    r_well["amounts"].pop(r_name, None)
                            r_well["vol"] = _clean_vol(r_well["vol"] - take)
                            nz["content"] = _clean_vol(nz["content"] + take)
                            if r_well["vol"] <= VOL_EPS:
                                r_well["vol"] = 0.0
                                r_well["amounts"].clear()
                                r_well["liquids"].clear()
                            else:
                                r_well["liquids"] = set(r_well["amounts"].keys())
                        if r_conc:
                            nz["touched"] |= set(r_conc.keys())
                            nz["touched_snapshots"].append(dict(r_conc))
                            nz["last_conc"] = dict(r_conc)

                    elif op == "dispense":
                        give = _clean_vol(min(max(vol, 0.0), nz["content"]))
                        nf["give"] = give
                        if give > VOL_EPS and nz["content"] > VOL_EPS:
                            frac = give / nz["content"]
                            for r_name, r_amt in list(nz["pip_amounts"].items()):
                                moved = _clean_vol(r_amt * frac)
                                r_well["amounts"][r_name] = _clean_vol(
                                    r_well["amounts"].get(r_name, 0.0) + moved
                                )
                                rem = _clean_vol(r_amt - moved)
                                if rem > VOL_EPS:
                                    nz["pip_amounts"][r_name] = rem
                                else:
                                    nz["pip_amounts"].pop(r_name, None)
                            nz["content"] = _clean_vol(nz["content"] - give)
                            if nz["content"] <= VOL_EPS:
                                nz["content"] = 0.0
                                nz["pip_amounts"].clear()
                            r_well["vol"] = _clean_vol(r_well["vol"] + give)
                            r_well["volume_known"] = True
                            r_well["liquids"] = set(r_well["amounts"].keys())
                            if not air_dispense:
                                r_well["liquids"] |= nz["touched"]
                                nz["touched"] |= r_well["liquids"]
                                post_conc = _conc_map(r_well)
                                if post_conc:
                                    nz["touched_snapshots"].append(post_conc)
                                    nz["last_conc"] = dict(post_conc)
                        else:
                            if r_well["vol"] > VOL_EPS and not air_dispense:
                                nz["touched"] |= r_well["liquids"]
                        nf["dest_after"] = r_well["vol"]
                        nf["max"] = r_well["max"]

                    elif op == "mix":
                        if r_well["vol"] > VOL_EPS and vol > VOL_EPS:
                            r_well["liquids"] |= nz["touched"]
                            nz["touched"] |= r_well["liquids"]
                            post_conc = _conc_map(r_well)
                            if post_conc:
                                nz["touched_snapshots"].append(post_conc)
                                nz["last_conc"] = dict(post_conc)

                    nozzle_facts.append(nf)

                fact["pipette_after"] = max(nf["pipette_after"] for nf in nozzle_facts)
                if op == "dispense":
                    fact["dest_after"] = max(nf["dest_after"] for nf in nozzle_facts)
                    fact["max"] = max(nf["max"] for nf in nozzle_facts)
                if op == "mix":
                    fact["reps"] = int(step.get("reps", 1))

            fact["nozzle_facts"] = nozzle_facts

    final_tips_left = sum(1 for v in tiprack.values() if v)
    plate = {
        k: v["vol"]
        for k, v in active_deck.items()
        if isinstance(k, str)
        and (k.startswith("plate:") or k.startswith("plate_"))
        and v["vol"] > VOL_EPS
    }
    any_tip_on = any(bool(ms["tip"]) for ms in mounts.values())
    return trace, {
        "tip_on": any_tip_on,
        "tips_used": initial_tips_left - final_tips_left,
        "tips_remaining": final_tips_left,
        "plate": plate,
        "deck": active_deck,
    }


# ---- verify rules: each returns a message on violation, else None ----
def schema_valid(f: Mapping[str, Any]) -> str | None:
    return f.get("schema_error")


def volume_in_range(f: Mapping[str, Any]) -> str | None:
    spec: DeckSpec = f.get("spec", DEFAULT_DECK_SPEC)
    pip_min = float(f.get("pip_min", spec.pip_min))
    pip_max = float(f.get("pip_max", spec.pip_max))
    if f["op"] in LIQUID_OPS:
        if f["vol"] < pip_min - VOL_EPS or f["vol"] > pip_max + VOL_EPS:
            return f"{f['vol']:g} uL outside pipette range {pip_min:g}-{pip_max:g} uL"
    pip_after = f.get("pipette_after", 0.0)
    if pip_after > pip_max + VOL_EPS:
        return f"pipette would hold {pip_after:g} uL > {pip_max:g} uL"
    return None


def tenth_ul_grid(f: Mapping[str, Any]) -> str | None:
    spec: DeckSpec = f.get("spec", DEFAULT_DECK_SPEC)
    if spec.strict_tenths and f["op"] in LIQUID_OPS:
        if not is_tenth_ul_exact(float(f["vol"])):
            return (
                f"{f['vol']} uL is not an exact integer multiple of 0.1 uL "
                "(strict formal tenth-uL mode)"
            )
    return None


def no_overfill(f: Mapping[str, Any]) -> str | None:
    if f["op"] == "dispense":
        for nf in f.get("nozzle_facts", ()):
            if nf["dest_after"] > nf["max"] + VOL_EPS:
                return f"{nf['loc']} would hold {nf['dest_after']:g} uL > {nf['max']:g} uL"
        if "dest_after" in f and f["dest_after"] > f["max"] + VOL_EPS:
            return f"{f['loc']} would hold {f['dest_after']:g} uL > {f['max']:g} uL"
    return None


def unknown_start_volume(f: Mapping[str, Any]) -> str | None:
    if f.get("start_volume_unknown"):
        return (
            f"{f['op']} {f['vol']:g} uL from {f['loc']} with undeclared starting "
            "liquid volume (missing loadLiquid)"
        )
    for nf in f.get("nozzle_facts", ()):
        if nf.get("start_volume_unknown"):
            return (
                f"{nf['op']} {nf['vol']:g} uL from {nf['loc']} with undeclared starting "
                "liquid volume (missing loadLiquid)"
            )
    return None


def enough_liquid(f: Mapping[str, Any]) -> str | None:
    if f.get("start_volume_unknown") or f.get("dispense_after_unknown_aspirate"):
        return None

    if f.get("shared_trough") and f.get("channels") == 8:
        if f["op"] in ("aspirate", "mix"):
            available = float(f["available"])
            dead_vol = float(f.get("dead_vol", 0.0))
            usable = max(0.0, _clean_vol(available - dead_vol))
            req_total = float(f["shared_total_vol"])
            if req_total > available + VOL_EPS:
                return (
                    f"8-channel {f['op']} 8x{f['vol']:g} uL ({req_total:g} uL total) "
                    f"from {f['loc']} holding {available:g} uL"
                )
            if req_total > usable + VOL_EPS:
                return (
                    f"8-channel {f['op']} 8x{f['vol']:g} uL ({req_total:g} uL total) "
                    f"from {f['loc']} holding {available:g} uL "
                    f"({usable:g} uL usable above {dead_vol:g} uL dead volume)"
                )
        if f["op"] == "dispense":
            for nf in f.get("nozzle_facts", ()):
                if nf.get("dispense_after_unknown_aspirate"):
                    continue
                if nf["vol"] > nf["in_pipette"] + VOL_EPS:
                    return (
                        f"dispense {nf['vol']:g} uL on nozzle {nf['nozzle'] + 1} "
                        f"but pipette holds {nf['in_pipette']:g} uL"
                    )
        return None

    if "nozzle_facts" in f:
        for nf in f["nozzle_facts"]:
            msg = enough_liquid(nf)
            if msg:
                return msg
        return None

    if f["op"] in ("aspirate", "mix"):
        available = float(f["available"])
        dead_vol = float(f.get("dead_vol", 0.0))
        usable = max(0.0, _clean_vol(available - dead_vol))
        if f["vol"] > available + VOL_EPS:
            return f"{f['op']} {f['vol']:g} uL from {f['loc']} holding {available:g} uL"
        if f["vol"] > usable + VOL_EPS:
            return (
                f"{f['op']} {f['vol']:g} uL from {f['loc']} holding {available:g} uL "
                f"({usable:g} uL usable above {dead_vol:g} uL dead volume)"
            )
    if f["op"] == "dispense" and f["vol"] > f["in_pipette"] + VOL_EPS:
        return f"dispense {f['vol']:g} uL but pipette holds {f['in_pipette']:g} uL"
    return None


def tip_present(f: Mapping[str, Any]) -> str | None:
    if f["op"] in LIQUID_OPS:
        if not f["tip"]:
            return f"{f['op']} without a tip"
        mismatch = f.get("channel_mismatch")
        if mismatch is not None:
            return (
                f"mounted {mismatch[0]}-channel tip(s) cannot execute "
                f"{mismatch[1]}-channel {f['op']}"
            )
    if f["op"] == "pick_up_tip":
        if f["tip"]:
            return "tip already attached"
        if f["tips_left"] == 0:
            return "tip rack empty"
        if f.get("bad_8ch_tip_row"):
            return (
                f"8-channel pick_up_tip must target row A (tiprack:A1..A12), "
                f"got {f.get('tip_slot')}"
            )
        if f.get("missing_tip_slots"):
            return (
                f"8-channel tip slot {f.get('tip_slot')} missing tip(s) "
                f"{f['missing_tip_slots']}"
            )
        if f.get("no_full_column"):
            return "no full 8-tip column available in tip rack"
        if not f.get("slot_has_tip", True):
            return f"tip slot {f.get('tip_slot')} is empty"
    if f["op"] == "drop_tip":
        if not f["tip"]:
            return "drop_tip without a tip"
        if f.get("in_pipette", 0.0) > VOL_EPS and not f.get("allow_wet_drop", False):
            return f"drop_tip with {f['in_pipette']:g} uL undispensed liquid in tip"
    return None


def no_aspirate_from_waste(f: Mapping[str, Any]) -> str | None:
    if "nozzle_facts" in f:
        for nf in f["nozzle_facts"]:
            msg = no_aspirate_from_waste(nf)
            if msg:
                return msg
        return None
    if f["op"] in ("aspirate", "mix") and f.get("role") == "waste":
        return f"{f['op']} from designated waste container {f['loc']} is forbidden"
    return None


def thawed_before_pipetting(f: Mapping[str, Any]) -> str | None:
    if "nozzle_facts" in f:
        for nf in f["nozzle_facts"]:
            msg = thawed_before_pipetting(nf)
            if msg:
                return msg
        return None
    if f["op"] in LIQUID_OPS and f.get("frozen", False):
        return f"cannot {f['op']} on frozen container {f['loc']} before thaw"
    if f["op"] == "incubate" and f.get("frozen", False):
        return f"cannot incubate frozen container {f['loc']} before thaw"
    return None


def unsealed_before_pipetting(f: Mapping[str, Any]) -> str | None:
    if "nozzle_facts" in f:
        for nf in f["nozzle_facts"]:
            msg = unsealed_before_pipetting(nf)
            if msg:
                return msg
        return None
    if f["op"] in LIQUID_OPS and f.get("sealed", False):
        return f"cannot {f['op']} through sealed container {f['loc']}"
    return None


def high_temp_requires_seal(f: Mapping[str, Any]) -> str | None:
    if f["op"] == "incubate":
        temp_c = float(f.get("temp_c", 20.0))
        if temp_c >= 80.0 - VOL_EPS and not f.get("sealed", False):
            return (
                f"incubate at {temp_c:g} C >= 80 C on unsealed {f['loc']} "
                "requires seal to prevent boil-off"
            )
    return None


def incubate_temp_duration_bounds(f: Mapping[str, Any]) -> str | None:
    if f["op"] == "incubate":
        temp_c = float(f.get("temp_c", 0.0))
        dur_s = float(f.get("duration_s", 0.0))
        if dur_s <= 0.0 or temp_c < 4.0 - VOL_EPS or temp_c > 98.0 + VOL_EPS:
            return (
                f"incubate requires duration_s > 0 and temp_c in [4, 98] C, "
                f"got temp_c={temp_c:g}, duration_s={dur_s:g}"
            )
    return None


def not_already_sealed(f: Mapping[str, Any]) -> str | None:
    if f["op"] == "seal" and f.get("was_sealed", False):
        return f"{f['loc']} is already sealed"
    return None


def thaw_requires_frozen(f: Mapping[str, Any]) -> str | None:
    if f["op"] == "thaw" and not f.get("was_frozen", True):
        return f"redundant thaw on already-thawed {f['loc']}"
    return None


def no_tip_reuse(f: Mapping[str, Any]) -> str | None:
    if not f["tip"]:
        return None
    if "nozzle_facts" in f:
        for nf in f["nozzle_facts"]:
            msg = no_tip_reuse(nf)
            if msg:
                return msg
        return None

    spec: DeckSpec = f.get("spec", DEFAULT_DECK_SPEC)
    diluents_lc = {d.lower() for d in spec.diluents}

    if f["op"] in ("aspirate", "mix"):
        foreign = f["touched"] - f["liquids"]
        if foreign:
            return (
                f"tip carrying {sorted(foreign)} reused in {f['loc']} "
                f"containing {sorted(f['liquids'])}"
            )
        target_conc: Mapping[str, float] = f.get("target_conc", {})
        for snap in f.get("touched_snapshots", ()):
            for r_name, prev_c in snap.items():
                if r_name.lower() in diluents_lc:
                    continue
                curr_c = float(target_conc.get(r_name, 0.0))
                if curr_c > prev_c * (1.0 + CONC_TOL) + VOL_EPS:
                    return (
                        f"tip diluted to {r_name}={prev_c:.4g} re-entered "
                        f"higher-concentration {f['loc']} ({r_name}={curr_c:.4g})"
                    )
        last_conc: Mapping[str, float] = f.get("last_conc", {})
        for r_name, tip_c in last_conc.items():
            if r_name.lower() in diluents_lc:
                continue
            curr_c = float(target_conc.get(r_name, 0.0))
            if tip_c > curr_c * (1.0 + CONC_TOL) + VOL_EPS:
                return (
                    f"tip carrying higher concentration {r_name}={tip_c:.4g} "
                    f"reused in {f['loc']} ({r_name}={curr_c:.4g})"
                )

    elif f["op"] == "dispense" and not f.get("air_dispense", False):
        if f.get("role") == "waste":
            return None
        if f.get("role") == "reagent":
            foreign_stock = (f["touched"] | f.get("pipette_liquids", set())) - f["liquids"]
            if foreign_stock:
                return (
                    f"tip carrying {sorted(foreign_stock)} dispensed into stock "
                    f"{f['loc']} containing {sorted(f['liquids'])}"
                )
        elif f.get("available", 0.0) > VOL_EPS:
            residue = f["touched"] - f.get("pipette_liquids", set()) - f["liquids"]
            if residue:
                return (
                    f"submerged dispense carries residue {sorted(residue)} into "
                    f"{f['loc']} containing {sorted(f['liquids'])}"
                )
    return None


STEP_RULES = [
    volume_in_range,
    tenth_ul_grid,
    no_overfill,
    unknown_start_volume,
    enough_liquid,
    tip_present,
    no_aspirate_from_waste,
    thawed_before_pipetting,
    unsealed_before_pipetting,
    incubate_temp_duration_bounds,
    high_temp_requires_seal,
    not_already_sealed,
    thaw_requires_frozen,
    no_tip_reuse,
]


def verify(
    trace: list[dict[str, Any]],
    end: Mapping[str, Any],
) -> tuple[bool, list[Violation]]:
    """Default-deny gate: approve only if protocol is non-empty and every step passes every rule."""
    violations: list[Violation] = []
    if end.get("protocol_error"):
        violations.append(Violation(0, "schema_valid", str(end["protocol_error"])))
        return False, violations
    if not trace:
        violations.append(Violation(0, "empty_protocol", "protocol must contain at least 1 step"))
        return False, violations

    checked = 0
    for f in trace:
        if not f["known"]:
            violations.append(
                Violation(f["i"], "known_step", f"unknown op/location {f['op']} {f['loc']}")
            )
            continue
        s_err = schema_valid(f)
        if s_err:
            violations.append(Violation(f["i"], "schema_valid", s_err))
            continue
        for rule in STEP_RULES:
            msg = rule(f)
            if msg:
                violations.append(Violation(f["i"], rule.__name__, msg))
        checked += 1

    if end.get("tip_on"):
        violations.append(Violation(len(trace), "all_tips_dropped", "tip still attached at end"))

    approved = bool(trace) and checked == len(trace) and not violations
    return approved, violations


def run(
    steps: Sequence[Mapping[str, Any]],
    approved: bool,
    *,
    executor: Callable[[Sequence[Mapping[str, Any]]], tuple[bool, list[str]]] | None = None,
) -> str:
    """Execute (or mock-execute) steps only when `verify()` approved the protocol."""
    msg, _, _ = run_detailed(steps, approved, executor=executor)
    return msg


def run_detailed(
    steps: Sequence[Mapping[str, Any]],
    approved: bool,
    *,
    executor: Callable[[Sequence[Mapping[str, Any]]], tuple[bool, list[str]]] | None = None,
) -> tuple[str, bool | None, list[str]]:
    """Return `(summary_text, run_passed, run_errors)` distinguishing gate block vs runtime fault."""
    if not approved:
        return "blocked by verify gate, nothing sent to the robot", None, []
    if executor is not None:
        ok, errors = executor(steps)
        if not ok:
            err_list = [str(e) for e in errors] or ["unspecified hardware execution fault"]
            return f"hardware execution failed ({'; '.join(err_list)})", False, err_list
    sent = Counter(str(s["op"]) for s in steps)
    summary = f"sent {sum(sent.values())} commands " + ", ".join(
        f"{k} x{v}" for k, v in sorted(sent.items())
    )
    return summary, True, []


def _iso8655_p300_profile(vol: float, pip_max: float = 300.0) -> tuple[float, float]:
    """Approximate ISO 8655-2 single-channel air-displacement systematic bias (%) and CV (%)."""
    frac = max(0.05, min(1.0, vol / max(pip_max, 1.0)))
    rel_sys = 0.006 / math.sqrt(frac)
    rel_cv = 0.002 / frac * 0.35 + 0.0015
    return rel_sys, rel_cv


def rehearse_monte_carlo(
    steps: Sequence[Mapping[str, Any]],
    *,
    n_rollouts: int = 100,
    seed: int = 42,
    spec: DeckSpec = DEFAULT_DECK_SPEC,
    systematic_bias_pct: float | None = None,
    cv_pct: float | None = None,
) -> dict[str, Any]:
    """Run seeded stochastic pipetting rollouts reporting overfill risk and concentration error bounds."""
    if n_rollouts < 1:
        raise ValueError("n_rollouts must be >= 1")
    det_trace, det_end = rehearse(list(steps), spec=spec)
    det_approved, det_violations = verify(det_trace, det_end)
    nom_deck = det_end["deck"]
    nom_vols = {
        k: float(v["vol"])
        for k, v in nom_deck.items()
        if isinstance(k, str) and k.startswith("plate:") and v["vol"] > VOL_EPS
    }
    nom_concs = {k: _conc_map(nom_deck[k]) for k in nom_vols}

    rng = random.Random(seed)
    overfill_rollouts = 0
    underflow_rollouts = 0
    max_well_vols: dict[str, float] = {k: 0.0 for k in nom_vols}
    min_well_vols: dict[str, float] = {k: float("inf") for k in nom_vols}
    max_rel_conc_err: dict[str, float] = {k: 0.0 for k in nom_vols}

    for _ in range(n_rollouts):
        noisy_steps: list[dict[str, Any]] = []
        for s in steps:
            if not isinstance(s, Mapping):
                continue
            sc = dict(s)
            if sc.get("op") in ("aspirate", "dispense") and _is_finite_number(sc.get("vol")):
                v = float(sc["vol"])
                _, p_max, _ = _resolve_step_pipette(sc, spec)
                iso_bias, iso_cv = _iso8655_p300_profile(v, p_max)
                bias = (systematic_bias_pct / 100.0) if systematic_bias_pct is not None else iso_bias
                cv = (cv_pct / 100.0) if cv_pct is not None else iso_cv
                sampled = max(0.1, rng.gauss(v * (1.0 + bias), max(1e-9, v * cv)))
                sc["vol"] = round(sampled, 6)
            noisy_steps.append(sc)

        r_trace, r_end = rehearse(noisy_steps, spec=spec)
        if any(no_overfill(f) is not None for f in r_trace if f.get("known") and not f.get("schema_error")):
            overfill_rollouts += 1
        if any(
            f.get("op") in ("aspirate", "mix") and enough_liquid(f) is not None
            for f in r_trace
            if f.get("known") and not f.get("schema_error")
        ):
            underflow_rollouts += 1

        r_deck = r_end["deck"]
        for k in nom_vols:
            rv = float(r_deck[k]["vol"])
            max_well_vols[k] = max(max_well_vols[k], rv)
            min_well_vols[k] = min(min_well_vols[k], rv)
            rc = _conc_map(r_deck[k])
            for r_name, nc in nom_concs[k].items():
                if nc > VOL_EPS:
                    rel_err = abs(rc.get(r_name, 0.0) - nc) / nc
                    max_rel_conc_err[k] = max(max_rel_conc_err[k], rel_err)

    return {
        "n_rollouts": n_rollouts,
        "seed": seed,
        "deterministic_approved": det_approved,
        "deterministic_violations": det_violations,
        "overfill_rate": overfill_rollouts / n_rollouts,
        "dead_volume_underflow_rate": underflow_rollouts / n_rollouts,
        "nominal_plate_volumes": nom_vols,
        "max_plate_volumes": {k: round(v, 4) for k, v in max_well_vols.items()},
        "min_plate_volumes": {k: round(v, 4) for k, v in min_well_vols.items()},
        "max_rel_conc_error": {k: round(v, 6) for k, v in max_rel_conc_err.items()},
    }


class RehearsalSession:
    """Stateful multi-protocol session optionally persisting deck/tiprack inventory across runs."""

    def __init__(
        self,
        *,
        memory_path: str | os.PathLike[str] | None = None,
        spec: DeckSpec = DEFAULT_DECK_SPEC,
        persist_deck: bool = False,
        fast_deny_known_failures: bool = False,
        max_artifact_retries: int = 2,
    ) -> None:
        self.spec = spec
        self.persist_deck = persist_deck
        self.fast_deny_known_failures = fast_deny_known_failures
        self.max_artifact_retries = max_artifact_retries
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None
        if memory_path is None:
            self._temp_dir = tempfile.TemporaryDirectory(prefix="lab_rehearsal_")
            mem_file = Path(self._temp_dir.name) / "runs.jsonl"
        else:
            mem_file = Path(memory_path)
        self.memory = MemoryStore(mem_file)
        self.deck = new_deck(spec) if persist_deck else None

    def close(self) -> None:
        if self._temp_dir is not None:
            self._temp_dir.cleanup()
            self._temp_dir = None

    def evaluate_protocol(
        self,
        name: str,
        steps: Any,
        *,
        executor: Callable[[Sequence[Mapping[str, Any]]], tuple[bool, list[str]]] | None = None,
        deck_epoch: int | None = None,
        bottom_trace: Mapping[str, Any] | None = None,
        attribution_outcome: str | None = None,
    ) -> dict[str, Any]:
        h = protocol_hash(steps, self.spec)
        sh = steps_hash(steps)
        eh = env_hash(self.spec)
        skel_h, shingles = compute_structural_signature(steps, self.spec)
        recall_msg = self.memory.recall(
            h,
            s_hash=sh,
            e_hash=eh,
            skeleton_hash=skel_h,
            shingles=shingles,
            live_deck_epoch=deck_epoch,
        )

        prior_artifacts = self.memory.consecutive_execution_artifacts(h)
        prior_records = self.memory._by_hash.get(h, [])
        retry_of_uuid = (
            str(prior_records[-1]["run_uuid"])
            if prior_artifacts > 0 and prior_records and prior_records[-1].get("run_uuid")
            else None
        )

        if self.fast_deny_known_failures:
            should_deny, deny_reason = self.memory.should_fast_deny(
                h, max_artifact_retries=self.max_artifact_retries
            )
            if should_deny:
                violations = [
                    Violation(
                        0,
                        "known_prior_failure",
                        f"fast-denied via episodic memory [{deny_reason}] ({recall_msg})",
                    )
                ]
                run_msg, run_passed, run_errors = run_detailed([], False)
                run_id = self.memory.append(
                    name,
                    h,
                    False,
                    violations,
                    s_hash=sh,
                    e_hash=eh,
                    skeleton_hash=skel_h,
                    shingles=shingles,
                    deck_epoch=deck_epoch,
                    bottom_trace=bottom_trace,
                    retry_count=prior_artifacts if prior_artifacts > 0 else None,
                    retry_of_run_uuid=retry_of_uuid,
                    run_passed=run_passed,
                    run_errors=run_errors,
                )
                rec_uuid = self.memory._by_run_id[run_id]["run_uuid"]
                return {
                    "protocol": name,
                    "hash": h,
                    "steps_hash": sh,
                    "env_hash": eh,
                    "skeleton_hash": skel_h,
                    "recall": recall_msg,
                    "fast_denied": True,
                    "approved": False,
                    "violations": violations,
                    "run": run_msg,
                    "run_id": run_id,
                    "run_uuid": rec_uuid,
                    "retry_count": prior_artifacts,
                    "end": {"tip_on": False, "tips_used": 0, "plate": {}},
                }

        target_deck = self.deck if self.persist_deck else None
        trace, end = rehearse(steps, spec=self.spec, deck=target_deck)
        approved, violations = verify(trace, end)
        run_msg, run_passed, run_errors = run_detailed(
            steps if isinstance(steps, list) else [],
            approved,
            executor=executor,
        )
        run_id = self.memory.append(
            name,
            h,
            approved,
            violations,
            s_hash=sh,
            e_hash=eh,
            skeleton_hash=skel_h,
            shingles=shingles,
            deck_epoch=deck_epoch,
            bottom_trace=bottom_trace,
            attribution_outcome=attribution_outcome,
            retry_count=prior_artifacts if prior_artifacts > 0 else None,
            retry_of_run_uuid=retry_of_uuid,
            run_passed=run_passed,
            run_errors=run_errors,
        )
        rec_uuid = self.memory._by_run_id[run_id]["run_uuid"]
        return {
            "protocol": name,
            "hash": h,
            "steps_hash": sh,
            "env_hash": eh,
            "skeleton_hash": skel_h,
            "recall": recall_msg,
            "fast_denied": False,
            "trace": trace,
            "end": end,
            "approved": approved,
            "violations": violations,
            "run": run_msg,
            "run_passed": run_passed,
            "run_errors": run_errors,
            "run_id": run_id,
            "run_uuid": rec_uuid,
            "retry_count": prior_artifacts,
        }


def validate_protocol_schema(steps: Any) -> list[str]:
    """Validate `steps` against strict protocol schema rules; return error list (empty if valid)."""
    if not isinstance(steps, list):
        return [f"protocol root must be a list, got {type(steps).__name__}"]
    if len(steps) == 0:
        return ["protocol must contain at least 1 step"]
    errors: list[str] = []
    for idx, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            errors.append(f"step {idx}: expected dict, got {type(step).__name__}")
            continue
        op = step.get("op")
        if not isinstance(op, str) or op not in KNOWN_OPS:
            errors.append(f"step {idx}: unknown op {op!r}")
            continue
        err = _validate_step_schema(step)
        if err:
            errors.append(f"step {idx}: {err}")
            continue
        if op in LIQUID_OPS:
            vol = float(step["vol"])
            if vol <= 0:
                errors.append(f"step {idx}: vol must be > 0, got {vol}")
            loc = step["loc"]
            if not _LIQUID_LOC_SCHEMA_RE.match(loc):
                errors.append(f"step {idx}: invalid loc {loc!r}")
        elif op in THERMAL_STATE_OPS:
            loc = step["loc"]
            if not _THERMAL_LOC_SCHEMA_RE.match(loc):
                errors.append(f"step {idx}: invalid loc {loc!r}")
            if op == "incubate" and float(step["duration_s"]) <= 0:
                errors.append(f"step {idx}: duration_s must be > 0, got {step['duration_s']}")
    return errors


def validate_run_record_schema(record: Any) -> list[str]:
    """Validate an episodic `runs.jsonl` record dict against `schema/run_record.schema.json` rules."""
    if not isinstance(record, dict):
        return [f"record must be a dict, got {type(record).__name__}"]
    required = {
        "run",
        "run_uuid",
        "timestamp_utc",
        "protocol",
        "hash",
        "steps_hash",
        "env_hash",
        "rules_version",
        "verify_passed",
        "run_passed",
        "passed",
        "violations",
        "run_errors",
    }
    optional = {
        "skeleton_hash",
        "shingles",
        "deck_epoch",
        "bottom_trace",
        "attribution_outcome",
        "retry_count",
        "retry_of_run_uuid",
        "certified_receipt_sha256",
    }
    errors: list[str] = []
    missing = sorted(required - set(record.keys()))
    if missing:
        errors.append(f"missing required keys: {missing}")
    extra = sorted(set(record.keys()) - (required | optional))
    if extra:
        errors.append(f"unexpected keys: {extra}")
    if missing:
        return errors
    if not _is_strict_int(record["run"]) or record["run"] < 1:
        errors.append("run must be integer >= 1")
    for h_key in ("hash", "steps_hash", "env_hash"):
        val = record.get(h_key)
        if not isinstance(val, str) or not re.fullmatch(r"[0-9a-f]{64}", val):
            errors.append(f"{h_key} must be 64-char lowercase hex SHA-256")
    if "skeleton_hash" in record:
        sk = record["skeleton_hash"]
        if not isinstance(sk, str) or not re.fullmatch(r"[0-9a-f]{64}", sk):
            errors.append("skeleton_hash must be 64-char lowercase hex SHA-256")
    if "shingles" in record:
        shs = record["shingles"]
        if not isinstance(shs, list) or not all(
            isinstance(x, str) and re.fullmatch(r"[0-9a-f]{16}", x) for x in shs
        ):
            errors.append("shingles must be a list of 16-char lowercase hex strings")
    if record.get("deck_epoch") is not None:
        if not _is_strict_int(record["deck_epoch"]) or record["deck_epoch"] < 0:
            errors.append("deck_epoch must be integer >= 0 or null")
    if record.get("bottom_trace") is not None:
        bt = record["bottom_trace"]
        bt_allowed = {
            "sop_id",
            "liquid_name",
            "deck_epoch",
            "calibrated_dead_vol_ul",
            "calibrated_evap_coeff",
            "liquid_calibrations",
            "active_invariants",
            "trace_sha256",
        }
        if not isinstance(bt, dict):
            errors.append("bottom_trace must be an object or null")
        else:
            bt_extra = sorted(set(bt.keys()) - bt_allowed)
            if bt_extra:
                errors.append(f"bottom_trace unexpected keys: {bt_extra}")
            if not _is_strict_int(bt.get("deck_epoch")) or bt["deck_epoch"] < 0:
                errors.append("bottom_trace.deck_epoch must be integer >= 0")
            tsha = bt.get("trace_sha256")
            if not isinstance(tsha, str) or not re.fullmatch(r"[0-9a-f]{64}", tsha):
                errors.append("bottom_trace.trace_sha256 must be 64-char lowercase hex SHA-256")
    if record.get("attribution_outcome") is not None:
        if record["attribution_outcome"] not in VALID_ATTRIBUTION_OUTCOMES:
            errors.append(
                f"attribution_outcome must be in {sorted(VALID_ATTRIBUTION_OUTCOMES)} or null"
            )
    if "retry_count" in record:
        if not _is_strict_int(record["retry_count"]) or record["retry_count"] < 0:
            errors.append("retry_count must be integer >= 0")
    if record.get("retry_of_run_uuid") is not None:
        if not isinstance(record["retry_of_run_uuid"], str) or len(record["retry_of_run_uuid"]) < 8:
            errors.append("retry_of_run_uuid must be string of length >= 8 or null")
    if record.get("certified_receipt_sha256") is not None:
        csha = record["certified_receipt_sha256"]
        if not isinstance(csha, str) or not re.fullmatch(r"[0-9a-f]{64}", csha):
            errors.append("certified_receipt_sha256 must be 64-char lowercase hex SHA-256 or null")
    for b_key in ("verify_passed", "passed"):
        if not isinstance(record.get(b_key), bool):
            errors.append(f"{b_key} must be boolean")
    if record.get("run_passed") is not None and not isinstance(record.get("run_passed"), bool):
        errors.append("run_passed must be boolean or null")
    if not isinstance(record.get("violations"), list):
        errors.append("violations must be a list")
    else:
        for i, v in enumerate(record["violations"]):
            if not isinstance(v, dict) or set(v.keys()) != {"step", "rule", "message"}:
                errors.append(f"violations[{i}] must have exact keys step, rule, message")
    if not isinstance(record.get("run_errors"), list) or not all(
        isinstance(e, str) for e in record.get("run_errors", [])
    ):
        errors.append("run_errors must be a list of strings")
    return errors
