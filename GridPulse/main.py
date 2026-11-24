"""
GridPulse - Desktop Energy Load & Peak-Usage Analyzer

    python main.py                       launch the desktop app
    python main.py --headless my.csv     analyse a CSV without opening a window
    python main.py --generate-sample     (re)create data/sample_energy_log.csv

Headless mode is handy for batch reports and for checking that an export
parses before you open the GUI.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def run_headless(csv_path: str, out_dir: str, tariff_path: str | None, dpi: int) -> int:
    from src import analytics_engine as ae
    from src import data_handler as dh
    from src import visualizer as viz

    try:
        dataset = dh.load_csv(csv_path)
        tariff = ae.Tariff.from_json(tariff_path) if tariff_path else _default_tariff()
    except (dh.DataLoadError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    analysis = ae.analyze(dataset.cube, dataset.dates, dataset.appliances, dataset.categories, tariff)
    plan = ae.auto_plan(dataset.cube, analysis)
    sim = None
    if plan:
        _, after, savings = ae.simulate(dataset.cube, dataset.dates, dataset.appliances,
                                        dataset.categories, tariff, plan, before=analysis)
        sim = {"before": analysis, "after": after, "savings": savings, "plan": plan}

    out = Path(out_dir)
    dh.export_csv(dh.summary_table(analysis, sim), out / "gridpulse_summary.csv")
    dh.export_csv(dh.hourly_table(analysis), out / "gridpulse_hourly_profile.csv")
    dh.export_csv(dh.appliance_table(analysis), out / "gridpulse_appliances.csv")
    charts = viz.export_all_charts(out, analysis, sim, dpi=dpi)

    print(dataset.describe())
    for line in dataset.report:
        print("  -", line)
    print(dh.summary_table(analysis, sim).to_string(index=False))
    print(f"\nWrote 3 CSV files and {len(charts)} charts to {out.resolve()}")
    return 0


def _default_tariff():
    from src import analytics_engine as ae
    from src.data_handler import PROJECT_ROOT
    path = PROJECT_ROOT / "data" / "tariff.json"
    return ae.Tariff.from_json(path) if path.exists() else ae.Tariff()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GridPulse - energy load & peak-usage analyzer")
    parser.add_argument("--headless", metavar="CSV", help="analyse CSV and export reports without a GUI")
    parser.add_argument("--out", default="reports", help="output folder for --headless (default: reports)")
    parser.add_argument("--tariff", help="tariff JSON file (default: data/tariff.json)")
    parser.add_argument("--dpi", type=int, default=200, help="PNG resolution for --headless")
    parser.add_argument("--generate-sample", action="store_true", help="regenerate the sample dataset")
    args = parser.parse_args(argv)

    if args.generate_sample:
        from src import data_handler as dh
        print("Wrote", dh.generate_sample_log())
        return 0
    if args.headless:
        return run_headless(args.headless, args.out, args.tariff, args.dpi)

    try:
        from src.app_ui import GridPulseApp
    except ModuleNotFoundError as exc:
        print(f"Missing dependency: {exc.name}. Install with:  pip install -r requirements.txt",
              file=sys.stderr)
        return 1
    GridPulseApp().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
