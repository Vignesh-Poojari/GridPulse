"""Run with:  python -m unittest discover -s tests -v   (pytest also works)"""
import io
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import analytics_engine as ae          # noqa: E402
from src import data_handler as dh              # noqa: E402

T = ae.Tariff()


class TariffMath(unittest.TestCase):
    def test_slab_boundaries(self):
        r = T.slab_rates
        self.assertAlmostEqual(float(ae.slab_energy_charge(100, T)), 100 * r[0])
        self.assertAlmostEqual(float(ae.slab_energy_charge(250, T)), 100 * r[0] + 150 * r[1])
        self.assertAlmostEqual(float(ae.slab_energy_charge(600, T)),
                               100 * r[0] + 200 * r[1] + 200 * r[2] + 100 * r[3])

    def test_vectorised(self):
        out = ae.slab_energy_charge(np.array([0, 100, 400]), T)
        self.assertEqual(out.shape, (3,))
        self.assertTrue(np.all(np.diff(out) > 0))

    def test_bad_tariff_rejected(self):
        with self.assertRaises(ValueError):
            ae.Tariff(slab_limits=[100, 50], slab_rates=[1, 2, 3]).validate()
        with self.assertRaises(ValueError):
            ae.Tariff(slab_limits=[100], slab_rates=[1]).validate()

    def test_window_wraps_midnight(self):
        m = ae.window_mask(22, 6)
        self.assertTrue(m[23] and m[0] and m[5])
        self.assertFalse(m[6] or m[21])


class Kpis(unittest.TestCase):
    def test_flat_load_has_lf_one(self):
        flat = np.full(48, 2.0)
        self.assertAlmostEqual(ae.load_factor(flat), 1.0)
        self.assertAlmostEqual(ae.peak_to_average_ratio(flat), 1.0)

    def test_par_is_inverse_of_lf(self):
        x = np.random.default_rng(1).uniform(0.1, 3, 240)
        self.assertAlmostEqual(ae.load_factor(x) * ae.peak_to_average_ratio(x), 1.0)

    def test_manual_dataset_numbers(self):
        geyser = dh.Appliance("Geyser", 2000, 1, 19)          # 2 kWh/day inside the peak window
        ds = dh.build_dataset_from_appliances([geyser], n_days=10)
        a = ae.analyze(ds.cube, ds.dates, ds.appliances, ds.categories, T)
        self.assertAlmostEqual(a["total_kwh"], 20.0)
        self.assertEqual(a["peak_hour"], 19)
        self.assertAlmostEqual(a["peak_share"], 1.0)
        self.assertAlmostEqual(a["par"], 24.0)                 # peak 2 kW / mean (2/24) kW


class Simulator(unittest.TestCase):
    def setUp(self):
        self.ds = dh.build_dataset_from_appliances(
            [dh.Appliance("Geyser", 2000, 1, 19), dh.Appliance("Fridge", 100, 24, 0)], n_days=14)
        self.a = ae.analyze(self.ds.cube, self.ds.dates, self.ds.appliances, self.ds.categories, T)

    def _run(self, plan):
        return ae.simulate(self.ds.cube, self.ds.dates, self.ds.appliances, self.ds.categories, T, plan, self.a)

    def test_moving_conserves_energy_and_saves_money(self):
        new, after, sav = self._run({0: {"target": 23, "move_pct": 100, "reduce_pct": 0}})
        self.assertAlmostEqual(new.sum(), self.ds.cube.sum())
        self.assertEqual(int(after["profile"].argmax()), 23)
        self.assertAlmostEqual(after["peak_share"], self.a["peak_share"] - 2 / self.a["daily_kwh"], places=6)
        self.assertGreater(sav["bill_month"], 0)

    def test_partial_move(self):
        new, _, _ = self._run({0: {"target": 2, "move_pct": 50, "reduce_pct": 0}})
        g = new[:, :, 0].mean(axis=0)
        self.assertAlmostEqual(g[19], 1.0)
        self.assertAlmostEqual(g[2], 1.0)

    def test_reduce_cuts_energy(self):
        new, _, sav = self._run({0: {"target": 22, "move_pct": 0, "reduce_pct": 25}})
        self.assertAlmostEqual(new[:, :, 0].sum(), self.ds.cube[:, :, 0].sum() * 0.75)
        self.assertGreater(sav["kwh_month"], 0)

    def test_empty_plan_is_noop(self):
        _, _, sav = self._run({})
        self.assertAlmostEqual(sav["bill_month"], 0.0)

    def test_auto_plan_does_not_raise_bill(self):
        plan = ae.auto_plan(self.ds.cube, self.a)
        _, _, sav = self._run(plan)
        self.assertGreaterEqual(sav["bill_month"], 0)


class Cleaning(unittest.TestCase):
    def _csv(self, text):
        return dh.clean_log(pd.read_csv(io.StringIO(text)), "test")

    def test_gaps_blanks_duplicates(self):
        rows = ["timestamp,appliance,kwh"]
        for d in (1, 2):
            for h in range(24):
                if (d, h) == (1, 5):
                    continue                                        # missing timestamp
                rows.append(f"2025-09-0{d} {h:02d}:00,Fridge,{'' if (d, h) == (2, 7) else 0.1}")
        rows.append("2025-09-02 10:00,Fridge,0.1")                  # exact duplicate
        ds = self._csv("\n".join(rows))
        self.assertEqual(ds.cube.shape, (2, 24, 1))
        self.assertFalse(np.isnan(ds.cube).any())
        self.assertAlmostEqual(ds.cube[0, 5, 0], 0.1, places=6)     # interpolated
        self.assertAlmostEqual(ds.cube.sum(), 4.8, places=6)

    def test_watts_and_whole_house(self):
        rows = ["Date,Hour,Watts"] + [f"2025-09-01,{h},500" for h in range(24)]
        ds = self._csv("\n".join(rows))
        self.assertEqual(ds.appliances, ["Whole House"])
        self.assertAlmostEqual(ds.cube.sum(), 12.0)                 # 0.5 kW x 24 h

    def test_quarter_hourly_is_aggregated(self):
        rows = ["timestamp,kwh"]
        for h in range(24):
            rows += [f"2025-09-01 {h:02d}:{m:02d},0.025" for m in (0, 15, 30, 45)]
        ds = self._csv("\n".join(rows))
        self.assertAlmostEqual(ds.cube[0, 3, 0], 0.1)

    def test_bad_files_raise_clear_errors(self):
        with self.assertRaises(dh.DataLoadError):
            self._csv("foo,bar\n1,2")
        with self.assertRaises(dh.DataLoadError):
            self._csv("timestamp,kwh\nnot-a-date,1")
        with self.assertRaises(dh.DataLoadError):
            dh.load_csv("definitely_missing_file.csv")


class Validation(unittest.TestCase):
    def test_appliance_rules(self):
        for args in (("", "100", "1", "1", "0", "Daily"), ("A", "abc", "1", "1", "0", "Daily"),
                     ("A", "100", "1", "25", "0", "Daily"), ("A", "-5", "1", "1", "0", "Daily"),
                     ("A", "100", "0", "1", "0", "Daily"), ("A", "100", "1", "1", "30", "Daily")):
            with self.assertRaises(ValueError, msg=str(args)):
                dh.parse_appliance(*args)

    def test_midnight_wrap_and_fractions(self):
        a = dh.Appliance("AC", 1000, 3.5, 22)
        f = a.hourly_fraction()
        self.assertAlmostEqual(f.sum(), 3.5)
        self.assertEqual((f[22], f[23], f[0], f[1]), (1.0, 1.0, 1.0, 0.5))

    def test_weekday_rule(self):
        a = dh.Appliance("Iron", 1000, 1, 8, days="Weekends only")
        ds = dh.build_dataset_from_appliances([a], 14)
        self.assertAlmostEqual(ds.cube.sum(), 4.0)                  # 4 weekend days in 14

    def test_sample_file_roundtrip(self):
        ds = dh.load_csv(dh.SAMPLE_PATH)
        self.assertEqual(ds.n_days, 30)
        self.assertGreater(len(ds.appliances), 5)


if __name__ == "__main__":
    unittest.main()
