# Architecture and History — `lab-rehearsal`

> **Auto-Lab Ecosystem Role**: Layer `L4 · Digital-Twin Rehearsal & Default-Deny Verification` in [Robot AI Auto-Lab (`auto-lab`)](https://github.com/yaniguan/auto-lab) ([Project Studio](https://yaniguan.github.io/auto-lab/)).

---

## 1. Object

### Problem Statement
Autonomous laboratory agents frequently generate plausible-looking liquid-handling procedures that violate physical, volumetric, or contamination constraints—such as aspirating from empty reservoirs, commanding volumes outside a pipette's calibrated operating range, overfilling microplate wells, reusing contaminated tips across distinct stock reagents, or leaving a tip mounted at the end of a run. Sending unvalidated step lists directly to robotic hardware risks sample loss, reagent cross-contamination, and mechanical damage.

`lab-rehearsal` establishes the **pre-execution rehearsal loop** for benchtop automation: every candidate protocol must pass through episodic memory lookup, deterministic digital-twin state simulation, and a default-deny rule gate before any hardware command is emitted, and every outcome (pass or fail) is recorded back to persistent episodic memory.

### Role in the Auto-Lab `L0`–`L6` Hierarchy
Within the [Auto-Lab](https://github.com/yaniguan/auto-lab) Brain/Body architecture (`L0`–`L6`), `lab-rehearsal` sits in **`L4 · Digital-Twin Rehearsal & Default-Deny Verification`** alongside [`lab-verify`](https://github.com/yaniguan/lab-verify) and [`lab-attrib`](https://github.com/yaniguan/lab-attrib):
- **Upstream (`L5` / `L3`)**: Receives candidate protocol step sequences proposed by executive planners ([`lab-decide`](https://github.com/yaniguan/lab-decide), [`labshift`](https://github.com/yaniguan/labshift)) or protocol compilers/benchmarks ([`stepbot`](https://github.com/yaniguan/stepbot), [`twinstep`](https://github.com/yaniguan/twinstep)).
- **Peer (`L4`)**: Complements [`lab-verify`](https://github.com/yaniguan/lab-verify) (default-deny SQL and `lean-mcp/v1` capability gates), [`lab-attrib`](https://github.com/yaniguan/lab-attrib) (post-execution 4-way causal blame, NNLS precursor deconvolution, and SHA-256 certified ledger), and the Lean 4 `LabLean` formal protocol verifier in [`YLM`](https://github.com/yaniguan/YLM) (`lean/`).
- **Downstream (`L1` / `L2` Body & `L3` Post-Training)**: Acts as the safety interlock immediately upstream of [`lab-verify`](https://github.com/yaniguan/lab-verify), [`stepbot`](https://github.com/yaniguan/stepbot) (Opentrons Python API v2 `2.16`), [`lab-hands`](https://github.com/yaniguan/lab-hands), and [`twinstep`](https://github.com/yaniguan/twinstep).

### Inputs, Outputs, and Scope Boundaries
- **Inputs**:
  - Machine-readable JSON liquid-handling protocol (`list[dict]`), where each step specifies `"op"` (`"pick_up_tip"`, `"drop_tip"`, `"aspirate"`, `"dispense"`, `"mix"`), optional `"loc"` (`"plate:A1"`..`"plate:H12"` or `"res:1"`..`"res:12"`), and optional `"vol"` (in $\mu\text{L}$).
  - Episodic run history in `demo/memory/runs.jsonl`.
- **Outputs**:
  - `recall` diagnostic string (prior pass/fail history and violated rule names for identical protocol hashes).
  - `rehearse` execution trace (`list[dict]` of per-step state facts) and deck summary (`tip_on`, `tips_used`, non-empty `plate` well volumes).
  - `verify` gate decision `(approved: bool, violations: list[str])`.
  - `run` mock execution summary (command counts when approved; explicit block message when denied).
  - Appended JSONL episode record in `demo/memory/runs.jsonl`.
- **Scope Boundaries**:
  - The current implementation (`demo/rehearse.py`) performs **discrete-event volumetric and qualitative reagent-set bookkeeping**, not rigid-body contact dynamics, fluid mechanics, meniscus sensing, or stochastic pipetting error simulation. Rigid-body and multiphysics simulation benchmarks (`Genesis`, `MuJoCo`, `IsaacLab`, `ManiSkill`, `AutoBio`, `LabUtopia`, `Chemistry3D`, `Labimus`, `MATTERIX`) are cataloged in `README.md` as external ecosystem references.

---

## 2. Data

### Bundled Protocol Fixtures (`demo/protocols/`)
Five hand-authored deterministic JSON liquid-handling protocols test positive execution and four distinct failure modes on a standardized virtual deck (`96`-well plate capped at $300\,\mu\text{L}$/well, `12`-column reservoir capped at $15{,}000\,\mu\text{L}$/column initialized with `res:1 = 10,000 uL buffer` and `res:2 = 2,000 uL sample`, `96`-tip rack, and $20\text{–}300\,\mu\text{L}$ single-channel pipette):

| File | Steps | SHA-256 Prefix (`[:12]`) | Target Condition | Expected Verdict |
|---|---:|---|---|---|
| `broken_aspirate_empty.json` | 4 | `43a2527121f7` | Aspirates $100\,\mu\text{L}$ from empty `res:3`, then dispenses $100\,\mu\text{L}$ to `plate:A1` | `FAIL` (2 `enough_liquid` violations) |
| `broken_out_of_range.json` | 4 | `2e03525111f3` | Aspirates and dispenses $10\,\mu\text{L}$ ($< 20\,\mu\text{L}$ pipette minimum) | `FAIL` (2 `volume_in_range` violations) |
| `broken_overfill.json` | 6 | `675b98ac918f` | Dispenses $2 \times 200 = 400\,\mu\text{L}$ into `plate:A1` ($> 300\,\mu\text{L}$ well capacity) | `FAIL` (1 `no_overfill` violation) |
| `broken_tip_reuse.json` | 6 | `2c4dbb514e7f` | Aspirates `sample` (`res:2`), dispenses to `plate:A1`, then dips same tip into `buffer` (`res:1`) | `FAIL` (1 `no_tip_reuse` violation) |
| `good_serial_dilution.json` | 43 | `e509e4ce8c28` | Pre-fills `B1..H1` with $100\,\mu\text{L}$ `buffer`, switches tip, loads $200\,\mu\text{L}$ `sample` in `A1`, performs 1:2 serial transfers + mixes across `A1->H1`, discards $100\,\mu\text{L}$ from `H1` to `res:12`, drops tip | `PASS` (0 violations; `A1..H1 = 100 uL`) |

### Episodic Run Ledger (`demo/memory/runs.jsonl`) and Reference Output (`demo/output.txt`)
- `demo/memory/runs.jsonl` stores 10 JSONL records generated by running all 5 protocols across 2 consecutive passes (`2 passed`, `8 failed`). Each line conforms to:
  ```json
  {"run": 1, "protocol": "broken_aspirate_empty.json", "hash": "43a2527121f7", "passed": false, "violations": ["..."]}
  ```
- `demo/output.txt` records the reference 77-line console output from Linux CPU execution (`2026-11-18`).

### Curated External Datasets and License Posture
`README.md` indexes 4 external embodied-lab simulation benchmarks (`LabUtopia`, `AutoBio`, `Chemistry3D`, `Labimus`), 10 open-source robotics/lab-automation repositories (`genesis-world`, `lerobot`, `mujoco`, `IsaacLab`, `ManiSkill`, `pylabrobot`, `opentrons`, `AutoBio`, `Chemistry3D`, `LabUtopia`), and 10 papers (`2017`–`2026`). No external dataset files are copied into the repository (`zero-redistribution`); license gaps on upstream benchmarks (`AutoBio` and `Chemistry3D` lacking repository `LICENSE` files) are explicitly flagged in `README.md`.

---

## 3. Model

`lab-rehearsal` contains **no neural network or statistical model**; its core artifact is a deterministic discrete-event digital twin paired with a rule-based default-deny verification gate in `demo/rehearse.py`.

### Canonical Protocol Hashing & Episodic Recall
1. **Protocol Fingerprinting (`protocol_hash`)**: Serializes the JSON step list via `json.dumps(steps, sort_keys=True, separators=(",", ":"))` and takes the first 12 hexadecimal characters (48 bits) of its SHA-256 digest.
2. **Episodic Recall (`recall`)**: Scans `runs.jsonl` for prior runs matching `hash`. If any prior run failed, extracts the set of violated rule identifiers and emits `WARNING: failed before in run(s) [...] (...)`; if all prior runs passed, emits `passed before in run(s) [...]`.

### Discrete-Event Deck State Machine (`rehearse`)
Maintains the tuple `(deck, tip, tips_left, content, touched)`:
- `deck`: 96 plate wells (`vol = 0.0`, `max = 300`, `liquids = set()`) and 12 reservoir columns (`max = 15000`, pre-populated per `START`).
- `tip`: boolean attachment state; `tips_left`: integer counter initialized to `96`.
- `content`: current liquid volume ($\mu\text{L}$) inside the pipette tip.
- `touched`: cumulative `set[str]` of liquid labels contacted since the current tip was picked up.
- At each step $i \in \{1, \dots, N\}$, records pre/post transition facts (`known`, `available`, `in_pipette`, `pipette_after`, `dest_after`, `touched`, `liquids`) without aborting early so all violations in a protocol are surfaced in a single rehearsal pass.

### Default-Deny Verification Gate (`verify`)
Approves a protocol (`approved = True`) if and only if `bool(trace) and checked == len(trace) and not violations`, evaluating:
1. **`known_step`**: `op` must be in `{"pick_up_tip", "drop_tip", "aspirate", "dispense", "mix"}`, and liquid operations must target an existing `plate:*` or `res:*` container.
2. **`volume_in_range`**: Every liquid operation requires $20 \le \text{vol} \le 300\,\mu\text{L}$, and post-aspiration pipette volume must satisfy $\text{pipette\_after} \le 300\,\mu\text{L}$.
3. **`no_overfill`**: Every `dispense` must satisfy $\text{dest\_after} \le \text{max}$ ($300\,\mu\text{L}$ for plate wells, $15{,}000\,\mu\text{L}$ for reservoir columns).
4. **`enough_liquid`**: `aspirate` and `mix` require $\text{vol} \le \text{available}$ in the target container; `dispense` requires $\text{vol} \le \text{in\_pipette}$.
5. **`tip_present`**: Liquid operations require `tip == True`; `pick_up_tip` requires `tip == False` and `tips_left > 0`.
6. **`no_tip_reuse`**: On `aspirate` with a tip attached, requires $\text{touched} \setminus \text{liquids}(\text{loc}) = \emptyset$ (preventing carryover of foreign reagents into clean stocks).
7. **`all_tips_dropped`**: End-of-protocol invariant requiring `end["tip_on"] == False`.

---

## 4. Training

- **Zero Parametric Training**: No gradient-based learning, reinforcement learning, or parameter fitting is performed in this repository (`requirements.txt` uses the Python standard library only).
- **Non-Parametric Episodic Memory Write-Back (`write_back`)**: System adaptation across passes occurs strictly through append-only episodic logging to `demo/memory/runs.jsonl`. In the two-pass demonstration (`main()`), Pass 1 starts from an empty memory (`no earlier runs of this protocol`) and populates runs `#1`–`#5`; Pass 2 queries those episodes during `RECALL`, surfacing immediate failure history warnings (`runs [1]..[4]`) and prior validation provenance (`run [5]`) before rehearsal executes.

---

## 5. History

| Commit | Date | Author | Branch / Ref | Summary |
|---|---|---|---|---|
| `8219695` (`82196953452df408ecb8bf51952933f04fc9c387`) | `2026-11-18` | Yani Guan | `main` | Initial commit (`Add curated resources and first demo`): introduced the 5-stage `recall -> rehearse -> verify -> run -> write_back` liquid-handling loop (`demo/rehearse.py`), 5 JSON protocol fixtures (`4` broken, `1` good 43-step serial dilution), 2-pass episodic trace (`demo/memory/runs.jsonl`, `demo/output.txt`), GitHub Actions workflow (`.github/workflows/demo.yml`), and curated simulation datasets, repositories, and papers (`2017`–`2026`). |

---

## 6. Unique Properties

1. **Five-Stage Closed Safety Loop (`Recall -> Rehearse -> Verify -> Run -> Write-Back`)**: Combines content-addressed episodic memory (`SHA-256` protocol hashing) with full-trajectory dry-run simulation and a default-deny gate so unsafe protocols never reach hardware actuation while still leaving an auditable failure record.
2. **Non-Short-Circuiting Diagnostic Trace**: Unlike fail-first parsers that mask downstream defects, `rehearse()` simulates the entire step sequence using clamped physical bounds (`min(vol, available)`) so `verify()` reports *all* cascading violations (e.g., empty aspiration followed by empty dispense in `broken_aspirate_empty.json`).
3. **Zero-Dependency Standard-Library Execution**: Runs end-to-end in milliseconds on any Python 3 environment without external packages, providing a lightweight reference semantics for liquid-handling safety rules.
4. **Auto-Lab Sibling Contracts**:
   - Shares volumetric and tip-hygiene safety concepts with the formally verified Lean 4 `LabLean` checker in [`YLM`](https://github.com/yaniguan/YLM) (`lean/`), where `LabLean` strengthens qualitative set-based tip tracking to quantitative concentration-monotonicity proofs.
   - Aligns with [`stepbot`](https://github.com/yaniguan/stepbot) (which compiles declarative protocols to Opentrons Python API v2 `2.16` and checks them in the Opentrons simulator) and [`twinstep`](https://github.com/yaniguan/twinstep) (which generates seeded protocol error twins for benchmark evaluation).

---

## 7. 15-Repository Auto-Lab Wire Contract (`autolab-wire/v1`)

`autolab_wire.py` (`REPO_ID = "lab-rehearsal"`, `LAYER = "L4"`) exposes `process_packet(packet: dict) -> dict` for the end-to-end 15-repository Auto-Lab pipeline (`lab-ears -> lab-ask-why -> lab-decide -> lab-automation-atlas -> buy-or-make -> labshift -> lab-rehearsal -> lab-verify -> stepbot -> lab-hands -> lab-feet -> lab-attrib -> twinstep -> labfill -> labbench-posttrain`):

- **Path & State Hygiene**: Resolves repository imports strictly via `REPO_DIR = Path(__file__).resolve().parent` (never `os.getcwd()`), deep-copies incoming packets, returns already-halted packets unchanged, and runs `RehearsalSession` inside an ephemeral temporary directory by default (or an explicit campaign `memory_path` / `rehearsal_memory_path` guarded against tracked `demo/memory/runs.jsonl`).
- **Upstream Interface**: Reads scheduled assay packets from [`labshift`](https://github.com/yaniguan/labshift) (`L5`), extracting `parsed_spans.volume_ul` (default `100.0` uL), optional `pipette_model`, and optional `rehearsal_steps` (native micro-steps, `twinstep` macro-steps, `LabLean` traces, or `stepbot` specs). Simulates 1- and 8-channel manifolds plus `seal`/`thaw`/`incubate` volatile evaporation and solute up-concentration, halting on any violation with `halted = True`, `halt_reason = "L4_REHEARSAL_BLOCK"`, and `protocol_yaml_dict = None`.
- **Downstream Interface**: Compiles approved multi-step trajectories (`transfer`, `distribute`, `mix`, `discard`, and `incubate`/`seal`/`thaw` delays/annotations) into `out["rehearsal"]["protocol_yaml_dict"]` (Opentrons API `2.16` deck slots `1`/`2`/`3`) for [`lab-verify`](https://github.com/yaniguan/lab-verify) (`lean-mcp/v1` capability envelope minting), [`stepbot`](https://github.com/yaniguan/stepbot) (`compile_stage` / `StepbotMcpExecutor`), and [`twinstep`](https://github.com/yaniguan/twinstep) (`L3` dependency DAGs and error-twin generation).
- **Post-Run `L4` Partner & Bottom Reasoning Trace**: Pairs pre-execution tenth-microliter capacity, barrel-conservation, and bidirectional concentration-monotonicity proofs (`invariant = "checkRehearsal_preserves_capacity"` in `lean/LabRehearsalLean.lean`) with post-execution [`lab-attrib`](https://github.com/yaniguan/lab-attrib) (`EXECUTION_ARTIFACT` bounded retry reconciliation vs permanent static safety fast-deny) and sub-linear coordinate-normalized skeleton + MinHash LSH shingle recall in `runs.jsonl`.
