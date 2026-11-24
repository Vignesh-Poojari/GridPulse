"""
data_handler.py  --  Member 1 : data ingestion, cleaning & export (pandas)

Responsibilities
----------------
* Read meter / appliance CSV logs of many shapes (hourly, sub-hourly, daily,
  kWh or Watts, with or without an appliance column) and turn them into ONE
  canonical structure: an ``EnergyDataset`` holding a 3-D NumPy "load cube"
  of shape (days, 24 hours, appliances) in kWh.
* Handle missing timestamps, blank / non-numeric / negative readings and
  duplicate rows, and tell the user exactly what was repaired.
* Build datasets from manual appliance schedules (watts x hours x time slot).
* Produce tidy pandas tables for the CSV report exporter.
* Generate the 30-day sample dataset shipped in ``data/``.
"""
from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_PATH = PROJECT_ROOT / "data" / "sample_energy_log.csv"

# --------------------------------------------------------------------------- #
#  Appliance categories & presets
# --------------------------------------------------------------------------- #
CATEGORY_KEYWORDS = {
    "Heating": ("geyser", "heater", "immersion", "induction", "kettle"),
    "Cooling": ("ac", "aircon", "conditioner", "fan", "cooler"),
    "Refrigeration": ("fridge", "refrigerator", "freezer"),
    "Laundry": ("washing", "washer", "dryer", "iron"),
    "Lighting": ("light", "lights", "lamp", "led", "tube", "bulb"),
    "Kitchen": ("microwave", "oven", "mixer", "grinder", "toaster", "cooker", "dishwasher"),
    "Electronics": ("tv", "television", "laptop", "computer", "pc", "monitor", "charger",
                    "chargers", "router", "phone", "projector", "printer"),
    "Water Pump": ("pump", "motor"),
}

# preset name -> (appliance name, watts, quantity, hours/day, start hour)
APPLIANCE_PRESETS = {
    "Custom": ("", "", "1", "", "18"),
    "Geyser / Water heater (2000 W)": ("Geyser", "2000", "1", "1", "18"),
    "Air conditioner 1.5 ton (1500 W)": ("Air Conditioner", "1500", "1", "6", "22"),
    "Washing machine (500 W)": ("Washing Machine", "500", "1", "1.5", "19"),
    "Iron (1000 W)": ("Iron", "1000", "1", "0.5", "19"),
    "Ceiling fan (75 W)": ("Ceiling Fans", "75", "3", "10", "18"),
    "Refrigerator (150 W)": ("Refrigerator", "60", "1", "24", "0"),
    "LED light (10 W)": ("LED Lights", "10", "6", "6", "18"),
    "Television (120 W)": ("Television", "120", "1", "4", "19"),
    "Water pump (750 W)": ("Water Pump", "750", "1", "1", "18"),
    "Microwave (1200 W)": ("Microwave", "1200", "1", "0.3", "20"),
    "Laptop + chargers (80 W)": ("Laptop & Chargers", "80", "1", "8", "14"),
}


def guess_category(name: str) -> str:
    """Best-effort appliance category from its name."""
    tokens = re.findall(r"[a-z]+", name.lower())
    for category, keywords in CATEGORY_KEYWORDS.items():
        for token in tokens:
            for kw in keywords:
                if token == kw or (len(kw) > 3 and token.startswith(kw)):
                    return category
    return "Other"


def weekday_index(dates) -> np.ndarray:
    """Monday=0 ... Sunday=6 for a datetime64[D] array (1970-01-01 was a Thursday)."""
    days = np.asarray(dates).astype("datetime64[D]").astype("int64")
    return (days + 3) % 7


# --------------------------------------------------------------------------- #
#  Core data structure
# --------------------------------------------------------------------------- #
class DataLoadError(Exception):
    """Raised when an input file cannot be converted into a usable dataset."""


@dataclass
class EnergyDataset:
    cube: np.ndarray                  # (days, 24, n_appliances) in kWh
    appliances: list[str]
    categories: list[str]
    dates: np.ndarray                 # datetime64[D], one per day
    source: str = ""
    report: list[str] = field(default_factory=list)

    @property
    def n_days(self) -> int:
        return int(self.cube.shape[0])

    @property
    def total_kwh(self) -> float:
        return float(self.cube.sum())

    def describe(self) -> str:
        d0 = np.datetime_as_string(self.dates[0], unit="D")
        d1 = np.datetime_as_string(self.dates[-1], unit="D")
        return (f"{self.source}: {self.n_days} day(s) [{d0} → {d1}], "
                f"{len(self.appliances)} appliance(s)/meter(s), {self.total_kwh:,.1f} kWh")

    def to_long(self) -> pd.DataFrame:
        """Cleaned, gap-free hourly log in tidy (timestamp, appliance, kWh) form."""
        n_app = self.cube.shape[2]
        stamps = (self.dates.astype("datetime64[h]")[:, None]
                  + np.arange(24).astype("timedelta64[h]")[None, :]).ravel()
        return pd.DataFrame({
            "timestamp": np.repeat(stamps, n_app),
            "appliance": np.tile(np.array(self.appliances, dtype=object), len(stamps)),
            "category": np.tile(np.array(self.categories, dtype=object), len(stamps)),
            "kwh": np.round(self.cube.reshape(-1, n_app).ravel(), 5),
        })


# --------------------------------------------------------------------------- #
#  CSV loading & cleaning
# --------------------------------------------------------------------------- #
_TIME_ALIASES = ("timestamp", "datetime", "date_time", "time", "ts", "date")
_APP_ALIASES = ("appliance", "appliance_name", "device", "load", "name", "meter")
_KWH_ALIASES = ("kwh", "energy_kwh", "consumption_kwh", "usage_kwh", "kwh_consumed",
                "consumption", "units", "energy", "usage")
_WATT_ALIASES = ("watts", "watt", "power_w", "power")
_CAT_ALIASES = ("category", "type", "group")


def _norm(col) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(col).strip().lower()).strip("_")


def _first(columns, aliases):
    for alias in aliases:
        if alias in columns:
            return alias
    return None


def _parse_datetimes(series: pd.Series) -> pd.Series:
    """Parse mixed date strings; picks day-first vs month-first by fewest failures."""
    text = series.astype(str).str.strip()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            month_first = pd.to_datetime(text, errors="coerce", dayfirst=False)
            day_first = pd.to_datetime(text, errors="coerce", dayfirst=True)
    except (ValueError, TypeError) as exc:
        raise DataLoadError(f"Could not understand the timestamp column: {exc}") from exc
    out = day_first if day_first.isna().sum() < month_first.isna().sum() else month_first
    if getattr(out.dt, "tz", None) is not None:
        out = out.dt.tz_localize(None)
    return out


def load_csv(path: str | Path) -> EnergyDataset:
    """Read a CSV meter/appliance log from disk and return a cleaned dataset."""
    path = Path(path)
    try:
        raw = pd.read_csv(path, skipinitialspace=True, encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise DataLoadError(f"File not found: {path}") from exc
    except pd.errors.EmptyDataError as exc:
        raise DataLoadError("The selected file is empty.") from exc
    except (pd.errors.ParserError, UnicodeDecodeError) as exc:
        raise DataLoadError(f"The file is not a valid CSV: {exc}") from exc
    return clean_log(raw, source=path.name)


def clean_log(raw: pd.DataFrame, source: str = "CSV") -> EnergyDataset:
    """Turn a raw log DataFrame into an :class:`EnergyDataset`, repairing problems."""
    report: list[str] = []
    df = raw.copy()
    df.columns = [_norm(c) for c in df.columns]
    cols = list(df.columns)
    if len(df) == 0:
        raise DataLoadError("The file has no data rows.")

    # ---- locate columns -------------------------------------------------- #
    tcol = _first(cols, _TIME_ALIASES)
    if tcol is None:
        raise DataLoadError("No timestamp column found. Expected one of: "
                            "timestamp, datetime, date (+ hour), time.\n"
                            f"Columns in file: {', '.join(cols)}")
    use_watts = False
    vcol = _first([c for c in cols if c != tcol], _KWH_ALIASES)
    if vcol is None:
        vcol = _first([c for c in cols if c != tcol], _WATT_ALIASES)
        use_watts = vcol is not None
    if vcol is None:
        raise DataLoadError("No energy column found. Expected one of: kwh, energy_kwh, "
                            "consumption, units (kWh) or watts / power (W).\n"
                            f"Columns in file: {', '.join(cols)}")
    acol = _first([c for c in cols if c not in (tcol, vcol)], _APP_ALIASES)
    ccol = _first([c for c in cols if c not in (tcol, vcol, acol)], _CAT_ALIASES)

    # ---- timestamps ------------------------------------------------------ #
    ts = _parse_datetimes(df[tcol])
    hour_col = "hour" if (tcol == "date" and "hour" in cols) else None
    if hour_col:
        hrs = pd.to_numeric(df[hour_col], errors="coerce")
        ts = ts.dt.normalize() + pd.to_timedelta(hrs, unit="h")
        report.append("Combined 'date' and 'hour' columns into timestamps.")

    # ---- values ---------------------------------------------------------- #
    vals = pd.to_numeric(df[vcol], errors="coerce")
    if acol:
        apps = df[acol].astype(str).str.strip().replace({"": "Unknown", "nan": "Unknown"})
    else:
        apps = pd.Series("Whole House", index=df.index)
        report.append("No appliance column – treating the file as one whole-house meter.")

    bad_ts = int(ts.isna().sum())
    missing_vals = int(vals.isna().sum())
    negatives = int((vals < 0).sum())
    vals = vals.where(vals >= 0)

    work = pd.DataFrame({"ts": ts, "app": apps, "val": vals})
    if bad_ts:
        report.append(f"Dropped {bad_ts} row(s) with unreadable timestamps.")
    work = work.dropna(subset=["ts"])
    if work.empty:
        raise DataLoadError("None of the timestamps could be read. Use e.g. 2025-09-01 14:00.")
    if missing_vals:
        report.append(f"{missing_vals} blank / non-numeric reading(s) will be re-estimated.")
    if negatives:
        report.append(f"{negatives} negative reading(s) treated as missing.")

    before = len(work)
    work = work.drop_duplicates(subset=["ts", "app", "val"])
    if before - len(work):
        report.append(f"Removed {before - len(work)} exact duplicate row(s).")

    # ---- hourly alignment (hourly / sub-hourly / daily) ------------------- #
    is_daily = bool((work["ts"] == work["ts"].dt.normalize()).all()) and hour_col is None
    work["hour"] = work["ts"].dt.floor("60min")
    if is_daily:
        report.append("DAILY data detected: each day's energy was spread evenly over 24 h. "
                      "Hour-of-day charts and peak analysis are NOT meaningful – use hourly data.")
        rep = work.loc[work.index.repeat(24)].reset_index(drop=True)
        rep["hour"] = rep["hour"] + pd.to_timedelta(np.tile(np.arange(24), len(work)), unit="h")
        if not use_watts:
            rep["val"] = rep["val"] / 24.0
        work = rep
    grouped = work.groupby(["hour", "app"])["val"]
    series = grouped.mean() if use_watts else grouped.sum(min_count=1)
    wide = series.unstack("app")
    if use_watts:
        wide = wide / 1000.0
        report.append("Power (W) readings converted to kWh assuming hourly averages.")

    # ---- gap handling ---------------------------------------------------- #
    start = wide.index.min().normalize()
    end = wide.index.max().normalize() + pd.Timedelta(hours=23)
    full_index = pd.date_range(start, end, freq=pd.Timedelta(hours=1))
    present_hours = int(wide.dropna(how="all").shape[0])
    wide = wide.reindex(full_index)
    absent_hours = len(full_index) - present_hours
    if absent_hours:
        report.append(f"{absent_hours} hour(s) had no data at all (missing timestamps).")

    interpolated = median_filled = 0
    for col in wide.columns:
        s = wide[col]
        n0 = int(s.isna().sum())
        if n0 == 0:
            continue
        s = s.interpolate(method="time", limit=3, limit_area="inside")
        n1 = int(s.isna().sum())
        typical = s.groupby(s.index.hour).transform("median")
        s = s.fillna(typical).fillna(0.0)
        wide[col] = s
        interpolated += n0 - n1
        median_filled += n1
    if interpolated:
        report.append(f"Filled {interpolated} short gap value(s) by time interpolation.")
    if median_filled:
        report.append(f"Filled {median_filled} longer-gap value(s) with that appliance's "
                      "typical value for the same hour of day.")
    if (interpolated + median_filled) == 0 and not absent_hours:
        report.append("No gaps found – data was already complete.")

    # ---- categories ------------------------------------------------------ #
    cat_map: dict[str, str] = {}
    if ccol:
        pairs = pd.DataFrame({"app": apps, "cat": df[ccol].astype(str).str.strip()})
        pairs = pairs[~pairs["cat"].isin(["", "nan"])]
        cat_map = pairs.drop_duplicates("app").set_index("app")["cat"].to_dict()

    order = wide.sum().sort_values(ascending=False).index.tolist()
    wide = wide[order]
    names = [str(n) for n in order]
    categories = [cat_map.get(n) or guess_category(n) for n in names]

    cube = wide.to_numpy(dtype=float).reshape(-1, 24, len(names))
    dates = full_index[::24].to_numpy().astype("datetime64[D]")
    report.insert(0, f"Loaded {before} reading(s) → {len(dates)} complete day(s) × 24 h × "
                     f"{len(names)} appliance(s)/meter(s).")
    return EnergyDataset(cube, names, categories, dates, source, report)


# --------------------------------------------------------------------------- #
#  Manual appliance schedules
# --------------------------------------------------------------------------- #
DAY_RULES = ("Daily", "Weekdays only", "Weekends only")


@dataclass
class Appliance:
    name: str
    watts: float
    hours_per_day: float
    start_hour: int
    quantity: int = 1
    days: str = "Daily"
    category: str = ""

    def __post_init__(self):
        self.name = self.name.strip()
        if not self.category:
            self.category = guess_category(self.name)

    def validate(self) -> None:
        if not self.name:
            raise ValueError("Appliance name cannot be empty.")
        if len(self.name) > 40:
            raise ValueError("Appliance name is too long (max 40 characters).")
        if not (0 < self.watts <= 15000):
            raise ValueError("Wattage must be between 1 and 15000 W.")
        if not (1 <= self.quantity <= 200):
            raise ValueError("Quantity must be a whole number between 1 and 200.")
        if not (0 < self.hours_per_day <= 24):
            raise ValueError("Hours per day must be greater than 0 and at most 24.")
        if not (0 <= self.start_hour <= 23):
            raise ValueError("Start hour must be between 0 and 23.")
        if self.days not in DAY_RULES:
            raise ValueError(f"Days must be one of: {', '.join(DAY_RULES)}.")

    def hourly_fraction(self) -> np.ndarray:
        """Fraction of each clock-hour the appliance is ON (wraps past midnight)."""
        frac = np.zeros(24)
        remaining = float(self.hours_per_day)
        for k in range(24):
            if remaining <= 1e-9:
                break
            frac[(self.start_hour + k) % 24] = min(1.0, remaining)
            remaining -= 1.0
        return frac

    @property
    def daily_kwh(self) -> float:
        return self.watts * self.quantity * self.hours_per_day / 1000.0


def parse_appliance(name: str, watts: str, quantity: str, hours: str,
                    start_hour: str, days: str) -> Appliance:
    """Convert raw form text to a validated Appliance (raises ValueError with a clear message)."""
    try:
        w = float(watts)
    except ValueError:
        raise ValueError("Wattage must be a number, e.g. 1500.") from None
    try:
        q = int(float(quantity))
    except ValueError:
        raise ValueError("Quantity must be a whole number, e.g. 2.") from None
    try:
        h = float(hours)
    except ValueError:
        raise ValueError("Hours per day must be a number, e.g. 1.5.") from None
    try:
        s = int(start_hour)
    except ValueError:
        raise ValueError("Start hour must be 0-23.") from None
    appliance = Appliance(name=name, watts=w, hours_per_day=h, start_hour=s, quantity=q, days=days)
    appliance.validate()
    return appliance


def _day_flags(dates: np.ndarray, rule: str) -> np.ndarray:
    wd = weekday_index(dates)
    if rule == "Weekdays only":
        return (wd < 5).astype(float)
    if rule == "Weekends only":
        return (wd >= 5).astype(float)
    return np.ones(len(dates))


def _appliance_cube(appliances: list[Appliance], dates: np.ndarray) -> np.ndarray:
    cube = np.zeros((len(dates), 24, len(appliances)))
    for j, a in enumerate(appliances):
        kw = a.watts * a.quantity / 1000.0
        cube[:, :, j] = np.outer(_day_flags(dates, a.days), a.hourly_fraction() * kw)
    return cube


def build_dataset_from_appliances(appliances: list[Appliance], n_days: int = 30,
                                  start: date | None = None) -> EnergyDataset:
    """Expand appliance schedules into a day x hour x appliance load cube."""
    if not appliances:
        raise ValueError("Add at least one appliance first.")
    if not (1 <= n_days <= 366):
        raise ValueError("Number of days must be between 1 and 366.")
    start = start or (date.today() - timedelta(days=n_days))
    dates = np.datetime64(start, "D") + np.arange(n_days)
    cube = _appliance_cube(appliances, dates)
    names = [a.name for a in appliances]
    return EnergyDataset(
        cube, names, [a.category for a in appliances], dates, "Manual appliance schedule",
        [f"Built {n_days}-day schedule from {len(appliances)} appliance(s); "
         f"estimated {cube.sum() / n_days:,.2f} kWh/day."])


def append_appliances(dataset: EnergyDataset, appliances: list[Appliance]) -> EnergyDataset:
    """Add manual appliances to a loaded dataset (same dates) – handy for what-if purchases."""
    clash = {a.name.lower() for a in appliances} & {n.lower() for n in dataset.appliances}
    if clash:
        raise ValueError(f"Already in dataset: {', '.join(sorted(clash))}. Rename and retry.")
    extra = _appliance_cube(appliances, dataset.dates)
    return EnergyDataset(
        np.concatenate([dataset.cube, extra], axis=2),
        dataset.appliances + [a.name for a in appliances],
        dataset.categories + [a.category for a in appliances],
        dataset.dates, dataset.source + " + manual",
        dataset.report + [f"Appended {len(appliances)} manual appliance(s)."])


# --------------------------------------------------------------------------- #
#  Report tables / export
# --------------------------------------------------------------------------- #
def summary_table(analysis: dict, sim: dict | None = None) -> pd.DataFrame:
    from .analytics_engine import fmt_hour
    a = analysis
    bill = a["bill"]
    rows = [
        ("Days analysed", a["n_days"], "days"),
        ("Total energy (period)", round(a["total_kwh"], 2), "kWh"),
        ("Monthly energy (30-day basis)", round(a["monthly_kwh"], 1), "kWh"),
        ("Estimated bill (30-day basis)", round(bill["total"], 2), "INR"),
        ("  Energy charge (slabs)", round(bill["energy"], 2), "INR"),
        ("  Time-of-day adjustment", round(bill["tod"], 2), "INR"),
        ("  Fixed charge", round(bill["fixed"], 2), "INR"),
        ("  Electricity duty", round(bill["duty"], 2), "INR"),
        ("Peak hour (average day)", fmt_hour(a["peak_hour"]), ""),
        ("Highest single-hour demand", round(a["peak_load"], 3), "kW"),
        ("Highest demand occurred", a["peak_stamp"], ""),
        ("Average load", round(a["avg_load"], 3), "kW"),
        ("Base load (5th percentile)", round(a["base_load"], 3), "kW"),
        ("Load factor", round(a["load_factor"], 3), "ratio"),
        ("Peak-to-average ratio (PAR)", round(a["par"], 3), "ratio"),
        ("Share of energy in tariff peak window", round(a["peak_share"] * 100, 1), "%"),
    ]
    if sim:
        s = sim["savings"]
        rows += [
            ("--- Load-shift simulation ---", "", ""),
            ("Simulated bill (30-day basis)", round(sim["after"]["bill"]["total"], 2), "INR"),
            ("Saving per month", round(s["bill_month"], 2), "INR"),
            ("Saving per year", round(s["bill_year"], 2), "INR"),
            ("Energy saved per month", round(s["kwh_month"], 1), "kWh"),
            ("PAR before → after", f"{s['par_before']:.2f} → {s['par_after']:.2f}", "ratio"),
            ("Load factor before → after", f"{s['lf_before']:.2f} → {s['lf_after']:.2f}", "ratio"),
            ("Peak-window share before → after",
             f"{s['peak_share_before'] * 100:.1f}% → {s['peak_share_after'] * 100:.1f}%", ""),
        ]
    return pd.DataFrame(rows, columns=["Metric", "Value", "Unit"])


def hourly_table(analysis: dict) -> pd.DataFrame:
    a = analysis
    t = a["tariff"]
    window = np.where(t.peak_mask(), "peak", np.where(t.offpeak_mask(), "off-peak", "normal"))
    return pd.DataFrame({
        "hour": [f"{h:02d}:00" for h in range(24)],
        "tariff_window": window,
        "avg_load_kw_all_days": np.round(a["profile"], 4),
        "avg_load_kw_weekday": np.round(a["weekday_profile"], 4),
        "avg_load_kw_weekend": np.round(a["weekend_profile"], 4),
    })


def appliance_table(analysis: dict) -> pd.DataFrame:
    a = analysis
    total = a["appliance_kwh"].sum() or 1.0
    peak_total = a["appliance_peak_kwh"].sum() or 1.0
    days = a["n_days"]
    return pd.DataFrame({
        "appliance": a["names"],
        "category": a["categories"],
        "total_kwh": np.round(a["appliance_kwh"], 2),
        "share_percent": np.round(a["appliance_kwh"] / total * 100, 1),
        "peak_window_kwh": np.round(a["appliance_peak_kwh"], 2),
        "share_of_peak_percent": np.round(a["appliance_peak_kwh"] / peak_total * 100, 1),
        "monthly_kwh_est": np.round(a["appliance_kwh"] / days * a["tariff"].billing_days, 1),
        "avg_watts": np.round(a["appliance_kwh"] / (days * 24) * 1000, 1),
    }).sort_values("total_kwh", ascending=False)


def export_csv(df: pd.DataFrame, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path


# --------------------------------------------------------------------------- #
#  Sample dataset generator (hostel / flat, 30 days, deliberately imperfect)
# --------------------------------------------------------------------------- #
def _prob(ranges, default=0.0) -> np.ndarray:
    arr = np.full(24, default, dtype=float)
    for start, end, p in ranges:
        for h in range(start, end):
            arr[h % 24] = p
    return arr


def generate_sample_log(path: str | Path = SAMPLE_PATH, seed: int = 7, n_days: int = 30,
                        start: str = "2025-09-01") -> Path:
    """Write a realistic 30-day hourly appliance log with a few gaps for cleaning demos."""
    rng = np.random.default_rng(seed)
    # name: (category, kW when on, weekday on-probability, weekend on-probability, min/max fraction)
    specs = {
        "Refrigerator": ("Refrigeration", 0.150, None, None, (0.38, 0.46)),
        "Air Conditioner": ("Cooling", 1.500,
                            _prob([(22, 24, .55), (0, 6, .6), (19, 22, .25), (13, 17, .12)]),
                            _prob([(22, 24, .65), (0, 7, .65), (19, 22, .3), (12, 18, .3)]), (0.5, 0.7)),
        "Ceiling Fans": ("Cooling", 0.225,
                         _prob([(0, 7, .9), (7, 10, .3), (10, 18, .2), (18, 24, .9)]),
                         _prob([(0, 8, .9), (8, 18, .6), (18, 24, .9)]), (0.9, 1.0)),
        "Geyser": ("Heating", 2.000,
                   _prob([(6, 7, .8), (19, 20, .55)]),
                   _prob([(7, 9, .7), (19, 20, .5)]), (0.4, 0.6)),
        "Water Pump": ("Water Pump", 0.750,
                       _prob([(6, 7, .8), (18, 19, .5)]),
                       _prob([(7, 8, .8), (18, 19, .5)]), (0.3, 0.5)),
        "Washing Machine": ("Laundry", 0.500,
                            _prob([(19, 20, .12)]),
                            _prob([(9, 11, .6), (18, 20, .4)]), (0.7, 1.0)),
        "Iron": ("Laundry", 1.000,
                 _prob([(19, 20, .2), (7, 8, .2)]),
                 _prob([(19, 20, .15)]), (0.2, 0.4)),
        "Television": ("Electronics", 0.120,
                       _prob([(19, 24, .8)]),
                       _prob([(14, 24, .6)]), (0.8, 1.0)),
        "Laptop & Chargers": ("Electronics", 0.080,
                              _prob([(9, 24, .7)]),
                              _prob([(10, 24, .6)]), (0.8, 1.0)),
        "LED Lights": ("Lighting", 0.060,
                       _prob([(18, 24, .95), (5, 8, .8)], default=.05),
                       _prob([(18, 24, .95), (5, 8, .6)], default=.05), (0.9, 1.0)),
        "Microwave": ("Kitchen", 1.200,
                      _prob([(8, 9, .4), (20, 21, .5)]),
                      _prob([(9, 10, .4), (20, 21, .5)]), (0.1, 0.2)),
    }
    first = np.datetime64(start, "D")
    rows = []
    for d in range(n_days):
        day = first + d
        weekend = bool(weekday_index(np.array([day]))[0] >= 5)
        for h in range(24):
            stamp = f"{day} {h:02d}:00"
            for name, (cat, kw, p_wd, p_we, frac_rng) in specs.items():
                if p_wd is None:  # always-on duty-cycled load
                    kwh = kw * rng.uniform(*frac_rng) * (1.15 if 13 <= h <= 18 else 1.0)
                else:
                    p = (p_we if weekend else p_wd)[h]
                    kwh = kw * rng.uniform(*frac_rng) if rng.random() < p else 0.0
                rows.append((stamp, name, cat, round(kwh * rng.normal(1.0, 0.04), 4)))
    df = pd.DataFrame(rows, columns=["timestamp", "appliance", "category", "kwh"])
    df["kwh"] = df["kwh"].clip(lower=0)

    # ---- make it realistically messy -------------------------------------- #
    outage = rng.choice(df["timestamp"].unique(), size=6, replace=False)      # meter outages
    df = df[~df["timestamp"].isin(outage)]
    df = df.drop(df.sample(frac=0.01, random_state=seed).index)               # dropped rows
    blank = df.sample(frac=0.004, random_state=seed + 1).index
    df.loc[blank, "kwh"] = np.nan                                              # blank readings
    df = pd.concat([df, df.sample(5, random_state=seed + 2)])                  # duplicate rows
    df = df.sort_values(["timestamp", "appliance"], kind="stable")
    return export_csv(df, path)
