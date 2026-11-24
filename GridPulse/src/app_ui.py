"""
app_ui.py  --  Member 4 : CustomTkinter desktop GUI + embedded Matplotlib canvases

Layout: one window, four tabs.
    1. Data Loader      - import CSV / add appliances / load tariff
    2. Analytics        - KPI cards + switchable charts + plain-language tips
    3. Peak-Shift Sim   - per-appliance what-if sliders, live savings
    4. Report Exporter  - CSV summaries + high-res PNG charts

``GridPulseApp`` owns the shared state (dataset, tariff, analysis); each tab is a
small class that reads that state and is told to ``refresh()`` when it changes.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

from . import analytics_engine as ae
from . import data_handler as dh
from . import visualizer as viz

HOUR_LABELS = [f"{h:02d}:00" for h in range(24)]
TAB_LOADER = "1  Data Loader"
TAB_DASH = "2  Analytics Dashboard"
TAB_SIM = "3  Peak-Shift Simulator"
TAB_EXPORT = "4  Report Exporter"


def rupees(x: float) -> str:
    return f"₹{x:,.0f}"


# --------------------------------------------------------------------------- #
#  Reusable widgets
# --------------------------------------------------------------------------- #
class KPICard(ctk.CTkFrame):
    def __init__(self, master, title: str, accent: str = "#2563eb"):
        super().__init__(master, corner_radius=10, border_width=1, border_color="#d1d5db")
        ctk.CTkLabel(self, text=title.upper(), font=ctk.CTkFont(size=11, weight="bold"),
                     text_color="#6b7280").pack(anchor="w", padx=12, pady=(10, 0))
        self.value = ctk.CTkLabel(self, text="—", font=ctk.CTkFont(size=24, weight="bold"),
                                  text_color=accent)
        self.value.pack(anchor="w", padx=12)
        self.sub = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=11), text_color="#6b7280",
                                wraplength=190, justify="left")
        self.sub.pack(anchor="w", padx=12, pady=(0, 10))

    def set(self, value: str, sub: str = ""):
        self.value.configure(text=value)
        self.sub.configure(text=sub)


class ChartPanel(ctk.CTkFrame):
    """A Matplotlib figure embedded in Tk with the standard zoom/pan toolbar."""

    def __init__(self, master):
        super().__init__(master, fg_color="white", corner_radius=8, border_width=1,
                         border_color="#d1d5db")
        self.fig = viz.new_figure()
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.toolbar = NavigationToolbar2Tk(self.canvas, self, pack_toolbar=False)
        self.toolbar.update()
        self.toolbar.pack(side="bottom", fill="x")                   # pack toolbar first
        self.canvas.get_tk_widget().pack(side="top", fill="both", expand=True, padx=4, pady=4)

    def render(self, builder, *args):
        self.fig.clear()
        builder(self.fig, *args)
        self.canvas.draw_idle()


def set_text(box: ctk.CTkTextbox, text: str):
    box.configure(state="normal")
    box.delete("1.0", "end")
    box.insert("end", text)
    box.configure(state="disabled")


# --------------------------------------------------------------------------- #
#  Tab 1 – Data loader & manual entry
# --------------------------------------------------------------------------- #
class LoaderTab(ctk.CTkFrame):
    def __init__(self, master, app: "GridPulseApp"):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.appliances: list[dh.Appliance] = []
        self.columnconfigure(0, weight=1, uniform="half")
        self.columnconfigure(1, weight=1, uniform="half")
        self.rowconfigure(0, weight=1)
        self._build_import_panel()
        self._build_manual_panel()

    # ---- left: import ------------------------------------------------------ #
    def _build_import_panel(self):
        panel = ctk.CTkFrame(self)
        panel.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=4)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(4, weight=1)
        ctk.CTkLabel(panel, text="Import meter / appliance log",
                     font=ctk.CTkFont(size=16, weight="bold")).grid(row=0, column=0, sticky="w", padx=14, pady=(12, 2))
        ctk.CTkLabel(panel, justify="left", text_color="#6b7280", wraplength=480,
                     text=("CSV columns:  timestamp, appliance, kwh   (category optional).\n"
                           "Also accepted: watts instead of kwh, a date + hour pair, no appliance "
                           "column (whole-house meter), 15-min or daily readings. Missing hours, "
                           "blanks and duplicates are repaired automatically.")
                     ).grid(row=1, column=0, sticky="w", padx=14)
        row = ctk.CTkFrame(panel, fg_color="transparent")
        row.grid(row=2, column=0, sticky="w", padx=10, pady=10)
        ctk.CTkButton(row, text="📂  Open CSV…", command=self.open_csv, width=130).pack(side="left", padx=4)
        ctk.CTkButton(row, text="Load sample data", command=self.load_sample, width=130,
                      fg_color="#059669", hover_color="#047857").pack(side="left", padx=4)
        ctk.CTkButton(row, text="Load tariff JSON…", command=self.load_tariff, width=130,
                      fg_color="#6b7280", hover_color="#4b5563").pack(side="left", padx=4)
        self.tariff_label = ctk.CTkLabel(panel, text="", text_color="#6b7280", anchor="w", wraplength=480,
                                         justify="left")
        self.tariff_label.grid(row=3, column=0, sticky="w", padx=14)
        self.log = ctk.CTkTextbox(panel, state="disabled", wrap="word")
        self.log.grid(row=4, column=0, sticky="nsew", padx=12, pady=12)
        set_text(self.log, "No data loaded yet.\n\nTip: click 'Load sample data' for an instant demo.")
        self.update_tariff_label()

    def update_tariff_label(self):
        t = self.app.tariff
        self.tariff_label.configure(
            text=f"Tariff: {t.name}\nPeak window {t.peak_label} (+₹{t.peak_surcharge:.2f}/kWh) · "
                 f"off-peak {t.offpeak_label} (−₹{t.offpeak_rebate:.2f}/kWh) · billing period {t.billing_days} days")

    def open_csv(self):
        path = filedialog.askopenfilename(title="Select energy log",
                                          filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if path:
            self._load_path(path)

    def load_sample(self):
        try:
            if not dh.SAMPLE_PATH.exists():
                dh.generate_sample_log()
            self._load_path(dh.SAMPLE_PATH)
        except OSError as exc:
            messagebox.showerror("Sample data", f"Could not create the sample file:\n{exc}")

    def _load_path(self, path):
        try:
            dataset = dh.load_csv(path)
        except dh.DataLoadError as exc:
            messagebox.showerror("Could not load file", str(exc))
            return
        except Exception as exc:  # unexpected – never crash the GUI
            messagebox.showerror("Unexpected error", f"{type(exc).__name__}: {exc}")
            return
        self.app.set_dataset(dataset)

    def load_tariff(self):
        path = filedialog.askopenfilename(title="Select tariff JSON",
                                          filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            self.app.set_tariff(ae.Tariff.from_json(path))
        except ValueError as exc:
            messagebox.showerror("Invalid tariff", str(exc))

    def show_dataset(self, ds: dh.EnergyDataset):
        lines = [ds.describe(), "", "Cleaning report:"] + [f"  • {r}" for r in ds.report]
        lines += ["", "Appliances / meters (by energy):"]
        total = ds.cube.sum() or 1.0
        for i, name in enumerate(ds.appliances):
            kwh = ds.cube[:, :, i].sum()
            lines.append(f"  {name:<24} {ds.categories[i]:<14} {kwh:9.1f} kWh  ({kwh / total * 100:4.1f}%)")
        set_text(self.log, "\n".join(lines))

    # ---- right: manual entry ----------------------------------------------- #
    def _build_manual_panel(self):
        panel = ctk.CTkFrame(self)
        panel.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=4)
        panel.columnconfigure((1, 3), weight=1)
        panel.rowconfigure(6, weight=1)
        ctk.CTkLabel(panel, text="Add appliances manually",
                     font=ctk.CTkFont(size=16, weight="bold")).grid(row=0, column=0, columnspan=4, sticky="w", padx=14, pady=(12, 6))

        self.preset = ctk.CTkOptionMenu(panel, values=list(dh.APPLIANCE_PRESETS), command=self._apply_preset)
        ctk.CTkLabel(panel, text="Preset").grid(row=1, column=0, sticky="e", padx=(14, 6), pady=3)
        self.preset.grid(row=1, column=1, columnspan=3, sticky="ew", padx=(0, 14), pady=3)

        self.e_name = self._entry(panel, "Name", 2, 0)
        self.e_watts = self._entry(panel, "Watts", 2, 2)
        self.e_qty = self._entry(panel, "Quantity", 3, 0)
        self.e_hours = self._entry(panel, "Hours / day", 3, 2)
        self.e_qty.insert(0, "1")

        ctk.CTkLabel(panel, text="Start time").grid(row=4, column=0, sticky="e", padx=(14, 6), pady=3)
        self.start_menu = ctk.CTkOptionMenu(panel, values=HOUR_LABELS, width=100)
        self.start_menu.set("18:00")
        self.start_menu.grid(row=4, column=1, sticky="w", pady=3)
        ctk.CTkLabel(panel, text="Runs on").grid(row=4, column=2, sticky="e", padx=(8, 6), pady=3)
        self.days_menu = ctk.CTkOptionMenu(panel, values=list(dh.DAY_RULES), width=140)
        self.days_menu.grid(row=4, column=3, sticky="w", padx=(0, 14), pady=3)

        btns = ctk.CTkFrame(panel, fg_color="transparent")
        btns.grid(row=5, column=0, columnspan=4, sticky="w", padx=10, pady=6)
        ctk.CTkButton(btns, text="＋ Add appliance", width=130, command=self.add_appliance).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Remove selected", width=120, fg_color="#6b7280", hover_color="#4b5563",
                      command=self.remove_selected).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Clear list", width=90, fg_color="#6b7280", hover_color="#4b5563",
                      command=self.clear_list).pack(side="left", padx=4)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", rowheight=26, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        holder = ctk.CTkFrame(panel, fg_color="transparent")
        holder.grid(row=6, column=0, columnspan=4, sticky="nsew", padx=12, pady=(0, 6))
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        cols = ("name", "watts", "qty", "hours", "start", "days", "kwh")
        self.tree = ttk.Treeview(holder, columns=cols, show="headings", height=7)
        for col, text, width in (("name", "Appliance", 150), ("watts", "Watts", 60), ("qty", "Qty", 40),
                                 ("hours", "Hrs/day", 60), ("start", "Start", 55), ("days", "Runs on", 95),
                                 ("kwh", "kWh/day", 65)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w" if col == "name" else "center")
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)

        build = ctk.CTkFrame(panel, fg_color="transparent")
        build.grid(row=7, column=0, columnspan=4, sticky="ew", padx=10, pady=(0, 12))
        ctk.CTkLabel(build, text="Days:").pack(side="left", padx=(4, 4))
        self.e_days = ctk.CTkEntry(build, width=55)
        self.e_days.insert(0, "30")
        self.e_days.pack(side="left")
        ctk.CTkButton(build, text="Build dataset from list", command=self.build_dataset).pack(side="left", padx=8)
        ctk.CTkButton(build, text="Append to loaded data", fg_color="#6b7280", hover_color="#4b5563",
                      command=self.append_to_loaded).pack(side="left")

    @staticmethod
    def _entry(panel, label, row, col):
        ctk.CTkLabel(panel, text=label).grid(row=row, column=col, sticky="e", padx=(14 if col == 0 else 8, 6), pady=3)
        entry = ctk.CTkEntry(panel)
        entry.grid(row=row, column=col + 1, sticky="ew", padx=(0, 14 if col == 2 else 0), pady=3)
        return entry

    def _apply_preset(self, choice: str):
        name, watts, qty, hours, start = dh.APPLIANCE_PRESETS[choice]
        for entry, value in ((self.e_name, name), (self.e_watts, watts), (self.e_qty, qty), (self.e_hours, hours)):
            entry.delete(0, "end")
            entry.insert(0, value)
        self.start_menu.set(f"{int(start):02d}:00")

    def add_appliance(self):
        try:
            appliance = dh.parse_appliance(self.e_name.get(), self.e_watts.get(), self.e_qty.get(),
                                           self.e_hours.get(), self.start_menu.get()[:2], self.days_menu.get())
            if appliance.name.lower() in {a.name.lower() for a in self.appliances}:
                raise ValueError(f"'{appliance.name}' is already in the list. Use a different name.")
        except ValueError as exc:
            messagebox.showwarning("Check your entry", str(exc))
            return
        self.appliances.append(appliance)
        self._refresh_tree()
        self.e_name.delete(0, "end")

    def remove_selected(self):
        selected = {int(i) for i in self.tree.selection()}
        self.appliances = [a for i, a in enumerate(self.appliances) if i not in selected]
        self._refresh_tree()

    def clear_list(self):
        self.appliances = []
        self._refresh_tree()

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for i, a in enumerate(self.appliances):
            self.tree.insert("", "end", iid=str(i), values=(
                a.name, f"{a.watts:g}", a.quantity, f"{a.hours_per_day:g}", f"{a.start_hour:02d}:00",
                a.days, f"{a.daily_kwh:.2f}"))

    def _days_value(self):
        try:
            return int(float(self.e_days.get()))
        except ValueError:
            raise ValueError("Days must be a whole number between 1 and 366.") from None

    def build_dataset(self):
        try:
            dataset = dh.build_dataset_from_appliances(self.appliances, self._days_value())
        except ValueError as exc:
            messagebox.showwarning("Cannot build dataset", str(exc))
            return
        self.app.set_dataset(dataset)

    def append_to_loaded(self):
        if self.app.dataset is None:
            messagebox.showinfo("No data", "Load a CSV (or the sample) first, or use 'Build dataset from list'.")
            return
        try:
            if not self.appliances:
                raise ValueError("Add at least one appliance to the list first.")
            self.app.set_dataset(dh.append_appliances(self.app.dataset, self.appliances))
        except ValueError as exc:
            messagebox.showwarning("Cannot append", str(exc))


# --------------------------------------------------------------------------- #
#  Tab 2 – Analytics dashboard
# --------------------------------------------------------------------------- #
class DashboardTab(ctk.CTkFrame):
    def __init__(self, master, app: "GridPulseApp"):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        cards = ctk.CTkFrame(self, fg_color="transparent")
        cards.grid(row=0, column=0, sticky="ew", pady=(2, 8))
        self.cards = {}
        for i, (key, title, accent) in enumerate((
                ("total", "Total energy", "#2563eb"), ("bill", "Estimated bill", "#dc2626"),
                ("peak", "Peak hour", "#d97706"), ("lf", "Load factor", "#059669"),
                ("par", "Peak-to-avg ratio", "#7c3aed"), ("base", "Base load", "#0e7490"))):
            card = KPICard(cards, title, accent)
            card.grid(row=0, column=i, sticky="nsew", padx=4)
            cards.columnconfigure(i, weight=1, uniform="kpi")
            self.cards[key] = card

        self.switch = ctk.CTkSegmentedButton(self, values=list(viz.CHART_TITLES), command=lambda _v: self.draw_chart())
        self.switch.set(viz.CHART_TITLES[0])
        self.switch.grid(row=1, column=0, sticky="w", pady=(0, 6))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.chart = ChartPanel(body)
        self.chart.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        side = ctk.CTkFrame(body, width=300)
        side.grid(row=0, column=1, sticky="ns")
        side.grid_propagate(False)
        side.rowconfigure(1, weight=1)
        side.columnconfigure(0, weight=1)
        ctk.CTkLabel(side, text="What the numbers say", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=(10, 4))
        self.tips = ctk.CTkTextbox(side, wrap="word", state="disabled", width=280)
        self.tips.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.refresh()

    def refresh(self):
        a = self.app.analysis
        if a is None:
            for card in self.cards.values():
                card.set("—", "")
            set_text(self.tips, "Load data in tab 1 to see KPIs and tips.")
            self.chart.render(viz.plot_empty)
            return
        t = a["tariff"]
        info = ae.slab_info(a["monthly_kwh"], t)
        b = a["bill"]
        self.cards["total"].set(f"{a['total_kwh']:,.1f} kWh",
                                f"{a['n_days']} day(s) · ≈{a['monthly_kwh']:,.0f} kWh per {t.billing_days} days")
        self.cards["bill"].set(rupees(b["total"]), f"per {t.billing_days} days · marginal ₹{info['rate']:.2f}/unit")
        self.cards["peak"].set(ae.fmt_hour(a["peak_hour"]),
                               f"avg {a['profile'][a['peak_hour']]:.2f} kW · max {a['peak_load']:.2f} kW")
        self.cards["lf"].set(f"{a['load_factor']:.2f}", "average ÷ peak load")
        self.cards["par"].set(f"{a['par']:.2f}", "peak ÷ average load")
        self.cards["base"].set(f"{a['base_load'] * 1000:.0f} W", "always-on (5th percentile)")

        potential = None
        try:
            plan = ae.auto_plan(self.app.dataset.cube, a)
            if plan:
                ds = self.app.dataset
                _, _, potential = ae.simulate(ds.cube, ds.dates, ds.appliances, ds.categories, t, plan, before=a)
                if potential["bill_month"] <= 0:
                    potential = None
        except Exception:  # tips are best-effort; never block the dashboard
            potential = None
        set_text(self.tips, "\n\n".join("• " + tip for tip in ae.generate_insights(a, potential)))
        self.draw_chart()

    def draw_chart(self):
        a = self.app.analysis
        if a is None:
            return
        index = viz.CHART_TITLES.index(self.switch.get())
        self.chart.render(viz.build_chart, index, a, self.app.simulator.last_result)

    def draw_chart_if_sim_selected(self):
        """Keep the 'Before vs After' chart in sync while sliders move in tab 3."""
        if self.switch.get() == viz.CHART_TITLES[3]:
            self.draw_chart()


# --------------------------------------------------------------------------- #
#  Tab 3 – Peak-shift simulator
# --------------------------------------------------------------------------- #
class SimulatorTab(ctk.CTkFrame):
    def __init__(self, master, app: "GridPulseApp"):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.rows: dict[int, dict] = {}
        self.last_result: dict | None = None
        self._job = None
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        left = ctk.CTkFrame(self, width=470)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 8), pady=2)
        left.grid_propagate(False)
        left.rowconfigure(3, weight=1)
        left.columnconfigure(0, weight=1)
        ctk.CTkLabel(left, text="What if I shifted these loads?", font=ctk.CTkFont(size=16, weight="bold")
                     ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 0))
        self.hint = ctk.CTkLabel(left, text="", text_color="#6b7280", wraplength=430, justify="left")
        self.hint.grid(row=1, column=0, sticky="w", padx=12)
        btns = ctk.CTkFrame(left, fg_color="transparent")
        btns.grid(row=2, column=0, sticky="w", padx=8, pady=6)
        ctk.CTkButton(btns, text="✨ Auto-suggest plan", width=150, command=self.auto_suggest).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Reset", width=80, fg_color="#6b7280", hover_color="#4b5563",
                      command=self.reset).pack(side="left", padx=4)
        self.scroll = ctk.CTkScrollableFrame(left)
        self.scroll.grid(row=3, column=0, sticky="nsew", padx=6, pady=(0, 8))

        right = ctk.CTkFrame(self, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        cards = ctk.CTkFrame(right, fg_color="transparent")
        cards.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.cards = {}
        for i, (key, title, accent) in enumerate((
                ("bill", "Bill: now → after", "#374151"), ("month", "Saving / month", "#059669"),
                ("year", "Saving / year", "#059669"), ("par", "Peak-to-avg ratio", "#7c3aed"),
                ("lf", "Load factor", "#0e7490"), ("share", "Peak-window share", "#d97706"))):
            card = KPICard(cards, title, accent)
            card.value.configure(font=ctk.CTkFont(size=19, weight="bold"))
            card.grid(row=i // 3, column=i % 3, sticky="nsew", padx=4, pady=3)
            cards.columnconfigure(i % 3, weight=1, uniform="simkpi")
            self.cards[key] = card
        self.chart = ChartPanel(right)
        self.chart.grid(row=1, column=0, sticky="nsew")
        self.refresh()

    # ---- building the control rows ---------------------------------------- #
    def refresh(self):
        """Called whenever the dataset or tariff changes."""
        for child in self.scroll.winfo_children():
            child.destroy()
        self.rows.clear()
        self.last_result = None
        a = self.app.analysis
        if a is None:
            self.hint.configure(text="Load data first (tab 1).")
            for card in self.cards.values():
                card.set("—", "")
            self.chart.render(viz.plot_empty, "Load data to run the simulator")
            return
        t = a["tariff"]
        self.hint.configure(
            text=f"Peak window {t.peak_label}. Choose when each appliance should run instead and how much of "
                 f"its peak-window use to move. 'Reduce use' models shorter runs / efficiency (this can drop you into a cheaper slab).")
        for i, name in enumerate(a["names"]):
            self._add_row(i, name, a["appliance_peak_kwh"][i] / a["n_days"], t)
        self.update_simulation()

    def _add_row(self, idx: int, name: str, peak_kwh_day: float, tariff: ae.Tariff):
        frame = ctk.CTkFrame(self.scroll, border_width=1, border_color="#e5e7eb")
        frame.pack(fill="x", pady=4, padx=2)
        frame.columnconfigure(1, weight=1)
        ctk.CTkLabel(frame, text=name, font=ctk.CTkFont(size=13, weight="bold"), anchor="w"
                     ).grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(6, 0))
        ctk.CTkLabel(frame, text=f"{peak_kwh_day:.2f} kWh/day in peak", text_color="#6b7280", font=ctk.CTkFont(size=11)
                     ).grid(row=0, column=2, sticky="e", padx=10, pady=(6, 0))

        ctk.CTkLabel(frame, text="Run at", width=80, anchor="w").grid(row=1, column=0, padx=(10, 0), pady=2)
        target = ctk.CTkOptionMenu(frame, values=HOUR_LABELS, width=90, command=lambda _v: self.schedule_update())
        target.set(f"{int(tariff.offpeak_start) % 24:02d}:00")
        target.grid(row=1, column=1, sticky="w", pady=2)

        move_lbl = ctk.CTkLabel(frame, text="0%", width=44)
        ctk.CTkLabel(frame, text="Move peak use", width=80, anchor="w").grid(row=2, column=0, padx=(10, 0))
        move = ctk.CTkSlider(frame, from_=0, to=100, number_of_steps=20,
                             command=lambda v, lbl=move_lbl: self._slider(lbl, v))
        move.set(0)
        move.grid(row=2, column=1, sticky="ew", padx=6)
        move_lbl.grid(row=2, column=2, padx=(0, 8))

        red_lbl = ctk.CTkLabel(frame, text="0%", width=44)
        ctk.CTkLabel(frame, text="Reduce use", width=80, anchor="w").grid(row=3, column=0, padx=(10, 0), pady=(0, 6))
        reduce = ctk.CTkSlider(frame, from_=0, to=50, number_of_steps=25,
                               command=lambda v, lbl=red_lbl: self._slider(lbl, v))
        reduce.set(0)
        reduce.grid(row=3, column=1, sticky="ew", padx=6, pady=(0, 6))
        red_lbl.grid(row=3, column=2, padx=(0, 8), pady=(0, 6))
        self.rows[idx] = {"target": target, "move": move, "reduce": reduce, "move_lbl": move_lbl, "red_lbl": red_lbl}

    def _slider(self, label: ctk.CTkLabel, value: float):
        label.configure(text=f"{value:.0f}%")
        self.schedule_update()

    def schedule_update(self):
        if self._job is not None:
            self.after_cancel(self._job)
        self._job = self.after(120, self.update_simulation)    # debounce while dragging

    def _collect_plan(self) -> dict:
        plan = {}
        for idx, row in self.rows.items():
            move, reduce = float(row["move"].get()), float(row["reduce"].get())
            if move > 0 or reduce > 0:
                plan[idx] = {"target": int(row["target"].get()[:2]), "move_pct": move, "reduce_pct": reduce}
        return plan

    def _apply_plan(self, plan: dict):
        for idx, row in self.rows.items():
            cfg = plan.get(idx, {"target": self.app.tariff.offpeak_start, "move_pct": 0, "reduce_pct": 0})
            row["target"].set(f"{int(cfg['target']) % 24:02d}:00")
            row["move"].set(cfg["move_pct"])
            row["reduce"].set(cfg["reduce_pct"])
            row["move_lbl"].configure(text=f"{cfg['move_pct']:.0f}%")
            row["red_lbl"].configure(text=f"{cfg['reduce_pct']:.0f}%")
        self.update_simulation()

    def auto_suggest(self):
        a, ds = self.app.analysis, self.app.dataset
        if a is None:
            return
        plan = ae.auto_plan(ds.cube, a)
        if not plan:
            messagebox.showinfo("Auto-suggest", "No shiftable appliances (geyser, washing machine, pump, iron, AC…) "
                                                "with peak-window use were found.")
            return
        self._apply_plan(plan)

    def reset(self):
        self._apply_plan({})

    # ---- running the simulation ------------------------------------------- #
    def update_simulation(self):
        self._job = None
        a, ds = self.app.analysis, self.app.dataset
        if a is None:
            return
        plan = self._collect_plan()
        _, after, savings = ae.simulate(ds.cube, ds.dates, ds.appliances, ds.categories, a["tariff"], plan, before=a)
        self.last_result = {"plan": plan, "before": a, "after": after, "savings": savings} if plan else None
        s = savings
        self.cards["bill"].set(f"{rupees(s['bill_before'])} → {rupees(s['bill_after'])}", f"per {a['tariff'].billing_days} days")
        self.cards["month"].set(rupees(s["bill_month"]), f"{s['kwh_month']:+.1f} kWh saved / month")
        self.cards["year"].set(rupees(s["bill_year"]), "if habits hold all year")
        self.cards["par"].set(f"{s['par_before']:.2f} → {s['par_after']:.2f}", "lower is flatter")
        self.cards["lf"].set(f"{s['lf_before']:.2f} → {s['lf_after']:.2f}", "higher is better")
        self.cards["share"].set(f"{s['peak_share_before'] * 100:.0f}% → {s['peak_share_after'] * 100:.0f}%",
                                f"of energy in {a['tariff'].peak_label}")
        self.chart.render(viz.plot_shift_comparison, a, after, savings)
        self.app.dashboard.draw_chart_if_sim_selected()


# --------------------------------------------------------------------------- #
#  Tab 4 – Report exporter
# --------------------------------------------------------------------------- #
class ExportTab(ctk.CTkFrame):
    def __init__(self, master, app: "GridPulseApp"):
        super().__init__(master, fg_color="transparent")
        self.app = app
        panel = ctk.CTkFrame(self)
        panel.pack(fill="both", expand=True, pady=4)
        panel.columnconfigure(1, weight=1)
        panel.rowconfigure(7, weight=1)
        ctk.CTkLabel(panel, text="Export reports", font=ctk.CTkFont(size=16, weight="bold")
                     ).grid(row=0, column=0, columnspan=3, sticky="w", padx=14, pady=(12, 8))

        ctk.CTkLabel(panel, text="Output folder").grid(row=1, column=0, padx=(14, 8), pady=4, sticky="e")
        self.folder = ctk.CTkEntry(panel)
        self.folder.insert(0, str(Path.home() / "GridPulse_Reports"))
        self.folder.grid(row=1, column=1, sticky="ew", pady=4)
        ctk.CTkButton(panel, text="Browse…", width=90, command=self.browse).grid(row=1, column=2, padx=14)

        self.var_csv = tk.BooleanVar(value=True)
        self.var_clean = tk.BooleanVar(value=False)
        self.var_png = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(panel, text="Summary CSVs (KPIs, hourly profile, per-appliance table)",
                        variable=self.var_csv).grid(row=2, column=1, sticky="w", pady=3)
        ctk.CTkCheckBox(panel, text="Cleaned hourly log (gap-free, tidy CSV)",
                        variable=self.var_clean).grid(row=3, column=1, sticky="w", pady=3)
        ctk.CTkCheckBox(panel, text="Chart images (PNG) — includes before/after chart if you ran a simulation",
                        variable=self.var_png).grid(row=4, column=1, sticky="w", pady=3)

        ctk.CTkLabel(panel, text="PNG resolution").grid(row=5, column=0, padx=(14, 8), pady=4, sticky="e")
        self.dpi = ctk.CTkOptionMenu(panel, values=["150 dpi", "200 dpi", "300 dpi"], width=110)
        self.dpi.set("200 dpi")
        self.dpi.grid(row=5, column=1, sticky="w", pady=4)

        ctk.CTkButton(panel, text="💾  Export now", width=160, height=36, command=self.export
                      ).grid(row=6, column=1, sticky="w", pady=10)
        self.log = ctk.CTkTextbox(panel, state="disabled", wrap="word")
        self.log.grid(row=7, column=0, columnspan=3, sticky="nsew", padx=14, pady=(0, 14))
        set_text(self.log, "Nothing exported yet.")

    def refresh(self):
        pass

    def browse(self):
        path = filedialog.askdirectory(title="Choose output folder")
        if path:
            self.folder.delete(0, "end")
            self.folder.insert(0, path)

    def export(self):
        a, ds = self.app.analysis, self.app.dataset
        if a is None:
            messagebox.showinfo("Nothing to export", "Load data first (tab 1).")
            return
        folder = Path(self.folder.get().strip() or ".")
        sim = self.app.simulator.last_result
        written: list[Path] = []
        try:
            if self.var_csv.get():
                written.append(dh.export_csv(dh.summary_table(a, sim), folder / "gridpulse_summary.csv"))
                written.append(dh.export_csv(dh.hourly_table(a), folder / "gridpulse_hourly_profile.csv"))
                written.append(dh.export_csv(dh.appliance_table(a), folder / "gridpulse_appliances.csv"))
            if self.var_clean.get():
                written.append(dh.export_csv(ds.to_long(), folder / "gridpulse_cleaned_log.csv"))
            if self.var_png.get():
                written += viz.export_all_charts(folder, a, sim, dpi=int(self.dpi.get().split()[0]))
        except OSError as exc:
            messagebox.showerror("Export failed", f"Could not write to '{folder}':\n{exc}")
            return
        except Exception as exc:
            messagebox.showerror("Export failed", f"{type(exc).__name__}: {exc}")
            return
        if not written:
            set_text(self.log, "No export option selected.")
            return
        note = "" if sim else "\n(Tip: move a slider in tab 3 first to include the before/after chart and savings.)"
        set_text(self.log, f"Exported {len(written)} file(s) to:\n{folder.resolve()}\n\n"
                           + "\n".join(f"  • {p.name}" for p in written) + note)
        self.app.set_status(f"Exported {len(written)} file(s) to {folder}")


# --------------------------------------------------------------------------- #
#  Main window
# --------------------------------------------------------------------------- #
class GridPulseApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        self.title("GridPulse — Energy Load & Peak-Usage Analyzer")
        self.geometry("1320x820")
        self.minsize(1120, 720)

        self.dataset: dh.EnergyDataset | None = None
        self.analysis: dict | None = None
        self.tariff = self._initial_tariff()

        header = ctk.CTkFrame(self, fg_color="#0f172a", corner_radius=0)
        header.pack(fill="x")
        ctk.CTkLabel(header, text="⚡ GridPulse", font=ctk.CTkFont(size=22, weight="bold"), text_color="white"
                     ).pack(side="left", padx=16, pady=10)
        ctk.CTkLabel(header, text="See which hours and appliances drive your electricity bill — offline.",
                     text_color="#94a3b8").pack(side="left")

        self.status = ctk.CTkLabel(self, text="Ready. Load a CSV, add appliances, or try the sample data.",
                                   anchor="w", text_color="#6b7280")
        self.status.pack(side="bottom", fill="x", padx=12, pady=4)

        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill="both", expand=True, padx=10, pady=(6, 0))
        for name in (TAB_LOADER, TAB_DASH, TAB_SIM, TAB_EXPORT):
            self.tabs.add(name)
        # simulator is needed by the dashboard's chart switcher, so build it first
        self.simulator = SimulatorTab(self.tabs.tab(TAB_SIM), self)
        self.loader = LoaderTab(self.tabs.tab(TAB_LOADER), self)
        self.dashboard = DashboardTab(self.tabs.tab(TAB_DASH), self)
        self.exporter = ExportTab(self.tabs.tab(TAB_EXPORT), self)
        for tab in (self.loader, self.dashboard, self.simulator, self.exporter):
            tab.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    @staticmethod
    def _initial_tariff() -> ae.Tariff:
        path = dh.PROJECT_ROOT / "data" / "tariff.json"
        try:
            return ae.Tariff.from_json(path) if path.exists() else ae.Tariff()
        except ValueError:
            return ae.Tariff()

    def set_status(self, text: str):
        self.status.configure(text=text)

    def _reanalyze(self):
        ds = self.dataset
        self.analysis = (ae.analyze(ds.cube, ds.dates, ds.appliances, ds.categories, self.tariff)
                         if ds is not None else None)

    def set_dataset(self, dataset: dh.EnergyDataset):
        self.dataset = dataset
        try:
            self._reanalyze()
        except Exception as exc:
            self.dataset = self.analysis = None
            messagebox.showerror("Analysis failed", f"{type(exc).__name__}: {exc}")
            return
        self.loader.show_dataset(dataset)
        self.simulator.refresh()
        self.dashboard.refresh()
        self.tabs.set(TAB_DASH)
        self.set_status(dataset.describe())

    def set_tariff(self, tariff: ae.Tariff):
        self.tariff = tariff
        self.loader.update_tariff_label()
        if self.dataset is not None:
            self._reanalyze()
            self.simulator.refresh()
            self.dashboard.refresh()
        self.set_status(f"Tariff loaded: {tariff.name}")

    def _on_close(self):
        if self.simulator._job is not None:
            self.after_cancel(self.simulator._job)
        self.destroy()
