# LabRehearsalLean (Lean 4.36.0 Stateful Twin, Rusty-SOP & Attribution Proofs)

`LabRehearsalLean` provides machine-checked L4 wet-lab rehearsal invariants in Lean 4 (`v4.36.0`), with zero `sorry`, zero custom `axiom` declarations, and zero compiler warnings.

## Covered Invariants and Theorems

1. **Stateful Deck + Pipette Barrel Twin & Conservation Laws ([LabRehearsalLean.lean](LabRehearsalLean.lean))**
   - `WellState` (`DeckWell`), `ConcSnapshot`, `TipStatus` (tracking barrel `pipVol`, `pipSolute`, `lastVol`, `lastSolute`, cumulative `snapshots`, and reagent flags), `DeckState`, and `RehearsalStep` (`.pickTip`, `.dropTip`, `.aspirate`, `.dispense`, `.transfer`, `.mix`, `.incubate`, `.sealWell`, `.thawWell`).
   - `step_conserves_deck_and_barrel` & `runRehearsalSteps_conserves_deck_and_barrel`: prove exact deck + pipette barrel volume conservation (`totalWellsVol s.wells + s.tip.vol = const`) across multi-dispense and multi-aspirate sequences.
   - `checkRehearsal_preserves_capacity`: proves universal well capacity (`vol <= cap`), deck liquid conservation, and tip-drop cleanup.
   - `step_conserves_total_solute` & `checkRehearsal_conserves_and_bounds_solute`: prove exact solute conservation and non-increase of `totalWellsSolute`.
   - `concentrated_tip_blocked_on_diluted_well` & `diluted_tip_blocked_on_concentrated_well`: prove bidirectional concentration monotonicity with `concTolPermille = 1`.
   - `failed_hash_always_blocked`: proves episodic recall fast-deny before step 0.
2. **Rusty-SOP Freshness & Deck-Epoch Gate (Idea #10)**
   - `SopFreshnessSpec`, `checkRustySopGate`, and `stale_or_epoch_shifted_sop_blocked`.
3. **4-Way Causal Attribution, Artifact Retry Routing & Negative-Result Ledger (Idea #11)**
   - `AssayTelemetry`, `AttributionOutcome`, `attributeAssayOutcome`, `admitToScientificMemory`, `shouldFastDenyRetry`.
   - `deterministic_failure_always_fast_denied`, `bounded_execution_artifact_permitted_to_retry`, `corrupted_run_never_poisons_memory`, and `certified_negative_sound`.

## Build and Verify

```bash
cd lean
lake build
```
