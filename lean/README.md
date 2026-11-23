# LabRehearsalLean (Lean 4.36.0 Stateful Twin, Rusty-SOP & Attribution Proofs)

`LabRehearsalLean` provides machine-checked L4 wet-lab rehearsal invariants in Lean 4 (`v4.36.0`), with zero `sorry` and zero custom `axiom` declarations.

## Covered Invariants and Theorems

1. **Stateful Deck Twin & Conservation Laws ([LabRehearsalLean.lean](LabRehearsalLean.lean))**
   - `WellState` (`DeckWell`), `TipStatus`, `DeckState`, and `RehearsalStep` (`.pickTip`, `.dropTip`, `.transfer`, `.mix`, `.incubate`, `.sealWell`, `.thawWell`).
   - `runRehearsalSteps_conserves`: proves exact deck-wide liquid volume conservation across arbitrary `N`-step protocols.
   - `checkRehearsal_preserves_capacity`: proves universal well capacity (`vol <= cap`), liquid conservation, and tip-drop cleanup.
   - `failed_hash_always_blocked`: proves episodic recall fast-deny before step 0.
2. **Rusty-SOP Freshness & Deck-Epoch Gate (Idea #10)**
   - `SopFreshnessSpec`, `checkRustySopGate`, and `stale_or_epoch_shifted_sop_blocked`.
3. **4-Way Causal Attribution & Certified Negative Result Ledger (Idea #11)**
   - `AssayTelemetry`, `AttributionOutcome`, `attributeAssayOutcome`, `admitToScientificMemory`.
   - `corrupted_run_never_poisons_memory` and `certified_negative_sound`.

## Build and Verify

```bash
cd lean
lake build
```
