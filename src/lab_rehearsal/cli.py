"""Command-line interface for `lab-rehearsal`."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Sequence

from .adapters import from_lablean, from_stepbot, from_twinstep
from .open_datasets import convert_open_protocol
from .core import (
    DEFAULT_DECK_SPEC,
    DeckSpec,
    MemoryStore,
    PLATE_DEAD_VOL,
    RES_DEAD_VOL,
    RehearsalSession,
    rehearse_monte_carlo,
)

EXPECTED_DEMO_VERDICTS: dict[str, tuple[bool, str | None]] = {
    "broken_aspirate_empty.json": (False, "enough_liquid"),
    "broken_mix_contamination.json": (False, "no_tip_reuse"),
    "broken_out_of_range.json": (False, "volume_in_range"),
    "broken_overfill.json": (False, "no_overfill"),
    "broken_tip_reuse.json": (False, "no_tip_reuse"),
    "good_serial_dilution.json": (True, None),
}


def _load_steps_file(path: Path, fmt: str = "auto") -> tuple[Any, DeckSpec]:
    text = path.read_text(encoding="utf-8")
    if fmt == "stepbot" or path.suffix.lower() in (".yaml", ".yml"):
        return from_stepbot(path)
    raw = json.loads(text)
    if fmt in ("opentrons_json", "bioplanner", "finebio", "bioprobench"):
        return convert_open_protocol(raw, fmt=fmt)
    if fmt == "auto" and isinstance(raw, dict) and any(
        k in raw
        for k in (
            "$otSharedSchema",
            "commands",
            "database",
            "annotations",
            "generated_pseudocode",
            "edited_pseudocode",
            "protocol_steps",
            "correct_steps",
            "corrected_text",
        )
    ):
        return convert_open_protocol(raw, fmt="auto")
    if fmt == "twinstep" or (
        isinstance(raw, list) and raw and isinstance(raw[0], dict) and "type" in raw[0]
    ):
        return from_twinstep(raw, tip_policy="smart"), DEFAULT_DECK_SPEC
    if fmt == "lablean" or (
        isinstance(raw, dict)
        and isinstance(raw.get("steps"), list)
        and raw["steps"]
        and isinstance(raw["steps"][0], dict)
        and "kind" in raw["steps"][0]
    ):
        return from_lablean(raw), DEFAULT_DECK_SPEC
    return raw, DEFAULT_DECK_SPEC


def run_demo_showcase(
    protocol_dir: Path,
    *,
    memory_path: Path | None = None,
    reset_memory: bool = False,
    spec: DeckSpec = DEFAULT_DECK_SPEC,
) -> int:
    """Run deterministic 2-pass showcase and assert every fixture matches expected verdict."""
    temp_ctx: tempfile.TemporaryDirectory[str] | None = None
    if memory_path is None:
        temp_ctx = tempfile.TemporaryDirectory(prefix="lab_rehearsal_demo_")
        active_mem = Path(temp_ctx.name) / "runs.jsonl"
    else:
        active_mem = memory_path
        if reset_memory and active_mem.exists():
            active_mem.unlink()

    try:
        session = RehearsalSession(memory_path=active_mem, spec=spec)
        names = sorted(n for n in os.listdir(protocol_dir) if n.endswith(".json"))
        unexpected_errors: list[str] = []

        for pass_no in (1, 2):
            print(f"######## pass {pass_no} ########")
            for name in names:
                with open(protocol_dir / name, "r", encoding="utf-8") as fh:
                    steps = json.load(fh)
                res = session.evaluate_protocol(name, steps)
                short_h = res["hash"][:12]
                end = res["end"]
                n_steps = len(steps) if isinstance(steps, list) else 0
                print(f"== {name} (hash {short_h}, {n_steps} steps)")
                print(f"  RECALL:   {res['recall']}")
                print(
                    f"  REHEARSE: tips used {end['tips_used']}, plate wells filled {len(end['plate'])}, "
                    f"tip on at end {end['tip_on']}"
                )
                approved = res["approved"]
                violations = res["violations"]
                print(f"  VERIFY:   {'PASS' if approved else 'FAIL'} ({len(violations)} violation(s))")
                for v in violations:
                    print(f"    - {v}")
                print(f"  RUN:      {res['run']}")
                if approved:
                    print(
                        "            final plate: "
                        + ", ".join(f"{k[6:]}={v:g}" for k, v in sorted(end["plate"].items()))
                    )
                print(f"  WRITE:    run #{res['run_id']} appended to memory/runs.jsonl")

                if name in EXPECTED_DEMO_VERDICTS:
                    exp_pass, exp_rule = EXPECTED_DEMO_VERDICTS[name]
                    if approved != exp_pass:
                        unexpected_errors.append(
                            f"{name}: expected approved={exp_pass}, got {approved}"
                        )
                    elif exp_rule is not None:
                        rules_hit = {v["rule"] for v in violations}
                        if exp_rule not in rules_hit:
                            unexpected_errors.append(
                                f"{name}: expected rule {exp_rule!r}, got {sorted(rules_hit)}"
                            )

        records = session.memory.records()
        passed = sum(1 for r in records if r["passed"])
        print(f"######## memory: {len(records)} runs, {passed} passed, {len(records) - passed} failed")
        return 1 if unexpected_errors else 0
    finally:
        if temp_ctx is not None:
            temp_ctx.cleanup()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lab-rehearsal",
        description="Digital-twin rehearsal, default-deny safety verification, and episodic write-back.",
    )
    parser.add_argument(
        "protocols",
        nargs="*",
        help="Protocol JSON/YAML file(s) to rehearse. If omitted (or --demo), runs the 2-pass showcase.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run the deterministic 2-pass showcase on demo/protocols in an isolated temp ledger.",
    )
    parser.add_argument(
        "--memory",
        type=Path,
        default=None,
        help="Path to episodic runs.jsonl (defaults to gitignored data/memory/runs.jsonl; isolated temp for --demo).",
    )
    parser.add_argument(
        "--reset-memory",
        action="store_true",
        help="Explicitly clear the target --memory ledger before running.",
    )
    parser.add_argument(
        "--fast-deny",
        action="store_true",
        help="Immediately block protocols whose exact hash previously failed in memory.",
    )
    parser.add_argument(
        "--persist-deck",
        action="store_true",
        help="Carry remaining container volumes and tiprack occupancy across sequential protocols.",
    )
    parser.add_argument(
        "--format",
        choices=(
            "auto",
            "native",
            "stepbot",
            "twinstep",
            "lablean",
            "opentrons_json",
            "bioplanner",
            "finebio",
            "bioprobench",
        ),
        default="auto",
        help="Input protocol IR format.",
    )
    parser.add_argument(
        "--no-dead-vol",
        action="store_true",
        help="Set container dead volumes to 0 uL instead of physical defaults (5 uL plate / 500 uL trough).",
    )
    parser.add_argument(
        "--monte-carlo",
        type=int,
        default=0,
        metavar="N",
        help="Run N seeded stochastic pipetting rollouts in addition to deterministic verification.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for --monte-carlo rollouts (default: 42).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON results to stdout.",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, default_demo_dir: Path | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[2]
    demo_proto_dir = default_demo_dir or (repo_root / "demo" / "protocols")

    base_spec = (
        DeckSpec(plate_dead_vol=0.0, res_dead_vol=0.0)
        if args.no_dead_vol
        else DeckSpec(plate_dead_vol=PLATE_DEAD_VOL, res_dead_vol=RES_DEAD_VOL)
    )

    if args.demo or not args.protocols:
        return run_demo_showcase(
            demo_proto_dir,
            memory_path=args.memory,
            reset_memory=args.reset_memory,
            spec=base_spec,
        )

    mem_path = args.memory if args.memory is not None else (repo_root / "data" / "memory" / "runs.jsonl")
    mem_store = MemoryStore(mem_path)
    if args.reset_memory:
        mem_store.reset()

    session = RehearsalSession(
        memory_path=mem_path,
        spec=base_spec,
        persist_deck=args.persist_deck,
        fast_deny_known_failures=args.fast_deny,
    )

    all_passed = True
    json_results: list[dict[str, Any]] = []
    for raw_p in args.protocols:
        p_path = Path(raw_p)
        steps, file_spec = _load_steps_file(p_path, fmt=args.format)
        if args.no_dead_vol:
            file_spec = DeckSpec(
                well_max=file_spec.well_max,
                res_max=file_spec.res_max,
                trash_max=file_spec.trash_max,
                pip_min=file_spec.pip_min,
                pip_max=file_spec.pip_max,
                tip_rack=file_spec.tip_rack,
                plate_dead_vol=0.0,
                res_dead_vol=0.0,
                start=file_spec.start,
                diluents=file_spec.diluents,
                waste_locs=file_spec.waste_locs,
                channels=file_spec.channels,
                pipettes=file_spec.pipettes,
                frozen_locs=file_spec.frozen_locs,
                sealed_locs=file_spec.sealed_locs,
                evap_coeffs=file_spec.evap_coeffs,
                strict_tenths=file_spec.strict_tenths,
            )
        session.spec = file_spec
        res = session.evaluate_protocol(p_path.name, steps)
        if not res["approved"]:
            all_passed = False

        mc_report = None
        if args.monte_carlo > 0 and isinstance(steps, list):
            mc_report = rehearse_monte_carlo(
                steps,
                n_rollouts=args.monte_carlo,
                seed=args.seed,
                spec=file_spec,
            )

        if args.json:
            json_results.append(
                {
                    "protocol": res["protocol"],
                    "hash": res["hash"],
                    "steps_hash": res["steps_hash"],
                    "env_hash": res["env_hash"],
                    "recall": res["recall"],
                    "approved": res["approved"],
                    "violations": list(res["violations"]),
                    "run": res["run"],
                    "run_id": res["run_id"],
                    "monte_carlo": mc_report,
                }
            )
        else:
            short_h = res["hash"][:12]
            end = res["end"]
            n_steps = len(steps) if isinstance(steps, list) else 0
            print(f"== {p_path.name} (hash {short_h}, {n_steps} steps)")
            print(f"  RECALL:   {res['recall']}")
            print(
                f"  REHEARSE: tips used {end['tips_used']}, plate wells filled {len(end['plate'])}, "
                f"tip on at end {end['tip_on']}"
            )
            print(
                f"  VERIFY:   {'PASS' if res['approved'] else 'FAIL'} ({len(res['violations'])} violation(s))"
            )
            for v in res["violations"]:
                print(f"    - {v}")
            print(f"  RUN:      {res['run']}")
            if mc_report is not None:
                print(
                    f"  MC({mc_report['n_rollouts']}): overfill_rate={mc_report['overfill_rate']:.2%}, "
                    f"underflow_rate={mc_report['dead_volume_underflow_rate']:.2%}"
                )
            print(f"  WRITE:    run #{res['run_id']} appended to {mem_path}")

    if args.json:
        print(json.dumps(json_results, indent=2))
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
