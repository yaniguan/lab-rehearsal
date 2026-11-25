/-!
# LabRehearsalLean: Formal L4 Wet-Lab Digital Twin, Rusty-SOP Gate & Negative-Result Ledger

Self-contained Lean 4.36.0 formalization for `lab-rehearsal` (L4 Stateful Wet-Lab Rehearsal
Twin & Epistemic Memory Layer). All liquid volumes are represented in tenths of a microliter
(`0.1 uL`) so deck + barrel volume conservation, solute conservation, well-capacity bounds,
dead-volume reserves, bidirectional concentration monotonicity (`concTolPermille = 1`),
rusty-SOP epoch checks, and 4-way causal attribution are decidable without rounding.
-/
namespace LabRehearsalLean

structure RegimeViolation where
  rule   : String
  detail : String
  deriving DecidableEq, Repr

def firstViolation : List (Bool × RegimeViolation) → Option RegimeViolation
  | [] => none
  | (true, _) :: rest => firstViolation rest
  | (false, v) :: _ => some v

@[simp] theorem firstViolation_nil : firstViolation [] = none := rfl

@[simp] theorem firstViolation_cons (b : Bool) (v : RegimeViolation)
    (rest : List (Bool × RegimeViolation)) :
    firstViolation ((b, v) :: rest) = none ↔ b = true ∧ firstViolation rest = none := by
  cases b <;> simp [firstViolation]

theorem firstViolation_none_iff (checks : List (Bool × RegimeViolation)) :
    firstViolation checks = none ↔ ∀ p ∈ checks, p.1 = true := by
  induction checks with
  | nil => simp [firstViolation]
  | cons hd tl ih =>
    rcases hd with ⟨b, v⟩
    cases b with
    | false => simp [firstViolation]
    | true  => simp [firstViolation, ih]

/-! ## 1. Stateful Deck + Barrel Twin, Conservation & Bidirectional Concentration Invariants -/

/-- Concentration rounding tolerance in permille (`1 / 1000 = 0.1%`, aligned with Python `CONC_TOL = 1e-3`). -/
def concTolPermille : Nat := 1

/-- Historical solute-to-volume concentration snapshot recorded on a mounted tip. -/
structure ConcSnapshot where
  vol    : Nat
  solute : Nat
  deriving DecidableEq, Repr

/-- State of an individual well on the rehearsal deck (tenths of uL `0.1 uL`). -/
structure WellState where
  wellId         : Nat
  vol            : Nat
  cap            : Nat
  deadVol        : Nat
  soluteAmt      : Nat
  hasBuffer      : Bool
  hasSample      : Bool
  frozen         : Bool
  sealed         : Bool
  isWaste        : Bool := false
  isReagentStock : Bool := false
  deriving DecidableEq, Repr

abbrev DeckWell := WellState

/-- Stateful pipette mount tracking both barrel contents (`pipVol`, `pipSolute`) and carryover history. -/
inductive TipStatus where
  | noTip
  | cleanTip
  | usedTip
      (pipVol : Nat)
      (pipSolute : Nat)
      (lastVol : Nat)
      (lastSolute : Nat)
      (snapshots : List ConcSnapshot)
      (touchedBuffer : Bool)
      (touchedSample : Bool)
  deriving DecidableEq, Repr

def TipStatus.vol : TipStatus → Nat
  | .noTip => 0
  | .cleanTip => 0
  | .usedTip pv _ _ _ _ _ _ => pv

def TipStatus.solute : TipStatus → Nat
  | .noTip => 0
  | .cleanTip => 0
  | .usedTip _ ps _ _ _ _ _ => ps

/-- Well-formed barrel invariant: an empty pipette barrel holds zero solute mass. -/
def TipWellFormed (t : TipStatus) : Prop :=
  t.vol = 0 → t.solute = 0

structure DeckState where
  wells        : List WellState
  tip          : TipStatus
  pipMin       : Nat
  pipMax       : Nat
  failedHashes : List Nat
  deriving DecidableEq, Repr

/-- Primitive wet-lab rehearsal step (including barrel `aspirate` & `dispense` for multi-dispense). -/
inductive RehearsalStep where
  | pickTip
  | dropTip
  | aspirate (srcId vol : Nat)
  | dispense (dstId vol : Nat) (airDispense : Bool)
  | transfer (srcId dstId vol : Nat)
  | mix (wellId vol reps : Nat)
  | incubate (wellId tempC durSec : Nat)
  | sealWell (wellId : Nat)
  | thawWell (wellId : Nat)
  deriving DecidableEq, Repr

def findWell (wid : Nat) : List WellState → Option WellState
  | [] => none
  | w :: ws => if w.wellId = wid then some w else findWell wid ws

def updateWell (wid : Nat) (f : WellState → WellState) : List WellState → List WellState
  | [] => []
  | w :: ws => if w.wellId = wid then f w :: ws else w :: updateWell wid f ws

def totalWellsVol : List WellState → Nat
  | [] => 0
  | w :: ws => w.vol + totalWellsVol ws

def totalWellsSolute : List WellState → Nat
  | [] => 0
  | w :: ws => w.soluteAmt + totalWellsSolute ws

/-- Check that target well does not exceed any prior diluted concentration (`curr_c <= prev_c * 1.001`). -/
def snapshotAllowsWell (sn : ConcSnapshot) (w : WellState) : Bool :=
  decide (sn.solute = 0 ∨ sn.vol = 0 ∨ w.vol = 0 ∨
          w.soluteAmt * sn.vol * 1000 ≤ sn.solute * w.vol * (1000 + concTolPermille))

/-- Check that tip's most recent solute concentration does not jump down into a pre-diluted well (`tip_c <= curr_c * 1.001`). -/
def lastConcAllowsWell (lVol lSol : Nat) (w : WellState) : Bool :=
  decide (lSol = 0 ∨ lVol = 0 ∨
          (0 < w.vol ∧ lSol * w.vol * 1000 ≤ w.soluteAmt * lVol * (1000 + concTolPermille)))

/-- Bidirectional lineage and concentration safety check on `aspirate` / `mix` / `transfer` source. -/
def tipMayEnterWell (t : TipStatus) (w : WellState) : Bool :=
  match t with
  | .noTip => false
  | .cleanTip => true
  | .usedTip _ _ lVol lSol snaps tBuf tSamp =>
    let classOK := (!tSamp || w.hasSample) && (!tBuf || w.hasBuffer)
    let snapsOK := snaps.all (fun sn => snapshotAllowsWell sn w)
    let lastOK  := lastConcAllowsWell lVol lSol w
    classOK && snapsOK && lastOK

/-- Submerged vs air-dispense contamination check on `dispense` / `transfer` destination. -/
def tipMayDispenseWell (t : TipStatus) (w : WellState) (airDispense : Bool) : Bool :=
  match t with
  | .noTip => false
  | .cleanTip => true
  | .usedTip _ pSol _ _ _ tBuf tSamp =>
    if airDispense || w.isWaste then true
    else if w.isReagentStock then
      (!tSamp || w.hasSample) && (!tBuf || w.hasBuffer)
    else if w.vol == 0 then true
    else
      !(tSamp && pSol == 0 && !w.hasSample)

def calcMovedSolute (sol v totalVol : Nat) : Nat :=
  if totalVol = 0 then 0 else (sol * v) / totalVol

def takeFromWell (v : Nat) (w : WellState) : WellState :=
  if w.vol - v = 0 then
    { w with vol := 0, soluteAmt := 0, hasBuffer := false, hasSample := false }
  else
    { w with vol := w.vol - v, soluteAmt := w.soluteAmt - calcMovedSolute w.soluteAmt v w.vol }

def addBarrelToWell (v givenSol : Nat) (pipHasBuf pipHasSamp touchBuf touchSamp airDispense : Bool)
    (w : WellState) : WellState :=
  let newBuf := w.hasBuffer || pipHasBuf || (!airDispense && touchBuf)
  let newSamp := w.hasSample || pipHasSamp || (!airDispense && touchSamp)
  { w with
    vol := w.vol + v,
    soluteAmt := w.soluteAmt + givenSol,
    hasBuffer := newBuf,
    hasSample := newSamp }

def addToWell (src : WellState) (v : Nat) (touchBuf touchSamp : Bool) (w : WellState) : WellState :=
  let movedSol := calcMovedSolute src.soluteAmt v src.vol
  { w with
    vol := w.vol + v,
    soluteAmt := w.soluteAmt + movedSol,
    hasBuffer := w.hasBuffer || src.hasBuffer || touchBuf,
    hasSample := w.hasSample || src.hasSample || touchSamp }

def tipTouchFlags : TipStatus → Bool × Bool
  | .noTip => (false, false)
  | .cleanTip => (false, false)
  | .usedTip _ _ _ _ _ tb ts => (tb, ts)

def tipSnapshots : TipStatus → List ConcSnapshot
  | .noTip => []
  | .cleanTip => []
  | .usedTip _ _ _ _ snaps _ _ => snaps

def stepTipAfterAspirate (t : TipStatus) (src : WellState) (v movedSol : Nat) : TipStatus :=
  let (tb, ts) := tipTouchFlags t
  let snaps := tipSnapshots t
  let newSnaps := if src.vol > 0 then snaps ++ [{ vol := src.vol, solute := src.soluteAmt }] else snaps
  .usedTip (t.vol + v) (t.solute + movedSol) src.vol src.soluteAmt newSnaps
    (tb || src.hasBuffer) (ts || src.hasSample)

def stepTipAfterDispense (t : TipStatus) (dstAfter : WellState) (v givenSol : Nat)
    (airDispense : Bool) : TipStatus :=
  let newPv := t.vol - v
  let newPs := t.solute - givenSol
  match t with
  | .noTip => .noTip
  | .cleanTip =>
    if airDispense then
      .usedTip newPv newPs 0 0 [] false false
    else
      .usedTip newPv newPs dstAfter.vol dstAfter.soluteAmt
        [{ vol := dstAfter.vol, solute := dstAfter.soluteAmt }]
        dstAfter.hasBuffer dstAfter.hasSample
  | .usedTip _ _ lVol lSol snaps tb ts =>
    if airDispense then
      .usedTip newPv newPs lVol lSol snaps tb ts
    else
      .usedTip newPv newPs dstAfter.vol dstAfter.soluteAmt
        (snaps ++ [{ vol := dstAfter.vol, solute := dstAfter.soluteAmt }])
        (tb || dstAfter.hasBuffer) (ts || dstAfter.hasSample)

def stepTipAfterTransfer (t : TipStatus) (src dstAfter : WellState) : TipStatus :=
  let (tb, ts) := tipTouchFlags t
  let snaps0 := tipSnapshots t
  let snaps1 := if src.vol > 0 then snaps0 ++ [{ vol := src.vol, solute := src.soluteAmt }] else snaps0
  let snaps2 := snaps1 ++ [{ vol := dstAfter.vol, solute := dstAfter.soluteAmt }]
  .usedTip 0 0 dstAfter.vol dstAfter.soluteAmt snaps2
    (tb || src.hasBuffer || dstAfter.hasBuffer)
    (ts || src.hasSample || dstAfter.hasSample)

def stepTipAfterMix (t : TipStatus) (wAfter : WellState) : TipStatus :=
  let (tb, ts) := tipTouchFlags t
  let snaps := tipSnapshots t
  .usedTip t.vol t.solute wAfter.vol wAfter.soluteAmt
    (snaps ++ [{ vol := wAfter.vol, solute := wAfter.soluteAmt }])
    (tb || wAfter.hasBuffer) (ts || wAfter.hasSample)

def checkDeckStep (s : DeckState) (st : RehearsalStep) : Option RegimeViolation :=
  match st with
  | .pickTip =>
    if s.tip == .noTip then none
    else some { rule := "pick_tip_requires_empty_mount",
                detail := "cannot pick_up_tip while a tip is already mounted" }
  | .dropTip =>
    firstViolation
      [ (decide (s.tip != .noTip),
          { rule := "drop_tip_requires_mounted_tip",
            detail := "cannot drop_tip when no tip is mounted" }),
        (decide (s.tip.vol = 0),
          { rule := "drop_tip_requires_empty_barrel",
            detail := "cannot drop_tip with undispensed liquid remaining in barrel" }) ]
  | .aspirate srcId v =>
    match findWell srcId s.wells with
    | some src =>
      firstViolation
        [ (decide (s.pipMin ≤ v ∧ v ≤ s.pipMax),
            { rule := "pipette_volume_range",
              detail := "aspirate volume outside pipette [pipMin, pipMax] bounds" }),
          (decide (s.tip.vol + v ≤ s.pipMax),
            { rule := "pipette_barrel_capacity",
              detail := "combined barrel volume exceeds pipMax" }),
          (decide (!src.frozen),
            { rule := "thawed_before_pipetting",
              detail := "cannot aspirate from a frozen container" }),
          (decide (!src.sealed),
            { rule := "unsealed_before_pipetting",
              detail := "cannot aspirate through a sealed plate" }),
          (decide (!src.isWaste),
            { rule := "no_aspirate_from_waste",
              detail := "aspirating from a designated waste container is forbidden" }),
          (tipMayEnterWell s.tip src,
            { rule := "no_tip_cross_contamination",
              detail := "used tip violates reagent lineage or bidirectional concentration order" }),
          (decide (src.deadVol + v ≤ src.vol),
            { rule := "enough_recoverable_volume",
              detail := "aspiration underflows source volume or dead-volume reserve" }) ]
    | none =>
      some { rule := "known_well_id",
             detail := "aspirate references an unknown well ID" }
  | .dispense dstId v airDispense =>
    match findWell dstId s.wells with
    | some dst =>
      firstViolation
        [ (decide (s.pipMin ≤ v ∧ v ≤ s.pipMax),
            { rule := "pipette_volume_range",
              detail := "dispense volume outside pipette [pipMin, pipMax] bounds" }),
          (decide (s.tip != .noTip),
            { rule := "tip_present",
              detail := "dispense requires a mounted tip" }),
          (decide (v ≤ s.tip.vol),
            { rule := "enough_pipette_liquid",
              detail := "dispense volume exceeds liquid held in pipette barrel" }),
          (decide (!dst.frozen),
            { rule := "thawed_before_pipetting",
              detail := "cannot dispense into a frozen container" }),
          (decide (!dst.sealed),
            { rule := "unsealed_before_pipetting",
              detail := "cannot dispense through a sealed plate" }),
          (tipMayDispenseWell s.tip dst airDispense,
            { rule := "no_tip_cross_contamination",
              detail := "submerged dispense contaminates stock or carries foreign residue" }),
          (decide (dst.vol + v ≤ dst.cap),
            { rule := "no_well_overfill",
              detail := "dispense exceeds destination well capacity" }) ]
    | none =>
      some { rule := "known_well_id",
             detail := "dispense references an unknown well ID" }
  | .transfer srcId dstId v =>
    match findWell srcId s.wells, findWell dstId s.wells with
    | some src, some dst =>
      firstViolation
        [ (decide (srcId ≠ dstId),
            { rule := "distinct_transfer_wells",
              detail := "transfer source and destination must be distinct wells" }),
          (decide (s.tip.vol = 0),
            { rule := "transfer_requires_empty_barrel",
              detail := "atomic transfer requires an empty barrel" }),
          (decide (s.pipMin ≤ v ∧ v ≤ s.pipMax),
            { rule := "pipette_volume_range",
              detail := "transfer volume outside pipette [pipMin, pipMax] bounds" }),
          (decide (!src.frozen && !dst.frozen),
            { rule := "thawed_before_pipetting",
              detail := "cannot aspirate from or dispense into a frozen container" }),
          (decide (!src.sealed && !dst.sealed),
            { rule := "unsealed_before_pipetting",
              detail := "cannot pipette through a sealed plate" }),
          (decide (!src.isWaste),
            { rule := "no_aspirate_from_waste",
              detail := "aspirating from a designated waste container is forbidden" }),
          (tipMayEnterWell s.tip src,
            { rule := "no_tip_cross_contamination",
              detail := "used tip violates reagent lineage or serial-dilution concentration order" }),
          (tipMayDispenseWell (stepTipAfterAspirate s.tip src v (calcMovedSolute src.soluteAmt v src.vol)) dst false,
            { rule := "no_dispense_cross_contamination",
              detail := "transfer dispense would contaminate destination stock or well" }),
          (decide (src.deadVol + v ≤ src.vol),
            { rule := "enough_recoverable_volume",
              detail := "aspiration underflows source volume or dead-volume reserve" }),
          (decide (dst.vol + v ≤ dst.cap),
            { rule := "no_well_overfill",
              detail := "dispense exceeds destination well capacity" }) ]
    | _, _ =>
      some { rule := "known_well_id",
             detail := "transfer references an unknown well ID" }
  | .mix wid v reps =>
    match findWell wid s.wells with
    | some w =>
      firstViolation
        [ (decide (0 < reps),
            { rule := "positive_mix_reps",
              detail := "mix repetitions must be >= 1" }),
          (decide (s.pipMin ≤ v ∧ v ≤ s.pipMax),
            { rule := "mix_barrel_volume_range",
              detail := "mix volume outside pipette [pipMin, pipMax] bounds" }),
          (decide (s.tip.vol + v ≤ s.pipMax),
            { rule := "mix_combined_barrel_capacity",
              detail := "mix stroke plus held barrel volume exceeds pipMax" }),
          (decide (!w.frozen && !w.sealed),
            { rule := "mix_open_thawed_well",
              detail := "cannot mix a frozen or sealed well" }),
          (decide (!w.isWaste),
            { rule := "no_aspirate_from_waste",
              detail := "mixing a designated waste container is forbidden" }),
          (tipMayEnterWell s.tip w,
            { rule := "no_mix_cross_contamination",
              detail := "used tip would cross-contaminate target well during mix" }),
          (decide (w.deadVol + v ≤ w.vol),
            { rule := "mix_sufficient_well_volume",
              detail := "mix volume exceeds recoverable liquid in well" }) ]
    | none =>
      some { rule := "known_well_id",
             detail := "mix references an unknown well ID" }
  | .incubate wid tempC durSec =>
    match findWell wid s.wells with
    | some w =>
      firstViolation
        [ (decide (0 < durSec ∧ 4 ≤ tempC ∧ tempC ≤ 98),
            { rule := "incubate_temp_duration_bounds",
              detail := "incubation requires durSec > 0 and temperature in [4, 98] C" }),
          (decide (!w.frozen),
            { rule := "thaw_before_incubate",
              detail := "frozen container must be thawed before incubation" }),
          (decide (tempC < 80 ∨ w.sealed = true),
            { rule := "high_temp_requires_seal",
              detail := "incubation >= 80 C requires a sealed plate to prevent evaporation" }) ]
    | none =>
      some { rule := "known_well_id",
             detail := "incubate references an unknown well ID" }
  | .sealWell wid =>
    match findWell wid s.wells with
    | some w =>
      if !w.sealed then none
      else some { rule := "not_already_sealed",
                  detail := "well/plate is already sealed" }
    | none =>
      some { rule := "known_well_id",
             detail := "seal references an unknown well ID" }
  | .thawWell wid =>
    match findWell wid s.wells with
    | some w =>
      if w.frozen then none
      else some { rule := "thaw_requires_frozen",
                  detail := "redundant thawWell on an already-thawed container" }
    | none =>
      some { rule := "known_well_id",
             detail := "thaw references an unknown well ID" }

def applyDeckStep (s : DeckState) (st : RehearsalStep) : DeckState :=
  match st with
  | .pickTip => { s with tip := .cleanTip }
  | .dropTip => { s with tip := .noTip }
  | .aspirate srcId v =>
    match findWell srcId s.wells with
    | some src =>
      let movedSol := calcMovedSolute src.soluteAmt v src.vol
      let w1 := updateWell srcId (takeFromWell v) s.wells
      { s with wells := w1, tip := stepTipAfterAspirate s.tip src v movedSol }
    | none => s
  | .dispense dstId v airDispense =>
    match findWell dstId s.wells with
    | some dst =>
      let givenSol := calcMovedSolute s.tip.solute v s.tip.vol
      let (tb, ts) := tipTouchFlags s.tip
      let pipHasSamp := givenSol > 0
      let pipHasBuf := v > givenSol || (tb && !ts)
      let w1 := updateWell dstId (addBarrelToWell v givenSol pipHasBuf pipHasSamp tb ts airDispense) s.wells
      let dstAfter := (findWell dstId w1).getD dst
      { s with wells := w1, tip := stepTipAfterDispense s.tip dstAfter v givenSol airDispense }
    | none => s
  | .transfer srcId dstId v =>
    match findWell srcId s.wells, findWell dstId s.wells with
    | some src, some _ =>
      let (tb, ts) := tipTouchFlags s.tip
      let w1 := updateWell srcId (takeFromWell v) s.wells
      let w2 := updateWell dstId (addToWell src v tb ts) w1
      let dstAfter := (findWell dstId w2).getD src
      { s with wells := w2, tip := stepTipAfterTransfer s.tip src dstAfter }
    | _, _ => s
  | .mix wid _ _ =>
    match findWell wid s.wells with
    | some w =>
      let (tb, ts) := tipTouchFlags s.tip
      let wAfter : WellState := { w with hasBuffer := w.hasBuffer || tb, hasSample := w.hasSample || ts }
      let w1 := updateWell wid (fun x => { x with hasBuffer := x.hasBuffer || tb, hasSample := x.hasSample || ts }) s.wells
      { s with wells := w1, tip := stepTipAfterMix s.tip wAfter }
    | none => s
  | .incubate _ _ _ => s
  | .sealWell wid => { s with wells := updateWell wid (fun w => { w with sealed := true }) s.wells }
  | .thawWell wid => { s with wells := updateWell wid (fun w => { w with frozen := false }) s.wells }

def wellIdsUnique : List WellState → Bool
  | [] => true
  | w :: ws => (findWell w.wellId ws).isNone && wellIdsUnique ws

def initDeckOK (s : DeckState) (protoHash : Nat) : Option RegimeViolation :=
  firstViolation
    [ (decide (protoHash ∉ s.failedHashes),
        { rule := "episodic_recall_fast_deny",
          detail := "protocol hash matches a previously failed run in memory/runs.jsonl" }),
      (decide (s.tip == .noTip),
        { rule := "initial_mount_empty",
          detail := "initial deck state must start with no tip mounted" }),
      (wellIdsUnique s.wells,
        { rule := "unique_well_ids",
          detail := "initial deck contains duplicate well IDs" }),
      (s.wells.all (fun w => decide (w.vol ≤ w.cap ∧ w.deadVol ≤ w.cap ∧ (w.vol = 0 → w.soluteAmt = 0))),
        { rule := "initial_state_within_capacity",
          detail := "initial deck state violates 0 <= vol <= cap or empty-well zero-solute" }),
      (decide (0 < s.pipMin ∧ s.pipMin ≤ s.pipMax),
        { rule := "valid_pipette_spec",
          detail := "pipette specification violates 0 < pipMin <= pipMax" }) ]

def runRehearsalSteps (s : DeckState) : List RehearsalStep → Except RegimeViolation DeckState
  | [] => .ok s
  | st :: rest =>
    match checkDeckStep s st with
    | some v => .error v
    | none   => runRehearsalSteps (applyDeckStep s st) rest

def checkRehearsal (s : DeckState) (protoHash : Nat) (steps : List RehearsalStep) :
    Except RegimeViolation DeckState :=
  match initDeckOK s protoHash with
  | some v => .error v
  | none   =>
    match runRehearsalSteps s steps with
    | .error v => .error v
    | .ok sEnd =>
      if sEnd.tip == .noTip then .ok sEnd
      else .error { rule := "tip_dropped_at_end",
                    detail := "protocol finished with a tip still mounted on the pipette" }

/-! ### Helper Lemmas on Wells, Arithmetic, and Barrel Invariants -/

theorem calcMovedSolute_le (sol v totalVol : Nat) (hv : v ≤ totalVol) :
    calcMovedSolute sol v totalVol ≤ sol := by
  unfold calcMovedSolute
  split
  · exact Nat.zero_le _
  · exact Nat.div_le_of_le_mul (by
      calc sol * v ≤ sol * totalVol := Nat.mul_le_mul_left sol hv
           _       = totalVol * sol := Nat.mul_comm sol totalVol)

theorem calcMovedSolute_eq_self (sol totalVol : Nat) (hpos : 0 < totalVol) :
    calcMovedSolute sol totalVol totalVol = sol := by
  unfold calcMovedSolute
  have hz : ¬(totalVol = 0) := Nat.ne_of_gt hpos
  simp [hz, Nat.mul_div_cancel sol hpos]

@[simp] theorem stepTipAfterAspirate_vol (t : TipStatus) (src : WellState) (v ms : Nat) :
    (stepTipAfterAspirate t src v ms).vol = t.vol + v := rfl

@[simp] theorem stepTipAfterAspirate_solute (t : TipStatus) (src : WellState) (v ms : Nat) :
    (stepTipAfterAspirate t src v ms).solute = t.solute + ms := rfl

@[simp] theorem stepTipAfterDispense_vol (t : TipStatus) (dstAfter : WellState) (v gs : Nat)
    (air : Bool) (ht : t ≠ .noTip) :
    (stepTipAfterDispense t dstAfter v gs air).vol = t.vol - v := by
  cases t with
  | noTip => exact absurd rfl ht
  | cleanTip => cases air <;> rfl
  | usedTip _ _ _ _ _ _ _ => cases air <;> rfl

@[simp] theorem stepTipAfterDispense_solute (t : TipStatus) (dstAfter : WellState) (v gs : Nat)
    (air : Bool) (ht : t ≠ .noTip) :
    (stepTipAfterDispense t dstAfter v gs air).solute = t.solute - gs := by
  cases t with
  | noTip => exact absurd rfl ht
  | cleanTip => cases air <;> rfl
  | usedTip _ _ _ _ _ _ _ => cases air <;> rfl

@[simp] theorem stepTipAfterTransfer_vol (t : TipStatus) (src dstAfter : WellState) :
    (stepTipAfterTransfer t src dstAfter).vol = 0 := rfl

@[simp] theorem stepTipAfterTransfer_solute (t : TipStatus) (src dstAfter : WellState) :
    (stepTipAfterTransfer t src dstAfter).solute = 0 := rfl

@[simp] theorem stepTipAfterMix_vol (t : TipStatus) (w : WellState) :
    (stepTipAfterMix t w).vol = t.vol := rfl

@[simp] theorem stepTipAfterMix_solute (t : TipStatus) (w : WellState) :
    (stepTipAfterMix t w).solute = t.solute := rfl

@[simp] theorem takeFromWell_wellId (v : Nat) (w : WellState) :
    (takeFromWell v w).wellId = w.wellId := by
  unfold takeFromWell
  split <;> rfl

@[simp] theorem takeFromWell_cap (v : Nat) (w : WellState) :
    (takeFromWell v w).cap = w.cap := by
  unfold takeFromWell
  split <;> rfl

@[simp] theorem takeFromWell_vol (v : Nat) (w : WellState) :
    (takeFromWell v w).vol = w.vol - v := by
  unfold takeFromWell
  split
  · next h => simp [h]
  · rfl

theorem takeFromWell_solute (v : Nat) (w : WellState) (hv : v ≤ w.vol)
    (hwz : w.vol = 0 → w.soluteAmt = 0) :
    (takeFromWell v w).soluteAmt + calcMovedSolute w.soluteAmt v w.vol = w.soluteAmt := by
  unfold takeFromWell
  split
  · next hz =>
    have heq : v = w.vol := by omega
    subst heq
    by_cases hw0 : w.vol = 0
    · have hs0 := hwz hw0
      simp [hw0, hs0, calcMovedSolute]
    · have hpos : 0 < w.vol := Nat.pos_of_ne_zero hw0
      simp [calcMovedSolute_eq_self w.soluteAmt w.vol hpos]
  · have hle := calcMovedSolute_le w.soluteAmt v w.vol hv
    simp only
    omega

theorem findWell_update_ne (a b : Nat) (f : WellState → WellState)
    (hf : ∀ w, (f w).wellId = w.wellId) (hne : a ≠ b) :
    ∀ ws, findWell b (updateWell a f ws) = findWell b ws
  | [] => rfl
  | w :: ws => by
    by_cases hw : w.wellId = a
    · simp [updateWell, findWell, hw, hf, hne]
    · by_cases hbw : w.wellId = b
      · have hba : b ≠ a := Ne.symm hne
        simp [updateWell, findWell, hbw, hba]
      · simp [updateWell, findWell, hw, hbw, findWell_update_ne a b f hf hne ws]

theorem totalWellsVol_update (wid : Nat) (f : WellState → WellState) (w0 : WellState) :
    ∀ ws, findWell wid ws = some w0 →
      totalWellsVol (updateWell wid f ws) + w0.vol = totalWellsVol ws + (f w0).vol
  | [], h => by simp [findWell] at h
  | w :: ws, h => by
    by_cases hw : w.wellId = wid
    · simp [findWell, hw] at h
      subst h
      simp [updateWell, hw, totalWellsVol]
      omega
    · simp [findWell, hw] at h
      have ih := totalWellsVol_update wid f w0 ws h
      simp [updateWell, hw, totalWellsVol]
      omega

theorem totalWellsSolute_update (wid : Nat) (f : WellState → WellState) (w0 : WellState) :
    ∀ ws, findWell wid ws = some w0 →
      totalWellsSolute (updateWell wid f ws) + w0.soluteAmt = totalWellsSolute ws + (f w0).soluteAmt
  | [], h => by simp [findWell] at h
  | w :: ws, h => by
    by_cases hw : w.wellId = wid
    · simp [findWell, hw] at h
      subst h
      simp [updateWell, hw, totalWellsSolute]
      omega
    · simp [findWell, hw] at h
      have ih := totalWellsSolute_update wid f w0 ws h
      simp [updateWell, hw, totalWellsSolute]
      omega

theorem mem_updateWell (wid : Nat) (f : WellState → WellState) (x : WellState) :
    ∀ ws, x ∈ updateWell wid f ws → x ∈ ws ∨ ∃ w0, findWell wid ws = some w0 ∧ x = f w0
  | [], h => by simp [updateWell] at h
  | w :: ws, h => by
    by_cases hw : w.wellId = wid
    · simp [updateWell, findWell, hw] at h ⊢
      rcases h with rfl | hmem
      · exact Or.inr rfl
      · exact Or.inl (Or.inr hmem)
    · simp [updateWell, findWell, hw] at h ⊢
      rcases h with rfl | hmem
      · exact Or.inl (Or.inl rfl)
      · rcases mem_updateWell wid f x ws hmem with h1 | ⟨w0, hf0, rfl⟩
        · exact Or.inl (Or.inr h1)
        · exact Or.inr ⟨w0, hf0, rfl⟩

theorem findWell_mem {wid : Nat} {w0 : WellState} :
    ∀ {ws}, findWell wid ws = some w0 → w0 ∈ ws
  | [], h => by simp [findWell] at h
  | w :: ws, h => by
    by_cases hw : w.wellId = wid
    · simp [findWell, hw] at h; subst h; simp
    · simp [findWell, hw] at h; exact List.mem_cons_of_mem w (findWell_mem h)

/-! ### Deck + Barrel Volume Conservation Theorems -/

/-- Single-step deck + barrel conservation across all 9 primitives (including `aspirate` & `dispense`). -/
theorem step_conserves_deck_and_barrel (s : DeckState) (st : RehearsalStep)
    (hok : checkDeckStep s st = none) :
    totalWellsVol (applyDeckStep s st).wells + (applyDeckStep s st).tip.vol =
      totalWellsVol s.wells + s.tip.vol := by
  cases st with
  | pickTip =>
    simp only [checkDeckStep] at hok
    by_cases ht : s.tip == .noTip
    · have heq : s.tip = .noTip := eq_of_beq ht
      simp [applyDeckStep, heq, TipStatus.vol]
    · simp [ht] at hok
  | dropTip =>
    simp only [checkDeckStep, firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
    rcases hok with ⟨_, hvol0⟩
    simp only [applyDeckStep]
    rw [hvol0]
    rfl
  | aspirate srcId v =>
    simp only [checkDeckStep] at hok
    cases hsrc : findWell srcId s.wells with
    | none => simp [hsrc] at hok
    | some src =>
      simp only [hsrc, firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
      rcases hok with ⟨_, _, _, _, _, _, hen⟩
      simp only [applyDeckStep, hsrc, stepTipAfterAspirate_vol]
      have h1 := totalWellsVol_update srcId (takeFromWell v) src s.wells hsrc
      rw [takeFromWell_vol] at h1
      omega
  | dispense dstId v air =>
    simp only [checkDeckStep] at hok
    cases hdst : findWell dstId s.wells with
    | none => simp [hdst] at hok
    | some dst =>
      simp only [hdst, firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
      rcases hok with ⟨_, htip, hvle, _, _, _, _⟩
      have hne : s.tip ≠ .noTip := by
        intro hc; rw [hc] at htip; simp at htip
      simp only [applyDeckStep, hdst, stepTipAfterDispense_vol _ _ _ _ _ hne]
      have h1 := totalWellsVol_update dstId
        (addBarrelToWell v (calcMovedSolute s.tip.solute v s.tip.vol)
          (v > calcMovedSolute s.tip.solute v s.tip.vol || ((tipTouchFlags s.tip).1 && !(tipTouchFlags s.tip).2))
          (calcMovedSolute s.tip.solute v s.tip.vol > 0)
          (tipTouchFlags s.tip).1 (tipTouchFlags s.tip).2 air) dst s.wells hdst
      simp only [addBarrelToWell] at h1
      omega
  | transfer srcId dstId v =>
    simp only [checkDeckStep] at hok
    cases hsrc : findWell srcId s.wells with
    | none => simp [hsrc] at hok
    | some src =>
      cases hdst : findWell dstId s.wells with
      | none => simp [hsrc, hdst] at hok
      | some dst =>
        simp only [hsrc, hdst, firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
        rcases hok with ⟨hne, htv0, _, _, _, _, _, _, hen, _⟩
        simp only [applyDeckStep, hsrc, hdst, stepTipAfterTransfer_vol]
        have h1 := totalWellsVol_update srcId (takeFromWell v) src s.wells hsrc
        have hdst2 : findWell dstId (updateWell srcId (takeFromWell v) s.wells) = some dst := by
          rw [findWell_update_ne srcId dstId (takeFromWell v) (takeFromWell_wellId v) hne]
          exact hdst
        have h2 := totalWellsVol_update dstId (addToWell src v (tipTouchFlags s.tip).1 (tipTouchFlags s.tip).2)
                    dst (updateWell srcId (takeFromWell v) s.wells) hdst2
        rw [takeFromWell_vol] at h1
        simp only [addToWell] at h2
        omega
  | mix wid v reps =>
    simp only [checkDeckStep] at hok
    cases hfw : findWell wid s.wells with
    | none => simp [hfw] at hok
    | some w =>
      simp only [applyDeckStep, hfw, stepTipAfterMix_vol]
      have hup := totalWellsVol_update wid
        (fun x => { x with hasBuffer := x.hasBuffer || (tipTouchFlags s.tip).1,
                           hasSample := x.hasSample || (tipTouchFlags s.tip).2 }) w s.wells hfw
      simp at hup
      omega
  | incubate wid tempC durSec => rfl
  | sealWell wid =>
    simp only [checkDeckStep] at hok
    cases hfw : findWell wid s.wells with
    | none => simp [hfw] at hok
    | some w =>
      have hup := totalWellsVol_update wid (fun x => { x with sealed := true }) w s.wells hfw
      simp at hup
      simp only [applyDeckStep]
      omega
  | thawWell wid =>
    simp only [checkDeckStep] at hok
    cases hfw : findWell wid s.wells with
    | none => simp [hfw] at hok
    | some w =>
      have hup := totalWellsVol_update wid (fun x => { x with frozen := false }) w s.wells hfw
      simp at hup
      simp only [applyDeckStep]
      omega

/-- Multi-step deck + barrel conservation: `totalWellsVol + tip.vol` is an exact invariant of `runRehearsalSteps`. -/
theorem runRehearsalSteps_conserves_deck_and_barrel (s sEnd : DeckState) (steps : List RehearsalStep)
    (hrun : runRehearsalSteps s steps = .ok sEnd) :
    totalWellsVol sEnd.wells + sEnd.tip.vol = totalWellsVol s.wells + s.tip.vol := by
  induction steps generalizing s with
  | nil =>
    simp [runRehearsalSteps] at hrun
    subst hrun
    rfl
  | cons st rest ih =>
    simp only [runRehearsalSteps] at hrun
    cases hck : checkDeckStep s st with
    | some v => simp [hck] at hrun
    | none =>
      simp only [hck] at hrun
      rw [ih (applyDeckStep s st) hrun, step_conserves_deck_and_barrel s st hck]

theorem runRehearsalSteps_conserves (s sEnd : DeckState) (steps : List RehearsalStep)
    (hinitTip : s.tip = .noTip) (hendTip : sEnd.tip = .noTip)
    (hrun : runRehearsalSteps s steps = .ok sEnd) :
    totalWellsVol sEnd.wells = totalWellsVol s.wells := by
  have h := runRehearsalSteps_conserves_deck_and_barrel s sEnd steps hrun
  simp [hinitTip, hendTip, TipStatus.vol] at h
  exact h

/-! ### Solute Conservation & Well Capacity Preservation Theorems -/

def AllWellsFit (ws : List WellState) : Prop := ∀ w ∈ ws, w.vol ≤ w.cap

def AllWellsZeroEmptySolute (ws : List WellState) : Prop :=
  ∀ w ∈ ws, w.vol = 0 → w.soluteAmt = 0

theorem step_preserves_pipMin (s : DeckState) (st : RehearsalStep) :
    (applyDeckStep s st).pipMin = s.pipMin := by
  cases st <;> simp only [applyDeckStep]
  · cases findWell _ s.wells <;> rfl
  · cases findWell _ s.wells <;> rfl
  · cases findWell _ s.wells with
    | none => rfl
    | some _ => cases findWell _ s.wells <;> rfl
  · cases findWell _ s.wells <;> rfl

theorem step_preserves_zero_empty_solute (s : DeckState) (st : RehearsalStep)
    (hok : checkDeckStep s st = none) (hpmin : 0 < s.pipMin)
    (hz : AllWellsZeroEmptySolute s.wells) :
    AllWellsZeroEmptySolute (applyDeckStep s st).wells := by
  cases st with
  | pickTip => exact hz
  | dropTip => exact hz
  | incubate _ _ _ => exact hz
  | sealWell wid =>
    intro x hx hx0
    rcases mem_updateWell wid _ x s.wells hx with hm | ⟨w0, hfw, rfl⟩
    · exact hz x hm hx0
    · exact hz w0 (findWell_mem hfw) hx0
  | thawWell wid =>
    intro x hx hx0
    rcases mem_updateWell wid _ x s.wells hx with hm | ⟨w0, hfw, rfl⟩
    · exact hz x hm hx0
    · exact hz w0 (findWell_mem hfw) hx0
  | mix wid _ _ =>
    simp only [applyDeckStep]
    cases hfw : findWell wid s.wells with
    | none => exact hz
    | some w =>
      intro x hx hx0
      rcases mem_updateWell wid _ x s.wells hx with hm | ⟨w0, hfw0, rfl⟩
      · exact hz x hm hx0
      · exact hz w0 (findWell_mem hfw0) hx0
  | aspirate srcId v =>
    simp only [applyDeckStep]
    cases hsrc : findWell srcId s.wells with
    | none => exact hz
    | some src =>
      intro x hx hx0
      rcases mem_updateWell srcId (takeFromWell v) x s.wells hx with hm | ⟨w0, hfw0, rfl⟩
      · exact hz x hm hx0
      · unfold takeFromWell at hx0 ⊢
        split at hx0
        · next hb => simp [hb]
        · next hnb => simp at hx0; exact absurd hx0 hnb
  | dispense dstId v air =>
    simp only [checkDeckStep] at hok
    cases hdst : findWell dstId s.wells with
    | none => simp [hdst] at hok
    | some dst =>
      simp only [hdst, firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
      rcases hok with ⟨⟨hvmin, _⟩, _, _, _, _, _, _⟩
      simp only [applyDeckStep, hdst]
      intro x hx hx0
      rcases mem_updateWell dstId _ x s.wells hx with hm | ⟨w0, hfw0, rfl⟩
      · exact hz x hm hx0
      · simp [addBarrelToWell] at hx0
        omega
  | transfer srcId dstId v =>
    simp only [checkDeckStep] at hok
    cases hsrc : findWell srcId s.wells with
    | none => simp [hsrc] at hok
    | some src =>
      cases hdst : findWell dstId s.wells with
      | none => simp [hsrc, hdst] at hok
      | some dst =>
        simp only [hsrc, hdst, firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
        rcases hok with ⟨hne, _, ⟨hvmin, _⟩, _, _, _, _, _, _, _⟩
        simp only [applyDeckStep, hsrc, hdst]
        intro x hx hx0
        rcases mem_updateWell dstId _ x _ hx with hx1 | ⟨w0, hfw0, rfl⟩
        · rcases mem_updateWell srcId (takeFromWell v) x s.wells hx1 with hm | ⟨w1, hfw1, rfl⟩
          · exact hz x hm hx0
          · unfold takeFromWell at hx0 ⊢
            split at hx0
            · next hb => simp [hb]
            · next hnb => simp at hx0; exact absurd hx0 hnb
        · simp [addToWell] at hx0
          omega

theorem step_preserves_tip_well_formed (s : DeckState) (st : RehearsalStep)
    (hok : checkDeckStep s st = none) (hpmin : 0 < s.pipMin) (htwf : TipWellFormed s.tip) :
    TipWellFormed (applyDeckStep s st).tip := by
  cases st with
  | pickTip => intro _; rfl
  | dropTip => intro _; rfl
  | incubate _ _ _ => exact htwf
  | sealWell _ => exact htwf
  | thawWell _ => exact htwf
  | mix wid _ _ =>
    simp only [applyDeckStep]
    cases findWell wid s.wells with
    | none => exact htwf
    | some w =>
      intro hv0
      simp only [stepTipAfterMix_vol] at hv0
      simp only [stepTipAfterMix_solute]
      exact htwf hv0
  | aspirate srcId v =>
    simp only [checkDeckStep] at hok
    cases hsrc : findWell srcId s.wells with
    | none => simp [hsrc] at hok
    | some src =>
      simp only [hsrc, firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
      rcases hok with ⟨⟨hvmin, _⟩, _, _, _, _, _, _⟩
      simp only [applyDeckStep, hsrc]
      intro hv0
      simp only [stepTipAfterAspirate_vol] at hv0
      omega
  | dispense dstId v air =>
    simp only [checkDeckStep] at hok
    cases hdst : findWell dstId s.wells with
    | none => simp [hdst] at hok
    | some dst =>
      simp only [hdst, firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
      rcases hok with ⟨⟨hvmin, _⟩, htip, hvle, _, _, _, _⟩
      have hne : s.tip ≠ .noTip := by
        intro hc; rw [hc] at htip; simp at htip
      have htvpos : 0 < s.tip.vol := by omega
      simp only [applyDeckStep, hdst]
      intro hv0
      rw [stepTipAfterDispense_vol _ _ _ _ _ hne] at hv0
      rw [stepTipAfterDispense_solute _ _ _ _ _ hne]
      have heq : s.tip.vol = v := by omega
      rw [← heq, calcMovedSolute_eq_self s.tip.solute s.tip.vol htvpos]
      omega
  | transfer srcId dstId v =>
    simp only [applyDeckStep]
    cases findWell srcId s.wells with
    | none => exact htwf
    | some _ =>
      cases findWell dstId s.wells with
      | none => exact htwf
      | some _ => intro _; rfl

/-- Single-step exact solute conservation across deck wells and pipette barrel. -/
theorem step_conserves_total_solute (s : DeckState) (st : RehearsalStep)
    (htwf : TipWellFormed s.tip)
    (hz : AllWellsZeroEmptySolute s.wells) (hok : checkDeckStep s st = none) :
    totalWellsSolute (applyDeckStep s st).wells + (applyDeckStep s st).tip.solute =
      totalWellsSolute s.wells + s.tip.solute := by
  cases st with
  | pickTip =>
    simp only [checkDeckStep] at hok
    by_cases ht : s.tip == .noTip
    · have heq : s.tip = .noTip := eq_of_beq ht
      simp [applyDeckStep, heq, TipStatus.solute]
    · simp [ht] at hok
  | dropTip =>
    simp only [checkDeckStep, firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
    rcases hok with ⟨_, hvol0⟩
    have hsol0 : s.tip.solute = 0 := htwf hvol0
    simp only [applyDeckStep]
    rw [hsol0]
    rfl
  | aspirate srcId v =>
    simp only [checkDeckStep] at hok
    cases hsrc : findWell srcId s.wells with
    | none => simp [hsrc] at hok
    | some src =>
      simp only [hsrc, firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
      rcases hok with ⟨_, _, _, _, _, _, hen⟩
      simp only [applyDeckStep, hsrc, stepTipAfterAspirate_solute]
      have h1 := totalWellsSolute_update srcId (takeFromWell v) src s.wells hsrc
      have hsrc_z : src.vol = 0 → src.soluteAmt = 0 := hz src (findWell_mem hsrc)
      have hsol := takeFromWell_solute v src (by omega) hsrc_z
      omega
  | dispense dstId v air =>
    simp only [checkDeckStep] at hok
    cases hdst : findWell dstId s.wells with
    | none => simp [hdst] at hok
    | some dst =>
      simp only [hdst, firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
      rcases hok with ⟨_, htip, hvle, _, _, _, _⟩
      have hne : s.tip ≠ .noTip := by
        intro hc; rw [hc] at htip; simp at htip
      simp only [applyDeckStep, hdst, stepTipAfterDispense_solute _ _ _ _ _ hne]
      have h1 := totalWellsSolute_update dstId
        (addBarrelToWell v (calcMovedSolute s.tip.solute v s.tip.vol)
          (v > calcMovedSolute s.tip.solute v s.tip.vol || ((tipTouchFlags s.tip).1 && !(tipTouchFlags s.tip).2))
          (calcMovedSolute s.tip.solute v s.tip.vol > 0)
          (tipTouchFlags s.tip).1 (tipTouchFlags s.tip).2 air) dst s.wells hdst
      simp only [addBarrelToWell] at h1
      have hle := calcMovedSolute_le s.tip.solute v s.tip.vol hvle
      omega
  | transfer srcId dstId v =>
    simp only [checkDeckStep] at hok
    cases hsrc : findWell srcId s.wells with
    | none => simp [hsrc] at hok
    | some src =>
      cases hdst : findWell dstId s.wells with
      | none => simp [hsrc, hdst] at hok
      | some dst =>
        simp only [hsrc, hdst, firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
        rcases hok with ⟨hne, htv0, _, _, _, _, _, _, hen, _⟩
        have hts0 : s.tip.solute = 0 := htwf htv0
        simp only [applyDeckStep, hsrc, hdst, stepTipAfterTransfer_solute, hts0]
        have h1 := totalWellsSolute_update srcId (takeFromWell v) src s.wells hsrc
        have hdst2 : findWell dstId (updateWell srcId (takeFromWell v) s.wells) = some dst := by
          rw [findWell_update_ne srcId dstId (takeFromWell v) (takeFromWell_wellId v) hne]
          exact hdst
        have h2 := totalWellsSolute_update dstId (addToWell src v (tipTouchFlags s.tip).1 (tipTouchFlags s.tip).2)
                    dst (updateWell srcId (takeFromWell v) s.wells) hdst2
        have hsrc_z : src.vol = 0 → src.soluteAmt = 0 := hz src (findWell_mem hsrc)
        have hsol := takeFromWell_solute v src (by omega) hsrc_z
        simp only [addToWell] at h2
        omega
  | mix wid _ _ =>
    simp only [checkDeckStep] at hok
    cases hfw : findWell wid s.wells with
    | none => simp [hfw] at hok
    | some w =>
      simp only [applyDeckStep, hfw, stepTipAfterMix_solute]
      have hup := totalWellsSolute_update wid
        (fun x => { x with hasBuffer := x.hasBuffer || (tipTouchFlags s.tip).1,
                           hasSample := x.hasSample || (tipTouchFlags s.tip).2 }) w s.wells hfw
      simp at hup
      omega
  | incubate _ _ _ => rfl
  | sealWell wid =>
    simp only [checkDeckStep] at hok
    cases hfw : findWell wid s.wells with
    | none => simp [hfw] at hok
    | some w =>
      have hup := totalWellsSolute_update wid (fun x => { x with sealed := true }) w s.wells hfw
      simp at hup
      simp only [applyDeckStep]
      omega
  | thawWell wid =>
    simp only [checkDeckStep] at hok
    cases hfw : findWell wid s.wells with
    | none => simp [hfw] at hok
    | some w =>
      have hup := totalWellsSolute_update wid (fun x => { x with frozen := false }) w s.wells hfw
      simp at hup
      simp only [applyDeckStep]
      omega

theorem runRehearsalSteps_conserves_solute (s sEnd : DeckState) (steps : List RehearsalStep)
    (hpmin : 0 < s.pipMin) (htwf : TipWellFormed s.tip)
    (hz : AllWellsZeroEmptySolute s.wells)
    (hrun : runRehearsalSteps s steps = .ok sEnd) :
    totalWellsSolute sEnd.wells + sEnd.tip.solute = totalWellsSolute s.wells + s.tip.solute := by
  induction steps generalizing s with
  | nil =>
    simp [runRehearsalSteps] at hrun
    subst hrun
    rfl
  | cons st rest ih =>
    simp only [runRehearsalSteps] at hrun
    cases hck : checkDeckStep s st with
    | some v => simp [hck] at hrun
    | none =>
      simp only [hck] at hrun
      have hp' : 0 < (applyDeckStep s st).pipMin := by rw [step_preserves_pipMin]; exact hpmin
      have htwf' := step_preserves_tip_well_formed s st hck hpmin htwf
      have hz' := step_preserves_zero_empty_solute s st hck hpmin hz
      rw [ih (applyDeckStep s st) hp' htwf' hz' hrun,
          step_conserves_total_solute s st htwf hz hck]

theorem step_preserves_capacity (s : DeckState) (st : RehearsalStep)
    (hok : checkDeckStep s st = none) (hfit : AllWellsFit s.wells) :
    AllWellsFit (applyDeckStep s st).wells := by
  cases st with
  | pickTip => exact hfit
  | dropTip => exact hfit
  | incubate wid tempC durSec => exact hfit
  | sealWell wid =>
    intro x hx
    rcases mem_updateWell wid _ x s.wells hx with hx0 | ⟨w0, hfw, rfl⟩
    · exact hfit x hx0
    · exact hfit w0 (findWell_mem hfw)
  | thawWell wid =>
    intro x hx
    rcases mem_updateWell wid _ x s.wells hx with hx0 | ⟨w0, hfw, rfl⟩
    · exact hfit x hx0
    · exact hfit w0 (findWell_mem hfw)
  | mix wid v reps =>
    simp only [applyDeckStep]
    cases hfw : findWell wid s.wells with
    | none => exact hfit
    | some w =>
      intro x hx
      rcases mem_updateWell wid _ x s.wells hx with hx0 | ⟨w0, hfw0, rfl⟩
      · exact hfit x hx0
      · exact hfit w0 (findWell_mem hfw0)
  | aspirate srcId v =>
    simp only [applyDeckStep]
    cases hsrc : findWell srcId s.wells with
    | none => exact hfit
    | some src =>
      intro x hx
      rcases mem_updateWell srcId (takeFromWell v) x s.wells hx with hx0 | ⟨w0, hfw0, rfl⟩
      · exact hfit x hx0
      · have hsfit := hfit w0 (findWell_mem hfw0)
        rw [takeFromWell_vol, takeFromWell_cap]
        omega
  | dispense dstId v air =>
    simp only [checkDeckStep] at hok
    cases hdst : findWell dstId s.wells with
    | none => simp [hdst] at hok
    | some dst =>
      simp only [hdst, firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
      rcases hok with ⟨_, _, _, _, _, _, hcap⟩
      simp only [applyDeckStep, hdst]
      intro x hx
      rcases mem_updateWell dstId _ x s.wells hx with hx0 | ⟨w0, hfw0, rfl⟩
      · exact hfit x hx0
      · rw [hdst] at hfw0; cases hfw0
        simpa [addBarrelToWell] using hcap
  | transfer srcId dstId v =>
    simp only [checkDeckStep] at hok
    cases hsrc : findWell srcId s.wells with
    | none => simp [hsrc] at hok
    | some src =>
      cases hdst : findWell dstId s.wells with
      | none => simp [hsrc, hdst] at hok
      | some dst =>
        simp only [hsrc, hdst, firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
        rcases hok with ⟨hne, _, _, _, _, _, _, _, _, hcap⟩
        simp only [applyDeckStep, hsrc, hdst]
        intro x hx
        have hdst2 : findWell dstId (updateWell srcId (takeFromWell v) s.wells) = some dst := by
          rw [findWell_update_ne srcId dstId (takeFromWell v) (takeFromWell_wellId v) hne]
          exact hdst
        rcases mem_updateWell dstId (addToWell src v (tipTouchFlags s.tip).1 (tipTouchFlags s.tip).2) x _ hx with hx1 | ⟨w0, hfw0, rfl⟩
        · rcases mem_updateWell srcId (takeFromWell v) x s.wells hx1 with hx0 | ⟨w1, hfw1, rfl⟩
          · exact hfit x hx0
          · rw [hsrc] at hfw1; cases hfw1
            have hsfit := hfit src (findWell_mem hsrc)
            rw [takeFromWell_vol, takeFromWell_cap]
            omega
        · rw [hdst2] at hfw0; cases hfw0
          simpa [addToWell] using hcap

theorem runRehearsalSteps_preserves_capacity (s sEnd : DeckState) (steps : List RehearsalStep)
    (hfit : AllWellsFit s.wells) (hr : runRehearsalSteps s steps = .ok sEnd) :
    AllWellsFit sEnd.wells := by
  induction steps generalizing s with
  | nil =>
    simp [runRehearsalSteps] at hr
    subst hr
    exact hfit
  | cons st rest ih =>
    simp only [runRehearsalSteps] at hr
    cases hck : checkDeckStep s st with
    | some v => simp [hck] at hr
    | none =>
      simp only [hck] at hr
      exact ih (applyDeckStep s st) (step_preserves_capacity s st hck hfit) hr

/-- Universal capacity, conservation, and tip-cleanup theorem for accepted rehearsals. -/
theorem checkRehearsal_preserves_capacity (s sEnd : DeckState) (protoHash : Nat)
    (steps : List RehearsalStep)
    (hok : checkRehearsal s protoHash steps = .ok sEnd) :
    AllWellsFit sEnd.wells ∧ totalWellsVol sEnd.wells = totalWellsVol s.wells ∧ sEnd.tip = .noTip := by
  unfold checkRehearsal at hok
  cases hi : initDeckOK s protoHash with
  | some v => simp [hi] at hok
  | none =>
    simp only [hi] at hok
    unfold initDeckOK at hi
    simp only [firstViolation_cons, firstViolation_nil, and_true, List.all_eq_true, decide_eq_true_eq] at hi
    rcases hi with ⟨_, htip0, _, hinitAll, _⟩
    have hfit0 : AllWellsFit s.wells := fun w hw => (hinitAll w hw).1
    have htipEq0 : s.tip = .noTip := eq_of_beq htip0
    cases hr : runRehearsalSteps s steps with
    | error v => simp [hr] at hok
    | ok sMid =>
      simp only [hr] at hok
      by_cases htip : sMid.tip == .noTip
      · simp [htip] at hok
        subst hok
        have hendEq : sMid.tip = .noTip := eq_of_beq htip
        exact ⟨runRehearsalSteps_preserves_capacity s sMid steps hfit0 hr,
               runRehearsalSteps_conserves s sMid steps htipEq0 hendEq hr,
               hendEq⟩
      · simp [htip] at hok

/-- Universal solute conservation and non-increase theorem for accepted rehearsals. -/
theorem checkRehearsal_conserves_and_bounds_solute (s sEnd : DeckState) (protoHash : Nat)
    (steps : List RehearsalStep)
    (hok : checkRehearsal s protoHash steps = .ok sEnd) :
    totalWellsSolute sEnd.wells = totalWellsSolute s.wells ∧
    totalWellsSolute sEnd.wells ≤ totalWellsSolute s.wells := by
  unfold checkRehearsal at hok
  cases hi : initDeckOK s protoHash with
  | some v => simp [hi] at hok
  | none =>
    simp only [hi] at hok
    unfold initDeckOK at hi
    simp only [firstViolation_cons, firstViolation_nil, and_true, List.all_eq_true, decide_eq_true_eq] at hi
    rcases hi with ⟨_, htip0, _, hinitAll, ⟨hpmin, _⟩⟩
    have hz0 : AllWellsZeroEmptySolute s.wells := fun w hw => (hinitAll w hw).2.2
    have htipEq0 : s.tip = .noTip := eq_of_beq htip0
    have htwf0 : TipWellFormed s.tip := by rw [htipEq0]; intro _; rfl
    cases hr : runRehearsalSteps s steps with
    | error v => simp [hr] at hok
    | ok sMid =>
      simp only [hr] at hok
      by_cases htip : sMid.tip == .noTip
      · simp [htip] at hok
        subst hok
        have hendEq : sMid.tip = .noTip := eq_of_beq htip
        have hsol := runRehearsalSteps_conserves_solute s sMid steps hpmin htwf0 hz0 hr
        simp [htipEq0, hendEq, TipStatus.solute] at hsol
        exact ⟨hsol, Nat.le_of_eq hsol⟩
      · simp [htip] at hok

/-- Bidirectional concentration theorem (high -> low): concentrated tip jumping into pre-diluted well is blocked. -/
theorem concentrated_tip_blocked_on_diluted_well
    (pv ps lVol lSol : Nat) (snaps : List ConcSnapshot) (tBuf tSamp : Bool) (w : WellState)
    (hlpos : 0 < lVol ∧ 0 < lSol ∧ 0 < w.vol)
    (hjump : w.soluteAmt * lVol * (1000 + concTolPermille) < lSol * w.vol * 1000) :
    tipMayEnterWell (.usedTip pv ps lVol lSol snaps tBuf tSamp) w = false := by
  rcases hlpos with ⟨hlv, hls, _⟩
  have hlast : lastConcAllowsWell lVol lSol w = false := by
    unfold lastConcAllowsWell
    rw [decide_eq_false_iff_not]
    rintro (h1 | h2 | ⟨_, hle⟩)
    · omega
    · omega
    · exact Nat.not_le_of_gt hjump hle
  simp [tipMayEnterWell, hlast]

/-- Bidirectional concentration theorem (low -> high): diluted tip re-entering higher-concentration well is blocked. -/
theorem diluted_tip_blocked_on_concentrated_well
    (pv ps lVol lSol : Nat) (sn : ConcSnapshot) (snaps : List ConcSnapshot)
    (tBuf tSamp : Bool) (w : WellState)
    (hmem : sn ∈ snaps)
    (hspos : 0 < sn.vol ∧ 0 < sn.solute ∧ 0 < w.vol)
    (hrev : sn.solute * w.vol * (1000 + concTolPermille) < w.soluteAmt * sn.vol * 1000) :
    tipMayEnterWell (.usedTip pv ps lVol lSol snaps tBuf tSamp) w = false := by
  rcases hspos with ⟨hsv, hss, hwv⟩
  have hsn : snapshotAllowsWell sn w = false := by
    unfold snapshotAllowsWell
    rw [decide_eq_false_iff_not]
    rintro (h1 | h2 | h3 | hle)
    · omega
    · omega
    · omega
    · exact Nat.not_le_of_gt hrev hle
  have hall : snaps.all (fun x => snapshotAllowsWell x w) = false := by
    rw [List.all_eq_false]
    exact ⟨sn, hmem, by simp [hsn]⟩
  simp [tipMayEnterWell, hall]

/-- Episodic recall fast-deny theorem: any protocol hash present in `failedHashes` is rejected. -/
theorem failed_hash_always_blocked (s : DeckState) (protoHash : Nat) (steps : List RehearsalStep)
    (hseen : protoHash ∈ s.failedHashes) :
    ∀ sEnd, checkRehearsal s protoHash steps ≠ .ok sEnd := by
  intro sEnd hok
  unfold checkRehearsal initDeckOK at hok
  simp [firstViolation, hseen] at hok

/-! ## 2. Idea #10: Rusty-SOP Freshness & Deck-Epoch Rehearsal Gate -/

structure SopFreshnessSpec where
  daysSinceLastRun     : Nat
  maxStaleDays         : Nat
  validatedDeckEpoch   : Nat
  liveDeckEpoch        : Nat
  bottomTraceRefreshed : Bool
  microRehearsalPassed : Bool
  deriving DecidableEq, Repr

def sopIsRusty (p : SopFreshnessSpec) : Bool :=
  decide (p.maxStaleDays < p.daysSinceLastRun ∨ p.validatedDeckEpoch ≠ p.liveDeckEpoch)

def checkRustySopGate (p : SopFreshnessSpec) : Option RegimeViolation :=
  if sopIsRusty p then
    firstViolation
      [ (p.microRehearsalPassed && p.bottomTraceRefreshed,
          { rule := "rusty_sop_requires_micro_rehearsal",
            detail := "stale or epoch-shifted SOP requires micro-rehearsal and Bottom Reasoning Trace refresh" }) ]
  else
    none

theorem stale_or_epoch_shifted_sop_blocked (p : SopFreshnessSpec)
    (hrusty : p.maxStaleDays < p.daysSinceLastRun ∨ p.validatedDeckEpoch ≠ p.liveDeckEpoch)
    (hunrefreshed : p.microRehearsalPassed = false ∨ p.bottomTraceRefreshed = false) :
    checkRustySopGate p ≠ none := by
  intro hok
  have hr : sopIsRusty p = true := decide_eq_true hrusty
  unfold checkRustySopGate at hok
  simp only [hr, ↓reduceIte, firstViolation_cons, firstViolation_nil, and_true, Bool.and_eq_true] at hok
  rcases hunrefreshed with hm | hb
  · rw [hm] at hok; simp at hok
  · rw [hb] at hok; simp at hok

/-! ## 3. Idea #11: 4-Way Causal Attribution, Retry Routing & Negative-Result Ledger -/

structure AssayTelemetry where
  preGateCertified          : Bool
  acousticNormal            : Bool
  scheduleWindowsRespected  : Bool
  tactileSeatedCleanly      : Bool
  posControlPassed          : Bool
  negControlBlankClean      : Bool
  spectralResidualPermille  : Nat
  maxResidualPermille       : Nat
  precursorOverlapPermille  : Nat
  maxOverlapPermille        : Nat
  signalAboveActivityThresh : Bool
  deriving DecidableEq, Repr

inductive AttributionOutcome where
  | executionArtifact
  | ambiguousCharacterization
  | certifiedPositive
  | certifiedNegative
  deriving DecidableEq, Repr

def executionClean (t : AssayTelemetry) : Bool :=
  t.preGateCertified &&
  t.acousticNormal &&
  t.scheduleWindowsRespected &&
  t.tactileSeatedCleanly &&
  t.posControlPassed &&
  t.negControlBlankClean

def characterizationIdentifiable (t : AssayTelemetry) : Bool :=
  decide (t.spectralResidualPermille ≤ t.maxResidualPermille ∧
          t.precursorOverlapPermille ≤ t.maxOverlapPermille)

def attributeAssayOutcome (t : AssayTelemetry) : AttributionOutcome :=
  if !executionClean t then
    .executionArtifact
  else if !characterizationIdentifiable t then
    .ambiguousCharacterization
  else if t.signalAboveActivityThresh then
    .certifiedPositive
  else
    .certifiedNegative

def admitToScientificMemory (o : AttributionOutcome) : Bool :=
  match o with
  | .certifiedPositive => true
  | .certifiedNegative => true
  | _                  => false

/-- Reconciled fast-deny policy distinguishing deterministic safety failures from bounded robot retries. -/
def shouldFastDenyRetry
    (hasDeterministicVerifyFailure : Bool)
    (latestOutcome : Option AttributionOutcome)
    (consecArtifacts maxRetries : Nat) : Bool :=
  if hasDeterministicVerifyFailure then
    true
  else
    match latestOutcome with
    | some .executionArtifact => decide (maxRetries < consecArtifacts)
    | some .ambiguousCharacterization => true
    | some .certifiedPositive => false
    | some .certifiedNegative => false
    | none => false

theorem deterministic_failure_always_fast_denied
    (o : Option AttributionOutcome) (c m : Nat) :
    shouldFastDenyRetry true o c m = true := rfl

theorem bounded_execution_artifact_permitted_to_retry
    (c m : Nat) (hb : c ≤ m) :
    shouldFastDenyRetry false (some .executionArtifact) c m = false := by
  simp [shouldFastDenyRetry]
  omega

theorem corrupted_run_never_poisons_memory (t : AssayTelemetry)
    (hfault : t.preGateCertified = false ∨ t.acousticNormal = false ∨
              t.scheduleWindowsRespected = false ∨ t.tactileSeatedCleanly = false ∨
              t.posControlPassed = false ∨ t.negControlBlankClean = false) :
    attributeAssayOutcome t = .executionArtifact ∧
    admitToScientificMemory (attributeAssayOutcome t) = false := by
  have hex : executionClean t = false := by
    unfold executionClean
    rcases hfault with h | h | h | h | h | h <;> simp [h]
  unfold attributeAssayOutcome
  simp [hex, admitToScientificMemory]

theorem certified_negative_sound (t : AssayTelemetry)
    (hneg : attributeAssayOutcome t = .certifiedNegative) :
    executionClean t = true ∧
    t.spectralResidualPermille ≤ t.maxResidualPermille ∧
    t.precursorOverlapPermille ≤ t.maxOverlapPermille ∧
    t.signalAboveActivityThresh = false := by
  unfold attributeAssayOutcome at hneg
  cases hex : executionClean t with
  | false => simp [hex] at hneg
  | true =>
    simp only [hex] at hneg
    cases hid : characterizationIdentifiable t with
    | false => simp [hid] at hneg
    | true =>
      simp only [hid] at hneg
      cases hsig : t.signalAboveActivityThresh with
      | true => simp [hsig] at hneg
      | false =>
        unfold characterizationIdentifiable at hid
        have ⟨hr, ho⟩ := of_decide_eq_true hid
        exact ⟨rfl, hr, ho, rfl⟩

/-! ## Compile-Time `#guard` Verification Witnesses -/

private def sampleDeck : DeckState :=
  { wells := [ { wellId := 1, vol := 1000, cap := 2000, deadVol := 100, soluteAmt := 500,
                 hasBuffer := true, hasSample := true, frozen := true, sealed := false },
               { wellId := 2, vol := 500, cap := 2000, deadVol := 100, soluteAmt := 0,
                 hasBuffer := true, hasSample := false, frozen := false, sealed := false } ],
    tip := .noTip,
    pipMin := 10,
    pipMax := 1000,
    failedHashes := [999] }

#guard (checkRehearsal sampleDeck 42
         [.thawWell 1, .pickTip, .transfer 1 2 200, .dropTip, .sealWell 2, .incubate 2 90 60]).isOk == true

#guard (checkRehearsal sampleDeck 999 [.pickTip, .dropTip]).isOk == false

#guard (checkRehearsal sampleDeck 42
         [.thawWell 1, .pickTip, .transfer 1 2 200, .dropTip, .incubate 2 90 60]).isOk == false

-- Multi-dispense witness: aspirate 200.0 uL -> air-dispense 100.0 uL into two wells cleanly
private def multiDispDeck : DeckState :=
  { wells := [ { wellId := 1, vol := 10000, cap := 150000, deadVol := 5000, soluteAmt := 0,
                 hasBuffer := true, hasSample := false, frozen := false, sealed := false,
                 isReagentStock := true },
               { wellId := 2, vol := 500, cap := 3000, deadVol := 50, soluteAmt := 500,
                 hasBuffer := false, hasSample := true, frozen := false, sealed := false },
               { wellId := 3, vol := 500, cap := 3000, deadVol := 50, soluteAmt := 0,
                 hasBuffer := true, hasSample := false, frozen := false, sealed := false } ],
    tip := .noTip,
    pipMin := 200,
    pipMax := 3000,
    failedHashes := [] }

#guard (checkRehearsal multiDispDeck 7
         [.pickTip, .aspirate 1 2000, .dispense 2 1000 true, .dispense 3 1000 true, .dropTip]).isOk == true

-- Submerged multi-dispense without airDispense carries sample residue from well 2 into well 3 -> blocked!
#guard (checkRehearsal multiDispDeck 7
         [.pickTip, .aspirate 1 2000, .dispense 2 1000 false, .dispense 3 1000 false, .dropTip]).isOk == false

-- Jump-down concentration check: mixing high-conc well 1 (1:2) then low-conc well 2 (1:10) without transfer -> blocked!
private def jumpConcDeck : DeckState :=
  { wells := [ { wellId := 1, vol := 1000, cap := 3000, deadVol := 50, soluteAmt := 500,
                 hasBuffer := true, hasSample := true, frozen := false, sealed := false },
               { wellId := 2, vol := 1000, cap := 3000, deadVol := 50, soluteAmt := 100,
                 hasBuffer := true, hasSample := true, frozen := false, sealed := false } ],
    tip := .noTip,
    pipMin := 200,
    pipMax := 3000,
    failedHashes := [] }

#guard (checkRehearsal jumpConcDeck 8
         [.pickTip, .mix 1 500 2, .mix 2 500 2, .dropTip]).isOk == false

-- Reverse-dilution check: mixing low-conc well 2 (1:10) then high-conc well 1 (1:2) -> blocked!
#guard (checkRehearsal jumpConcDeck 9
         [.pickTip, .mix 2 500 2, .mix 1 500 2, .dropTip]).isOk == false

#guard (checkRustySopGate
         { daysSinceLastRun := 30, maxStaleDays := 14, validatedDeckEpoch := 3,
           liveDeckEpoch := 3, bottomTraceRefreshed := false, microRehearsalPassed := false }).isSome == true

#guard (checkRustySopGate
         { daysSinceLastRun := 30, maxStaleDays := 14, validatedDeckEpoch := 3,
           liveDeckEpoch := 4, bottomTraceRefreshed := true, microRehearsalPassed := true }).isNone == true

#guard attributeAssayOutcome
         { preGateCertified := true, acousticNormal := true, scheduleWindowsRespected := true,
           tactileSeatedCleanly := true, posControlPassed := true, negControlBlankClean := true,
           spectralResidualPermille := 40, maxResidualPermille := 100,
           precursorOverlapPermille := 25, maxOverlapPermille := 120,
           signalAboveActivityThresh := false } == .certifiedNegative

end LabRehearsalLean
