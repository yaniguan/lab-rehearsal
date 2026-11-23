"""Idea #10: Context-Adaptive Digital Twin (CALM-DT) & Rusty-SOP Rehearsal Gate.

Inspired by CALM-DT context-adaptive twin calibration and Anthropic's Components of
Agent Memory. Standard wet-lab digital twins assume static water-calibrated dead-volume
and evaporation constants, drifting badly on viscous or heated reagents (glycerol, PEG,
DMSO, ethanol, serum) and failing silently when an SOP sits untouched across deck
reconfigurations ("rusty SOPs"). This module provides:
  1. Online Bayesian + EWMA per-liquid calibration of effective dead-volume and thermal
     evaporation rate directly from gravimetric/optical post-run readouts (zero retraining).
  2. A Rusty-SOP Rehearsal Gate blocking full-scale execution whenever an SOP exceeds
     `stale_days` or encounters a new `deck_epoch` until micro-rehearsal and Bottom
     Reasoning Trace refresh succeed.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass
from typing import Any


@dataclass
class LiquidPosteriorState:
    """Conjugate Bayesian + EWMA online parameter estimate for a specific liquid class."""

    liquid_name: str
    dead_vol_mean_ul: float = 10.0
    dead_vol_var: float = 36.0
    evap_coeff_mean_ul_per_unit: float = 0.08
    evap_coeff_var: float = 0.25
    ewma_residual_ul: float = 0.0
    observations: int = 0


@dataclass(frozen=True)
class BottomReasoningTrace:
    """Grounded physical precondition trace bound to a specific SOP and deck_epoch."""

    sop_id: str
    liquid_name: str
    deck_epoch: int
    calibrated_dead_vol_ul: float
    calibrated_evap_coeff: float
    trace_sha256: str


@dataclass
class SopMemoryRecord:
    """Episodic freshness state for a registered laboratory SOP."""

    sop_id: str
    liquid_name: str
    last_validated_day: int
    validated_deck_epoch: int
    bottom_trace: BottomReasoningTrace | None = None


@dataclass(frozen=True)
class RustySopDecision:
    """Gate decision mirroring LabRehearsalLean.checkRustySopGate."""

    sop_id: str
    is_rusty: bool
    allowed_full_scale: bool
    rule_triggered: str | None
    refreshed_trace: BottomReasoningTrace | None


def compute_thermal_exposure(temp_c: float, open_duration_min: float) -> float:
    """Effective thermal evaporation exposure unit for heated open-well operations."""
    thermal_factor = 1.0 + max(0.0, temp_c - 20.0) / 18.0
    return open_duration_min * thermal_factor


class StaticNominalTwin:
    """Baseline digital twin using fixed nominal aqueous dead-volume and evaporation constants."""

    def __init__(
        self,
        nominal_dead_vol_ul: float = 10.0,
        nominal_evap_coeff: float = 0.08,
    ) -> None:
        self.nominal_dead_vol_ul = nominal_dead_vol_ul
        self.nominal_evap_coeff = nominal_evap_coeff

    def predict_recoverable_volume_ul(
        self,
        initial_vol_ul: float,
        dispensed_total_ul: float,
        temp_c: float,
        open_duration_min: float,
    ) -> float:
        evap_loss = self.nominal_evap_coeff * compute_thermal_exposure(temp_c, open_duration_min)
        return max(0.0, initial_vol_ul - dispensed_total_ul - evap_loss - self.nominal_dead_vol_ul)


class CalmAdaptiveTwin:
    """Context-Adaptive Digital Twin (CALM-DT) with online Bayesian/EWMA residual updates
    and Rusty-SOP Rehearsal Gate."""

    def __init__(
        self,
        stale_days: int = 14,
        obs_noise_var_dead: float = 1.5,
        obs_noise_var_evap: float = 0.015,
        ewma_alpha: float = 0.30,
        process_drift_var: float = 0.20,
    ) -> None:
        self.stale_days = stale_days
        self.obs_noise_var_dead = obs_noise_var_dead
        self.obs_noise_var_evap = obs_noise_var_evap
        self.ewma_alpha = ewma_alpha
        self.process_drift_var = process_drift_var
        self._liquids: dict[str, LiquidPosteriorState] = {}
        self._sops: dict[str, SopMemoryRecord] = {}

    def get_liquid_state(self, liquid_name: str) -> LiquidPosteriorState:
        if liquid_name not in self._liquids:
            self._liquids[liquid_name] = LiquidPosteriorState(liquid_name=liquid_name)
        return self._liquids[liquid_name]

    def register_sop(
        self,
        sop_id: str,
        liquid_name: str,
        validated_day: int,
        deck_epoch: int,
    ) -> SopMemoryRecord:
        trace = self._mint_bottom_trace(sop_id, liquid_name, deck_epoch)
        rec = SopMemoryRecord(
            sop_id=sop_id,
            liquid_name=liquid_name,
            last_validated_day=validated_day,
            validated_deck_epoch=deck_epoch,
            bottom_trace=trace,
        )
        self._sops[sop_id] = rec
        return rec

    def _mint_bottom_trace(
        self,
        sop_id: str,
        liquid_name: str,
        deck_epoch: int,
    ) -> BottomReasoningTrace:
        st = self.get_liquid_state(liquid_name)
        payload = json.dumps(
            {
                "sop_id": sop_id,
                "liquid_name": liquid_name,
                "deck_epoch": deck_epoch,
                "dead_vol_ul": round(st.dead_vol_mean_ul, 4),
                "evap_coeff": round(st.evap_coeff_mean_ul_per_unit, 5),
            },
            sort_keys=True,
        ).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        return BottomReasoningTrace(
            sop_id=sop_id,
            liquid_name=liquid_name,
            deck_epoch=deck_epoch,
            calibrated_dead_vol_ul=round(st.dead_vol_mean_ul, 4),
            calibrated_evap_coeff=round(st.evap_coeff_mean_ul_per_unit, 5),
            trace_sha256=digest,
        )

    def predict_recoverable_volume_ul(
        self,
        liquid_name: str,
        initial_vol_ul: float,
        dispensed_total_ul: float,
        temp_c: float,
        open_duration_min: float,
    ) -> float:
        st = self.get_liquid_state(liquid_name)
        exposure = compute_thermal_exposure(temp_c, open_duration_min)
        evap_loss = st.evap_coeff_mean_ul_per_unit * exposure
        pred = (
            initial_vol_ul
            - dispensed_total_ul
            - evap_loss
            - st.dead_vol_mean_ul
            + st.ewma_residual_ul
        )
        return max(0.0, pred)

    def predict_safe_recoverable_lcb_ul(
        self,
        liquid_name: str,
        initial_vol_ul: float,
        dispensed_total_ul: float,
        temp_c: float,
        open_duration_min: float,
        z_sigma: float = 2.5,
    ) -> float:
        """Posterior lower confidence bound (LCB) preventing viscous/heated air-draw faults."""
        mean_rec = self.predict_recoverable_volume_ul(
            liquid_name, initial_vol_ul, dispensed_total_ul, temp_c, open_duration_min
        )
        st = self.get_liquid_state(liquid_name)
        exposure = compute_thermal_exposure(temp_c, open_duration_min)
        total_var = (
            st.dead_vol_var
            + (exposure * exposure) * st.evap_coeff_var
            + self.obs_noise_var_dead
            + 0.25
        )
        return max(0.0, mean_rec - z_sigma * math.sqrt(total_var))

    def update_from_post_run_readout(
        self,
        liquid_name: str,
        initial_vol_ul: float,
        dispensed_total_ul: float,
        temp_c: float,
        open_duration_min: float,
        measured_recoverable_vol_ul: float,
        gravimetric_evap_loss_ul: float | None = None,
    ) -> LiquidPosteriorState:
        """Bayesian Kalman/precision + EWMA online update from post-run gravimetric/optical data."""
        st = self.get_liquid_state(liquid_name)
        exposure = max(1e-6, compute_thermal_exposure(temp_c, open_duration_min))

        # 1. Update thermal evaporation coefficient if gravimetric/optical mass loss is observed
        if gravimetric_evap_loss_ul is not None:
            obs_coeff = max(0.0, gravimetric_evap_loss_ul / exposure)
            prior_var_e = st.evap_coeff_var + 0.002
            kalman_e = prior_var_e / (prior_var_e + self.obs_noise_var_evap)
            st.evap_coeff_mean_ul_per_unit = (
                st.evap_coeff_mean_ul_per_unit + kalman_e * (obs_coeff - st.evap_coeff_mean_ul_per_unit)
            )
            st.evap_coeff_var = (1.0 - kalman_e) * prior_var_e

        # 2. Infer observed effective dead-volume (wall film + unrecoverable heel)
        est_evap = st.evap_coeff_mean_ul_per_unit * exposure
        obs_dead_vol = max(
            0.0,
            initial_vol_ul - dispensed_total_ul - est_evap - measured_recoverable_vol_ul,
        )
        prior_var_d = st.dead_vol_var + self.process_drift_var
        kalman_d = prior_var_d / (prior_var_d + self.obs_noise_var_dead)
        st.dead_vol_mean_ul = st.dead_vol_mean_ul + kalman_d * (obs_dead_vol - st.dead_vol_mean_ul)
        st.dead_vol_var = (1.0 - kalman_d) * prior_var_d

        # 3. Update bounded EWMA fine residual tracker for subtle ambient humidity drift
        post_pred = (
            initial_vol_ul
            - dispensed_total_ul
            - st.evap_coeff_mean_ul_per_unit * exposure
            - st.dead_vol_mean_ul
        )
        raw_res = measured_recoverable_vol_ul - post_pred
        st.ewma_residual_ul = (
            (1.0 - self.ewma_alpha) * st.ewma_residual_ul + self.ewma_alpha * raw_res
        )
        st.observations += 1
        return st

    def check_rusty_sop_gate(
        self,
        sop_id: str,
        current_day: int,
        live_deck_epoch: int,
        *,
        run_micro_rehearsal: bool = False,
        micro_rehearsal_obs: tuple[float, float, float, float, float, float] | None = None,
    ) -> RustySopDecision:
        """Mirror of LabRehearsalLean.checkRustySopGate with optional micro-rehearsal refresh."""
        if sop_id not in self._sops:
            return RustySopDecision(
                sop_id=sop_id,
                is_rusty=True,
                allowed_full_scale=False,
                rule_triggered="unregistered_sop_requires_rehearsal",
                refreshed_trace=None,
            )

        rec = self._sops[sop_id]
        days_since = max(0, current_day - rec.last_validated_day)
        is_rusty = (
            days_since > self.stale_days
            or rec.validated_deck_epoch != live_deck_epoch
            or rec.bottom_trace is None
        )
        if not is_rusty:
            return RustySopDecision(
                sop_id=sop_id,
                is_rusty=False,
                allowed_full_scale=True,
                rule_triggered=None,
                refreshed_trace=rec.bottom_trace,
            )

        if not run_micro_rehearsal:
            return RustySopDecision(
                sop_id=sop_id,
                is_rusty=True,
                allowed_full_scale=False,
                rule_triggered="rusty_sop_requires_micro_rehearsal",
                refreshed_trace=None,
            )

        # Rusty SOP (stale days or deck/labware epoch shift): inflate prior variance before micro-rehearsal
        st = self.get_liquid_state(rec.liquid_name)
        reset_var = 16.0 if rec.validated_deck_epoch != live_deck_epoch else 6.0
        st.dead_vol_var = max(st.dead_vol_var, reset_var)
        if rec.validated_deck_epoch != live_deck_epoch:
            st.ewma_residual_ul = 0.0

        if micro_rehearsal_obs is not None:
            init_v, disp_v, temp_c, dur_m, meas_rec, meas_evap = micro_rehearsal_obs
            self.update_from_post_run_readout(
                rec.liquid_name,
                init_v,
                disp_v,
                temp_c,
                dur_m,
                meas_rec,
                gravimetric_evap_loss_ul=meas_evap,
            )

        new_trace = self._mint_bottom_trace(sop_id, rec.liquid_name, live_deck_epoch)
        rec.last_validated_day = current_day
        rec.validated_deck_epoch = live_deck_epoch
        rec.bottom_trace = new_trace
        return RustySopDecision(
            sop_id=sop_id,
            is_rusty=True,
            allowed_full_scale=True,
            rule_triggered=None,
            refreshed_trace=new_trace,
        )


TRUE_LIQUID_PHYSICS: dict[str, tuple[float, float]] = {
    # (true_dead_vol_ul, true_evap_coeff_ul_per_unit)
    "glycerol_50pct": (28.5, 0.06),
    "peg_400": (25.0, 0.05),
    "dmso_90pct": (16.5, 0.14),
    "ethanol_70pct": (14.0, 0.42),
    "whole_serum": (21.5, 0.16),
}


def run_benchmark(num_runs: int = 50, seed: int = 2026) -> dict[str, Any]:
    """Compare static_nominal_twin vs calm_adaptive_twin across 50 viscous/heated wet-lab runs."""
    rng = random.Random(seed)
    static_twin = StaticNominalTwin(nominal_dead_vol_ul=10.0, nominal_evap_coeff=0.08)
    calm_twin = CalmAdaptiveTwin(stale_days=14)

    liquid_names = list(TRUE_LIQUID_PHYSICS.keys())
    for idx, liq in enumerate(liquid_names):
        calm_twin.register_sop(f"SOP-{idx + 1:02d}", liq, validated_day=0, deck_epoch=1)

    static_abs_errors: list[float] = []
    static_sq_errors: list[float] = []
    calm_abs_errors: list[float] = []
    calm_sq_errors: list[float] = []

    rusty_events_total = 0
    static_rusty_faults = 0
    calm_rusty_interceptions = 0
    calm_rusty_faults = 0

    current_day = 1
    live_epoch = 1

    for run_idx in range(num_runs):
        liq_idx = run_idx % len(liquid_names)
        liq = liquid_names[liq_idx]
        sop_id = f"SOP-{liq_idx + 1:02d}"
        base_dead_ul, base_evap = TRUE_LIQUID_PHYSICS[liq]

        # Simulate periodic deck reconfigurations (labware geometry shift +4.5 uL dead vol)
        # and multi-week gaps creating rusty SOP conditions
        if run_idx in (12, 27, 40):
            live_epoch += 1
            current_day += 18
        else:
            current_day += rng.choice([1, 2, 4])

        epoch_geometry_shift = 3.5 * (live_epoch - 1)
        true_dead_ul = base_dead_ul + epoch_geometry_shift
        true_evap = base_evap

        initial_vol = rng.choice([180.0, 200.0, 220.0])
        dispensed = rng.choice([110.0, 125.0, 140.0])
        temp_c = rng.choice([37.0, 45.0, 60.0, 72.0])
        open_dur_min = rng.choice([12.0, 18.0, 25.0])

        exposure = compute_thermal_exposure(temp_c, open_dur_min)
        noise_evap = rng.gauss(0.0, 0.25)
        true_evap_loss = max(0.0, true_evap * exposure + noise_evap)
        noise_vol = rng.gauss(0.0, 0.55)
        measured_rec_vol = max(
            0.0, initial_vol - dispensed - true_evap_loss - true_dead_ul + noise_vol
        )

        # Check Rusty-SOP gate before full-scale execution
        pre_check = calm_twin.check_rusty_sop_gate(sop_id, current_day, live_epoch)
        if pre_check.is_rusty:
            rusty_events_total += 1
            calm_rusty_interceptions += 1
            # Static twin has no rusty-SOP gate: attempts marginal aspiration assuming 10 uL dead vol
            planned_final_aspirate = initial_vol - dispensed - 12.0
            actual_available_before_last = initial_vol - dispensed - true_evap_loss - true_dead_ul
            if planned_final_aspirate > actual_available_before_last:
                static_rusty_faults += 1

            # CALM-DT runs mandatory 20 uL micro-rehearsal + refreshes Bottom Reasoning Trace first!
            micro_exp = compute_thermal_exposure(temp_c, 8.0)
            micro_evap = max(0.0, true_evap * micro_exp + rng.gauss(0.0, 0.15))
            micro_rec = max(0.0, 80.0 - 20.0 - micro_evap - true_dead_ul + rng.gauss(0.0, 0.35))
            refreshed = calm_twin.check_rusty_sop_gate(
                sop_id,
                current_day,
                live_epoch,
                run_micro_rehearsal=True,
                micro_rehearsal_obs=(80.0, 20.0, temp_c, 8.0, micro_rec, micro_evap),
            )
            assert refreshed.allowed_full_scale
            # With refreshed posterior + 2.5-sigma LCB margin, CALM-DT caps aspiration safely
            calm_safe_aspirate = calm_twin.predict_safe_recoverable_lcb_ul(
                liq, initial_vol, dispensed, temp_c, open_dur_min
            )
            if calm_safe_aspirate > 0.0 and calm_safe_aspirate > actual_available_before_last:
                calm_rusty_faults += 1

        # Record pre-readout volume prediction error on this run
        s_pred = static_twin.predict_recoverable_volume_ul(
            initial_vol, dispensed, temp_c, open_dur_min
        )
        c_pred = calm_twin.predict_recoverable_volume_ul(
            liq, initial_vol, dispensed, temp_c, open_dur_min
        )

        s_err = abs(s_pred - measured_rec_vol)
        c_err = abs(c_pred - measured_rec_vol)
        static_abs_errors.append(s_err)
        static_sq_errors.append(s_err * s_err)
        calm_abs_errors.append(c_err)
        calm_sq_errors.append(c_err * c_err)

        # Online post-run Bayesian + EWMA update
        calm_twin.update_from_post_run_readout(
            liq,
            initial_vol,
            dispensed,
            temp_c,
            open_dur_min,
            measured_rec_vol,
            gravimetric_evap_loss_ul=true_evap_loss,
        )

    s_mae = sum(static_abs_errors) / len(static_abs_errors)
    c_mae = sum(calm_abs_errors) / len(calm_abs_errors)
    s_rmse = math.sqrt(sum(static_sq_errors) / len(static_sq_errors))
    c_rmse = math.sqrt(sum(calm_sq_errors) / len(calm_sq_errors))

    static_prev_rate = (
        round(1.0 - (static_rusty_faults / rusty_events_total), 4) if rusty_events_total else 1.0
    )
    calm_prev_rate = (
        round(1.0 - (calm_rusty_faults / rusty_events_total), 4) if rusty_events_total else 1.0
    )

    return {
        "benchmark": "idea_10_calm_twin_calibrator",
        "num_runs": num_runs,
        "rusty_sop_events": rusty_events_total,
        "static_nominal_twin": {
            "volume_mae_ul": round(s_mae, 3),
            "volume_rmse_ul": round(s_rmse, 3),
            "rusty_sop_faults_incurred": static_rusty_faults,
            "rusty_sop_fault_prevention_rate": static_prev_rate,
        },
        "calm_adaptive_twin": {
            "volume_mae_ul": round(c_mae, 3),
            "volume_rmse_ul": round(c_rmse, 3),
            "mae_reduction_pct": round(100.0 * (s_mae - c_mae) / s_mae, 2),
            "rusty_sop_interceptions": calm_rusty_interceptions,
            "rusty_sop_faults_incurred": calm_rusty_faults,
            "rusty_sop_fault_prevention_rate": calm_prev_rate,
        },
    }


if __name__ == "__main__":
    import json as _json

    print(_json.dumps(run_benchmark(), indent=2))
