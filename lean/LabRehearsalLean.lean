/-!
# LabRehearsalLean: Formal L4 Wet-Lab Digital Twin, Rusty-SOP Gate & Negative-Result Ledger

Self-contained Lean 4.36.0 formalization for `lab-rehearsal` (L4 Stateful Wet-Lab Rehearsal
Twin & Epistemic Memory Layer). All liquid volumes are represented in tenths of a microliter
(`0.1 uL`) so conservation, well-capacity bounds, dead-volume reserves, cross-contamination
lineage, rusty-SOP epoch checks, and 4-way causal attribution are decidable without rounding.
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

/-! ## 1. Stateful Deck Twin, Volume Conservation & Capacity Invariants -/

/-- State of an individual well on the rehearsal deck (tenths of uL `0.1 uL`). -/
structure WellState where
  wellId    : Nat
  vol       : Nat
  cap       : Nat
  deadVol   : Nat
  soluteAmt : Nat
  hasBuffer : Bool
  hasSample : Bool
  frozen    : Bool
  sealed    : Bool
  deriving DecidableEq, Repr

abbrev DeckWell := WellState

inductive TipStatus where
  | noTip
  | cleanTip
  | usedTip (lastVol : Nat) (lastSolute : Nat) (touchedBuffer : Bool) (touchedSample : Bool)
  deriving DecidableEq, Repr

structure DeckState where
  wells        : List WellState
  tip          : TipStatus
  pipMin       : Nat
  pipMax       : Nat
  failedHashes : List Nat
  deriving DecidableEq, Repr

/-- Primitive wet-lab rehearsal step (uses `.sealWell` / `.thawWell`, avoiding Lean tactic keywords). -/
inductive RehearsalStep where
  | pickTip
  | dropTip
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

def tipMayEnterWell (t : TipStatus) (w : WellState) : Bool :=
  match t with
  | .noTip => false
  | .cleanTip => true
  | .usedTip lVol lSol tBuf tSamp =>
    let classOK := (!tSamp || w.hasSample) && (!tBuf || (w.hasBuffer || w.hasSample))
    let concOK  := if lSol == 0 then true
                   else decide (0 < lVol ∧ 0 < w.vol ∧ w.soluteAmt * lVol ≤ lSol * w.vol)
    classOK && concOK

def takeFromWell (v : Nat) (w : WellState) : WellState :=
  let movedSol := if w.vol == 0 then 0 else (w.soluteAmt * v) / w.vol
  { w with vol := w.vol - v, soluteAmt := w.soluteAmt - movedSol }

def addToWell (src : WellState) (v : Nat) (w : WellState) : WellState :=
  let movedSol := if src.vol == 0 then 0 else (src.soluteAmt * v) / src.vol
  { w with
    vol := w.vol + v,
    soluteAmt := w.soluteAmt + movedSol,
    hasBuffer := w.hasBuffer || src.hasBuffer,
    hasSample := w.hasSample || src.hasSample }

def stepTipAfterWell (w : WellState) : TipStatus :=
  .usedTip w.vol w.soluteAmt w.hasBuffer w.hasSample

def checkDeckStep (s : DeckState) (st : RehearsalStep) : Option RegimeViolation :=
  match st with
  | .pickTip =>
    if s.tip == .noTip then none
    else some { rule := "pick_tip_requires_empty_mount",
                detail := "cannot pick_up_tip while a tip is already mounted" }
  | .dropTip =>
    if s.tip != .noTip then none
    else some { rule := "drop_tip_requires_mounted_tip",
                detail := "cannot drop_tip when no tip is mounted" }
  | .transfer srcId dstId v =>
    match findWell srcId s.wells, findWell dstId s.wells with
    | some src, some dst =>
      firstViolation
        [ (decide (srcId ≠ dstId),
            { rule := "distinct_transfer_wells",
              detail := "transfer source and destination must be distinct wells" }),
          (decide (s.pipMin ≤ v ∧ v ≤ s.pipMax),
            { rule := "pipette_volume_range",
              detail := "transfer volume outside pipette [pipMin, pipMax] bounds" }),
          (decide (!src.frozen && !dst.frozen),
            { rule := "thawed_before_pipetting",
              detail := "cannot aspirate from or dispense into a frozen container" }),
          (decide (!src.sealed && !dst.sealed),
            { rule := "unsealed_before_pipetting",
              detail := "cannot pipette through a sealed plate" }),
          (tipMayEnterWell s.tip src,
            { rule := "no_tip_cross_contamination",
              detail := "used tip violates reagent lineage or serial-dilution concentration order" }),
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
          (decide (!w.frozen && !w.sealed),
            { rule := "mix_open_thawed_well",
              detail := "cannot mix a frozen or sealed well" }),
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
  | .transfer srcId dstId v =>
    match findWell srcId s.wells, findWell dstId s.wells with
    | some src, some _ =>
      let w1 := updateWell srcId (takeFromWell v) s.wells
      let w2 := updateWell dstId (addToWell src v) w1
      let dstAfter := (findWell dstId w2).getD src
      { s with wells := w2, tip := stepTipAfterWell dstAfter }
    | _, _ => s
  | .mix wid _ _ =>
    match findWell wid s.wells with
    | some w => { s with tip := stepTipAfterWell w }
    | none   => s
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
      (wellIdsUnique s.wells,
        { rule := "unique_well_ids",
          detail := "initial deck contains duplicate well IDs" }),
      (s.wells.all (fun w => decide (w.vol ≤ w.cap ∧ w.deadVol ≤ w.cap)),
        { rule := "initial_state_within_capacity",
          detail := "initial deck state violates 0 <= vol <= cap" }),
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

theorem step_conserves_total_liquid (s : DeckState) (st : RehearsalStep)
    (hok : checkDeckStep s st = none) :
    totalWellsVol (applyDeckStep s st).wells = totalWellsVol s.wells := by
  cases st with
  | pickTip => rfl
  | dropTip => rfl
  | mix wid v reps =>
    simp only [applyDeckStep]
    cases findWell wid s.wells <;> rfl
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
  | transfer srcId dstId v =>
    simp only [checkDeckStep] at hok
    cases hsrc : findWell srcId s.wells with
    | none => simp [hsrc] at hok
    | some src =>
      cases hdst : findWell dstId s.wells with
      | none => simp [hsrc, hdst] at hok
      | some dst =>
        simp only [hsrc, hdst, firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
        rcases hok with ⟨hne, _, _, _, _, hen, _⟩
        simp only [applyDeckStep, hsrc, hdst]
        have h1 := totalWellsVol_update srcId (takeFromWell v) src s.wells hsrc
        have hdst2 : findWell dstId (updateWell srcId (takeFromWell v) s.wells) = some dst := by
          rw [findWell_update_ne srcId dstId (takeFromWell v) (fun _ => rfl) hne]
          exact hdst
        have h2 := totalWellsVol_update dstId (addToWell src v) dst
                    (updateWell srcId (takeFromWell v) s.wells) hdst2
        simp only [takeFromWell, addToWell] at h1 h2
        omega

/-- Multi-step liquid conservation: any accepted `N`-step rehearsal conserves total deck liquid! -/
theorem runRehearsalSteps_conserves (s sEnd : DeckState) (steps : List RehearsalStep)
    (hrun : runRehearsalSteps s steps = .ok sEnd) :
    totalWellsVol sEnd.wells = totalWellsVol s.wells := by
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
      rw [ih (applyDeckStep s st) hrun, step_conserves_total_liquid s st hck]

def AllWellsFit (ws : List WellState) : Prop := ∀ w ∈ ws, w.vol ≤ w.cap

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

theorem step_preserves_capacity (s : DeckState) (st : RehearsalStep)
    (hok : checkDeckStep s st = none) (hfit : AllWellsFit s.wells) :
    AllWellsFit (applyDeckStep s st).wells := by
  cases st with
  | pickTip => exact hfit
  | dropTip => exact hfit
  | mix wid v reps =>
    simp only [applyDeckStep]
    cases findWell wid s.wells <;> exact hfit
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
  | transfer srcId dstId v =>
    simp only [checkDeckStep] at hok
    cases hsrc : findWell srcId s.wells with
    | none => simp [hsrc] at hok
    | some src =>
      cases hdst : findWell dstId s.wells with
      | none => simp [hsrc, hdst] at hok
      | some dst =>
        simp only [hsrc, hdst, firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
        rcases hok with ⟨hne, _, _, _, _, _, hcap⟩
        simp only [applyDeckStep, hsrc, hdst]
        intro x hx
        have hdst2 : findWell dstId (updateWell srcId (takeFromWell v) s.wells) = some dst := by
          rw [findWell_update_ne srcId dstId (takeFromWell v) (fun _ => rfl) hne]
          exact hdst
        rcases mem_updateWell dstId (addToWell src v) x _ hx with hx1 | ⟨w0, hfw0, rfl⟩
        · rcases mem_updateWell srcId (takeFromWell v) x s.wells hx1 with hx0 | ⟨w1, hfw1, rfl⟩
          · exact hfit x hx0
          · rw [hsrc] at hfw1; cases hfw1
            have hsfit := hfit src (findWell_mem hsrc)
            simp only [takeFromWell]; omega
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
    rcases hi with ⟨_, _, hinitAll, _⟩
    have hfit0 : AllWellsFit s.wells := fun w hw => (hinitAll w hw).1
    cases hr : runRehearsalSteps s steps with
    | error v => simp [hr] at hok
    | ok sMid =>
      simp only [hr] at hok
      by_cases htip : sMid.tip == .noTip
      · simp [htip] at hok
        subst hok
        exact ⟨runRehearsalSteps_preserves_capacity s sMid steps hfit0 hr,
               runRehearsalSteps_conserves s sMid steps hr,
               eq_of_beq htip⟩
      · simp [htip] at hok

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

/-! ## 3. Idea #11: 4-Way Causal Attribution & Proof-Carrying Negative Result Ledger -/

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
