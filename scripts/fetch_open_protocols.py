"""Fetch (or load offline) 2024-2026 open wet-lab protocols, convert, and dry-run rehearse."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from lab_rehearsal.open_datasets import (  # noqa: E402
    check_dataset_ingestion_policy,
    rehearse_open_protocol,
)

LIVE_UPSTREAM_SOURCES: list[dict[str, Any]] = [
    {
        "sample_id": "opentrons_simple_v6",
        "dataset_id": "opentrons-shared-protocols",
        "fmt": "opentrons_json",
        "url": "https://raw.githubusercontent.com/Opentrons/opentrons/edge/shared-data/protocol/fixtures/6/simpleV6.json",
    },
    {
        "sample_id": "opentrons_simple_v7",
        "dataset_id": "opentrons-shared-protocols",
        "fmt": "opentrons_json",
        "url": "https://raw.githubusercontent.com/Opentrons/opentrons/edge/shared-data/protocol/fixtures/7/simpleV7.json",
    },
    {
        "sample_id": "bioplanner_bioprot_10051",
        "dataset_id": "bioplanner",
        "fmt": "bioplanner",
        "url": "https://raw.githubusercontent.com/bioplanner/bioplanner/main/bioprot/10051.json",
    },
    {
        "sample_id": "bioprobench_ord_sample",
        "dataset_id": "bioprobench",
        "fmt": "bioprobench",
        "url": "https://raw.githubusercontent.com/YuyangSunshine/bioprobench/main/Data/ORD_test.json",
        "record_index": 1,
    },
]

CURATED_OFFLINE_SAMPLES: list[dict[str, Any]] = [
    {
        "sample_id": "opentrons_v6_p10_transfer",
        "dataset_id": "opentrons-shared-protocols",
        "fmt": "opentrons_json",
        "payload": {
            "$otSharedSchema": "#/protocol/schemas/6",
            "schemaVersion": 6,
            "pipettes": {"pipetteId": {"name": "p10_single"}},
            "labware": {
                "fixedTrash": {
                    "displayName": "Trash",
                    "definitionId": "opentrons/opentrons_1_trash_1100ml_fixed/1",
                },
                "tipRackId": {
                    "displayName": "Opentrons 96 Tip Rack 10 uL",
                    "definitionId": "opentrons/opentrons_96_tiprack_10ul/1",
                },
                "sourcePlateId": {
                    "displayName": "Source Plate",
                    "definitionId": "example/plate/1",
                },
                "destPlateId": {
                    "displayName": "Sample Collection Plate",
                    "definitionId": "example/plate/1",
                },
            },
            "liquids": {"waterId": {"displayName": "Water"}},
            "commands": [
                {
                    "commandType": "loadLiquid",
                    "params": {
                        "liquidId": "waterId",
                        "labwareId": "sourcePlateId",
                        "volumeByWell": {"A1": 100, "B1": 100},
                    },
                },
                {
                    "commandType": "pickUpTip",
                    "params": {
                        "pipetteId": "pipetteId",
                        "labwareId": "tipRackId",
                        "wellName": "B1",
                    },
                },
                {
                    "commandType": "aspirate",
                    "params": {
                        "pipetteId": "pipetteId",
                        "labwareId": "sourcePlateId",
                        "wellName": "A1",
                        "volume": 5.0,
                    },
                },
                {"commandType": "delay", "params": {"seconds": 42}},
                {
                    "commandType": "dispense",
                    "params": {
                        "pipetteId": "pipetteId",
                        "labwareId": "destPlateId",
                        "wellName": "B1",
                        "volume": 4.5,
                    },
                },
                {
                    "commandType": "blowout",
                    "params": {
                        "pipetteId": "pipetteId",
                        "labwareId": "destPlateId",
                        "wellName": "B1",
                    },
                },
                {
                    "commandType": "dropTip",
                    "params": {
                        "pipetteId": "pipetteId",
                        "labwareId": "fixedTrash",
                        "wellName": "A1",
                    },
                },
            ],
        },
    },
    {
        "sample_id": "opentrons_v7_8ch_thermocycler",
        "dataset_id": "opentrons-shared-protocols",
        "fmt": "opentrons_json",
        "payload": {
            "$otSharedSchema": "#/protocol/schemas/7",
            "schemaVersion": 7,
            "liquids": {"mastermixId": {"displayName": "Buffer MasterMix"}},
            "commands": [
                {
                    "commandType": "loadPipette",
                    "params": {"pipetteId": "p300m", "pipetteName": "p300_multi_gen2"},
                },
                {
                    "commandType": "loadLabware",
                    "params": {
                        "labwareId": "tips1",
                        "loadName": "opentrons_96_tiprack_300ul",
                        "displayName": "Tip Rack",
                    },
                },
                {
                    "commandType": "loadLabware",
                    "params": {
                        "labwareId": "res1",
                        "loadName": "nest_12_reservoir_15ml",
                        "displayName": "Reservoir",
                    },
                },
                {
                    "commandType": "loadLabware",
                    "params": {
                        "labwareId": "pcrPlate",
                        "loadName": "armadillo_96_wellplate_200ul_pcr_full_skirt",
                        "displayName": "PCR Plate",
                    },
                },
                {
                    "commandType": "loadLiquid",
                    "params": {
                        "liquidId": "mastermixId",
                        "labwareId": "res1",
                        "volumeByWell": {"A1": 8000.0},
                    },
                },
                {
                    "commandType": "pickUpTip",
                    "params": {"pipetteId": "p300m", "labwareId": "tips1", "wellName": "A1"},
                },
                {
                    "commandType": "aspirate",
                    "params": {
                        "pipetteId": "p300m",
                        "labwareId": "res1",
                        "wellName": "A1",
                        "volume": 50.0,
                    },
                },
                {
                    "commandType": "dispense",
                    "params": {
                        "pipetteId": "p300m",
                        "labwareId": "pcrPlate",
                        "wellName": "A1",
                        "volume": 50.0,
                    },
                },
                {
                    "commandType": "dropTip",
                    "params": {"pipetteId": "p300m", "labwareId": "fixedTrash", "wellName": "A1"},
                },
                {"commandType": "thermocycler/closeLid", "params": {}},
                {
                    "commandType": "thermocycler/setTargetBlockTemperature",
                    "params": {"celsius": 95.0},
                },
                {"commandType": "waitForDuration", "params": {"seconds": 120.0}},
            ],
        },
    },
    {
        "sample_id": "bioplanner_edna_12s_pcr",
        "dataset_id": "bioplanner",
        "fmt": "bioplanner",
        "payload": {
            "id": "10051",
            "title": "Environmental DNA (eDNA) 12S metabarcoding Illumina MiSeq NGS PCR Protocol",
            "generated_pseudocode": (
                "primary_pcr("
                "reaction_params={"
                "'template': '2ul gDNA extract (1:10 dilution)', "
                "'mastermix': '10 ul Hotstar MasterMix (2X)', "
                "'primers': '0.8 ul of each primer', "
                "'water': '7.2 ul molecular biology grade water'}, "
                "cycling_params={'initial': (95, 5, 'min'), 'cycles': 40})\n"
                "secondary_pcr("
                "reaction_params={"
                "'amplicon': '2ul primary PCR product', "
                "'mastermix': '12.5 ul MasterMix', "
                "'water': '10.5 ul water'}, "
                "cycling_params={'initial': (95, 3, 'min'), 'cycles': 12})\n"
            ),
        },
    },
    {
        "sample_id": "finebio_p03_pipette_mix_trace",
        "dataset_id": "finebio",
        "fmt": "finebio_trace",
        "payload": {
            "database": {
                "P03_02_01": {
                    "subset": "test",
                    "annotations": [
                        {"atomic_operation_label": "take-blue_pipette"},
                        {"atomic_operation_label": "open-blue_tip_rack"},
                        {"atomic_operation_label": "insert-blue_pipette-blue_tip"},
                        {"atomic_operation_label": "press-blue_pipette"},
                        {"atomic_operation_label": "insert-blue_pipette-50ml_tube"},
                        {"atomic_operation_label": "release-blue_pipette"},
                        {"atomic_operation_label": "detach-blue_pipette-50ml_tube"},
                        {"atomic_operation_label": "insert-blue_pipette-cell_culture_plate"},
                        {"atomic_operation_label": "press-blue_pipette"},
                        {"atomic_operation_label": "release-blue_pipette"},
                        {"atomic_operation_label": "detach-blue_pipette-cell_culture_plate"},
                        {"atomic_operation_label": "eject-blue_pipette-blue_tip"},
                        {"atomic_operation_label": "insert-blue_pipette-blue_tip"},
                        {"atomic_operation_label": "press-blue_pipette"},
                        {"atomic_operation_label": "insert-blue_pipette-15ml_tube"},
                        {"atomic_operation_label": "release-blue_pipette"},
                        {"atomic_operation_label": "detach-blue_pipette-15ml_tube"},
                        {
                            "atomic_operation_label": "insert-blue_pipette-cell_culture_plate",
                            "well": "A1",
                        },
                        {"atomic_operation_label": "press-blue_pipette"},
                        {"atomic_operation_label": "release-blue_pipette"},
                        {"atomic_operation_label": "press-blue_pipette"},
                        {"atomic_operation_label": "release-blue_pipette"},
                        {"atomic_operation_label": "press-blue_pipette"},
                        {"atomic_operation_label": "release-blue_pipette"},
                        {
                            "atomic_operation_label": "detach-blue_pipette-cell_culture_plate",
                            "well": "A1",
                        },
                        {"atomic_operation_label": "eject-blue_pipette-blue_tip"},
                    ],
                }
            }
        },
    },
    {
        "sample_id": "bioprobench_ord_rbc_lysis",
        "dataset_id": "bioprobench",
        "fmt": "bioprobench",
        "payload": {
            "id": "TEST-ORD-000001",
            "correct_steps": [
                "Wash the spleen cells suspension with 3 mL of complete RPMI medium.",
                "Discard the supernatant and resuspend cells in 3 mL of ice-cold ACK buffer.",
                "Incubate for 5 minutes at room temperature with occasional shaking.",
                "Stop the lysis reaction by adding complete RPMI medium to a final volume of 10 mL.",
            ],
            "wrong_steps": [
                "Discard the supernatant and resuspend cells in 3 mL of ice-cold ACK buffer.",
                "Wash the spleen cells suspension with 3 mL of complete RPMI medium.",
                "Incubate for 5 minutes at room temperature with occasional shaking.",
                "Stop the lysis reaction by adding complete RPMI medium to a final volume of 10 mL.",
            ],
        },
    },
    {
        "sample_id": "lab_bench_protocolqa_pcr_prep",
        "dataset_id": "lab-bench",
        "fmt": "protocolqa",
        "payload": {
            "id": "protocolqa-01",
            "protocol": [
                "Thaw frozen cDNA sample stock on ice.",
                "Transfer 50 uL of thawed sample stock into assay plate.",
                "Add 150 uL of PCR mastermix buffer and mix gently.",
                "Seal the assay plate with optical adhesive film.",
                "Incubate at 95 C for 3 minutes for initial denaturation.",
            ],
        },
    },
]


def _fetch_json(url: str, timeout: float = 15.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "lab-rehearsal-open-datasets/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_and_rehearse_datasets(
    *,
    dataset_filter: str = "all",
    online: bool = False,
    purpose: str = "rehearsal_eval",
    require_commercial_ok: bool = False,
    out_dir: Path | None = None,
    catalog_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Load offline or live open-dataset protocols, enforce SPDX/eval_only gates, and dry-run."""
    entries = LIVE_UPSTREAM_SOURCES if online else CURATED_OFFLINE_SAMPLES
    results: list[dict[str, Any]] = []

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    for item in entries:
        ds_id = str(item["dataset_id"])
        if dataset_filter != "all" and ds_id != dataset_filter:
            continue

        sample_id = str(item["sample_id"])
        policy_ok, policy_reasons = check_dataset_ingestion_policy(
            ds_id,
            purpose=purpose,
            require_commercial_ok=require_commercial_ok,
            catalog_path=catalog_path,
        )
        if not policy_ok:
            results.append(
                {
                    "sample_id": sample_id,
                    "dataset_id": ds_id,
                    "policy_approved": False,
                    "policy_reasons": policy_reasons,
                    "approved": False,
                    "num_steps": 0,
                    "violations": [],
                }
            )
            continue

        if online:
            raw_payload = _fetch_json(str(item["url"]))
            if isinstance(raw_payload, list) and "record_index" in item:
                raw_payload = raw_payload[int(item["record_index"])]
        else:
            raw_payload = item["payload"]

        reh = rehearse_open_protocol(
            raw_payload,
            dataset_id=ds_id,
            fmt=str(item["fmt"]),
            purpose=purpose,
            require_commercial_ok=require_commercial_ok,
            protocol_name=f"{sample_id}.json",
            catalog_path=catalog_path,
        )

        steps = reh["steps"]
        if out_dir is not None and reh["policy_approved"]:
            (out_dir / f"{sample_id}.steps.json").write_text(
                json.dumps(steps, indent=2) + "\n", encoding="utf-8"
            )

        results.append(
            {
                "sample_id": sample_id,
                "dataset_id": ds_id,
                "policy_approved": reh["policy_approved"],
                "policy_reasons": reh["policy_reasons"],
                "approved": reh["approved"],
                "schema_errors": reh["schema_errors"],
                "num_steps": len(steps),
                "violations": [str(v) for v in reh["violations"]],
            }
        )

    if out_dir is not None:
        (out_dir / "manifest.json").write_text(
            json.dumps({"online": online, "purpose": purpose, "results": results}, indent=2) + "\n",
            encoding="utf-8",
        )

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert and dry-run 2024-2026 open wet-lab protocol datasets in lab-rehearsal."
    )
    parser.add_argument(
        "--dataset",
        default="all",
        help="Dataset id from catalog/simulators.json, or 'all' (default).",
    )
    parser.add_argument(
        "--online",
        action="store_true",
        help="Fetch live protocol JSONs from verified upstream GitHub repositories.",
    )
    parser.add_argument(
        "--purpose",
        choices=("rehearsal_eval", "training", "redistribution"),
        default="rehearsal_eval",
        help="Governance purpose checked against eval_only and redistribution_ok flags.",
    )
    parser.add_argument(
        "--require-commercial",
        action="store_true",
        help="Deny datasets with commercial_use_ok=false.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Optional directory to write converted twin step JSONs and manifest.json.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print structured JSON summary to stdout.",
    )
    args = parser.parse_args(argv)

    results = fetch_and_rehearse_datasets(
        dataset_filter=args.dataset,
        online=args.online,
        purpose=args.purpose,
        require_commercial_ok=args.require_commercial,
        out_dir=args.out_dir,
    )

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            status = (
                "POLICY_BLOCKED"
                if not r["policy_approved"]
                else ("PASS" if r["approved"] else "FAIL")
            )
            print(
                f"[{status}] {r['sample_id']} ({r['dataset_id']}): {r['num_steps']} twin steps"
            )
            for reason in r["policy_reasons"]:
                print(f"  - policy: {reason}")
            for v in r["violations"]:
                print(f"  - violation: {v}")

    return 0 if results and all(r["approved"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
