# lab-rehearsal

Before a robot touches a real plate, *rehearse it*: recall past runs, dry-run in a digital twin, verify, run, and write the result back.

> **Auto-Lab Ecosystem**: Layer `L4 · Digital-Twin Rehearsal & Default-Deny Verification` in [auto-lab](https://github.com/yaniguan/auto-lab) · [Project Studio](https://yaniguan.github.io/auto-lab/)

## Where it fits

In the [Auto-Lab](https://github.com/yaniguan/auto-lab) system ([yaniguan.github.io/auto-lab](https://yaniguan.github.io/auto-lab/)), the plan is to simulate each step before running it. The loop is: **recall** what happened before, **rehearse** in a digital twin, **verify** with a default-deny gate, **run**, and **write back** the outcome so the next plan can learn from it. This repo collects lab simulators and benchmarks, and starts with a minimal loop for liquid handling.

## System Profile: Object · Data · Model · Training · History · Unique Properties

Full architectural documentation, state-machine invariants, commit history, and sibling-repository contracts are maintained in [`docs/ARCHITECTURE_AND_HISTORY.md`](docs/ARCHITECTURE_AND_HISTORY.md):

- **Object**: Pre-execution digital-twin rehearsal and default-deny safety gate (`L4`) sitting between `L5` protocol planners/compilers (`lab-decide`, `labshift`, `stepbot`) and `L1`/`L2` hardware drivers (`lab-hands`, Opentrons, PyLabRobot).
- **Data**: 6 deterministic JSON liquid-handling protocols (`1` valid 43-step 8-well serial dilution, `5` failure fixtures), a 12-run two-pass episodic ledger (`demo/memory/runs.jsonl`), reference output (`demo/output.txt`), and a zero-copy SPDX-governed catalog (`catalog/simulators.json`) of 14 open wet-lab protocol & embodied laboratory manipulation/simulation datasets (`2024`–`2026`, with `commercial_use_ok` and `eval_only` gates) and 18 open-source simulators/benchmarks.
- **Model**: Deterministic discrete-event deck state machine (`96`-well plate, `12`-column reservoir, `96`-tip rack, $20\text{–}300\,\mu\text{L}$ pipette) coupled with 7 default-deny verification checks (`known_step`, `volume_in_range`, `no_overfill`, `enough_liquid`, `tip_present`, `no_tip_reuse`, `all_tips_dropped`). No neural network is trained in this repo.
- **Training**: Zero parametric training; cross-run adaptation operates strictly via non-parametric SHA-256 content-addressed episodic write-back to `demo/memory/runs.jsonl`.
- **History**: Initialized in `8219695` (`2026-11-18`) with the 5-stage rehearsal loop, protocol test fixtures, and curated robotics/simulation references.
- **Unique Properties**: Non-short-circuiting trajectory simulation that surfaces all cascading step violations in one pass, strict default-deny execution blocking, zero external Python dependencies, and conceptual alignment with `YLM/lean` (`LabLean`), `stepbot`, and `twinstep`.

## 15-Repository Auto-Lab Wire Contract (`autolab-wire/v1`)

`autolab_wire.py` (`REPO_ID = "lab-rehearsal"`, `LAYER = "L4"`) connects `lab-rehearsal` into the deterministic 15-repository [Auto-Lab](https://github.com/yaniguan/auto-lab) packet pipeline (`lab-ears -> lab-ask-why -> lab-decide -> lab-automation-atlas -> buy-or-make -> labshift -> lab-rehearsal -> lab-verify -> stepbot -> lab-hands -> lab-feet -> lab-attrib -> twinstep -> labfill -> labbench-posttrain`):

- **Upstream (`L5`)**: Consumes scheduled assay packets from [`labshift`](https://github.com/yaniguan/labshift) together with `parsed_spans.volume_ul` (and optional `rehearsal_steps`).
- **Pre-Execution Twin Gate (`L4`)**: `process_packet(packet, *, memory_path=None, run_lean_check=None)` deep-copies the `autolab-wire/v1` packet, bypasses already-halted packets unchanged, rejects non-finite/non-positive/non-numeric `volume_ul` (`invalid_requested_volume`) and unrehearsable single-well volumes (`requested_volume_not_rehearsable`) without silent capping, executes `RehearsalSession` (defaulting to an isolated temporary directory without touching tracked `demo/memory/runs.jsonl`, or persisting across multi-packet campaigns when `memory_path` / `rehearsal_memory_path` is provided), models 1- and 8-channel pipettes plus open-well volatile evaporation (`incubate`, `seal`, `thaw`), re-rehearses compiled `protocol_yaml_dict` via `from_stepbot` (`compiled_protocol_mismatch` gate), halts on any safety violation (`no_tip_reuse`, `no_overfill`, `unknown_start_volume`, `enough_liquid`, `volume_in_range`, `tip_present`, `thawed_before_pipetting`, `unsealed_before_pipetting`, `high_temp_requires_seal`, `all_tips_dropped`) with `halt_reason = "L4_REHEARSAL_BLOCK"` and `protocol_yaml_dict = None`, and records `invariant = "lean_model_compatible"` in `bottom_reasoning_traces` (upgraded to `"checkRehearsal_preserves_capacity"` only when `run_lean_check=True` verifies the packet with the compiled Lean 4.36.0 `lab-rehearsal-check` binary).
- **Downstream (`L4` / `L1` / `L3`)**: Compiles approved multi-step trajectories (`transfer`, `distribute`, `mix`, `discard`, and `incubate`/`seal`/`thaw` delays/annotations) directly into `rehearsal.protocol_yaml_dict` (Opentrons Python API `2.16` specification) for [`lab-verify`](https://github.com/yaniguan/lab-verify) (`lean-mcp/v1` envelope minting) and [`stepbot`](https://github.com/yaniguan/stepbot) compilation, and feeds rehearsed step semantics downstream to [`twinstep`](https://github.com/yaniguan/twinstep) (`L3` causal ordering DAGs and seeded error twins).
- **Post-Run `L4` Partner**: Pairs with [`lab-attrib`](https://github.com/yaniguan/lab-attrib) (`L4` post-execution 4-way causal blame, NNLS precursor deconvolution, and SHA-256 certified ledger) so transient `EXECUTION_ARTIFACT` hardware faults permit bounded clean retries while permanent safety violations are fast-denied and sub-linear structural skeleton/shingle recall flags isomorphic prior failures across `runs.jsonl`.

## Repository Layout

```text
lab-rehearsal/
├── .github/workflows/
│   └── demo.yml                          # CI (Python 3.10-3.12 + Lean 4.36.0): pytest, Lean parity, demo diff, catalog
├── catalog/
│   └── simulators.json                   # SPDX & license-governance manifest for datasets and simulators
├── community_extensions/
│   ├── calm_twin_calibrator.py           # Context-Adaptive Digital Twin (CALM-DT) & Rusty-SOP gate
│   └── certified_negative_ledger.py      # 4-way causal attribution & hash-chained negative ledger
├── demo/
│   ├── memory/
│   │   └── runs.jsonl                    # Reference 2-pass structured episodic run history (12 runs)
│   ├── protocols/
│   │   ├── broken_aspirate_empty.json    # Empty-reservoir aspiration & dispense fixture
│   │   ├── broken_mix_contamination.json # Dirty-tip mix cross-contamination fixture
│   │   ├── broken_out_of_range.json      # Sub-minimum pipette volume (10 uL < 20 uL) fixture
│   │   ├── broken_overfill.json          # Well capacity overflow (400 uL > 300 uL) fixture
│   │   ├── broken_tip_reuse.json         # Cross-reagent tip contamination fixture
│   │   └── good_serial_dilution.json     # Valid 43-step 8-well 1:2 serial dilution protocol
│   ├── output.txt                        # Reference stdout capture from 2-pass demo
│   └── rehearse.py                       # CLI / showcase entrypoint wrapping lab_rehearsal
├── docs/
│   └── ARCHITECTURE_AND_HISTORY.md       # Deep technical profile (Object, Data, Model, History)
├── lean/
│   ├── LabRehearsalLean.lean             # Lean 4.36.0 capacity, conservation, rusty-SOP & attribution proofs
│   ├── Main.lean                         # Compiled CLI runner (`lab-rehearsal-check`) for `checkRehearsal`
│   └── lakefile.toml                     # Lake package manifest building library & CLI checker
├── schema/
│   ├── protocol.schema.json              # Strict JSON Schema for micro-step protocols
│   └── run_record.schema.json            # Strict JSON Schema for episodic runs.jsonl records
├── scripts/
│   ├── check_catalog.py                  # Automated SPDX license, eval_only & markdown URL validator
│   ├── fetch_open_protocols.py           # Offline/online open-protocol converter & rehearsal runner
│   └── fetch_opentrons_fixtures.py       # Pinned SHA-256 fetcher & evaluator for 19 Opentrons fixtures
├── src/lab_rehearsal/
│   ├── __init__.py                       # Public package API
│   ├── adapters.py                       # Bidirectional stepbot / twinstep / LabLean IR converters
│   ├── cli.py                            # CLI (--demo, --memory, --fast-deny, --monte-carlo, --json)
│   ├── core.py                           # Quantitative deck twin, default-deny gate, and MemoryStore
│   └── open_datasets.py                  # Opentrons JSON, BioPlanner, FineBio & BioProBench converters
├── tests/
│   ├── fixtures/twinstep_twins.jsonl     # Corrupted/clean twinstep benchmark fixture
│   ├── test_autolab_wire.py              # autolab-wire/v1 contract & L4_REHEARSAL_BLOCK unit tests
│   ├── test_community_extensions.py      # CALM-DT, Rusty-SOP, and negative-ledger benchmark tests
│   ├── test_open_datasets_2026.py        # 2024-2026 open dataset governance & dry-run unit tests
│   ├── test_phase2_audit_fixes.py        # Phase-2 multi-channel, evaporation & Lean parity tests
│   └── test_rehearse.py                  # Regression & unit test suite
├── autolab_wire.py                       # L4 Auto-Lab 15-repo wire node (process_packet)
├── LICENSE                               # MIT License
├── pyproject.toml                        # Package metadata and console script
├── README.md                             # Overview, demo output, and curated simulation catalog
└── requirements.txt                      # Python standard library core
```

## First attempt

A virtual deck (96-well plate with 5 uL dead volume, 12-column reservoir with 500 uL trough dead volume and designated waste sink `res:12`, addressable 96-tip rack `tiprack:A1..H12`, 20-300 uL single-channel pipette). Six JSON protocols: one good 8-well serial dilution and five broken ones (overfill, out-of-range volume, aspirate from empty, aspirate tip reuse, mix cross-contamination). Each goes through recall, rehearse, verify, run (mock) and write back (`--demo` uses an isolated temporary ledger so tracked `demo/memory/runs.jsonl` stays clean).

```bash
python demo/rehearse.py --demo
```

Output:

```
######## pass 1 ########
== broken_aspirate_empty.json (hash f0c0e69b50c4, 4 steps)
  RECALL:   no earlier runs of this protocol
  REHEARSE: tips used 1, plate wells filled 0, tip on at end False
  VERIFY:   FAIL (2 violation(s))
    - step 2 enough_liquid: aspirate 100 uL from res:3 holding 0 uL
    - step 3 enough_liquid: dispense 100 uL but pipette holds 0 uL
  RUN:      blocked by verify gate, nothing sent to the robot
  WRITE:    run #1 appended to memory/runs.jsonl
== broken_mix_contamination.json (hash 64f008f68f8f, 5 steps)
  RECALL:   no earlier runs of this protocol
  REHEARSE: tips used 1, plate wells filled 1, tip on at end False
  VERIFY:   FAIL (1 violation(s))
    - step 4 no_tip_reuse: tip carrying ['sample'] reused in res:1 containing ['buffer']
  RUN:      blocked by verify gate, nothing sent to the robot
  WRITE:    run #2 appended to memory/runs.jsonl
== broken_out_of_range.json (hash b17defa68f72, 4 steps)
  RECALL:   no earlier runs of this protocol
  REHEARSE: tips used 1, plate wells filled 1, tip on at end False
  VERIFY:   FAIL (2 violation(s))
    - step 2 volume_in_range: 10 uL outside pipette range 20-300 uL
    - step 3 volume_in_range: 10 uL outside pipette range 20-300 uL
  RUN:      blocked by verify gate, nothing sent to the robot
  WRITE:    run #3 appended to memory/runs.jsonl
== broken_overfill.json (hash 8c84d8ed7942, 6 steps)
  RECALL:   no earlier runs of this protocol
  REHEARSE: tips used 1, plate wells filled 1, tip on at end False
  VERIFY:   FAIL (1 violation(s))
    - step 5 no_overfill: plate:A1 would hold 400 uL > 300 uL
  RUN:      blocked by verify gate, nothing sent to the robot
  WRITE:    run #4 appended to memory/runs.jsonl
== broken_tip_reuse.json (hash f59a730bc07f, 6 steps)
  RECALL:   no earlier runs of this protocol
  REHEARSE: tips used 1, plate wells filled 2, tip on at end False
  VERIFY:   FAIL (1 violation(s))
    - step 4 no_tip_reuse: tip carrying ['sample'] reused in res:1 containing ['buffer']
  RUN:      blocked by verify gate, nothing sent to the robot
  WRITE:    run #5 appended to memory/runs.jsonl
== good_serial_dilution.json (hash ac7be679b3d9, 43 steps)
  RECALL:   no earlier runs of this protocol
  REHEARSE: tips used 2, plate wells filled 8, tip on at end False
  VERIFY:   PASS (0 violation(s))
  RUN:      sent 43 commands aspirate x16, dispense x16, drop_tip x2, mix x7, pick_up_tip x2
            final plate: A1=100, B1=100, C1=100, D1=100, E1=100, F1=100, G1=100, H1=100
  WRITE:    run #6 appended to memory/runs.jsonl
######## pass 2 ########
== broken_aspirate_empty.json (hash f0c0e69b50c4, 4 steps)
  RECALL:   WARNING: failed before in run(s) [1] (enough_liquid)
  REHEARSE: tips used 1, plate wells filled 0, tip on at end False
  VERIFY:   FAIL (2 violation(s))
    - step 2 enough_liquid: aspirate 100 uL from res:3 holding 0 uL
    - step 3 enough_liquid: dispense 100 uL but pipette holds 0 uL
  RUN:      blocked by verify gate, nothing sent to the robot
  WRITE:    run #7 appended to memory/runs.jsonl
== broken_mix_contamination.json (hash 64f008f68f8f, 5 steps)
  RECALL:   WARNING: failed before in run(s) [2] (no_tip_reuse)
  REHEARSE: tips used 1, plate wells filled 1, tip on at end False
  VERIFY:   FAIL (1 violation(s))
    - step 4 no_tip_reuse: tip carrying ['sample'] reused in res:1 containing ['buffer']
  RUN:      blocked by verify gate, nothing sent to the robot
  WRITE:    run #8 appended to memory/runs.jsonl
== broken_out_of_range.json (hash b17defa68f72, 4 steps)
  RECALL:   WARNING: failed before in run(s) [3] (volume_in_range)
  REHEARSE: tips used 1, plate wells filled 1, tip on at end False
  VERIFY:   FAIL (2 violation(s))
    - step 2 volume_in_range: 10 uL outside pipette range 20-300 uL
    - step 3 volume_in_range: 10 uL outside pipette range 20-300 uL
  RUN:      blocked by verify gate, nothing sent to the robot
  WRITE:    run #9 appended to memory/runs.jsonl
== broken_overfill.json (hash 8c84d8ed7942, 6 steps)
  RECALL:   WARNING: failed before in run(s) [4] (no_overfill)
  REHEARSE: tips used 1, plate wells filled 1, tip on at end False
  VERIFY:   FAIL (1 violation(s))
    - step 5 no_overfill: plate:A1 would hold 400 uL > 300 uL
  RUN:      blocked by verify gate, nothing sent to the robot
  WRITE:    run #10 appended to memory/runs.jsonl
== broken_tip_reuse.json (hash f59a730bc07f, 6 steps)
  RECALL:   WARNING: failed before in run(s) [5] (no_tip_reuse)
  REHEARSE: tips used 1, plate wells filled 2, tip on at end False
  VERIFY:   FAIL (1 violation(s))
    - step 4 no_tip_reuse: tip carrying ['sample'] reused in res:1 containing ['buffer']
  RUN:      blocked by verify gate, nothing sent to the robot
  WRITE:    run #11 appended to memory/runs.jsonl
== good_serial_dilution.json (hash ac7be679b3d9, 43 steps)
  RECALL:   passed before in run(s) [6]
  REHEARSE: tips used 2, plate wells filled 8, tip on at end False
  VERIFY:   PASS (0 violation(s))
  RUN:      sent 43 commands aspirate x16, dispense x16, drop_tip x2, mix x7, pick_up_tip x2
            final plate: A1=100, B1=100, C1=100, D1=100, E1=100, F1=100, G1=100, H1=100
  WRITE:    run #12 appended to memory/runs.jsonl
######## memory: 12 runs, 2 passed, 10 failed
```

Only the good protocol reaches the mock executor; each broken one is stopped by the rule it breaks, and on the second pass recall flags it before rehearsal. Optional seeded ISO 8655 pipetting error rollouts (`--monte-carlo N`) quantify overfill risk and concentration drift bounds, while `lab_rehearsal.adapters` provides bidirectional protocol IR conversion across `stepbot`, `twinstep`, and `YLM/lean` (`LabLean`).

## Open-Dataset Protocol Rehearsal (`2024`–`2026`)

`lab_rehearsal.open_datasets` and `scripts/fetch_open_protocols.py` enforce `catalog/simulators.json` SPDX, `commercial_use_ok`, `redistribution_ok`, and `eval_only` benchmark contamination guards before converting external open-source protocol representations into `lab-rehearsal` twin step lists (`schema/protocol.schema.json`) for 96-well dry-run verification:

- **Opentrons JSON (`v6`/`v7`/`v8`) & `protoBuilds`** (`from_opentrons_json`): Lowers `loadPipette`, `loadLabware`, `loadLiquid`, `pickUpTip`, `aspirate`, `dispense`, `blowout`, `thermocycler/closeLid`, `waitForDuration`, and `dropTip` commands onto distinct per-`labwareId` container and tip-rack namespaces (`plate`, `plate_2..`, `tiprack`, `tiprack_2..`, `res`, `res_2..`) with true `labwareDefinitions` geometry capacities and default-deny `unknown_start_volume` when `loadLiquid` is omitted (never inventing starting stock).
- **BioPlanner (`BioProt`) & WLP Structured Steps** (`from_bioplanner`): Statically parses EMNLP `BioProt` Pythonic reaction/thermocycling pseudocode (`ast.parse`, zero `exec`/`eval`) and structured wet-lab action dictionaries into multi-pipette (`P20`/`P300`) liquid transfers and sealed high-temperature incubations.
- **FineBio Atomic Manipulation Traces** (`from_finebio_trace`): Translates CVPR 2024 / IJCV 2025 `verb-manipulated-affected` plunger/vessel traces (`insert-*-tip`, `press`, `release`, `detach`, `eject-*-tip`) into single- and 8-channel aspirate, dispense, and multi-cycle mix operations while catching human tip-reuse contamination.
- **BioProBench (`ORD`/`ERR`/`GEN`) & `LAB-Bench` (`ProtocolQA`)** (`from_bioprobench`): Converts procedural step-ordering and error-correction records into twin trajectories where valid protocols pass and scrambled/corrupted variants fail physical invariants (`enough_liquid`, `no_overfill`, `thawed_before_pipetting`, `high_temp_requires_seal`).

```bash
python scripts/fetch_open_protocols.py
python scripts/fetch_open_protocols.py --online
python scripts/fetch_opentrons_fixtures.py
```

### Official Opentrons Protocol Fixtures (`84a9956efcca` — 19 Fixtures: `4 PASS`, `15 BLOCK`)

`scripts/fetch_opentrons_fixtures.py` downloads all **19** official JSON fixtures from [`Opentrons/opentrons`](https://github.com/Opentrons/opentrons/tree/84a9956efccae8783f1a49ec14873222956f3284/shared-data/protocol/fixtures) (`Apache-2.0`, pinned at commit `84a9956efccae8783f1a49ec14873222956f3284` with SHA-256 verification into gitignored `data/opentrons_fixtures/`). Every `labwareId` and tip rack receives its own isolated namespace (zero container or tip-rack merging errors; supplying `loadLiquid` to `6/multipleTipracks.json` approves all 52 steps across 97 tips on `tiprack` + `tiprack_2`):

| Fixture (`shared-data/protocol/fixtures/`) | Steps | Verdict | Rules Triggered | Reason |
|---|---:|---|---|---|
| `6/heaterShakerCommands.json` | 3 | `BLOCK` | `all_tips_dropped:1` | Transfers 5 uL declared `waterId` (`sourcePlateId -> destPlateId`) cleanly; omits final `dropTip` |
| `6/heaterShakerCommandsWithResultsKey.json` | 0 | `BLOCK` | `empty_protocol:1` | Deck/module setup fixture only (0 liquid-handling, tip, or thermal commands) |
| `6/multipleTempModules.json` | 44 | `BLOCK` | `unknown_start_volume:11` | 11 transfers across 5 distinct plates, 1 reservoir & 2 tip racks; omits `loadLiquid` |
| `6/multipleTipracks.json` | 52 | `BLOCK` | `unknown_start_volume:13` | 13 transfers across 2 independent 96-tip racks & 12-reservoir; omits `loadLiquid` |
| `6/multipleTipracksWithTC.json` | 53 | `BLOCK` | `unknown_start_volume:13` | Thermocycler lid seal + 13 transfers across 2 independent tip racks; omits `loadLiquid` |
| `6/oneTiprack.json` | 4 | `BLOCK` | `unknown_start_volume:1` | Single 5 uL transfer between distinct source/dest plates; omits `loadLiquid` |
| `6/simpleV6.json` | 4 | `PASS` | `none` | `p10_single` transfers 5 uL declared `waterId` (`sourcePlateId:A1 -> destPlateId:B1`) and drops tip |
| `6/tempAndMagModuleCommands.json` | 3 | `BLOCK` | `all_tips_dropped:1` | Transfers 5 uL declared `waterId` between distinct plates; omits final `dropTip` |
| `6/transferSettings.json` | 44 | `BLOCK` | `unknown_start_volume:11` | 11 transfers across 5 distinct plates, 1 reservoir & 2 tip racks; omits `loadLiquid` |
| `7/moveLabware.json` | 1 | `BLOCK` | `all_tips_dropped:1` | Picks up tip and moves labware without dropping tip at end |
| `7/simpleV7.json` | 4 | `BLOCK` | `schema_valid:1, tip_present:3, unknown_start_volume:1` | Multi-channel `p1000_96` targets non-row-A `B1` without single-nozzle config; `C1..H1` undeclared |
| `8/moveLabware8.json` | 1 | `BLOCK` | `all_tips_dropped:1` | Picks up tip and moves labware without dropping tip at end |
| `8/moveLabwareFlex8.json` | 1 | `BLOCK` | `all_tips_dropped:1` | Picks up tip on Flex deck and moves labware without dropping tip at end |
| `8/simpleFlexV10CommandsV8.json` | 4 | `BLOCK` | `schema_valid:1, tip_present:3, unknown_start_volume:1` | Multi-channel `p1000_96` targets non-row-A `B1` without single-nozzle config; `C1..H1` undeclared |
| `8/simpleFlexV8.json` | 4 | `BLOCK` | `schema_valid:1, tip_present:3, unknown_start_volume:1` | Multi-channel `p1000_96` targets non-row-A `B1` without single-nozzle config; `C1..H1` undeclared |
| `8/simpleFlexV9CommandsV8.json` | 4 | `BLOCK` | `schema_valid:1, tip_present:3, unknown_start_volume:1` | Multi-channel `p1000_96` targets non-row-A `B1` without single-nozzle config; `C1..H1` undeclared |
| `8/simpleV10CommandsV8.json` | 4 | `PASS` | `none` | `p10_single` transfers 5 uL declared `waterId` (`sourcePlateId:A1 -> destPlateId:B1`) and drops tip |
| `8/simpleV8.json` | 4 | `PASS` | `none` | `p10_single` transfers 5 uL declared `waterId` (`sourcePlateId:A1 -> destPlateId:B1`) and drops tip |
| `8/simpleV9CommandsV8.json` | 4 | `PASS` | `none` | `p10_single` transfers 5 uL declared `waterId` (`sourcePlateId:A1 -> destPlateId:B1`) and drops tip |

### `BioProBench` `ERR` Evaluation (`ERR_test.json` — 1,200 Real Test Records)

Evaluating the full ICML 2026 `BioProBench` `ERR_test.json` split (`1,200` records: `602` `is_correct=true`, `598` `is_correct=false`, `eval_only=true` into gitignored `data/bioprobench/`) through `from_bioprobench(..., variant="candidate")` + `rehearse` + `verify` under default single-P300 (`20–300 uL`) 96-well plate geometry shows the physical scope boundary between robotic deck rehearsal and general bench biology:

| Subset | Records | Twin `PASS` | Twin `BLOCK` | Dominant Rules / Notes |
|---|---:|---:|---:|---|
| Corrupted (`is_correct=false`, `>= 1` extracted step) | 342 | 266 | 76 | `volume_in_range:78`, `enough_liquid:41`, `no_overfill:32`, `high_temp_requires_seal:3`, `incubate_temp_duration_bounds:1` (`4 / 342` paired `corrupted -> BLOCK` & `corrected -> PASS` flips) |
| Clean (`is_correct=true`, `>= 1` extracted step) | 319 | 247 | 72 | `volume_in_range:98`, `enough_liquid:32`, `no_overfill:31` (micro-volumes `< 20 uL` or macro tube volumes `> 300 uL` outside single-P300 96-well bounds) |
| Non-liquid / non-convertible bench steps | 539 | — | — | Centrifugation RCF, CO2 %, molarity, microscopy, and gel steps without pipetting/thermal ops |

## Datasets

| Dataset | What | SPDX License | `commercial_use_ok` | `eval_only` |
|---|---|---|---|---|
| [Opentrons Shared Protocol Fixtures](https://github.com/Opentrons/opentrons/tree/edge/shared-data/protocol) | Official Opentrons JSON v6/v7/v8 liquid-handling and module protocol schemas and fixtures | `Apache-2.0` | `true` | `false` |
| [Opentrons Protocol Library](https://github.com/Opentrons/Protocols) | Public OT-2/Flex Python API v2 and `protoBuilds` JSON library (no top-level `LICENSE` file; default-deny redistribution) | `NOASSERTION` | `false` | `false` |
| [FineBio](https://github.com/aistairc/FineBio) | CVPR 2024 / IJCV 2025 fine-grained wet-lab manipulation dataset (32 participants, hierarchical atomic operations) | `LicenseRef-FineBio-NC` | `false` | `false` |
| [BioPlanner (BioProt)](https://github.com/bioplanner/bioplanner) | EMNLP biological protocol planning corpus mapping wet-lab protocols to structured Pythonic pseudocode | `MIT` | `true` | `true` |
| [BioProBench](https://huggingface.co/datasets/BioProBench/BioProBench) | ICML 2026 biological protocol benchmark (`PQA`, `ORD` step ordering, `ERR` error correction, `GEN` generation) | `CC-BY-NC-4.0` | `false` | `true` |
| [LAB-Bench (ProtocolQA)](https://huggingface.co/datasets/futurehouse/lab-bench) | FutureHouse wet-lab reasoning and `ProtocolQA` troubleshooting benchmark (carries canary string) | `CC-BY-SA-4.0` | `true` | `true` |
| [SciKnowEval](https://huggingface.co/datasets/hicai-zju/SciKnowEval) | Multi-level chemistry and biology experimental procedure and wet-lab safety benchmark | `MIT` | `true` | `true` |
| [EmbodiedLab](https://github.com/HexaFuture/EmbodiedLab) | 35 physics-based simulated laboratory experiments benchmarking embodied act-to-know agents | `Apache-2.0` | `true` | `true` |
| [MATTERIX](https://github.com/AccelerationConsortium/Matterix) | Multiscale chemistry laboratory digital twin for robotic liquid/powder manipulation and reaction kinetics | `BSD-3-Clause` | `true` | `false` |
| [WETOP-Bench](https://huggingface.co/datasets/InternRobotics/WETOP-Bench-Validation) | Simulated wet-lab vessel manipulation and liquid-pouring validation benchmark | `MIT` | `true` | `true` |
| [LabUtopia](https://github.com/Rui-li023/LabUtopia) | Lab scenes and hierarchical tasks for embodied agents | `MIT` (code) | `true` | `false` |
| [AutoBio](https://github.com/autobio-bench/AutoBio) | Biology lab tasks in simulation (pipette, centrifuge tube, thermal cycler) | `NOASSERTION` (repo lacks `LICENSE`) | `false` | `true` |
| [Chemistry3D](https://github.com/huangyan28/Chemistry3D) | Chemistry experiment interaction benchmark | `NOASSERTION` (repo lacks `LICENSE`) | `false` | `true` |
| [Labimus](https://labimus.github.io/) | Humanoid dexterous manipulation in organic chemistry labs | `NOASSERTION` (project site) | `false` | `true` |

## Open-source repos

Stars as of 2026-11-25.

| Repo | What | Stars | License |
|---|---|---:|---|
| [Genesis-Embodied-AI/genesis-world](https://github.com/Genesis-Embodied-AI/genesis-world) | Simulation platform for general-purpose robotics & embodied AI learning. | 30,254 | Apache-2.0 |
| [huggingface/lerobot](https://github.com/huggingface/lerobot) | LeRobot: Making AI for Robotics more accessible with end-to-end learning | 28,954 | Apache-2.0 |
| [google-deepmind/mujoco](https://github.com/google-deepmind/mujoco) | Multi-Joint dynamics with Contact. A general purpose physics simulator. | 18,270 | Apache-2.0 |
| [isaac-sim/IsaacLab](https://github.com/isaac-sim/IsaacLab) | Unified framework for robot learning with multi-physics/renderer support | 8,675 | BSD-3-Clause (see repo) |
| [mani-skill/ManiSkill](https://github.com/mani-skill/ManiSkill) | Manipulation Skill Framework, an open source GPU parallelized robotics simulator and... | 3,524 | Apache-2.0 |
| [PyLabRobot/pylabrobot](https://github.com/PyLabRobot/pylabrobot) | interactive & hardware agnostic SDK for lab automation | 657 | MIT |
| [Opentrons/opentrons](https://github.com/Opentrons/opentrons) | Software for writing protocols and running them on the Opentrons Flex and Opentrons OT-2 | 537 | Apache-2.0 |
| [Future-House/LAB-Bench](https://github.com/Future-House/LAB-Bench) | Evaluation benchmark for AI scientific research capabilities in biology (`ProtocolQA`) | 136 | CC-BY-SA-4.0 |
| [autobio-bench/AutoBio](https://github.com/autobio-bench/AutoBio) | Preliminary version of AutoBio (https://arxiv.org/abs/2505.14030) | 101 | no license file |
| [huangyan28/Chemistry3D](https://github.com/huangyan28/Chemistry3D) | Chemistry3D benchmark | 79 | no license file |
| [YuyangSunshine/bioprobench](https://github.com/YuyangSunshine/bioprobench) | BioProBench biological protocol understanding and reasoning benchmark (ICML 2026) | 73 | CC-BY-NC-4.0 |
| [AccelerationConsortium/Matterix](https://github.com/AccelerationConsortium/Matterix) | Digital Twin for Robotics-Assisted Chemistry Lab Automation | 68 | BSD-3-Clause |
| [Opentrons/Protocols](https://github.com/Opentrons/Protocols) | Public Opentrons Protocol Library (`protocols/` and `protoBuilds/`) | 65 | no license file |
| [Rui-li023/LabUtopia](https://github.com/Rui-li023/LabUtopia) | LabUtopia simulation and benchmark | 62 | MIT |
| [aistairc/FineBio](https://github.com/aistairc/FineBio) | Fine-grained video and atomic manipulation benchmark of biological experiments | 32 | MIT (code) |
| [HICAI-ZJU/SciKnowEval](https://github.com/HICAI-ZJU/SciKnowEval) | Evaluating multi-level scientific knowledge and procedural safety of LLMs | 32 | MIT |
| [bioplanner/bioplanner](https://github.com/bioplanner/bioplanner) | Automatic evaluation of LLMs on protocol planning in biology (`BioProt`) | 30 | MIT |
| [HexaFuture/EmbodiedLab](https://github.com/HexaFuture/EmbodiedLab) | Physics-based laboratory benchmark testing embodied agents that act to know | 24 | Apache-2.0 |

## Papers

| Paper | Venue | Link |
|---|---|---|
| EmbodiedLab: Beyond Manipulation, Benchmarking Embodied Agents That Act to Know | arXiv 2026 | [arXiv:2610.35385](https://arxiv.org/abs/2610.35385) |
| BioProBench: Comprehensive Dataset and Benchmark in Biological Protocol Understanding and Reasoning | ICML 2026 | [arXiv:2505.07889](https://arxiv.org/abs/2505.07889) |
| LabUtopia: High-Fidelity Simulation and Hierarchical Benchmark for Scientific Embodied Agents | NeurIPS 2025 D&B | [arXiv:2505.22634](https://arxiv.org/abs/2505.22634) |
| AutoBio: A Simulation and Benchmark for Robotic Automation in Digital Biology Laboratory | arXiv 2025 | [arXiv:2505.14030](https://arxiv.org/abs/2505.14030) |
| LAB-Bench: Measuring Capabilities of Language Models for Biology Research | arXiv 2024 | [arXiv:2407.10362](https://arxiv.org/abs/2407.10362) |
| SciKnowEval: Evaluating Multi-level Scientific Knowledge of Large Language Models | arXiv 2024 | [arXiv:2406.09098](https://arxiv.org/abs/2406.09098) |
| Chemistry3D: Robotic Interaction Benchmark for Chemistry Experiments | arXiv 2024 | [arXiv:2406.08160](https://arxiv.org/abs/2406.08160) |
| FineBio: A Fine-Grained Video Dataset of Biological Experiments with Hierarchical Annotation | CVPR 2024 / IJCV 2025 | [arXiv:2402.00293](https://arxiv.org/abs/2402.00293) |
| ManiSkill3: GPU Parallelized Robotics Simulation and Rendering for Generalizable Embodied AI | arXiv 2024 | [arXiv:2410.00425](https://arxiv.org/abs/2410.00425) |
| BioPlanner: Automatic Evaluation of LLMs on Protocol Planning in Biology | EMNLP 2023 | [arXiv:2310.10632](https://arxiv.org/abs/2310.10632) |
| Orbit: A Unified Simulation Framework for Interactive Robot Learning Environments | RA-L 2023 | [arXiv:2301.04195](https://arxiv.org/abs/2301.04195) |
| Domain Randomization for Transferring Deep Neural Networks from Simulation to the Real World | IROS 2017 | [arXiv:1703.06907](https://arxiv.org/abs/1703.06907) |

## Conferences and workshops

- [CoRL - Conference on Robot Learning](https://www.corl.org/)
- [RSS - Robotics: Science and Systems](https://roboticsconference.org/)
- [ICRA - IEEE Intl. Conference on Robotics and Automation](https://www.ieee-ras.org/conferences-workshops/fully-sponsored/icra)
- [NeurIPS (main track, Datasets and Benchmarks, workshops)](https://neurips.cc/)
- [SLAS - Society for Laboratory Automation and Screening](https://www.slas.org/)

## Progress

Recent results, newest first. Date = first arXiv version; each line is a short summary of the abstract.

| Date | Paper | In one line |
|---|---|---|
| 2026-10-16 | [Self-Building Labs](https://arxiv.org/abs/2610.23826) | Self-Building Labs: labs that adapt their own setup to new experiments, not only run fixed procedures. |
| 2026-06-30 | [Labimus: A Simulation and Benchmark for Humanoid Dexterous Manipulation in Chemical Laboratory](https://arxiv.org/abs/2606.31037) | Labimus: 30+ real-to-sim assets from organic chemistry workstations for humanoid manipulation. |
| 2026-01-19 | [MATTERIX: toward a digital twin for robotics-assisted chemistry laboratory automation](https://arxiv.org/abs/2601.13232) | MATTERIX: a multiscale digital twin of chemistry labs: robot manipulation, powders and liquids, heat transfer, simple kinetics. |
| 2026-01-08 | [PRISM: Protocol Refinement through Intelligent Simulation Modeling](https://arxiv.org/abs/2601.05356) | PRISM: language-model agents design, validate in simulation and run protocols on off-the-shelf instruments. |

## Contributing

Found a broken link or a missing dataset, repo or paper? Open an issue or a pull request.

## License

Code in this repo: MIT. Listed datasets, repos and papers keep their own licenses.
