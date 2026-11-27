"""Download, SHA-256 verify, and dry-run rehearse all 19 official Opentrons protocol fixtures.

Pins `Opentrons/opentrons` (`Apache-2.0`) `shared-data/protocol/fixtures/{6,7,8}` at commit
`84a9956efccae8783f1a49ec14873222956f3284` into gitignored `data/opentrons_fixtures/`.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from typing import Any
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from lab_rehearsal import (  # noqa: E402
    check_dataset_ingestion_policy,
    from_opentrons_json,
    rehearse,
    validate_protocol_schema,
    verify,
)

PINNED_OPENTRONS_COMMIT = "84a9956efccae8783f1a49ec14873222956f3284"
DEFAULT_FIXTURE_DIR = REPO_ROOT / "data" / "opentrons_fixtures"

EXPECTED_FIXTURE_SHA256: dict[str, str] = {
    "6/heaterShakerCommands.json": "0d128492998e1b5d81f82c767fa62bc2caebb6cd6a65f2f424e930c4a0f66a4d",
    "6/heaterShakerCommandsWithResultsKey.json": "c89c39438c714eab3bff2beccedd64f454ff681e20858bdf140b3526927ae093",
    "6/multipleTempModules.json": "266691542161e068af1720206ea68595b8512dd98d5ea687417a59c4dfa31ffa",
    "6/multipleTipracks.json": "5a4358a628dc1c6c38c0f307a11a95a8065b9dddd122b89e2c20132e0f1f4350",
    "6/multipleTipracksWithTC.json": "b48b9d932dbc82af50ff09f14dc8748c08784b2a2d322295a0559a10b36605bf",
    "6/oneTiprack.json": "963e4a9ab816b503b5584130cf784463d0259e5df8477455d813b373872c65e9",
    "6/simpleV6.json": "296b971925ae9b4cb4e5710afda0cec00458d18bb453df7d7a4dc9b88e28eb3e",
    "6/tempAndMagModuleCommands.json": "a08e3648a86ba68b31bb812ce991945030033c6ccf1ee8b3161fce828faaab89",
    "6/transferSettings.json": "a35a82be7e04a6750bfe6896d355ac296b01e661adc12c55a88aca6eb772821d",
    "7/moveLabware.json": "ccc70657750e4d31c5e0251ce5685487babc5b301c4eca01934fadd1abab8b03",
    "7/simpleV7.json": "82134aa0cb6b75fec8a6675262d02149863fc5290e7a80aabf2df16bc2e94f2e",
    "8/moveLabware8.json": "3a246014731da6996dc3fa478dd26696a4c33ef04d9bd61b9c617b9cc93455b5",
    "8/moveLabwareFlex8.json": "de3b0d9d42bb7c23b00f0162486426f31fe7accfae70d137fde115fd470b2d45",
    "8/simpleFlexV10CommandsV8.json": "9e28c1f68d5ea94a9b8ff9772db52cadd79bc35a98036417f7e079e01d2e12e7",
    "8/simpleFlexV8.json": "60fe4ca1dfda9b926d1d8efe470a7188803ee194331a105e81dbdd8a64083dc0",
    "8/simpleFlexV9CommandsV8.json": "2c3dd1474d19429a8ea27b2321b56caa5530835dbace3e8edf6a4b099e4e0a6e",
    "8/simpleV10CommandsV8.json": "f7b6dfa2c268c26b144819a06113f4580128125647fc17b5d73ca9d68280c38e",
    "8/simpleV8.json": "a29437ec512f7742fb6b5fee82b9c457955bb92feb3c0c82bdce6fe6c691029a",
    "8/simpleV9CommandsV8.json": "d32f28a7d44685c66b543b513f155ed44d1b6af118cd1f053b81c2924c1010af",
}

EXPECTED_FIXTURE_VERDICTS: dict[str, tuple[bool, tuple[str, ...], str]] = {
    "6/heaterShakerCommands.json": (
        False,
        ("all_tips_dropped",),
        "Transfers 5 uL water (sourcePlateId -> destPlateId) cleanly; omits final dropTip",
    ),
    "6/heaterShakerCommandsWithResultsKey.json": (
        False,
        ("empty_protocol",),
        "Deck/module setup fixture only (0 liquid-handling, tip, or thermal commands)",
    ),
    "6/multipleTempModules.json": (
        False,
        ("unknown_start_volume",),
        "11 transfers across 5 distinct plates, 1 reservoir & 2 tip racks; omits loadLiquid",
    ),
    "6/multipleTipracks.json": (
        False,
        ("unknown_start_volume",),
        "13 transfers across 2 independent 96-tip racks & 12-reservoir; omits loadLiquid",
    ),
    "6/multipleTipracksWithTC.json": (
        False,
        ("unknown_start_volume",),
        "Thermocycler lid seal + 13 transfers across 2 independent tip racks; omits loadLiquid",
    ),
    "6/oneTiprack.json": (
        False,
        ("unknown_start_volume",),
        "Single 5 uL transfer between distinct source/dest plates; omits loadLiquid",
    ),
    "6/simpleV6.json": (
        True,
        (),
        "p10_single transfers 5 uL declared waterId (sourcePlateId:A1 -> destPlateId:B1) and drops tip",
    ),
    "6/tempAndMagModuleCommands.json": (
        False,
        ("all_tips_dropped",),
        "Transfers 5 uL declared waterId between distinct plates; omits final dropTip",
    ),
    "6/transferSettings.json": (
        False,
        ("unknown_start_volume",),
        "11 transfers across 5 distinct plates, 1 reservoir & 2 tip racks; omits loadLiquid",
    ),
    "7/moveLabware.json": (
        False,
        ("all_tips_dropped",),
        "Picks up tip and moves labware without dropping tip at end",
    ),
    "7/simpleV7.json": (
        False,
        ("schema_valid", "tip_present", "unknown_start_volume"),
        "Multi-channel p1000_96 targets non-row-A B1 without single-nozzle config; C1..H1 undeclared",
    ),
    "8/moveLabware8.json": (
        False,
        ("all_tips_dropped",),
        "Picks up tip and moves labware without dropping tip at end",
    ),
    "8/moveLabwareFlex8.json": (
        False,
        ("all_tips_dropped",),
        "Picks up tip on Flex deck and moves labware without dropping tip at end",
    ),
    "8/simpleFlexV10CommandsV8.json": (
        False,
        ("schema_valid", "tip_present", "unknown_start_volume"),
        "Multi-channel p1000_96 targets non-row-A B1 without single-nozzle config; C1..H1 undeclared",
    ),
    "8/simpleFlexV8.json": (
        False,
        ("schema_valid", "tip_present", "unknown_start_volume"),
        "Multi-channel p1000_96 targets non-row-A B1 without single-nozzle config; C1..H1 undeclared",
    ),
    "8/simpleFlexV9CommandsV8.json": (
        False,
        ("schema_valid", "tip_present", "unknown_start_volume"),
        "Multi-channel p1000_96 targets non-row-A B1 without single-nozzle config; C1..H1 undeclared",
    ),
    "8/simpleV10CommandsV8.json": (
        True,
        (),
        "p10_single transfers 5 uL declared waterId (sourcePlateId:A1 -> destPlateId:B1) and drops tip",
    ),
    "8/simpleV8.json": (
        True,
        (),
        "p10_single transfers 5 uL declared waterId (sourcePlateId:A1 -> destPlateId:B1) and drops tip",
    ),
    "8/simpleV9CommandsV8.json": (
        True,
        (),
        "p10_single transfers 5 uL declared waterId (sourcePlateId:A1 -> destPlateId:B1) and drops tip",
    ),
}


def verify_cached_fixture_sha256(fixture_dir: Path = DEFAULT_FIXTURE_DIR) -> list[str]:
    """Verify every pinned Opentrons fixture exists under `fixture_dir` with exact SHA-256."""
    errors: list[str] = []
    for rel_path, expected_sha in sorted(EXPECTED_FIXTURE_SHA256.items()):
        target = fixture_dir / rel_path
        if not target.is_file():
            errors.append(f"missing cached fixture: {target}")
            continue
        actual_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual_sha != expected_sha:
            errors.append(
                f"SHA-256 mismatch for {rel_path}: expected {expected_sha}, got {actual_sha}"
            )
    return errors


def fetch_opentrons_fixtures(
    fixture_dir: Path = DEFAULT_FIXTURE_DIR,
    *,
    force: bool = False,
    timeout: float = 20.0,
) -> list[Path]:
    """Download all 19 pinned Opentrons JSON fixtures and verify SHA-256 digests."""
    policy_ok, policy_reasons = check_dataset_ingestion_policy(
        "opentrons-shared-protocols", purpose="rehearsal_eval"
    )
    if not policy_ok:
        raise RuntimeError(f"Catalog policy blocked opentrons-shared-protocols: {policy_reasons}")

    written: list[Path] = []
    for rel_path, expected_sha in sorted(EXPECTED_FIXTURE_SHA256.items()):
        dest = fixture_dir / rel_path
        if dest.is_file() and not force:
            existing_sha = hashlib.sha256(dest.read_bytes()).hexdigest()
            if existing_sha == expected_sha:
                written.append(dest)
                continue

        url = (
            f"https://raw.githubusercontent.com/Opentrons/opentrons/"
            f"{PINNED_OPENTRONS_COMMIT}/shared-data/protocol/fixtures/{rel_path}"
        )
        req = urllib.request.Request(
            url, headers={"User-Agent": "lab-rehearsal-opentrons-fixtures/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload_bytes = resp.read()
        actual_sha = hashlib.sha256(payload_bytes).hexdigest()
        if actual_sha != expected_sha:
            raise ValueError(
                f"SHA-256 verification failed for {url}: expected {expected_sha}, got {actual_sha}"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload_bytes)
        written.append(dest)
    return written


def evaluate_opentrons_fixtures(fixture_dir: Path = DEFAULT_FIXTURE_DIR) -> list[dict[str, Any]]:
    """Verify SHA-256 of all 19 fixtures and run `from_opentrons_json` + `rehearse` + `verify`."""
    sha_errors = verify_cached_fixture_sha256(fixture_dir)
    if sha_errors:
        raise FileNotFoundError("; ".join(sha_errors))

    results: list[dict[str, Any]] = []
    for rel_path, expected_sha in sorted(EXPECTED_FIXTURE_SHA256.items()):
        p = fixture_dir / rel_path
        doc = json.loads(p.read_bytes().decode("utf-8"))
        steps, spec = from_opentrons_json(doc)
        schema_errs = validate_protocol_schema(steps) if steps else []
        trace, end = rehearse(steps, spec=spec)
        approved, violations = verify(trace, end)
        rule_counts = dict(sorted(Counter(v["rule"] for v in violations).items()))
        exp_ok, exp_rules, exp_reason = EXPECTED_FIXTURE_VERDICTS[rel_path]
        results.append(
            {
                "fixture": rel_path,
                "sha256": expected_sha,
                "num_steps": len(steps),
                "declared_start_wells": len(spec.start),
                "containers": sorted(spec.containers.keys()),
                "extra_tipracks": list(spec.tipracks),
                "schema_errors": schema_errs,
                "approved": approved,
                "expected_approved": exp_ok,
                "rule_counts": rule_counts,
                "expected_rules": list(exp_rules),
                "reason": exp_reason,
                "verdict_matches_expected": (
                    approved == exp_ok and tuple(sorted(rule_counts.keys())) == exp_rules
                ),
            }
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch and SHA-256 verify all 19 official Opentrons protocol fixtures."
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_FIXTURE_DIR,
        help="Target directory for cached Opentrons fixtures (default: data/opentrons_fixtures).",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Verify and evaluate existing cached fixtures without network access.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download fixtures even if SHA-256-verified files already exist.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print structured JSON evaluation table to stdout.",
    )
    args = parser.parse_args(argv)

    if not args.offline:
        fetch_opentrons_fixtures(args.out_dir, force=args.force)

    results = evaluate_opentrons_fixtures(args.out_dir)
    if args.json:
        print(
            json.dumps(
                {
                    "commit": PINNED_OPENTRONS_COMMIT,
                    "total_fixtures": len(results),
                    "approved_count": sum(1 for r in results if r["approved"]),
                    "rejected_count": sum(1 for r in results if not r["approved"]),
                    "results": results,
                },
                indent=2,
            )
        )
    else:
        n_pass = sum(1 for r in results if r["approved"])
        print(
            f"Opentrons fixtures @ {PINNED_OPENTRONS_COMMIT[:12]}: "
            f"{len(results)} total ({n_pass} PASS, {len(results) - n_pass} BLOCK)"
        )
        for r in results:
            verdict = "PASS" if r["approved"] else "BLOCK"
            rules_s = ", ".join(f"{k}:{v}" for k, v in r["rule_counts"].items()) or "none"
            print(
                f"  [{verdict:5s}] {r['fixture']:40s} steps={r['num_steps']:2d} "
                f"rules=({rules_s}) -- {r['reason']}"
            )

    return 0 if all(r["verdict_matches_expected"] and not r["schema_errors"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
