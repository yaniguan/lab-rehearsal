"""Tests for 2024-2026 open wet-lab protocol & embodied dataset catalog and converters."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"
for _p in (str(SRC_DIR), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import check_catalog  # noqa: E402
import fetch_open_protocols  # noqa: E402
from lab_rehearsal import (  # noqa: E402
    RehearsalSession,
    check_dataset_ingestion_policy,
    convert_open_protocol,
    from_bioplanner,
    from_bioprobench,
    from_finebio_trace,
    from_opentrons_json,
    get_dataset_entry,
    list_open_datasets,
    load_dataset_catalog,
    rehearse,
    rehearse_open_protocol,
    validate_protocol_schema,
    verify,
)

CJK_RE = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")

EXPECTED_NEW_2024_2026_DATASETS: dict[str, tuple[str, bool, bool, bool]] = {
    # id: (spdx_license, commercial_use_ok, redistribution_ok, eval_only)
    "opentrons-shared-protocols": ("Apache-2.0", True, True, False),
    "opentrons-protocols": ("NOASSERTION", False, False, False),
    "finebio": ("LicenseRef-FineBio-NC", False, False, False),
    "bioplanner": ("MIT", True, True, True),
    "bioprobench": ("CC-BY-NC-4.0", False, True, True),
    "lab-bench": ("CC-BY-SA-4.0", True, True, True),
    "sciknoweval": ("MIT", True, True, True),
    "embodiedlab": ("Apache-2.0", True, True, True),
    "matterix": ("BSD-3-Clause", True, True, False),
    "wetop-bench": ("MIT", True, True, True),
}


def _rules(violations) -> list[str]:
    return [v["rule"] for v in violations]


def test_catalog_simulators_2026_schema_and_spdx_governance() -> None:
    errs = check_catalog.validate_catalog(REPO_ROOT, online=False)
    assert errs == [], f"catalog validation errors: {errs}"

    catalog = load_dataset_catalog()
    datasets_by_id = {d["id"]: d for d in catalog["datasets"]}
    assert len(datasets_by_id) >= 14

    for ds_id, (exp_spdx, exp_comm, exp_redist, exp_eval_only) in EXPECTED_NEW_2024_2026_DATASETS.items():
        assert ds_id in datasets_by_id, f"missing expected dataset {ds_id}"
        entry = get_dataset_entry(ds_id)
        assert entry["spdx_license"] == exp_spdx
        assert entry["commercial_use_ok"] is exp_comm
        assert entry["redistribution_ok"] is exp_redist
        assert entry["eval_only"] is exp_eval_only
        assert entry["url"].startswith("https://")
        assert 2024 <= int(entry["year"]) <= 2026

    recent_verified = list_open_datasets(min_year=2024, verified_only=True)
    recent_ids = {d["id"] for d in recent_verified}
    for verified_id in (
        "opentrons-shared-protocols",
        "bioplanner",
        "bioprobench",
        "lab-bench",
        "sciknoweval",
        "embodiedlab",
        "matterix",
        "wetop-bench",
    ):
        assert verified_id in recent_ids


def test_dataset_ingestion_policy_eval_only_and_commercial_guards() -> None:
    # 1. Benchmark eval_only datasets allowed for rehearsal_eval, blocked for training
    for eval_ds in ("bioplanner", "bioprobench", "lab-bench", "sciknoweval", "embodiedlab", "wetop-bench"):
        ok_eval, reasons_eval = check_dataset_ingestion_policy(eval_ds, purpose="rehearsal_eval")
        assert ok_eval is True and reasons_eval == []

        ok_train, reasons_train = check_dataset_ingestion_policy(eval_ds, purpose="training")
        assert ok_train is False
        assert any("eval_only_guard" in r for r in reasons_train)

    # 2. Non-eval-only simulation/protocol datasets allowed for training
    for train_ds in ("opentrons-shared-protocols", "matterix", "labutopia"):
        ok_train, reasons_train = check_dataset_ingestion_policy(
            train_ds, purpose="training", require_commercial_ok=True
        )
        assert ok_train is True and reasons_train == []

    # 3. Non-commercial datasets blocked when require_commercial_ok=True
    for nc_ds in ("bioprobench", "finebio", "opentrons-protocols"):
        ok_comm, reasons_comm = check_dataset_ingestion_policy(
            nc_ds, purpose="rehearsal_eval", require_commercial_ok=True
        )
        assert ok_comm is False
        assert any("non_commercial_license" in r for r in reasons_comm)

    # 4. Redistribution blocked when redistribution_ok=False
    ok_redist, reasons_redist = check_dataset_ingestion_policy("finebio", purpose="redistribution")
    assert ok_redist is False
    assert any("redistribution_denied" in r for r in reasons_redist)

    # 5. Unknown dataset & invalid purpose default-deny
    ok_unk, r_unk = check_dataset_ingestion_policy("nonexistent-ds-999")
    assert ok_unk is False and any("unknown_dataset" in r for r in r_unk)

    ok_purp, r_purp = check_dataset_ingestion_policy("bioplanner", purpose="scrape_raw")
    assert ok_purp is False and any("invalid_purpose" in r for r in r_purp)


def test_opentrons_json_v6_v7_and_protobuild_rehearsal() -> None:
    proto_schema = json.loads((REPO_ROOT / "schema" / "protocol.schema.json").read_text(encoding="utf-8"))

    # 1. Opentrons v6 p10_single with touchTip + blowout coalescing
    v6_sample = fetch_open_protocols.CURATED_OFFLINE_SAMPLES[0]["payload"]
    steps_v6, spec_v6 = from_opentrons_json(v6_sample)
    jsonschema.validate(steps_v6, proto_schema)
    assert validate_protocol_schema(steps_v6) == []
    t6, e6 = rehearse(steps_v6, spec=spec_v6)
    ok6, viol6 = verify(t6, e6)
    assert ok6 is True, viol6
    assert e6["plate"]["plate:B1"] == pytest.approx(5.0)
    assert e6["tip_on"] is False

    # 2. Opentrons v7 8-channel p300_multi_gen2 + thermocycler lid seal & 95 C hold
    v7_sample = fetch_open_protocols.CURATED_OFFLINE_SAMPLES[1]["payload"]
    steps_v7, spec_v7 = from_opentrons_json(v7_sample)
    jsonschema.validate(steps_v7, proto_schema)
    t7, e7 = rehearse(steps_v7, spec=spec_v7)
    ok7, viol7 = verify(t7, e7)
    assert ok7 is True, viol7
    # All 8 rows of column 1 filled with 50 uL
    for row in "ABCDEFGH":
        assert e7["plate"][f"plate:{row}1"] == pytest.approx(50.0)
    assert e7["deck"]["plate:A1"]["sealed"] is True

    # 3. Opentrons/Protocols protoBuilds static AST extraction
    protobuild_doc = {
        "content": (
            "def run(ctx):\n"
            "    tips = ctx.load_labware('opentrons_96_tiprack_300ul', '1')\n"
            "    res = ctx.load_labware('nest_12_reservoir_15ml', '2')\n"
            "    plate = ctx.load_labware('corning_96_wellplate_360ul_flat', '3')\n"
            "    p300 = ctx.load_instrument('p300_single_gen2', 'right', tip_racks=[tips])\n"
            "    p300.transfer(120, res['A1'], plate['A1'])\n"
            "    p300.transfer(80, res['A1'], plate['B1'])\n"
        )
    }
    pb_steps, pb_spec = convert_open_protocol(protobuild_doc, fmt="auto")
    jsonschema.validate(pb_steps, proto_schema)
    tpb, epb = rehearse(pb_steps, spec=pb_spec)
    ok_pb, viol_pb = verify(tpb, epb)
    assert ok_pb is True, viol_pb
    assert epb["plate"] == {"plate:A1": 120.0, "plate:B1": 80.0}

    # 4. Negative control: Opentrons JSON missing dropTip caught by all_tips_dropped
    bad_v6 = dict(v6_sample)
    bad_v6["commands"] = list(v6_sample["commands"][:-1])
    bad_steps, bad_spec = from_opentrons_json(bad_v6)
    t_bad, e_bad = rehearse(bad_steps, spec=bad_spec)
    ok_bad, v_bad = verify(t_bad, e_bad)
    assert ok_bad is False
    assert "all_tips_dropped" in _rules(v_bad)


def test_bioplanner_bioprot_pseudocode_and_wlp_structured_steps() -> None:
    proto_schema = json.loads((REPO_ROOT / "schema" / "protocol.schema.json").read_text(encoding="utf-8"))

    # 1. BioProt 10051 two-stage eDNA PCR pseudocode
    bp_sample = fetch_open_protocols.CURATED_OFFLINE_SAMPLES[2]["payload"]
    steps_bp, spec_bp = from_bioplanner(bp_sample)
    jsonschema.validate(steps_bp, proto_schema)
    t_bp, e_bp = rehearse(steps_bp, spec=spec_bp)
    ok_bp, v_bp = verify(t_bp, e_bp)
    assert ok_bp is True, v_bp
    assert e_bp["plate"]["plate:A1"] == pytest.approx(20.0)
    assert e_bp["plate"]["plate:B1"] == pytest.approx(25.0)
    assert e_bp["deck"]["plate:A1"]["sealed"] is True
    assert e_bp["deck"]["plate:B1"]["sealed"] is True

    # 2. Structured WLP multi-step enzymatic assay (thaw -> add -> mix -> seal -> 90 C heat-inactivate)
    wlp_valid = [
        {"action": "thaw", "loc": "res:2"},
        {"action": "add", "reagent": "assay_buffer", "volume": "100 uL", "destination": "A1"},
        {"action": "transfer", "source": "res:2", "volume": "50 uL", "destination": "A1", "mix_after": True, "reps": 4},
        {"action": "seal", "target": "A1"},
        {"action": "incubate", "target": "A1", "temp_c": 90.0, "duration": "3 min"},
    ]
    steps_wlp, spec_wlp = convert_open_protocol(wlp_valid, fmt="auto")
    jsonschema.validate(steps_wlp, proto_schema)
    t_wlp, e_wlp = rehearse(steps_wlp, spec=spec_wlp)
    ok_wlp, v_wlp = verify(t_wlp, e_wlp)
    assert ok_wlp is True, v_wlp
    assert e_wlp["plate"]["plate:A1"] == pytest.approx(150.0)

    # 3. Negative control: WLP unsealed 95 C denaturation & pipetting frozen stock before thaw
    wlp_broken = [
        {"action": "transfer", "source": "res:2", "frozen": True, "volume": "50 uL", "destination": "A1"},
        {"action": "incubate", "target": "A1", "temp_c": 95.0, "duration": "2 min"},
    ]
    steps_brk, spec_brk = from_bioplanner(wlp_broken)
    t_brk, e_brk = rehearse(steps_brk, spec=spec_brk)
    ok_brk, v_brk = verify(t_brk, e_brk)
    assert ok_brk is False
    assert "thawed_before_pipetting" in _rules(v_brk)
    assert "high_temp_requires_seal" in _rules(v_brk)


def test_finebio_atomic_operation_traces_single_and_8channel() -> None:
    proto_schema = json.loads((REPO_ROOT / "schema" / "protocol.schema.json").read_text(encoding="utf-8"))

    # 1. Clean FineBio P03_02_01 trace (buffer -> A1, tip change, sample -> A1 + 2x mix, tip eject)
    fb_sample = fetch_open_protocols.CURATED_OFFLINE_SAMPLES[3]["payload"]
    steps_fb, spec_fb = from_finebio_trace(fb_sample, video_id="P03_02_01")
    jsonschema.validate(steps_fb, proto_schema)
    t_fb, e_fb = rehearse(steps_fb, spec=spec_fb)
    ok_fb, v_fb = verify(t_fb, e_fb)
    assert ok_fb is True, v_fb
    assert e_fb["plate"]["plate:A1"] == pytest.approx(200.0)
    assert e_fb["tips_used"] == 2

    # 2. 8-channel FineBio trace into 8_tube_stripes + PCR machine thermal step
    fb_8ch = [
        "take-8_channel_pipette",
        "insert-8_channel_pipette-8_channel_tip",
        "press-8_channel_pipette",
        "insert-8_channel_pipette-50ml_tube",
        "release-8_channel_pipette",
        "detach-8_channel_pipette-50ml_tube",
        "insert-8_channel_pipette-8_tube_stripes",
        "press-8_channel_pipette",
        "release-8_channel_pipette",
        "detach-8_channel_pipette-8_tube_stripes",
        "eject-8_channel_pipette-8_channel_tip",
        "put-8_tube_stripes-pcr_machine",
        "press-pcr_machine",
    ]
    steps_8ch, spec_8ch = convert_open_protocol(fb_8ch, fmt="auto")
    jsonschema.validate(steps_8ch, proto_schema)
    t8, e8 = rehearse(steps_8ch, spec=spec_8ch)
    ok8, v8 = verify(t8, e8)
    assert ok8 is True, v8
    for row in "ABCDEFGH":
        assert e8["plate"][f"plate:{row}1"] == pytest.approx(100.0)

    # 3. Negative control: FineBio participant reuses sample-contaminated tip in 50ml_tube buffer
    fb_dirty = [
        "insert-blue_pipette-blue_tip",
        "press-blue_pipette",
        "insert-blue_pipette-15ml_tube",
        "release-blue_pipette",
        "detach-blue_pipette-15ml_tube",
        "insert-blue_pipette-cell_culture_plate",
        "press-blue_pipette",
        "release-blue_pipette",
        "detach-blue_pipette-cell_culture_plate",
        # Missing eject-blue_pipette-blue_tip!
        "press-blue_pipette",
        "insert-blue_pipette-50ml_tube",
        "release-blue_pipette",
        "detach-blue_pipette-50ml_tube",
        "eject-blue_pipette-blue_tip",
    ]
    steps_dirty, spec_dirty = from_finebio_trace(fb_dirty)
    td, ed = rehearse(steps_dirty, spec=spec_dirty)
    ok_d, v_d = verify(td, ed)
    assert ok_d is False
    assert "no_tip_reuse" in _rules(v_d)


def test_bioprobench_ord_err_and_protocolqa_verdicts() -> None:
    proto_schema = json.loads((REPO_ROOT / "schema" / "protocol.schema.json").read_text(encoding="utf-8"))

    # 1. BioProBench ORD: correct_steps PASSES while wrong_steps FAILS enough_liquid
    ord_sample = fetch_open_protocols.CURATED_OFFLINE_SAMPLES[4]["payload"]
    c_steps, c_spec = from_bioprobench(ord_sample, variant="correct")
    jsonschema.validate(c_steps, proto_schema)
    tc, ec = rehearse(c_steps, spec=c_spec)
    ok_c, vc = verify(tc, ec)
    assert ok_c is True, vc
    assert ec["plate"]["plate:A1"] == pytest.approx(200.0)

    w_steps, w_spec = from_bioprobench(ord_sample, variant="wrong")
    tw, ew = rehearse(w_steps, spec=w_spec)
    ok_w, vw = verify(tw, ew)
    assert ok_w is False
    assert "enough_liquid" in _rules(vw)

    # 2. BioProBench ERR: corrected_text PASSES while corrupted_text (420 uL > 300 uL) FAILS no_overfill
    err_record = {
        "id": "TEST-ERR-96WELL-OVERFILL",
        "context": {"prior_step": "Wash assay wells with 100 uL PBS buffer."},
        "corrupted_text": "Add 250 uL of stop solution to each 96-well microplate well.",
        "corrected_text": "Add 100 uL of stop solution to each 96-well microplate well.",
        "is_correct": False,
        "type": "parameter",
    }
    ok_err_res = rehearse_open_protocol(
        err_record, dataset_id="bioprobench", fmt="bioprobench", variant="corrected"
    )
    assert ok_err_res["policy_approved"] is True
    assert ok_err_res["approved"] is True

    bad_err_res = rehearse_open_protocol(
        err_record, dataset_id="bioprobench", fmt="bioprobench", variant="corrupted"
    )
    assert bad_err_res["policy_approved"] is True
    assert bad_err_res["approved"] is False
    assert "no_overfill" in _rules(bad_err_res["violations"])

    # 3. LAB-Bench ProtocolQA: blocked when purpose="training" due to eval_only=True
    pqa_sample = fetch_open_protocols.CURATED_OFFLINE_SAMPLES[5]["payload"]
    blocked = rehearse_open_protocol(pqa_sample, dataset_id="lab-bench", purpose="training")
    assert blocked["policy_approved"] is False
    assert blocked["approved"] is False


def test_offline_fetch_open_protocols_session_and_cli_integration(tmp_path: Path) -> None:
    out_dir = tmp_path / "converted"
    results = fetch_open_protocols.fetch_and_rehearse_datasets(
        online=False,
        purpose="rehearsal_eval",
        out_dir=out_dir,
    )
    assert len(results) == 6
    assert all(r["policy_approved"] is True for r in results)
    assert all(r["approved"] is True for r in results)
    assert all(r["schema_errors"] == [] for r in results)
    assert (out_dir / "manifest.json").exists()

    # Rehearse one raw Opentrons JSON file directly through lab-rehearsal CLI with auto format
    ot_raw_file = tmp_path / "sample_ot_v6.json"
    ot_raw_file.write_text(
        json.dumps(fetch_open_protocols.CURATED_OFFLINE_SAMPLES[0]["payload"], indent=2),
        encoding="utf-8",
    )
    mem_path = tmp_path / "runs.jsonl"
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "demo" / "rehearse.py"),
            str(ot_raw_file),
            "--memory",
            str(mem_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    parsed = json.loads(proc.stdout)
    assert len(parsed) == 1
    assert parsed[0]["approved"] is True

    # Verify episodic recall on RehearsalSession across open-dataset dry-runs
    sess = RehearsalSession(memory_path=tmp_path / "session_runs.jsonl")
    r1 = rehearse_open_protocol(
        fetch_open_protocols.CURATED_OFFLINE_SAMPLES[2]["payload"],
        dataset_id="bioplanner",
        session=sess,
        protocol_name="bioprot_10051.json",
    )
    assert r1["approved"] is True
    assert r1["run_id"] == 1
    sess.close()


def test_zero_cjk_characters_across_repository() -> None:
    text_exts = {".py", ".md", ".json", ".toml", ".yml", ".yaml", ".txt", ".jsonl", ".lean"}
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
            continue
        if path.suffix.lower() in text_exts:
            text = path.read_text(encoding="utf-8")
            match = CJK_RE.search(text)
            assert match is None, f"found CJK character {match.group(0)!r} in {path}"
