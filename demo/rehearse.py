"""Recall -> rehearse -> verify -> run -> write back, for liquid-handling protocols.

Thin executable wrapper around `lab_rehearsal` preserving module-level helpers while
defaulting persistent memory to gitignored `data/memory/runs.jsonl` and running the
2-pass showcase in an isolated temporary directory so tracked files stay clean.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
SRC_DIR = os.path.join(REPO_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from lab_rehearsal.cli import main as _cli_main  # noqa: E402
from lab_rehearsal.core import (  # noqa: E402,F401
    ALLOWED_KEYS_BY_OP,
    DEFAULT_DECK_SPEC,
    KNOWN_OPS,
    LIQUID_OPS,
    PIP_MAX,
    PIP_MIN,
    PLATE_DEAD_VOL,
    RES_DEAD_VOL,
    RES_MAX,
    RULES_VERSION,
    START,
    STEP_RULES,
    TIPRACK_COORDS,
    TIP_RACK,
    VOL_EPS,
    WELL_MAX,
    DeckSpec,
    MemoryStore,
    RehearsalSession,
    Violation,
    enough_liquid,
    env_hash,
    extract_violation_rule,
    new_deck,
    no_aspirate_from_waste,
    no_overfill,
    no_tip_reuse,
    protocol_hash,
    rehearse,
    rehearse_monte_carlo,
    run,
    run_detailed,
    schema_valid,
    steps_hash,
    tip_present,
    validate_protocol_schema,
    validate_run_record_schema,
    verify,
    volume_in_range,
)

PROTOCOL_DIR = os.path.join(HERE, "protocols")
# Default persistent CLI memory lives under gitignored `data/memory/runs.jsonl`
MEMORY = os.path.join(REPO_ROOT, "data", "memory", "runs.jsonl")


def load_memory(memory_path: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    target = MEMORY if memory_path is None else memory_path
    return MemoryStore(target).records()


def recall(
    h: str,
    *,
    memory_path: str | os.PathLike[str] | None = None,
    s_hash: str | None = None,
    e_hash: str | None = None,
) -> str:
    target = MEMORY if memory_path is None else memory_path
    return MemoryStore(target).recall(h, s_hash=s_hash, e_hash=e_hash)


def write_back(
    name: str,
    h: str,
    approved: bool,
    violations: Sequence[Any],
    *,
    memory_path: str | os.PathLike[str] | None = None,
    s_hash: str | None = None,
    e_hash: str | None = None,
    run_passed: bool | None = None,
    run_errors: Sequence[str] = (),
) -> int:
    target = MEMORY if memory_path is None else memory_path
    return MemoryStore(target).append(
        name,
        h,
        approved,
        violations,
        s_hash=s_hash,
        e_hash=e_hash,
        run_passed=run_passed,
        run_errors=run_errors,
    )


def main(argv: Sequence[str] | None = None) -> int:
    return _cli_main(argv, default_demo_dir=Path(PROTOCOL_DIR))


if __name__ == "__main__":
    raise SystemExit(main())
