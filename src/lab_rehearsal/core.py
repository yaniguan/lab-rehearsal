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

LIQUID_OPS: frozenset[str] = frozenset({"aspirate", "dispense", "mix"})
KNOWN_OPS: frozenset[str] = LIQUID_OPS | {"pick_up_tip", "drop_tip"}

ALLOWED_KEYS_BY_OP: dict[str, frozenset[str]] = {
    "pick_up_tip": frozenset({"op", "loc"}),
    "drop_tip": frozenset({"op", "allow_wet_drop"}),
    "aspirate": frozenset({"op", "loc", "vol"}),
    "dispense": frozenset({"op", "loc", "vol", "air_dispense"}),
    "mix": frozenset({"op", "loc", "vol", "reps"}),
}
REQUIRED_KEYS_BY_OP: dict[str, frozenset[str]] = {
    "pick_up_tip": frozenset({"op"}),
    "drop_tip": frozenset({"op"}),
    "aspirate": frozenset({"op", "loc", "vol"}),
    "dispense": frozenset({"op", "loc", "vol"}),
    "mix": frozenset({"op", "loc", "vol"}),
}

TIPRACK_COORDS: tuple[str, ...] = tuple(
    f"tiprack:{row}{col}" for row in "ABCDEFGH" for col in range(1, 13)
)
_TIPRACK_RE = re.compile(r"^tiprack:[A-H](?:1[0-2]|[1-9])$")


def _clean_vol(val: float) -> float:
    if abs(val) <= VOL_EPS:
        return 0.0
    return round(float(val), 9)


def _is_finite_number(val: Any) -> bool:
    return not isinstance(val, bool) and isinstance(val, (int, float)) and math.isfinite(float(val))


def _is_strict_int(val: Any) -> bool:
    return not isinstance(val, bool) and isinstance(val, int)


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

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
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


DEFAULT_DECK_SPEC = DeckSpec()


def new_deck(spec: DeckSpec = DEFAULT_DECK_SPEC) -> dict[str, Any]:
    """Initialize a fresh virtual deck with 96-well plate, 12-col reservoir, waste, and 96-tip rack."""
    deck: dict[str, Any] = {}
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
            }
    for col in range(1, 13):
        loc = f"res:{col}"
        init_vol, init_liq = spec.start.get(loc, (0.0, None))
        v = _clean_vol(float(init_vol))
        role = "waste" if loc in spec.waste_locs else "reagent"
        deck[loc] = {
            "vol": v,
            "max": float(spec.res_max),
            "dead_vol": 0.0 if role == "waste" else float(spec.res_dead_vol),
            "role": role,
            "liquids": {init_liq} if (init_liq and v > VOL_EPS) else set(),
            "amounts": {init_liq: v} if (init_liq and v > VOL_EPS) else {},
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
            }
    tiprack: dict[str, bool] = {}
    for idx, coord in enumerate(TIPRACK_COORDS):
        tiprack[coord] = idx < spec.tip_rack
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
    """UTF-8, corruption-resilient, O(1)-indexed episodic run ledger."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._loaded = False
        self._records: list[dict[str, Any]] = []
        self._by_hash: dict[str, list[dict[str, Any]]] = {}
        self._by_steps_hash: dict[str, list[dict[str, Any]]] = {}
        self._corrupted_lines: int = 0

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._records.clear()
        self._by_hash.clear()
        self._by_steps_hash.clear()
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
        self._records.append(rec)
        h = str(rec.get("hash", ""))
        if h:
            self._by_hash.setdefault(h, []).append(rec)
        sh = str(rec.get("steps_hash", ""))
        if sh:
            self._by_steps_hash.setdefault(sh, []).append(rec)

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
        self._records.clear()
        self._by_hash.clear()
        self._by_steps_hash.clear()
        self._corrupted_lines = 0

    def recall(
        self,
        h: str,
        *,
        s_hash: str | None = None,
        e_hash: str | None = None,
    ) -> str:
        """Return warning/status summary for matching prior runs (including env-drift checks)."""
        self._ensure_loaded()
        past = list(self._by_hash.get(h, ()))
        if not past and len(h) < 64:
            past = [r for r in self._records if str(r.get("hash", "")).startswith(h)]

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
            return "no earlier runs of this protocol"

        verify_failed = [
            r for r in past if not r.get("verify_passed", r.get("passed", False))
        ]
        run_failed = [
            r
            for r in past
            if r.get("verify_passed", r.get("passed", False))
            and r.get("run_passed") is False
        ]

        drift_suffix = (
            f"; env changed vs run(s) {env_drift_runs}" if env_drift_runs else ""
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
            detail = f" ({', '.join(errs)})" if errs else ""
            return (
                f"WARNING: runtime hardware failure in run(s) {[r['run'] for r in run_failed]}"
                f"{detail}{drift_suffix}"
            )
        return f"passed before in run(s) {[r['run'] for r in past]}{drift_suffix}"

    def has_known_failure(self, h: str) -> bool:
        self._ensure_loaded()
        return any(not r.get("passed", False) for r in self._by_hash.get(h, ()))

    def append(
        self,
        protocol: str,
        h: str,
        verify_passed: bool,
        violations: Sequence[Mapping[str, Any] | str],
        *,
        s_hash: str | None = None,
        e_hash: str | None = None,
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

    if op in LIQUID_OPS:
        if not isinstance(step.get("loc"), str) or not step["loc"].strip():
            return f"'loc' must be a non-empty string for op '{op}'"
        raw_vol = step.get("vol")
        if not _is_finite_number(raw_vol):
            return f"invalid volume {raw_vol!r} (must be a finite number)"

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

    tip = False
    content = 0.0
    pip_amounts: dict[str, float] = {}
    touched: set[str] = set()
    touched_snapshots: list[dict[str, float]] = []
    last_conc: dict[str, float] = {}
    trace: list[dict[str, Any]] = []

    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            trace.append(
                {
                    "i": i,
                    "op": None,
                    "loc": None,
                    "vol": 0.0,
                    "tip": tip,
                    "known": False,
                    "schema_error": f"step must be an object/dict, got {type(step).__name__}",
                    "spec": active_spec,
                }
            )
            continue

        op = step.get("op")
        loc = step.get("loc")
        schema_err = _validate_step_schema(step)

        raw_vol = step.get("vol", 0.0)
        if _is_finite_number(raw_vol):
            vol = _clean_vol(float(raw_vol))
        else:
            vol = 0.0
            if schema_err is None:
                schema_err = f"invalid volume {raw_vol!r} (must be a finite number)"

        well = active_deck.get(loc) if isinstance(loc, str) and not loc.startswith("_") else None
        is_known_op = isinstance(op, str) and op in KNOWN_OPS
        is_known_loc = True
        if op in LIQUID_OPS:
            is_known_loc = well is not None
        elif op == "pick_up_tip" and loc is not None:
            is_known_loc = isinstance(loc, str) and loc in tiprack

        known = is_known_op and is_known_loc
        tips_left = sum(1 for v in tiprack.values() if v)

        fact: dict[str, Any] = {
            "i": i,
            "op": op,
            "loc": loc,
            "vol": vol,
            "tip": tip,
            "known": known,
            "schema_error": schema_err,
            "spec": active_spec,
        }
        trace.append(fact)

        if not known or schema_err is not None:
            continue

        if op == "pick_up_tip":
            fact["tips_left"] = tips_left
            if loc is not None:
                slot_has_tip = bool(tiprack.get(loc, False))
                fact["tip_slot"] = loc
                fact["slot_has_tip"] = slot_has_tip
                if not tip and slot_has_tip:
                    tiprack[loc] = False
                    tip = True
                    content = 0.0
                    pip_amounts = {}
                    touched = set()
                    touched_snapshots = []
                    last_conc = {}
            else:
                next_slot = next((k for k in TIPRACK_COORDS if tiprack.get(k, False)), None)
                fact["tip_slot"] = next_slot
                fact["slot_has_tip"] = next_slot is not None
                if not tip and next_slot is not None:
                    tiprack[next_slot] = False
                    tip = True
                    content = 0.0
                    pip_amounts = {}
                    touched = set()
                    touched_snapshots = []
                    last_conc = {}

        elif op == "drop_tip":
            fact["in_pipette"] = content
            fact["allow_wet_drop"] = bool(step.get("allow_wet_drop", False))
            tip = False
            content = 0.0
            pip_amounts = {}
            touched = set()
            touched_snapshots = []
            last_conc = {}

        else:
            assert well is not None
            target_conc = _conc_map(well)
            fact.update(
                available=well["vol"],
                dead_vol=well["dead_vol"],
                role=well["role"],
                in_pipette=content,
                touched=set(touched),
                liquids=set(well["liquids"]),
                pipette_liquids=set(pip_amounts.keys()),
                touched_snapshots=[dict(s) for s in touched_snapshots],
                last_conc=dict(last_conc),
                target_conc=dict(target_conc),
            )
            if op == "aspirate":
                fact["pipette_after"] = _clean_vol(content + vol)
                take = _clean_vol(min(max(vol, 0.0), well["vol"]))
                if take > VOL_EPS and well["vol"] > VOL_EPS:
                    frac = take / well["vol"]
                    for r_name, r_amt in list(well["amounts"].items()):
                        moved = _clean_vol(r_amt * frac)
                        pip_amounts[r_name] = _clean_vol(pip_amounts.get(r_name, 0.0) + moved)
                        rem = _clean_vol(r_amt - moved)
                        if rem > VOL_EPS:
                            well["amounts"][r_name] = rem
                        else:
                            well["amounts"].pop(r_name, None)
                    well["vol"] = _clean_vol(well["vol"] - take)
                    content = _clean_vol(content + take)
                    if well["vol"] <= VOL_EPS:
                        well["vol"] = 0.0
                        well["amounts"].clear()
                        well["liquids"].clear()
                    else:
                        well["liquids"] = set(well["amounts"].keys())
                if target_conc:
                    touched |= set(target_conc.keys())
                    touched_snapshots.append(dict(target_conc))
                    last_conc = dict(target_conc)

            elif op == "dispense":
                air_dispense = bool(step.get("air_dispense", False))
                fact["air_dispense"] = air_dispense
                give = _clean_vol(min(max(vol, 0.0), content))
                fact["give"] = give
                if give > VOL_EPS and content > VOL_EPS:
                    frac = give / content
                    for r_name, r_amt in list(pip_amounts.items()):
                        moved = _clean_vol(r_amt * frac)
                        well["amounts"][r_name] = _clean_vol(well["amounts"].get(r_name, 0.0) + moved)
                        rem = _clean_vol(r_amt - moved)
                        if rem > VOL_EPS:
                            pip_amounts[r_name] = rem
                        else:
                            pip_amounts.pop(r_name, None)
                    content = _clean_vol(content - give)
                    if content <= VOL_EPS:
                        content = 0.0
                        pip_amounts.clear()
                    well["vol"] = _clean_vol(well["vol"] + give)
                    well["liquids"] = set(well["amounts"].keys())
                    if not air_dispense:
                        well["liquids"] |= touched
                        touched |= well["liquids"]
                        post_conc = _conc_map(well)
                        if post_conc:
                            touched_snapshots.append(post_conc)
                            last_conc = dict(post_conc)
                else:
                    if well["vol"] > VOL_EPS and not air_dispense:
                        touched |= well["liquids"]
                fact.update(dest_after=well["vol"], max=well["max"])

            elif op == "mix":
                reps = int(step.get("reps", 1))
                fact["reps"] = reps
                fact["pipette_after"] = _clean_vol(content + vol)
                if well["vol"] > VOL_EPS and vol > VOL_EPS:
                    well["liquids"] |= touched
                    touched |= well["liquids"]
                    post_conc = _conc_map(well)
                    if post_conc:
                        touched_snapshots.append(post_conc)
                        last_conc = dict(post_conc)

    final_tips_left = sum(1 for v in tiprack.values() if v)
    plate = {
        k: v["vol"]
        for k, v in active_deck.items()
        if isinstance(k, str) and k.startswith("plate:") and v["vol"] > VOL_EPS
    }
    return trace, {
        "tip_on": tip,
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
    if f["op"] in LIQUID_OPS:
        if f["vol"] < spec.pip_min - VOL_EPS or f["vol"] > spec.pip_max + VOL_EPS:
            return f"{f['vol']:g} uL outside pipette range {spec.pip_min:g}-{spec.pip_max:g} uL"
    pip_after = f.get("pipette_after", 0.0)
    if pip_after > spec.pip_max + VOL_EPS:
        return f"pipette would hold {pip_after:g} uL > {spec.pip_max:g} uL"
    return None


def no_overfill(f: Mapping[str, Any]) -> str | None:
    if f["op"] == "dispense" and f["dest_after"] > f["max"] + VOL_EPS:
        return f"{f['loc']} would hold {f['dest_after']:g} uL > {f['max']:g} uL"
    return None


def enough_liquid(f: Mapping[str, Any]) -> str | None:
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
    if f["op"] in LIQUID_OPS and not f["tip"]:
        return f"{f['op']} without a tip"
    if f["op"] == "pick_up_tip":
        if f["tip"]:
            return "tip already attached"
        if f["tips_left"] == 0:
            return "tip rack empty"
        if not f.get("slot_has_tip", True):
            return f"tip slot {f.get('tip_slot')} is empty"
    if f["op"] == "drop_tip":
        if not f["tip"]:
            return "drop_tip without a tip"
        if f.get("in_pipette", 0.0) > VOL_EPS and not f.get("allow_wet_drop", False):
            return f"drop_tip with {f['in_pipette']:g} uL undispensed liquid in tip"
    return None


def no_aspirate_from_waste(f: Mapping[str, Any]) -> str | None:
    if f["op"] in ("aspirate", "mix") and f.get("role") == "waste":
        return f"{f['op']} from designated waste container {f['loc']} is forbidden"
    return None


def no_tip_reuse(f: Mapping[str, Any]) -> str | None:
    if not f["tip"]:
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
    no_overfill,
    enough_liquid,
    tip_present,
    no_aspirate_from_waste,
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
    # At 100% nominal: ~0.6% bias, ~0.2% CV; at 10% nominal: ~2.5% bias, ~0.8% CV
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
                iso_bias, iso_cv = _iso8655_p300_profile(v, spec.pip_max)
                bias = (systematic_bias_pct / 100.0) if systematic_bias_pct is not None else iso_bias
                cv = (cv_pct / 100.0) if cv_pct is not None else iso_cv
                sampled = max(0.1, rng.gauss(v * (1.0 + bias), max(1e-9, v * cv)))
                sc["vol"] = round(sampled, 6)
            noisy_steps.append(sc)

        r_trace, r_end = rehearse(noisy_steps, spec=spec)
        if any(f.get("op") == "dispense" and f.get("dest_after", 0.0) > f.get("max", spec.well_max) + VOL_EPS for f in r_trace):
            overfill_rollouts += 1
        if any(
            f.get("op") in ("aspirate", "mix")
            and f.get("vol", 0.0) > max(0.0, f.get("available", 0.0) - f.get("dead_vol", 0.0)) + VOL_EPS
            for f in r_trace
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
    ) -> None:
        self.spec = spec
        self.persist_deck = persist_deck
        self.fast_deny_known_failures = fast_deny_known_failures
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
    ) -> dict[str, Any]:
        h = protocol_hash(steps, self.spec)
        sh = steps_hash(steps)
        eh = env_hash(self.spec)
        recall_msg = self.memory.recall(h, s_hash=sh, e_hash=eh)

        if self.fast_deny_known_failures and self.memory.has_known_failure(h):
            violations = [
                Violation(0, "known_prior_failure", f"fast-denied via episodic memory ({recall_msg})")
            ]
            run_msg, run_passed, run_errors = run_detailed([], False)
            run_id = self.memory.append(
                name,
                h,
                False,
                violations,
                s_hash=sh,
                e_hash=eh,
                run_passed=run_passed,
                run_errors=run_errors,
            )
            return {
                "protocol": name,
                "hash": h,
                "steps_hash": sh,
                "env_hash": eh,
                "recall": recall_msg,
                "fast_denied": True,
                "approved": False,
                "violations": violations,
                "run": run_msg,
                "run_id": run_id,
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
            run_passed=run_passed,
            run_errors=run_errors,
        )
        return {
            "protocol": name,
            "hash": h,
            "steps_hash": sh,
            "env_hash": eh,
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
            if not re.match(r"^(plate:[A-H](?:1[0-2]|[1-9])|res:(?:1[0-2]|[1-9])|trash:1)$", loc):
                errors.append(f"step {idx}: invalid loc {loc!r}")
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
    errors: list[str] = []
    missing = sorted(required - set(record.keys()))
    if missing:
        errors.append(f"missing required keys: {missing}")
    extra = sorted(set(record.keys()) - required)
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
