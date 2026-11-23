# lab-rehearsal

Before a robot touches a real plate, *rehearse it*: recall past runs, dry-run in a digital twin, verify, run, and write the result back.

> **Auto-Lab Ecosystem**: Layer `L4 · Digital-Twin Rehearsal & Default-Deny Verification` in [auto-lab](https://github.com/yaniguan/auto-lab) · [Project Studio](https://yaniguan.github.io/auto-lab/)

## Where it fits

In the [Auto-Lab](https://github.com/yaniguan/auto-lab) system ([yaniguan.github.io/auto-lab](https://yaniguan.github.io/auto-lab/)), the plan is to simulate each step before running it. The loop is: **recall** what happened before, **rehearse** in a digital twin, **verify** with a default-deny gate, **run**, and **write back** the outcome so the next plan can learn from it. This repo collects lab simulators and benchmarks, and starts with a minimal loop for liquid handling.

## System Profile: Object · Data · Model · Training · History · Unique Properties

Full architectural documentation, state-machine invariants, commit history, and sibling-repository contracts are maintained in [`docs/ARCHITECTURE_AND_HISTORY.md`](docs/ARCHITECTURE_AND_HISTORY.md):

- **Object**: Pre-execution digital-twin rehearsal and default-deny safety gate (`L4`) sitting between `L5` protocol planners/compilers (`lab-decide`, `labshift`, `stepbot`) and `L1`/`L2` hardware drivers (`lab-hands`, Opentrons, PyLabRobot).
- **Data**: 5 deterministic JSON liquid-handling protocols (`1` valid 43-step 8-well serial dilution, `4` failure fixtures), a 10-run two-pass episodic ledger (`demo/memory/runs.jsonl`), reference output (`demo/output.txt`), and a zero-copy catalog of 4 embodied lab datasets and 10 open-source simulators.
- **Model**: Deterministic discrete-event deck state machine (`96`-well plate, `12`-column reservoir, `96`-tip rack, $20\text{–}300\,\mu\text{L}$ pipette) coupled with 7 default-deny verification checks (`known_step`, `volume_in_range`, `no_overfill`, `enough_liquid`, `tip_present`, `no_tip_reuse`, `all_tips_dropped`). No neural network is trained in this repo.
- **Training**: Zero parametric training; cross-run adaptation operates strictly via non-parametric SHA-256 content-addressed episodic write-back to `demo/memory/runs.jsonl`.
- **History**: Initialized in `8219695` (`2026-11-18`) with the 5-stage rehearsal loop, protocol test fixtures, and curated robotics/simulation references.
- **Unique Properties**: Non-short-circuiting trajectory simulation that surfaces all cascading step violations in one pass, strict default-deny execution blocking, zero external Python dependencies, and conceptual alignment with `YLM/lean` (`LabLean`), `stepbot`, and `twinstep`.

## Repository Layout

```text
lab-rehearsal/
├── .github/workflows/
│   └── demo.yml                          # CI workflow running demo/rehearse.py on Python 3.12
├── demo/
│   ├── memory/
│   │   └── runs.jsonl                    # Append-only 2-pass episodic run history (10 runs)
│   ├── protocols/
│   │   ├── broken_aspirate_empty.json    # Empty-reservoir aspiration & dispense fixture
│   │   ├── broken_out_of_range.json      # Sub-minimum pipette volume (10 uL < 20 uL) fixture
│   │   ├── broken_overfill.json          # Well capacity overflow (400 uL > 300 uL) fixture
│   │   ├── broken_tip_reuse.json         # Cross-reagent tip contamination fixture
│   │   └── good_serial_dilution.json     # Valid 43-step 8-well 1:2 serial dilution protocol
│   ├── output.txt                        # Reference stdout capture from 2-pass demo
│   └── rehearse.py                       # Recall -> Rehearse -> Verify -> Run -> Write-Back loop
├── docs/
│   └── ARCHITECTURE_AND_HISTORY.md       # Deep technical profile (Object, Data, Model, History)
├── LICENSE                               # MIT License
├── README.md                             # Overview, demo output, and curated simulation catalog
└── requirements.txt                      # Python standard library only
```

## First attempt

A virtual deck (96-well plate, 12-column reservoir, 96-tip rack, 20-300 uL single-channel pipette). Five JSON protocols: one good 8-well serial dilution and four broken ones (overfill, out-of-range volume, aspirate from empty, tip reuse). Each goes through recall, rehearse, verify, run (mock) and write back to `demo/memory/runs.jsonl`. All five run twice so the second pass shows recall warnings.

```bash
python demo/rehearse.py
```

Output (Linux, CPU only, 2026-11-18):

```
######## pass 1 ########
== broken_aspirate_empty.json (hash 43a2527121f7, 4 steps)
  RECALL:   no earlier runs of this protocol
  REHEARSE: tips used 1, plate wells filled 0, tip on at end False
  VERIFY:   FAIL (2 violation(s))
    - step 2 enough_liquid: aspirate 100 uL from res:3 holding 0 uL
    - step 3 enough_liquid: dispense 100 uL but pipette holds 0 uL
  RUN:      blocked by verify gate, nothing sent to the robot
  WRITE:    run #1 appended to memory/runs.jsonl
== broken_out_of_range.json (hash 2e03525111f3, 4 steps)
  RECALL:   no earlier runs of this protocol
  REHEARSE: tips used 1, plate wells filled 1, tip on at end False
  VERIFY:   FAIL (2 violation(s))
    - step 2 volume_in_range: 10 uL outside pipette range 20-300 uL
    - step 3 volume_in_range: 10 uL outside pipette range 20-300 uL
  RUN:      blocked by verify gate, nothing sent to the robot
  WRITE:    run #2 appended to memory/runs.jsonl
== broken_overfill.json (hash 675b98ac918f, 6 steps)
  RECALL:   no earlier runs of this protocol
  REHEARSE: tips used 1, plate wells filled 1, tip on at end False
  VERIFY:   FAIL (1 violation(s))
    - step 5 no_overfill: plate:A1 would hold 400 uL > 300 uL
  RUN:      blocked by verify gate, nothing sent to the robot
  WRITE:    run #3 appended to memory/runs.jsonl
== broken_tip_reuse.json (hash 2c4dbb514e7f, 6 steps)
  RECALL:   no earlier runs of this protocol
  REHEARSE: tips used 1, plate wells filled 2, tip on at end False
  VERIFY:   FAIL (1 violation(s))
    - step 4 no_tip_reuse: tip carrying ['sample'] reused in res:1 containing ['buffer']
  RUN:      blocked by verify gate, nothing sent to the robot
  WRITE:    run #4 appended to memory/runs.jsonl
== good_serial_dilution.json (hash e509e4ce8c28, 43 steps)
  RECALL:   no earlier runs of this protocol
  REHEARSE: tips used 2, plate wells filled 8, tip on at end False
  VERIFY:   PASS (0 violation(s))
  RUN:      sent 43 commands aspirate x16, dispense x16, drop_tip x2, mix x7, pick_up_tip x2
            final plate: A1=100, B1=100, C1=100, D1=100, E1=100, F1=100, G1=100, H1=100
  WRITE:    run #5 appended to memory/runs.jsonl
######## pass 2 ########
== broken_aspirate_empty.json (hash 43a2527121f7, 4 steps)
  RECALL:   WARNING: failed before in run(s) [1] (enough_liquid)
  REHEARSE: tips used 1, plate wells filled 0, tip on at end False
  VERIFY:   FAIL (2 violation(s))
    - step 2 enough_liquid: aspirate 100 uL from res:3 holding 0 uL
    - step 3 enough_liquid: dispense 100 uL but pipette holds 0 uL
  RUN:      blocked by verify gate, nothing sent to the robot
  WRITE:    run #6 appended to memory/runs.jsonl
== broken_out_of_range.json (hash 2e03525111f3, 4 steps)
  RECALL:   WARNING: failed before in run(s) [2] (volume_in_range)
  REHEARSE: tips used 1, plate wells filled 1, tip on at end False
  VERIFY:   FAIL (2 violation(s))
    - step 2 volume_in_range: 10 uL outside pipette range 20-300 uL
    - step 3 volume_in_range: 10 uL outside pipette range 20-300 uL
  RUN:      blocked by verify gate, nothing sent to the robot
  WRITE:    run #7 appended to memory/runs.jsonl
== broken_overfill.json (hash 675b98ac918f, 6 steps)
  RECALL:   WARNING: failed before in run(s) [3] (no_overfill)
  REHEARSE: tips used 1, plate wells filled 1, tip on at end False
  VERIFY:   FAIL (1 violation(s))
    - step 5 no_overfill: plate:A1 would hold 400 uL > 300 uL
  RUN:      blocked by verify gate, nothing sent to the robot
  WRITE:    run #8 appended to memory/runs.jsonl
== broken_tip_reuse.json (hash 2c4dbb514e7f, 6 steps)
  RECALL:   WARNING: failed before in run(s) [4] (no_tip_reuse)
  REHEARSE: tips used 1, plate wells filled 2, tip on at end False
  VERIFY:   FAIL (1 violation(s))
    - step 4 no_tip_reuse: tip carrying ['sample'] reused in res:1 containing ['buffer']
  RUN:      blocked by verify gate, nothing sent to the robot
  WRITE:    run #9 appended to memory/runs.jsonl
== good_serial_dilution.json (hash e509e4ce8c28, 43 steps)
  RECALL:   passed before in run(s) [5]
  REHEARSE: tips used 2, plate wells filled 8, tip on at end False
  VERIFY:   PASS (0 violation(s))
  RUN:      sent 43 commands aspirate x16, dispense x16, drop_tip x2, mix x7, pick_up_tip x2
            final plate: A1=100, B1=100, C1=100, D1=100, E1=100, F1=100, G1=100, H1=100
  WRITE:    run #10 appended to memory/runs.jsonl
######## memory: 10 runs, 2 passed, 8 failed
```

Only the good protocol reaches the mock executor; each broken one is stopped by the rule it breaks, and on the second pass recall flags it before rehearsal. This is bookkeeping, not physics. Next: replay the same steps in a physics simulator (MuJoCo-based AutoBio or a lab scene) and add pipetting error models.

## Datasets

| Dataset | What | License |
|---|---|---|
| [LabUtopia](https://github.com/Rui-li023/LabUtopia) | Lab scenes and hierarchical tasks for embodied agents | MIT (code) |
| [AutoBio](https://github.com/autobio-bench/AutoBio) | Biology lab tasks in simulation (pipette, centrifuge tube, thermal cycler) | Datasets MIT; repo has no license file |
| [Chemistry3D](https://github.com/huangyan28/Chemistry3D) | Chemistry experiment interaction benchmark | No license file - check before use |
| [Labimus](https://labimus.github.io/) | Humanoid dexterous manipulation in organic chemistry labs | See project site |

## Open-source repos

Stars as of 2026-11-18.

| Repo | What | Stars | License |
|---|---|---:|---|
| [Genesis-Embodied-AI/genesis-world](https://github.com/Genesis-Embodied-AI/genesis-world) | Simulation platform for general-purpose robotics & embodied AI learning. | 30,254 | Apache-2.0 |
| [huggingface/lerobot](https://github.com/huggingface/lerobot) | LeRobot: Making AI for Robotics more accessible with end-to-end learning | 28,954 | Apache-2.0 |
| [google-deepmind/mujoco](https://github.com/google-deepmind/mujoco) | Multi-Joint dynamics with Contact. A general purpose physics simulator. | 18,270 | Apache-2.0 |
| [isaac-sim/IsaacLab](https://github.com/isaac-sim/IsaacLab) | Unified framework for robot learning with multi-physics/renderer support | 8,675 | BSD-3-Clause (see repo) |
| [mani-skill/ManiSkill](https://github.com/mani-skill/ManiSkill) | Manipulation Skill Framework, an open source GPU parallelized robotics simulator and... | 3,524 | Apache-2.0 |
| [PyLabRobot/pylabrobot](https://github.com/PyLabRobot/pylabrobot) | interactive & hardware agnostic SDK for lab automation | 657 | MIT |
| [Opentrons/opentrons](https://github.com/Opentrons/opentrons) | Software for writing protocols and running them on the Opentrons Flex and Opentrons OT-2 | 537 | Apache-2.0 |
| [autobio-bench/AutoBio](https://github.com/autobio-bench/AutoBio) | Preliminary version of AutoBio (https://arxiv.org/abs/2505.14030) | 101 | no license file |
| [huangyan28/Chemistry3D](https://github.com/huangyan28/Chemistry3D) | Chemistry3D benchmark | 79 | no license file |
| [Rui-li023/LabUtopia](https://github.com/Rui-li023/LabUtopia) | LabUtopia simulation and benchmark | 62 | MIT |

## Papers

| Paper | Venue | Link |
|---|---|---|
| LabUtopia: High-Fidelity Simulation and Hierarchical Benchmark for Scientific Embodied Agents | NeurIPS 2025 D&B | [arXiv:2505.22634](https://arxiv.org/abs/2505.22634) |
| AutoBio: A Simulation and Benchmark for Robotic Automation in Digital Biology Laboratory | arXiv 2025 | [arXiv:2505.14030](https://arxiv.org/abs/2505.14030) |
| Chemistry3D: Robotic Interaction Benchmark for Chemistry Experiments | arXiv 2024 | [arXiv:2406.08160](https://arxiv.org/abs/2406.08160) |
| Orbit: A Unified Simulation Framework for Interactive Robot Learning Environments | RA-L 2023 | [arXiv:2301.04195](https://arxiv.org/abs/2301.04195) |
| ManiSkill3: GPU Parallelized Robotics Simulation and Rendering for Generalizable Embodied AI | arXiv 2024 | [arXiv:2410.00425](https://arxiv.org/abs/2410.00425) |
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
