"""Analytic regression cases for physical quantities and conservative missingness."""
import unittest

import numpy as np
import pandas as pd

from battery_insight.features import extract_features, finalize_cycles


def segment(start, duration, current, voltage, segment_id, *, points=31, cycle=1,
            test_type="Cycle", end_flag="complete", mode="CC", soc=None):
    elapsed = np.linspace(0, duration, points)
    voltage = np.full(points, voltage) if np.isscalar(voltage) else np.asarray(voltage)
    current = np.full(points, current) if np.isscalar(current) else np.asarray(current)
    capacity = np.r_[0., np.cumsum((abs(current[:-1]) + abs(current[1:])) / 2 * np.diff(elapsed) / 3600)]
    frame = pd.DataFrame({"test_id": "test-A", "cell_id": "cell-A", "cycle": cycle,
        "test_type": test_type, "segment_id": segment_id, "step_id": str(segment_id),
        "time_s": start + elapsed, "step_time_s": elapsed, "voltage_v": voltage,
        "current_a": current, "capacity_ah": capacity, "mode": mode,
        "temperature_c": 25., "soc_pct": np.nan if soc is None else soc,
        "end_flag": "", "source_path": "fixture.csv", "source_hash": "fixture"})
    frame.loc[frame.index[-1], "end_flag"] = end_flag
    return frame


def combine(*frames):
    data = pd.concat(frames, ignore_index=True)
    data["source_row"] = np.arange(1, len(data) + 1)
    return data


def full_cycle(cycle=1, start=0, scale=1., test_type="Cycle", end_flag="complete"):
    return combine(
        segment(start, 1800, 2 * scale, 4., 1, cycle=cycle, test_type=test_type, end_flag=end_flag),
        segment(start + 1801, 3600, -scale, 3.5, 2, points=61, cycle=cycle, test_type=test_type, end_flag=end_flag),
    )


NATIVE_SETTINGS = {"complete_end_flags": ["current_end", "voltage_end", "time_end",
                                         "soc_end", "step_complete", "next_step", "endtime"]}


def native_cycle():
    """Native end/start records share time; measured onset current remains zero."""
    rest = segment(0, 2, [8e-6, -9e-6, 0], 4, 1, points=3, mode="unknown", end_flag="time_end")
    charge = segment(2, 60, np.r_[0., np.full(60, 2.)], 4, 2, points=61, mode="unknown", end_flag="current_end")
    middle = segment(62, 2, [0, -7e-6, 8e-6], 4, 3, points=3, mode="unknown", end_flag="time_end")
    discharge = segment(64, 60, np.r_[0., np.full(60, -2.)], 3.5, 4, points=61, mode="unknown", end_flag="voltage_end")
    end = segment(124, 0, 4e-9, 3.5, 5, points=1, mode="unknown", end_flag="step_complete")
    for part, kind in zip((rest, charge, middle, discharge, end), ("rest", "charge", "rest", "discharge", "end")):
        part["step_kind"] = kind
        part["step_end"] = part.end_flag.ne("")
    return combine(rest, charge, middle, discharge, end)


class FeatureTests(unittest.TestCase):
    def test_analytic_capacity_energy_and_efficiency(self):
        cycles, _, _ = extract_features(full_cycle())
        row = cycles.iloc[0]
        self.assertAlmostEqual(row.charge_capacity_ah, 1.)
        self.assertAlmostEqual(row.discharge_capacity_ah, 1.)
        self.assertAlmostEqual(row.charge_capacity_measured_ah, 1.)
        self.assertAlmostEqual(row.charge_energy_wh, 4.)
        self.assertAlmostEqual(row.discharge_energy_wh, 3.5)
        self.assertAlmostEqual(row.energy_efficiency_pct, 87.5)
        self.assertAlmostEqual(row.coulombic_efficiency_pct, 100.)
        self.assertEqual(row.completion_status, "complete")

    def test_completion_must_have_explicit_evidence(self):
        cycles, _, issues = extract_features(full_cycle(end_flag=""))
        self.assertEqual(cycles.iloc[0].completion_status, "unknown")
        self.assertFalse(cycles.iloc[0].capacity_complete)
        self.assertTrue(np.isnan(cycles.iloc[0].coulombic_efficiency_pct))
        self.assertTrue(np.isnan(cycles.iloc[0].capacity_retention_pct))
        self.assertIn("cycle_completion_unverified", issues.code.tolist())

    def test_unidirectional_cycle_is_incomplete(self):
        cycles, _, _ = extract_features(combine(segment(0, 1800, 2, 4, 1)))
        self.assertEqual(cycles.iloc[0].completion_status, "incomplete")
        self.assertTrue(np.isnan(cycles.iloc[0].discharge_capacity_ah))

    def test_type_baselines_and_life_counter(self):
        frames = [full_cycle(1, 0), full_cycle(2, 6000, .8),
                  full_cycle(3, 12000, .5, "RPT"), full_cycle(4, 18000, .4, "RPT"),
                  full_cycle(5, 24000, .9, "Insitu"), full_cycle(6, 30000, .2, "Formation")]
        streamed = [extract_features(frame)[0] for frame in frames]
        cycles = finalize_cycles(pd.concat(streamed, ignore_index=True))
        np.testing.assert_allclose(cycles.capacity_retention_pct, [100, 80, 100, 80, 100, 100])
        np.testing.assert_allclose(cycles.life_index, [1, 2, np.nan, np.nan, 3, np.nan], equal_nan=True)
        np.testing.assert_allclose(cycles.rpt_index, [np.nan, np.nan, 1, 2, np.nan, np.nan], equal_nan=True)
        missing = finalize_cycles(cycles, {"baseline_cycle": 99})
        self.assertTrue(missing.capacity_retention_pct.isna().all())

    def test_gap_invalidates_capacity_without_bridging(self):
        frame = full_cycle()
        frame.loc[10:, "time_s"] += 1000
        cycles, _, issues = extract_features(frame)
        self.assertTrue(np.isnan(cycles.iloc[0].charge_capacity_ah))
        self.assertEqual(cycles.iloc[0].completion_status, "incomplete")
        self.assertIn("sampling_gap", issues.code.tolist())

    def test_non_increasing_time_invalidates_capacity(self):
        frame = full_cycle()
        frame.loc[5, "time_s"] = frame.loc[4, "time_s"]
        cycles, _, issues = extract_features(frame)
        self.assertTrue(np.isnan(cycles.iloc[0].charge_capacity_ah))
        self.assertIn("non_increasing_time", issues.code.tolist())

    def test_repeated_step_ids_do_not_bridge_rest(self):
        frames = [segment(0, 360, 2, 4, 7, points=7),
                  segment(361, 100, 0, 4, 8, mode="unknown"),
                  segment(462, 360, 2, 4, 7, points=7)]
        cycles, _, _ = extract_features(combine(*frames))
        self.assertAlmostEqual(cycles.iloc[0].charge_capacity_ah, .4)
        self.assertEqual(cycles.iloc[0].segment_count, 3)

    def test_measured_capacity_is_not_substituted_or_unit_guessed(self):
        frame = full_cycle()
        frame.capacity_ah *= 1000
        cycles, _, _ = extract_features(frame)
        self.assertAlmostEqual(cycles.iloc[0].charge_capacity_ah, 1.)
        self.assertAlmostEqual(cycles.iloc[0].charge_capacity_measured_ah, 1000.)

    def test_pulse_resistance_and_explicit_eoc(self):
        charge = segment(0, 60, 2, 4, 1, points=7, soc=100.)
        rest = segment(61, 4, 0, 4, 2, points=5, mode="unknown", soc=100.)
        pulse = segment(66, 60, -2, 3.9, 3, points=601, soc=100.)
        cycles, responses, _ = extract_features(combine(charge, rest, pulse))
        selected = responses.loc[responses.feature_type == "pulse"]
        self.assertEqual(len(selected), 5)
        np.testing.assert_allclose(selected.resistance_ohm, .05)
        self.assertAlmostEqual(cycles.iloc[0].EoC_dchgR_60s_mohm, 50.)
        self.assertTrue(selected.eoc_discharge_eligible.all())
        self.assertTrue((selected.response_source_row_left >= selected.source_row_start).all())
        without_soc = combine(charge, rest, pulse).drop(columns="soc_pct")
        no_eoc, _, _ = extract_features(without_soc)
        self.assertTrue(np.isnan(no_eoc.iloc[0].EoC_dchgR_60s_mohm))

    def test_no_pulse_extrapolation_before_first_sample_or_after_last(self):
        rest = segment(0, 4, 0, 4, 1, points=5, mode="unknown")
        pulse = segment(6, 9, -2, 3.9, 2, points=10)
        pulse.step_time_s += 1  # First acquired pulse record is at t=1 second.
        _, responses, _ = extract_features(combine(rest, pulse))
        self.assertEqual(responses.pulse_time_s.tolist(), [1., 10.])

    def test_unknown_pulse_onset_is_not_inferred(self):
        rest = segment(0, 4, 0, 4, 1, points=5, mode="unknown")
        pulse = segment(5, 60, -2, 3.9, 2, points=61).drop(columns="step_time_s")
        _, responses, issues = extract_features(combine(rest, pulse))
        self.assertTrue(responses.empty)
        self.assertIn("pulse_onset_unknown", issues.code.tolist())

    def test_qc_uses_true_soc_and_brackets(self):
        frame = combine(segment(0, 100, 2, np.linspace(3, 4.2, 101), 1, points=101,
                                test_type="QC", soc=np.linspace(0, 100, 101)))
        cycles, _, _ = extract_features(frame)
        self.assertAlmostEqual(cycles.iloc[0].qc_charge_time_10_80_s, 70)
        absent, _, _ = extract_features(frame.drop(columns="soc_pct"))
        self.assertTrue(np.isnan(absent.iloc[0].qc_charge_time_10_80_s))
        short, _, _ = extract_features(frame.loc[frame.soc_pct >= 20])
        self.assertTrue(np.isnan(short.iloc[0].qc_charge_time_10_80_s))

    def test_curve_does_not_sort_voltage(self):
        voltage = np.tile([3., 3.3, 3.1, 3.4], 10)
        frame = combine(segment(0, 390, 2, voltage, 1, points=40))
        cycles, _, issues = extract_features(frame)
        self.assertEqual(cycles.iloc[0].charge_curve_points, 0)
        self.assertTrue(np.isnan(cycles.iloc[0].charge_dqdv_peak_ah_per_v))
        self.assertIn("curve_insufficient", issues.code.tolist())

    def test_low_soc_proxy_is_separate_from_true_soc(self):
        frame = combine(segment(0, 3600, -1, np.linspace(4.2, 3.2, 61), 1, points=61))
        cycles, _, _ = extract_features(frame)
        row = cycles.iloc[0]
        self.assertAlmostEqual(row.discharge_dvdq_low_soc_proxy_v_per_ah, 1.)
        self.assertTrue(np.isnan(row.discharge_dvdq_low_soc_v_per_ah))


    def test_dqdv_peak_preserves_acquisition_order(self):
        v = np.linspace(3., 4., 101)
        current = 1. + 10. * np.exp(-((v - 3.5) / .1) ** 2)
        frame = combine(segment(0, 100, current, v, 1, points=101))
        cycles, _, _ = extract_features(frame)
        row = cycles.iloc[0]
        self.assertEqual(row.charge_curve_points, 101)
        self.assertGreaterEqual(row.charge_dqdv_peak_count, 1)
        self.assertAlmostEqual(row.charge_dqdv_peak_voltage_v, 3.5, delta=.02)
        self.assertAlmostEqual(row.charge_dqdv_peak_ah_per_v, 11 / 36, delta=.01)

    def test_timed_pulse_cannot_cross_a_sampling_gap(self):
        rest = segment(0, 4, 0, 4, 1, points=5, mode="unknown")
        pulse = segment(5, 60, -2, 3.9, 2, points=61)
        pulse = pulse.loc[pulse.step_time_s.isin([0., 1., 10., 30., 60.])]
        _, responses, _ = extract_features(combine(rest, pulse), {"max_gap_s": 2})
        self.assertEqual(responses.pulse_time_s.tolist(), [.1, 1.])

    def test_unstable_pulse_current_is_excluded(self):
        rest = segment(0, 4, 0, 4, 1, points=5, mode="unknown")
        pulse = segment(5, 60, -2, 3.9, 2, points=61)
        pulse.loc[pulse.step_time_s >= 2, "current_a"] = -1
        _, responses, issues = extract_features(combine(rest, pulse))
        self.assertEqual(responses.pulse_time_s.tolist(), [.1, 1.])
        self.assertIn("pulse_current_unstable", issues.code.tolist())

    def test_unlabelled_mixed_current_does_not_erase_valid_opposite_leg(self):
        frame = full_cycle()
        frame.loc[3, "current_a"] = 0
        cycles, _, issues = extract_features(frame)
        self.assertTrue(np.isnan(cycles.iloc[0].charge_capacity_ah))
        self.assertAlmostEqual(cycles.iloc[0].discharge_capacity_ah, 1.)
        self.assertAlmostEqual(cycles.iloc[0].discharge_mean_voltage_v, 3.5)
        self.assertFalse(cycles.iloc[0].capacity_complete)
        self.assertIn("invalid_segment_current", issues.code.tolist())

    def test_reset_measured_counter_does_not_affect_integral(self):
        frame = full_cycle()
        frame.loc[10, "capacity_ah"] = 0
        cycles, _, issues = extract_features(frame)
        self.assertAlmostEqual(cycles.iloc[0].charge_capacity_ah, 1.)
        self.assertTrue(np.isnan(cycles.iloc[0].charge_capacity_measured_ah))
        self.assertIn("capacity_counter_invalid", issues.code.tolist())

    def test_explicit_abort_anywhere_prevents_completion(self):
        frame = full_cycle()
        frame.loc[10, "end_flag"] = "aborted"
        cycles, _, _ = extract_features(frame)
        self.assertEqual(cycles.iloc[0].completion_status, "incomplete")
        self.assertTrue(np.isnan(cycles.iloc[0].capacity_retention_pct))


    def test_later_configured_baseline_never_leaks_into_earlier_cycles(self):
        frame = combine(full_cycle(1, 0, 1.), full_cycle(2, 6000, .9),
                        full_cycle(3, 12000, .8), full_cycle(4, 18000, .7))
        cycles, _, _ = extract_features(frame, {"baseline_cycle": 3})
        np.testing.assert_allclose(cycles.capacity_retention_pct, [np.nan, np.nan, 100., 87.5], equal_nan=True)
        self.assertEqual(cycles.baseline_cycle.tolist(), [3., 3., 3., 3.])


    def test_missing_leading_capacity_prevents_full_completion(self):
        frame = full_cycle()
        frame = frame.loc[frame.source_row > 1]
        cycles, _, issues = extract_features(frame)
        self.assertEqual(cycles.iloc[0].capacity_start_coverage, "partial")
        self.assertEqual(cycles.iloc[0].completion_status, "unknown")
        self.assertTrue(np.isnan(cycles.iloc[0].coulombic_efficiency_pct))
        self.assertIn("leading_segment_samples_missing", issues.code.tolist())
        assumed, _, _ = extract_features(frame, {"assume_complete_bidirectional_cycles": True})
        self.assertFalse(assumed.iloc[0].capacity_complete)

    def test_contradictory_pulse_onset_is_rejected(self):
        rest = segment(95, 4, 0, 4, 1, points=5, mode="unknown")
        pulse = segment(100, 60, -2, 3.9, 2, points=61)
        pulse.step_time_s += 10  # Implies pulse started before the rest baseline.
        _, responses, issues = extract_features(combine(rest, pulse))
        self.assertTrue(responses.empty)
        self.assertIn("pulse_onset_conflict", issues.code.tolist())

    def test_native_zero_onset_noisy_rest_and_end_record_preserve_observations(self):
        frame = native_cycle()
        before = frame.copy(deep=True)
        cycles, _, issues = extract_features(frame, NATIVE_SETTINGS)
        row = cycles.iloc[0]
        self.assertAlmostEqual(row.charge_capacity_ah, 119 / 3600)
        self.assertAlmostEqual(row.discharge_capacity_ah, 119 / 3600)
        self.assertAlmostEqual(row.charge_mean_voltage_v, 4.)
        self.assertAlmostEqual(row.discharge_mean_voltage_v, 3.5)
        self.assertEqual(row.completion_status, "complete")
        self.assertEqual(row.capacity_start_coverage, "complete")
        self.assertEqual(row.segment_count, 5)
        self.assertNotIn("invalid_segment_current", issues.code.tolist())
        self.assertNotIn("cycle_clock_invalid", issues.code.tolist())
        pd.testing.assert_frame_equal(frame, before)

    def test_native_nullable_and_arrow_columns_preserve_first_sample(self):
        for backend in ("numpy_nullable", "pyarrow"):
            with self.subTest(backend=backend):
                frame = native_cycle().convert_dtypes(dtype_backend=backend)
                cycles, _, _ = extract_features(frame, NATIVE_SETTINGS)
                row = cycles.iloc[0]
                self.assertAlmostEqual(row.charge_capacity_ah, 119 / 3600)
                self.assertAlmostEqual(row.discharge_capacity_ah, 119 / 3600)
                self.assertEqual(row.source_row_start, 1)
                self.assertEqual(row.sample_count, len(frame))
                self.assertEqual(row.completion_status, "complete")

    def test_declared_charge_rejects_opposite_current_without_erasing_discharge(self):
        frame = native_cycle()
        frame.loc[(frame.step_kind == "charge") & frame.step_time_s.eq(10), "current_a"] = -2.
        cycles, _, issues = extract_features(frame, NATIVE_SETTINGS)
        row = cycles.iloc[0]
        self.assertTrue(np.isnan(row.charge_capacity_ah))
        self.assertAlmostEqual(row.discharge_capacity_ah, 119 / 3600)
        self.assertAlmostEqual(row.discharge_mean_voltage_v, 3.5)
        self.assertEqual(row.completion_status, "incomplete")
        self.assertTrue(np.isnan(row.coulombic_efficiency_pct))
        self.assertIn("invalid_segment_current", issues.code.tolist())

    def test_missing_declared_rest_current_keeps_observed_active_leg_totals(self):
        frame = native_cycle()
        frame.loc[1, "current_a"] = np.nan
        cycles, _, _ = extract_features(frame, NATIVE_SETTINGS)
        row = cycles.iloc[0]
        self.assertAlmostEqual(row.charge_capacity_ah, 119 / 3600)
        self.assertAlmostEqual(row.discharge_capacity_ah, 119 / 3600)
        self.assertFalse(row.capacity_complete)

    def test_native_internal_clock_duplicate_never_integrates_across_it(self):
        frame = native_cycle()
        indices = frame.index[frame.step_kind == "charge"]
        frame.loc[indices[10], "time_s"] = frame.loc[indices[9], "time_s"]
        cycles, _, issues = extract_features(frame, NATIVE_SETTINGS)
        row = cycles.iloc[0]
        self.assertTrue(np.isnan(row.charge_capacity_ah))
        self.assertAlmostEqual(row.discharge_capacity_ah, 119 / 3600)
        self.assertFalse(row.capacity_complete)
        self.assertIn("non_increasing_time", issues.code.tolist())

    def test_native_clock_reset_across_steps_prevents_completion(self):
        frame = native_cycle()
        frame.loc[frame.segment_id >= 4, "time_s"] -= 10
        cycles, _, issues = extract_features(frame, NATIVE_SETTINGS)
        self.assertEqual(cycles.iloc[0].completion_status, "incomplete")
        self.assertIn("cycle_clock_invalid", issues.code.tolist())
        self.assertTrue(np.isnan(cycles.iloc[0].capacity_retention_pct))

    def test_native_equal_clock_requires_end_evidence_and_new_zero_onset(self):
        for missing in ("end", "onset"):
            with self.subTest(missing=missing):
                frame = native_cycle()
                if missing == "end":
                    frame.loc[frame.segment_id.eq(1), "end_flag"] = ""
                    frame.loc[frame.segment_id.eq(1), "step_end"] = False
                else:
                    frame.loc[frame.segment_id.eq(2), "step_time_s"] += 1
                cycles, _, issues = extract_features(frame, NATIVE_SETTINGS)
                self.assertFalse(cycles.iloc[0].capacity_complete)
                self.assertIn("cycle_clock_invalid", issues.code.tolist())

    def test_program_end_alone_is_not_full_capacity_even_with_assumption(self):
        for flag in ("time_end", "endtime", "soc_end", "step_complete", "next_step"):
            for assume in (False, True):
                with self.subTest(flag=flag, assume=assume):
                    frame = native_cycle()
                    final = frame.index[frame.step_kind == "discharge"][-1]
                    frame.loc[final, "end_flag"] = flag
                    settings = {**NATIVE_SETTINGS, "assume_complete_bidirectional_cycles": assume}
                    cycles, _, issues = extract_features(frame, settings)
                    row = cycles.iloc[0]
                    self.assertEqual(row.completion_status, "unknown")
                    self.assertTrue(np.isnan(row.coulombic_efficiency_pct))
                    self.assertTrue(np.isnan(row.capacity_retention_pct))
                    self.assertAlmostEqual(row.discharge_capacity_ah, 119 / 3600)
                    self.assertIn("capacity_endpoint_unverified", issues.code.tolist())

    def test_soc_end_requires_measured_full_endpoint(self):
        for soc, complete in ((50., False), (np.nan, False), (0., True)):
            with self.subTest(soc=soc):
                frame = native_cycle()
                final = frame.index[frame.step_kind == "discharge"][-1]
                frame.loc[final, ["end_flag", "soc_pct"]] = ["soc_end", soc]
                cycles, _, _ = extract_features(frame, NATIVE_SETTINGS)
                self.assertEqual(bool(cycles.iloc[0].capacity_complete), complete)

    def test_multiple_active_direction_pairs_do_not_produce_ce_or_retention(self):
        for kinds in (("discharge", "charge", "discharge"),
                      ("charge", "discharge", "charge", "discharge"),
                      ("discharge", "charge")):
            with self.subTest(kinds=kinds):
                parts = []
                for index, kind in enumerate(kinds):
                    part = segment(index * 61, 60, 2 if kind == "charge" else -2, 4,
                                   index + 1, points=61, end_flag="voltage_end")
                    part["step_kind"] = kind
                    parts.append(part)
                cycles, _, issues = extract_features(combine(*parts),
                    {**NATIVE_SETTINGS, "assume_complete_bidirectional_cycles": True})
                row = cycles.iloc[0]
                self.assertGreater(row.charge_capacity_ah, 0)
                self.assertGreater(row.discharge_capacity_ah, 0)
                self.assertEqual(row.completion_status, "unknown")
                self.assertTrue(np.isnan(row.coulombic_efficiency_pct))
                self.assertTrue(np.isnan(row.capacity_retention_pct))
                self.assertIn("capacity_sequence_unverified", issues.code.tolist())

    def test_native_missing_onset_never_becomes_complete(self):
        frame = native_cycle()
        frame = frame.loc[~(frame.step_kind.eq("charge") & frame.step_time_s.eq(0))]
        cycles, _, issues = extract_features(frame, NATIVE_SETTINGS)
        self.assertEqual(cycles.iloc[0].capacity_start_coverage, "partial")
        self.assertEqual(cycles.iloc[0].completion_status, "unknown")
        self.assertTrue(np.isnan(cycles.iloc[0].coulombic_efficiency_pct))
        self.assertIn("leading_segment_samples_missing", issues.code.tolist())


if __name__ == "__main__":
    unittest.main()
