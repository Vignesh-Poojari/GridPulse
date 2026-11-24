# ⚡ GridPulse — Desktop Energy Load & Peak-Usage Analyzer

> Your electricity bill says *how much*. GridPulse shows you **which appliances and which hours** drive it — and what you'd save by changing habits **before** you change them.

A Python desktop app (CustomTkinter + embedded Matplotlib) that works **fully offline** on any PC.

## 1. Problem & novelty
| | |
|---|---|
| **Problem** | Bills show only total units (kWh). Households, hostels/PGs and small labs can't see what drives peak demand, higher slab tariffs or peak-hour surcharges. |
| **Users** | Residential consumers, hostel/PG residents, small campus labs. |
| **Gap** | Utility portals show monthly totals; smart-meter apps need IoT hardware. GridPulse needs only a CSV export *or* a manual appliance list, and **simulates "what-if" load shifts** first. |

## 2. Features
1. **Data Loader** – import CSV logs (hourly, 15-min, daily; kWh or Watts; with/without appliance column) or add appliances manually (watts × qty × hours × start time × weekday/weekend). Missing timestamps, blanks, negatives and duplicates are repaired and **reported**.
2. **Analytics Dashboard** – KPI cards (Total kWh, Estimated Bill ₹, Peak Hour, Load Factor, Peak-to-Average Ratio, Base Load), four switchable zoomable charts, plus plain-language tips (peak-window share, slab proximity, standby load, "quick win" savings).
3. **Peak-Shift Simulator** – per-appliance dropdown (new run time) + sliders (% of peak-window use to move, % usage reduction). New PAR, load factor, bill and monthly/yearly ₹ savings update live. **Auto-suggest** builds a plan that defers geyser/washing machine/pump/iron to the quietest off-peak hours and trims AC 10 %.
4. **Report Exporter** – summary CSVs (KPIs, hourly profile, per-appliance table), cleaned gap-free log, and 150–300 dpi PNG charts (incl. before/after).

## 3. Quick start
```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py                                         # launch the desktop app
```
Click **Load sample data** for an instant demo (30-day hostel/flat log with deliberate gaps).
Linux users may also need Tk: `sudo apt install python3-tk`.

Other commands:
```bash
python main.py --headless my_log.csv --out reports     # no GUI: CSVs + PNGs
python main.py --generate-sample                       # regenerate the sample dataset
python -m unittest discover -s tests -v                # run the tests
```

## 4. CSV format
```csv
timestamp,appliance,kwh,category
2025-09-01 19:00,Geyser,0.92,Heating
```
Accepted aliases — time: `timestamp/datetime/date(+hour)/time`; energy: `kwh/energy_kwh/consumption/units` or `watts/power`; appliance: `appliance/device/load/name`; `category` optional (auto-guessed otherwise). Day-first (`25-12-2025`) and month-first dates are auto-detected. **Daily** data loads but the app warns that hour-of-day analysis isn't meaningful.

## 5. Tariff
`data/tariff.json` holds slab limits/rates, fixed charge, duty %, the **peak window** (default 6–10 PM, +₹1/kWh) and **off-peak window** (10 PM–6 AM, −₹1/kWh). **The defaults are illustrative — edit them to match your DISCOM's current tariff order** (or load another JSON from the app).

## 6. How the numbers work
* Load cube: `(days, 24, appliances)` in kWh — a cell is also the average kW for that hour.
* **Load Factor** = average ÷ peak load · **PAR** = peak ÷ average = 1 / LF
* **Slab bill** = Σ rateᵢ · clip(units − lowerᵢ, 0, upperᵢ − lowerᵢ) (vectorised)
* **ToD adjustment** = Σₕ kWhₕ · (surcharge in peak − rebate off-peak)
* **Simulator** – *moving* a load conserves energy, so it saves via the ToD term only; *reducing* use lowers units and can drop a slab. Both are shown honestly: shifting everything to one hour can create a new, taller peak (PAR rises) — the simulator shows that trade-off.

## 7. Project structure & 4-person split
```
GridPulse/
├── data/  sample_energy_log.csv · tariff.json
├── src/
│   ├── data_handler.py      # Member 1 – pandas ingestion, cleaning, report tables, sample generator
│   ├── analytics_engine.py  # Member 2 – NumPy tariff/KPIs/simulator, auto-plan, insights
│   ├── visualizer.py        # Member 3 – 4 Matplotlib charts, peak shading, PNG export
│   └── app_ui.py            # Member 4 – CustomTkinter GUI, canvases, dialogs, integration
├── tests/test_core.py       # 20 unit tests (billing, KPIs, simulator, cleaning, validation)
├── main.py                  # Member 4 – GUI launcher + --headless mode
└── requirements.txt
```
Data flow: `CSV / manual → data_handler.EnergyDataset → analytics_engine.analyze() → visualizer / app_ui / exports`.

## 8. Viva talking points
* Why a 3-D NumPy cube? One representation → every KPI, weekday/weekend split and simulation is a vectorised reduction.
* Why does shifting not change slab? Units are conserved; only ToD changes. Reducing use changes slab.
* Cleaning policy: short gaps (≤3 h) → time interpolation; longer gaps → that appliance's median for that hour; everything is reported to the user.

## 9. Limitations / future work
Tariff is user-configured (no live utility lookup); simulation assumes the average-day shape of each appliance; no per-appliance ToD for 3-phase/commercial tariffs; ideas: solar/battery what-if, PDF reports, appliance auto-detection (NILM).
