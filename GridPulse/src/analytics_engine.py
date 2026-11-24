"""
analytics_engine.py  --  Member 2 : NumPy maths (tariffs, KPIs, load-shift simulator)

Everything here works on the *load cube*: a float array of shape
(days, 24 hours, appliances) in kWh.  Because each cell is energy over one hour,
its numeric value is also the average power in kW for that hour.

Key formulas
------------
    Load Factor (LF) = average load / peak load
    Peak-to-Average Ratio (PAR) = peak load / average load = 1 / LF
    Slab bill  = sum_i  rate_i * clip(units - lower_i, 0, upper_i - lower_i)
    ToD adjust = sum_h  kWh_h * (surcharge in peak hours  -  rebate in off-peak hours)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields
from pathlib import Path

import numpy as np

HOURS = np.arange(24)


# --------------------------------------------------------------------------- #
#  Small helpers
# --------------------------------------------------------------------------- #
def fmt_hour(h: int) -> str:
    h = int(h) % 24
    return f"{(h % 12) or 12} {'AM' if h < 12 else 'PM'}"


def window_mask(start: int, end: int) -> np.ndarray:
    """Boolean mask over 24 hours for [start, end); wraps past midnight if start > end."""
    if start < end:
        return (HOURS >= start) & (HOURS < end)
    return (HOURS >= start) | (HOURS < end)


def weekday_index(dates) -> np.ndarray:
    days = np.asarray(dates).astype("datetime64[D]").astype("int64")
    return (days + 3) % 7          # Monday = 0


def _tokens(name: str) -> list[str]:
    return re.findall(r"[a-z]+", name.lower())


# --------------------------------------------------------------------------- #
#  Tariff model
# --------------------------------------------------------------------------- #
@dataclass
class Tariff:
    """Slab tariff + optional time-of-day (ToD) adjustments.

    The defaults are ILLUSTRATIVE (modelled loosely on an Indian residential
    slab structure).  Edit ``data/tariff.json`` to match your DISCOM's latest order.
    """
    name: str = "Illustrative residential slab tariff"
    slab_limits: list = field(default_factory=lambda: [100.0, 300.0, 500.0])   # kWh/month upper limits
    slab_rates: list = field(default_factory=lambda: [4.71, 10.29, 14.55, 16.64])  # Rs/kWh, one more than limits
    fixed_charge: float = 128.0        # Rs / month
    duty_pct: float = 16.0             # % electricity duty on (energy + fixed)
    peak_start: int = 18
    peak_end: int = 22
    peak_surcharge: float = 1.0        # Rs/kWh added to units used in the peak window
    offpeak_start: int = 22
    offpeak_end: int = 6
    offpeak_rebate: float = 1.0        # Rs/kWh deducted for units used off-peak
    billing_days: int = 30

    def validate(self) -> "Tariff":
        lim = np.asarray(self.slab_limits, dtype=float)
        rates = np.asarray(self.slab_rates, dtype=float)
        if len(rates) != len(lim) + 1:
            raise ValueError("slab_rates must have exactly one more entry than slab_limits.")
        if np.any(lim <= 0) or np.any(np.diff(lim) <= 0):
            raise ValueError("slab_limits must be positive and strictly increasing.")
        if np.any(rates < 0):
            raise ValueError("slab_rates cannot be negative.")
        for key in ("peak_start", "peak_end", "offpeak_start", "offpeak_end"):
            if not (0 <= int(getattr(self, key)) <= 24):
                raise ValueError(f"{key} must be an hour between 0 and 24.")
        if self.billing_days < 1:
            raise ValueError("billing_days must be at least 1.")
        return self

    @classmethod
    def from_dict(cls, data: dict) -> "Tariff":
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Unknown tariff field(s): {', '.join(sorted(unknown))}")
        return cls(**data).validate()

    @classmethod
    def from_json(cls, path: str | Path) -> "Tariff":
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return cls.from_dict(json.load(fh))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Tariff file is not valid JSON: {exc}") from exc
        except OSError as exc:
            raise ValueError(f"Could not read tariff file: {exc}") from exc

    def peak_mask(self) -> np.ndarray:
        return window_mask(int(self.peak_start), int(self.peak_end))

    def offpeak_mask(self) -> np.ndarray:
        return window_mask(int(self.offpeak_start), int(self.offpeak_end)) & ~self.peak_mask()

    def tod_adjustment(self) -> np.ndarray:
        """Rs/kWh adjustment for each of the 24 hours."""
        return (self.peak_mask() * self.peak_surcharge) - (self.offpeak_mask() * self.offpeak_rebate)

    @property
    def peak_label(self) -> str:
        return f"{fmt_hour(self.peak_start)}–{fmt_hour(self.peak_end)}"

    @property
    def offpeak_label(self) -> str:
        return f"{fmt_hour(self.offpeak_start)}–{fmt_hour(self.offpeak_end)}"


# --------------------------------------------------------------------------- #
#  Vectorised billing
# --------------------------------------------------------------------------- #
def slab_energy_charge(units, tariff: Tariff):
    """Slab-wise energy charge for scalar or array ``units`` (kWh / billing period)."""
    units = np.asarray(units, dtype=float)
    lower = np.concatenate(([0.0], np.asarray(tariff.slab_limits, dtype=float)))
    upper = np.concatenate((np.asarray(tariff.slab_limits, dtype=float), [np.inf]))
    in_slab = np.clip(units[..., None] - lower, 0.0, upper - lower)     # (..., n_slabs)
    return in_slab @ np.asarray(tariff.slab_rates, dtype=float)


def compute_bill(monthly_kwh: float, tod_adjustment: float, tariff: Tariff) -> dict:
    energy = float(slab_energy_charge(monthly_kwh, tariff))
    tod = max(float(tod_adjustment), -energy)              # a rebate can't push the bill negative
    subtotal = energy + tod + tariff.fixed_charge
    duty = subtotal * tariff.duty_pct / 100.0
    return {"energy": energy, "tod": tod, "fixed": float(tariff.fixed_charge),
            "duty": duty, "total": subtotal + duty}


def slab_info(monthly_kwh: float, tariff: Tariff) -> dict:
    """Which slab the user is in, the marginal rate, and the distance to the next slab."""
    limits = np.asarray(tariff.slab_limits, dtype=float)
    rates = np.asarray(tariff.slab_rates, dtype=float)
    idx = int(np.searchsorted(limits, monthly_kwh, side="right"))
    return {
        "index": idx,
        "rate": float(rates[idx]),
        "to_next": float(limits[idx] - monthly_kwh) if idx < len(limits) else None,
        "next_rate": float(rates[idx + 1]) if idx < len(limits) else None,
        "above_lower": float(monthly_kwh - limits[idx - 1]) if idx > 0 else None,
        "lower_rate": float(rates[idx - 1]) if idx > 0 else None,
    }


def load_factor(hourly_kw: np.ndarray) -> float:
    peak = float(np.max(hourly_kw))
    return float(np.mean(hourly_kw)) / peak if peak > 0 else 0.0


def peak_to_average_ratio(hourly_kw: np.ndarray) -> float:
    avg = float(np.mean(hourly_kw))
    return float(np.max(hourly_kw)) / avg if avg > 0 else 0.0


# --------------------------------------------------------------------------- #
#  Full analysis
# --------------------------------------------------------------------------- #
def analyze(cube: np.ndarray, dates: np.ndarray, names: list[str],
            categories: list[str], tariff: Tariff) -> dict:
    """Compute every KPI, profile and breakdown the UI / charts / reports need."""
    cube = np.asarray(cube, dtype=float)
    n_days = cube.shape[0]
    series = cube.sum(axis=2)                      # (days, 24) total kW per hour
    flat = series.ravel()
    total = float(flat.sum())
    profile = series.mean(axis=0)                  # (24,) typical day

    wd = weekday_index(dates)
    weekend = wd >= 5
    nan24 = np.full(24, np.nan)
    wd_profile = series[~weekend].mean(axis=0) if (~weekend).any() else nan24
    we_profile = series[weekend].mean(axis=0) if weekend.any() else nan24

    scale = tariff.billing_days / n_days
    monthly_kwh = total * scale
    tod_month = float((profile * tariff.tod_adjustment()).sum() * tariff.billing_days)
    bill = compute_bill(monthly_kwh, tod_month, tariff)

    peak_mask = tariff.peak_mask()
    d_idx, h_idx = np.unravel_index(int(np.argmax(series)), series.shape)

    return {
        "tariff": tariff, "names": list(names), "categories": list(categories),
        "n_days": n_days, "dates": dates,
        "total_kwh": total, "monthly_kwh": monthly_kwh, "daily_kwh": float(profile.sum()),
        "avg_load": float(flat.mean()), "peak_load": float(flat.max()),
        "base_load": float(np.percentile(flat, 5)),
        "load_factor": load_factor(flat), "par": peak_to_average_ratio(flat),
        "peak_hour": int(np.argmax(profile)),
        "peak_stamp": f"{np.datetime_as_string(dates[d_idx], unit='D')} {h_idx:02d}:00",
        "profile": profile, "weekday_profile": wd_profile, "weekend_profile": we_profile,
        "n_weekday": int((~weekend).sum()), "n_weekend": int(weekend.sum()),
        "appliance_kwh": cube.sum(axis=(0, 1)),
        "appliance_peak_kwh": cube[:, peak_mask, :].sum(axis=(0, 1)),
        "peak_share": float(profile[peak_mask].sum() / profile.sum()) if profile.sum() > 0 else 0.0,
        "bill": bill, "tod_month": tod_month,
    }


# --------------------------------------------------------------------------- #
#  Peak-shift simulator
# --------------------------------------------------------------------------- #
def simulate(cube: np.ndarray, dates: np.ndarray, names: list[str], categories: list[str],
             tariff: Tariff, plan: dict, before: dict | None = None):
    """Apply a what-if plan and return (new_cube, analysis_after, savings).

    ``plan`` maps appliance index -> {"target": hour, "move_pct": 0-100, "reduce_pct": 0-100}
      * reduce_pct : cut the appliance's usage everywhere (e.g. shorter runs, higher AC set-point)
      * move_pct   : share of its peak-window energy relocated; the moved block keeps its shape
                     and starts at ``target`` hour (wrapping past midnight).
    Total energy is conserved by moving, so slab charges only change via reduce_pct;
    moving saves money through the time-of-day adjustment.
    """
    if before is None:
        before = analyze(cube, dates, names, categories, tariff)
    new = np.array(cube, dtype=float, copy=True)
    peak = tariff.peak_mask().astype(float)
    order = (int(tariff.peak_start) + HOURS) % 24

    for idx, cfg in plan.items():
        reduce = float(np.clip(cfg.get("reduce_pct", 0.0), 0, 100)) / 100.0
        frac = float(np.clip(cfg.get("move_pct", 0.0), 0, 100)) / 100.0
        target = int(cfg.get("target", tariff.offpeak_start)) % 24
        usage = new[:, :, idx] * (1.0 - reduce)                 # (days, 24)
        moved = usage * peak * frac
        stays = usage - moved
        moved_profile = moved.sum(axis=0)
        if moved_profile.sum() > 1e-12:
            first = int(order[np.argmax(moved_profile[order] > 1e-12)])
            moved = np.roll(moved, (target - first) % 24, axis=1)
        new[:, :, idx] = stays + moved

    after = analyze(new, dates, names, categories, tariff)
    return new, after, compare(before, after)


def compare(before: dict, after: dict) -> dict:
    scale = before["tariff"].billing_days
    return {
        "kwh_month": before["monthly_kwh"] - after["monthly_kwh"],
        "bill_month": before["bill"]["total"] - after["bill"]["total"],
        "bill_year": (before["bill"]["total"] - after["bill"]["total"]) * 12,
        "par_before": before["par"], "par_after": after["par"],
        "lf_before": before["load_factor"], "lf_after": after["load_factor"],
        "peak_share_before": before["peak_share"], "peak_share_after": after["peak_share"],
        "peak_load_before": before["peak_load"], "peak_load_after": after["peak_load"],
        "bill_before": before["bill"]["total"], "bill_after": after["bill"]["total"],
        "billing_days": scale,
    }


_SHIFTABLE = ("geyser", "heater", "immersion", "washing", "washer", "dryer", "iron",
              "dishwasher", "pump", "motor", "ev")
_AC_WORDS = ("ac", "aircon", "conditioner")


def auto_plan(cube: np.ndarray, analysis: dict) -> dict:
    """Sensible starting plan: defer shiftable appliances to off-peak, trim AC use by 10 %.

    Each deferrable appliance is greedily placed at the off-peak start hour that gives
    the lowest resulting peak demand, so loads don't stack on top of existing night load.
    """
    tariff = analysis["tariff"]
    names, cats, dates = analysis["names"], analysis["categories"], analysis["dates"]
    candidates = [int(h) for h in HOURS if tariff.offpeak_mask()[int(h)]] or [int(tariff.offpeak_start) % 24]
    plan: dict = {}
    for i, name in enumerate(names):
        tokens = _tokens(name)
        if any(t in _AC_WORDS for t in tokens):
            plan[i] = {"target": int(tariff.offpeak_start), "move_pct": 0.0, "reduce_pct": 10.0}
    for i, name in enumerate(names):
        tokens = _tokens(name)
        if i in plan or analysis["appliance_peak_kwh"][i] <= 0:
            continue
        if any(t == kw or (len(kw) > 3 and t.startswith(kw)) for t in tokens for kw in _SHIFTABLE):
            best_hour, best_key = candidates[0], None
            for hour in candidates:
                trial = dict(plan)
                trial[i] = {"target": hour, "move_pct": 100.0, "reduce_pct": 0.0}
                _, after, _ = simulate(cube, dates, names, cats, tariff, trial, before=analysis)
                key = (round(after["peak_load"], 6), round(after["bill"]["total"], 2))
                if best_key is None or key < best_key:
                    best_hour, best_key = hour, key
            plan[i] = {"target": best_hour, "move_pct": 100.0, "reduce_pct": 0.0}
    return plan


# --------------------------------------------------------------------------- #
#  Plain-language insights
# --------------------------------------------------------------------------- #
def generate_insights(a: dict, potential: dict | None = None) -> list[str]:
    t: Tariff = a["tariff"]
    tips: list[str] = []
    days = a["n_days"]

    tips.append(f"{a['peak_share'] * 100:.0f}% of your daily energy "
                f"({a['profile'][t.peak_mask()].sum():.1f} of {a['daily_kwh']:.1f} kWh) is used in the "
                f"{t.peak_label} peak window.")

    peak_kwh = a["appliance_peak_kwh"]
    if peak_kwh.sum() > 0:
        top = np.argsort(peak_kwh)[::-1][:3]
        parts = [f"{a['names'][i]} ({peak_kwh[i] / peak_kwh.sum() * 100:.0f}%)"
                 for i in top if peak_kwh[i] > 0]
        tips.append("Biggest peak-window users: " + ", ".join(parts) + ".")

    info = slab_info(a["monthly_kwh"], t)
    tips.append(f"At ~{a['monthly_kwh']:.0f} units/month you are in the ₹{info['rate']:.2f}/unit slab.")
    if info["to_next"] is not None and info["to_next"] <= 0.15 * max(a["monthly_kwh"], 1):
        tips.append(f"Only {info['to_next']:.0f} more units would push you into the ₹{info['next_rate']:.2f}/unit slab.")
    if info["above_lower"] is not None and info["above_lower"] <= 0.25 * max(a["monthly_kwh"], 1):
        tips.append(f"Cutting just {info['above_lower']:.0f} units/month would drop you to the "
                    f"cheaper ₹{info['lower_rate']:.2f}/unit slab.")

    lf = a["load_factor"]
    if lf < 0.35:
        tips.append(f"Load factor {lf:.2f} is low: demand is very spiky. Spread heavy appliances out.")
    elif lf < 0.55:
        tips.append(f"Load factor {lf:.2f} is moderate: there is room to flatten the peaks.")
    else:
        tips.append(f"Load factor {lf:.2f} is healthy: your usage is fairly even through the day.")

    base_kwh_month = a["base_load"] * 24 * t.billing_days
    tips.append(f"Always-on base load is ~{a['base_load'] * 1000:.0f} W (≈{base_kwh_month:.0f} kWh/month). "
                "Check standby devices.")

    if potential:
        tips.append(f"Quick win: an automatic plan (defer geyser / washing / pump / iron to off-peak, "
                    f"trim AC 10%) could save about ₹{potential['bill_month']:.0f}/month "
                    f"(₹{potential['bill_year']:.0f}/year). Try it in the Peak-Shift Simulator tab.")
    return tips
