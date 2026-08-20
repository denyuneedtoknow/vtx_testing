#!/usr/bin/env python3
"""Desktop GUI for the VTX tester command-line workflows."""

from __future__ import annotations

import csv
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from tkinter import BOTH, END, LEFT, RIGHT, VERTICAL, W, X, Canvas, StringVar, Tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

from vtx_a1_measure import format_vtx
from vtx_msp_probe import MSP_VTX_CONFIG, MspClient, MspError, open_serial, parse_vtx_config, read_vtx_power_levels, resolve_port
from vtx_power_logger import NrpzError, NrpzSensor, watts_to_millwatts
from measurement_path import load_measurement_path, offset_db_at_frequency
from vtx_power_table_measure import (
    DIRECT_FREQUENCY_SWEEP_PLAN_FILENAMES, POWER_TABLE_PLAN_FILENAMES, load_direct_frequency_sweep_plan,
    load_power_table_plan, measurement_points, measurement_power_levels,
)
from vtx_three_point_test import PLAN_DIRECTORY, VTX_PLAN_FILENAMES, load_plan, power_status, switch_and_confirm, wait_for_stable_power


ROOT = Path(__file__).resolve().parent
LOG_DIRECTORY = ROOT / "logs"
STATUS_COLORS = {"OK": "#16803c", "LOW": "#c98300", "FAIL": "#c33131", "MEASURING": "#2563eb", "PENDING": "#98a2b3", "N/A": "#667085"}


class VtxTesterGui:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("VTX Tester")
        self.root.minsize(980, 650)
        self.process: subprocess.Popen[str] | None = None
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.vtx = StringVar(value="5.8")
        self.mode = StringVar(value="three")
        self.port = StringVar()
        self.samples = StringVar(value="3")
        self.frequency = StringVar(value="5945")
        self.power = StringVar(value="4000")
        self.output_path: Path | None = None
        self.run_failed = False
        self.current_frequency: int | None = None
        self._animation_generation: dict[int, int] = {}
        self.full_progress: dict[str, int] | None = None
        self._build()
        self.root.after(80, self._drain_events)

    def _build(self) -> None:
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill=BOTH, expand=True)
        controls = ttk.LabelFrame(main, text="Test control", padding=10)
        controls.pack(fill=X)
        ttk.Label(controls, text="VTX").grid(row=0, column=0, sticky=W, padx=(0, 5))
        self.vtx_box = ttk.Combobox(controls, state="readonly", width=13, textvariable=self.vtx, values=sorted(POWER_TABLE_PLAN_FILENAMES))
        self.vtx_box.grid(row=0, column=1, sticky=W)
        self.vtx_box.bind("<<ComboboxSelected>>", lambda _: self._sync_mode())
        ttk.Label(controls, text="COM port (optional)").grid(row=0, column=2, sticky=W, padx=(16, 5))
        ttk.Entry(controls, width=12, textvariable=self.port).grid(row=0, column=3, sticky=W)
        ttk.Label(controls, text="Output readings per point/cell").grid(row=0, column=4, sticky=W, padx=(16, 5))
        ttk.Spinbox(controls, from_=1, to=30, width=5, textvariable=self.samples).grid(row=0, column=5, sticky=W)

        modes = ttk.Frame(controls)
        modes.grid(row=1, column=0, columnspan=6, sticky=W, pady=(10, 3))
        for text, value in (("Three-point test", "three"), ("Full power table", "full"), ("Manual frequency switch", "manual")):
            ttk.Radiobutton(modes, text=text, variable=self.mode, value=value, command=self._sync_mode).pack(side=LEFT, padx=(0, 16))

        self.manual_frame = ttk.Frame(controls)
        ttk.Label(self.manual_frame, text="Frequency, MHz").pack(side=LEFT)
        self.manual_frequency_box = ttk.Combobox(self.manual_frame, state="readonly", width=19, textvariable=self.frequency)
        self.manual_frequency_box.pack(side=LEFT, padx=(5, 14))
        ttk.Label(self.manual_frame, text="Power, mW").pack(side=LEFT)
        ttk.Combobox(self.manual_frame, state="readonly", width=7, textvariable=self.power, values=("25", "2000", "4000")).pack(side=LEFT, padx=(5, 14))
        self.start_button = ttk.Button(controls, text="Start test", command=self.start)
        self.start_button.grid(row=3, column=0, sticky=W, pady=(10, 0))
        self.stop_button = ttk.Button(controls, text="Stop", state="disabled", command=self.stop)
        self.stop_button.grid(row=3, column=1, sticky=W, pady=(10, 0), padx=(8, 0))

        tabs = ttk.Notebook(main)
        tabs.pack(fill=BOTH, expand=True, pady=(12, 0))
        log_tab = ttk.Frame(tabs, padding=6)
        result_tab = ttk.Frame(tabs, padding=6)
        tabs.add(result_tab, text="Result summary")
        tabs.add(log_tab, text="Live test log")
        ttk.Label(log_tab, text="Technical output from the tester. It is useful for diagnosing a connection or measurement problem.").pack(anchor=W, pady=(0, 5))
        self.log = ttk.Treeview(log_tab, columns=("line",), show="headings")
        self.log.heading("line", text="Output")
        self.log.column("line", width=900, anchor=W)
        scrollbar = ttk.Scrollbar(log_tab, orient=VERTICAL, command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.pack(side=RIGHT, fill="y")
        self.result_status = StringVar(value="Ready. Choose a test and press Start test.")
        self.result_status_label = ttk.Label(result_tab, textvariable=self.result_status, font=("TkDefaultFont", 11, "bold"))
        self.result_status_label.pack(anchor=W, pady=(0, 7))
        self.result_steps = ttk.Treeview(result_tab, columns=("step", "details"), show="headings", height=4)
        self.result_steps.heading("step", text="Step")
        self.result_steps.heading("details", text="Result")
        self.result_steps.column("step", width=105, anchor=W, stretch=False)
        self.result_steps.column("details", width=800, anchor=W)
        self.result_steps.tag_configure("error", foreground="#b42318")
        self.result_steps.tag_configure("ok", foreground="#067647")
        self.result_steps.tag_configure("warning", foreground="#9a6700")
        self.result_steps.pack(fill=X, pady=(0, 7))
        self.result_canvas = Canvas(result_tab, background="white", highlightthickness=0)
        self.result_canvas.pack(fill=BOTH, expand=True)
        self.result_canvas.bind("<Configure>", lambda _: self._draw_result_chart())
        self.result_message = "Run a three-point test or open its CSV to see reference comparison."
        self.results: list[dict[str, float | int | str]] = []
        self._sync_mode()

    def _sync_mode(self) -> None:
        manual = self.mode.get() == "manual"
        # The 64-channel table is a deliberately reduced Betaflight table.
        # A full test of the wide-band 5.8 model must use all 176 documented
        # factory channels through direct frequency control.
        if self.mode.get() in {"full", "manual"} and self.vtx.get() == "5.8":
            self.vtx.set("5.8.all")
        if manual:
            self.manual_frame.grid(row=2, column=0, columnspan=6, sticky=W, pady=(8, 0))
            self.start_button.configure(text="Switch VTX")
            self._sync_manual_frequencies()
        else:
            self.manual_frame.grid_forget()
            self.start_button.configure(text="Start test")
        if self.mode.get() in {"full", "manual"}:
            options = sorted({*POWER_TABLE_PLAN_FILENAMES, *DIRECT_FREQUENCY_SWEEP_PLAN_FILENAMES})
            self.vtx_box.configure(values=options)
        else:
            self.vtx_box.configure(values=sorted(VTX_PLAN_FILENAMES))
            if self.vtx.get() not in VTX_PLAN_FILENAMES:
                self.vtx.set("5.8")

    def _sync_manual_frequencies(self) -> None:
        """Populate manual selection with real configured channels for this VTX."""
        try:
            vtx = self.vtx.get()
            if vtx in DIRECT_FREQUENCY_SWEEP_PLAN_FILENAMES:
                plan = load_direct_frequency_sweep_plan(PLAN_DIRECTORY / DIRECT_FREQUENCY_SWEEP_PLAN_FILENAMES[vtx])
            else:
                plan = load_plan(PLAN_DIRECTORY / VTX_PLAN_FILENAMES[vtx])
            profile = plan["_profile"]
            values = [
                f"{band}{channel} · {frequency_mhz} MHz"
                for band, frequencies in profile["bands"].items()
                for channel, frequency_mhz in enumerate(frequencies, start=1)
                if isinstance(frequency_mhz, int) and frequency_mhz > 0
            ]
        except (OSError, ValueError, KeyError, TypeError):
            values = []
        self.manual_frequency_box.configure(values=values)
        if values and self.frequency.get() not in values:
            self.frequency.set(values[0])

    def _line(self, text: str) -> None:
        self.log.insert("", END, values=(text,))
        children = self.log.get_children()
        if children:
            self.log.see(children[-1])

    def _result_step(self, step: str, details: str, tag: str = "") -> None:
        self.result_steps.insert("", END, values=(step, details), tags=(tag,) if tag else ())
        children = self.result_steps.get_children()
        if children:
            self.result_steps.see(children[-1])

    def _record_progress(self, line: str) -> None:
        """Keep the primary UI concise; full output remains in the log tab."""
        if line.startswith("Setting "):
            match = re.search(r"\((\d+) MHz\)", line)
            self.current_frequency = int(match.group(1)) if match else None
            self._result_step("Switching", line.removeprefix("Setting "))
            self._draw_result_chart()
        elif line.startswith("Sample "):
            match = re.fullmatch(r"Sample (\d+): ([0-9.]+) mW", line)
            if match and self.current_frequency is not None:
                output_mw = float(match.group(2))
                for result in self.results:
                    if int(result.get("frequency", 0)) == self.current_frequency:
                        samples = result.setdefault("samples", [])
                        assert isinstance(samples, list)
                        samples.append(output_mw)
                        reference = float(result["reference"])
                        average = fmean(samples)
                        result["measured"] = average
                        result["percent"] = average / reference * 100
                        result["status"] = "MEASURING"
                        self._animate_indicator(int(result["frequency"]), output_mw)
                        self._result_step("Sample", f"{match.group(1)}/{self.samples.get()}: {output_mw:.0f} mW; running avg {average:.0f} mW")
                        self._draw_result_chart()
                        break
        elif line.startswith("VTX output stable:"):
            self._result_step("Stable", line.removeprefix("VTX output stable:").strip(), "ok")
        elif line.startswith("VTX output (path-corrected):"):
            if self.current_frequency is not None:
                for result in self.results:
                    if int(result.get("frequency", 0)) == self.current_frequency:
                        result["final"] = True
                        result["status"] = power_status(
                            float(result["measured"]), float(result["reference"]), {"green_min": 90.0, "yellow_min": 80.0}
                        )
                        self._animate_indicator(int(result["frequency"]), float(result["measured"]))
                        self._draw_result_chart()
                        break
            self._result_step("Measured", line.removeprefix("VTX output (path-corrected):").strip())
        elif line.startswith("Returning VTX"):
            self._result_step("Safety", line, "warning")
        elif line.startswith("Final VTX:"):
            self._result_step("Completed", line.removeprefix("Final VTX:").strip(), "ok")
        elif line.startswith("Error:"):
            self.run_failed = True
            self.result_status.set("Test stopped with an error. See the technical log for details.")
            self._result_step("Error", line.removeprefix("Error:").strip(), "error")
        elif self.full_progress is not None and re.match(r"^\s{2}\S+\s+\(\d+ MHz\)\s+\d+ mW", line):
            self.full_progress["cells_completed"] = min(
                self.full_progress["cells_completed"] + 1, self.full_progress["cells_total"]
            )
            completed = self.full_progress["cells_completed"]
            total = self.full_progress["cells_total"]
            readings = completed * self.full_progress["samples_per_cell"]
            total_readings = total * self.full_progress["samples_per_cell"]
            self.result_message = f"Full table: {completed} / {total} cells · {readings} / {total_readings} output readings"
            self.result_status.set(f"Full-table test is running: {completed / total * 100:.1f}% complete.")
            self._draw_result_chart()

    def _command(self) -> list[str]:
        vtx, mode = self.vtx.get(), self.mode.get()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if mode == "three":
            self.output_path = LOG_DIRECTORY / f"gui-three-point-{vtx}-{timestamp}.csv"
            command = [*self._tool_command("VTX-Three-Point-Test", "vtx_three_point_test.py"), "--vtx", vtx, "--samples", self.samples.get(), "--csv", str(self.output_path)]
        else:
            self.output_path = LOG_DIRECTORY / f"gui-power-table-{vtx}-{timestamp}.csv"
            command = [*self._tool_command("VTX-Power-Table", "vtx_power_table_measure.py"), "--vtx", vtx, "--samples", self.samples.get(), "--csv", str(self.output_path)]
        if self.port.get().strip():
            command.extend(["--port", self.port.get().strip()])
        return command

    @staticmethod
    def _tool_command(executable_name: str, script_name: str) -> list[str]:
        """Use source scripts in development and sibling executables in a release."""
        if getattr(sys, "frozen", False):
            suffix = ".exe" if sys.platform == "win32" else ""
            release_directory = Path(sys.executable).resolve().parent.parent
            executable = release_directory / executable_name / f"{executable_name}{suffix}"
            if not executable.is_file():
                raise ValueError(f"Packaged tool was not found: {executable}")
            return [str(executable)]
        return [sys.executable, "-u", str(ROOT / script_name)]

    def _prepare_threepoint_preview(self) -> None:
        """Show all expected points before the first MSP command is sent."""
        try:
            plan = load_plan(PLAN_DIRECTORY / VTX_PLAN_FILENAMES[self.vtx.get()])
            self.results = [
                {
                    "label": f"{point['band']}{point['channel']} · {point['frequency_mhz']} MHz",
                    "frequency": int(point["frequency_mhz"]),
                    "reference": float(point["reference_output_mw"]),
                    "samples": [],
                    "status": "PENDING",
                    "final": False,
                }
                for point in plan["points"]
            ]
            self.result_message = "Live measurement: the blue indicator moves to every new sample, then to the final average."
        except (OSError, ValueError, KeyError):
            self.results = []

    def _prepare_full_progress(self) -> None:
        try:
            vtx = self.vtx.get()
            direct = vtx in DIRECT_FREQUENCY_SWEEP_PLAN_FILENAMES
            plan_path = PLAN_DIRECTORY / (DIRECT_FREQUENCY_SWEEP_PLAN_FILENAMES[vtx] if direct else POWER_TABLE_PLAN_FILENAMES[vtx])
            plan = load_direct_frequency_sweep_plan(plan_path) if direct else load_power_table_plan(plan_path)
            cells = len(measurement_points(plan["_profile"])) * len(measurement_power_levels(plan["_profile"]))
            sample_count = int(self.samples.get())
            self.full_progress = {"cells_completed": 0, "cells_total": cells, "samples_per_cell": sample_count}
            self.result_message = f"Full table: 0 / {cells} cells · 0 / {cells * sample_count} output readings"
        except (OSError, ValueError, KeyError):
            self.full_progress = None

    def _animate_indicator(self, frequency: int, target_mw: float) -> None:
        """Smoothly move the live bar from its previous sample to the next one."""
        result = next((item for item in self.results if int(item.get("frequency", 0)) == frequency), None)
        if result is None:
            return
        start_mw = float(result.get("display_mw", 0.0))
        generation = self._animation_generation.get(frequency, 0) + 1
        self._animation_generation[frequency] = generation
        frames = 8

        def frame(index: int) -> None:
            if self._animation_generation.get(frequency) != generation:
                return
            result["display_mw"] = start_mw + (target_mw - start_mw) * index / frames
            self._draw_result_chart()
            if index < frames:
                self.root.after(30, lambda: frame(index + 1))

        frame(1)

    def start(self) -> None:
        if self.process is not None:
            return
        if self.mode.get() == "manual":
            self._switch_manual()
            return
        try:
            command = self._command()
        except ValueError as exc:
            messagebox.showerror("Invalid settings", str(exc), parent=self.root)
            return
        self.log.delete(*self.log.get_children())
        self.result_steps.delete(*self.result_steps.get_children())
        self.results = []
        self.run_failed = False
        self.current_frequency = None
        self._animation_generation.clear()
        self.full_progress = None
        self.result_message = "Measurement is running…"
        if self.mode.get() == "three":
            self._prepare_threepoint_preview()
        elif self.mode.get() == "full":
            self._prepare_full_progress()
        self.result_status.set("Test is running. Key switching and measurement steps will appear below.")
        self._draw_result_chart()
        self._line("$ " + " ".join(command))
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        threading.Thread(target=self._run_process, args=(command,), daemon=True).start()

    def _run_process(self, command: list[str]) -> None:
        try:
            environment = os.environ.copy()
            # Windows uses the local ANSI code page for a redirected Python
            # process by default. The terminal reference chart contains Unicode
            # blocks, so force UTF-8 before the child starts printing.
            environment["PYTHONIOENCODING"] = "utf-8"
            self.process = subprocess.Popen(
                command, cwd=ROOT, text=True, encoding="utf-8", errors="replace", env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1,
            )
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.events.put(("log", line.rstrip()))
            code = self.process.wait()
            self.events.put(("finished", str(code)))
        except OSError as exc:
            self.events.put(("error", str(exc)))

    def _switch_manual(self) -> None:
        try:
            match = re.search(r"(\d+) MHz$", self.frequency.get())
            if not match:
                raise ValueError("Choose a frequency from the VTX channel list.")
            frequency, power = int(match.group(1)), int(self.power.get())
            if power not in {25, 2000, 4000}:
                raise ValueError("Choose 25, 2000 or 4000 mW.")
        except ValueError as exc:
            messagebox.showerror("Invalid manual setting", str(exc), parent=self.root)
            return
        self.result_steps.delete(*self.result_steps.get_children())
        self.results = [{"label": self.frequency.get(), "frequency": frequency, "reference": float(power), "samples": [], "status": "MEASURING", "final": False}]
        self.current_frequency = frequency
        self.full_progress = None
        self.result_message = "Manual measurement: the indicator moves for each new output reading."
        self.result_status.set("Switching VTX, waiting for stable output, then measuring samples.")
        self._draw_result_chart()
        self.start_button.configure(state="disabled")
        self._line(f"Switching directly to {frequency} MHz / {power} mW…")
        threading.Thread(target=self._manual_worker, args=(frequency, power, int(self.samples.get())), daemon=True).start()

    def _manual_worker(self, frequency: int, power: int, sample_count: int) -> None:
        sensor: NrpzSensor | None = None
        try:
            port = resolve_port(self.port.get().strip() or None, 115200, 2.0)
            with open_serial(port, 115200, 2.0) as serial_port:
                client = MspClient(serial_port, 2.0)
                current = parse_vtx_config(client.request(MSP_VTX_CONFIG))
                count = int(current.get("vtx_table_power_levels") or 0)
                levels = read_vtx_power_levels(client, count)
                matching = [int(level["index"]) for level in levels if int(level["value"]) == power]
                if not matching:
                    raise MspError(f"Flight Controller VTX table has no {power} mW power level.")
                updated = switch_and_confirm(client, current, band=1, channel=1, frequency_mhz=frequency, power_index=matching[0], settle_time_s=3.0, direct_frequency=True)
            self.events.put(("manual", "VTX active: " + format_vtx(updated)))
            measurement_path = load_measurement_path(ROOT / "configs" / "measurement_path_control.json")
            sensor = NrpzSensor()
            sensor.open("*")
            sensor.configure(frequency * 1_000_000, 16)
            sensor.set_level_offset(offset_db_at_frequency(measurement_path, frequency))
            wait_for_stable_power(sensor, timeout_ms=5_000, sample_interval_s=.5, required_samples=3, tolerance_percent=2.0, max_wait_s=15.0)
            readings: list[float] = []
            for index in range(1, sample_count + 1):
                value = watts_to_millwatts(sensor.read_watts(5_000))
                readings.append(value)
                self.events.put(("manual_sample", f"{index}|{value:.2f}"))
                if index < sample_count:
                    time.sleep(.5)
            self.events.put(("manual_done", f"{fmean(readings):.2f}"))
        except Exception as exc:
            # This worker must never die silently: otherwise the GUI remains
            # disabled while the operator has no way to recover.
            self.events.put(("error", str(exc)))
        finally:
            if sensor is not None:
                try:
                    sensor.close()
                except NrpzError:
                    pass

    def stop(self) -> None:
        if self.process is not None:
            if not messagebox.askyesno(
                "Stop measurement",
                "Stopping immediately can prevent the script from returning the VTX to 25 mW. "
                "Use manual switch afterwards to set 25 mW. Continue?",
                parent=self.root,
            ):
                return
            self.process.terminate()
            self._line("Stop requested. Set the VTX to 25 mW manually after the process exits.")

    def _drain_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self._line(value)
                    self._record_progress(value)
                elif kind == "finished":
                    self._line(f"Process finished with exit code {value}.")
                    self.process = None
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    if value != "0":
                        self.run_failed = True
                        self.result_status.set("Test stopped with an error. Partial results, if any, are shown below.")
                        self._result_step("Error", f"Process finished with exit code {value}.", "error")
                    else:
                        self.result_status.set("Test completed. Compare the values and colors with the reference below.")
                    if self.output_path and self.output_path.exists():
                        self.load_result(self.output_path)
                elif kind == "manual":
                    self._line(value)
                    self.result_status.set("Frequency and power were set. Measuring output now.")
                    self._result_step("Active VTX", value.removeprefix("VTX active: "), "ok")
                elif kind == "manual_sample":
                    _, raw_value = value.split("|", 1)
                    self._record_progress(f"Sample {value.split('|', 1)[0]}: {raw_value} mW")
                elif kind == "manual_done":
                    if self.results:
                        result = self.results[0]
                        result["measured"] = float(value)
                        result["percent"] = float(value) / float(result["reference"]) * 100
                        result["final"] = True
                        result["status"] = "N/A"
                        self._animate_indicator(int(result["frequency"]), float(value))
                        self._result_step("Average", f"{float(value):.0f} mW", "ok")
                    self.result_status.set("Manual measurement completed. The VTX remains at the selected frequency and power.")
                    self.start_button.configure(state="normal")
                elif kind == "error":
                    self._line("Error: " + value)
                    self.process = None
                    self.run_failed = True
                    self.result_status.set("Action failed. See the error below and the technical log.")
                    self._result_step("Error", value, "error")
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    messagebox.showerror("VTX Tester", value, parent=self.root)
        except queue.Empty:
            pass
        self.root.after(80, self._drain_events)

    def open_csv(self) -> None:
        path = filedialog.askopenfilename(parent=self.root, title="Open test CSV", filetypes=(("CSV files", "*.csv"), ("All files", "*")))
        if path:
            self.load_result(Path(path))

    def load_result(self, path: Path) -> None:
        if "power-table" in path.name:
            self.results = []
            self.result_message = f"Full table saved: {path.name}."
            if not self.run_failed:
                self.result_status.set("Full table completed and saved. Use Open CSV to review its matrix.")
            self._draw_result_chart()
            return
        try:
            plan = load_plan(PLAN_DIRECTORY / VTX_PLAN_FILENAMES[self.vtx.get()])
            by_frequency: dict[int, list[float]] = defaultdict(list)
            with path.open(encoding="utf-8-sig", newline="") as source:
                for row in csv.DictReader(source):
                    by_frequency[int(row["frequency_mhz"])].append(float(row["vtx_output_mw"]))
            thresholds = plan["_reference_thresholds_percent"]
            self.results = []
            for point in plan["points"]:
                frequency = int(point["frequency_mhz"])
                values = by_frequency.get(frequency, [])
                if not values:
                    continue
                average = fmean(values)
                reference = float(point["reference_output_mw"])
                self.results.append({"label": f"{point['band']}{point['channel']} · {frequency} MHz", "frequency": frequency, "samples": values, "measured": average, "reference": reference, "percent": average / reference * 100, "status": power_status(average, reference, thresholds), "final": True})
            self.result_message = f"Reference comparison: {path.name}"
            if not self.run_failed:
                self.result_status.set("Test completed. Green = OK, yellow = LOW, red = FAIL.")
        except (OSError, ValueError, KeyError, csv.Error) as exc:
            self.results = []
            self.result_message = f"Cannot read results: {exc}"
            self.result_status.set("Cannot build the result summary. See the technical log.")
        self._draw_result_chart()

    def _draw_result_chart(self) -> None:
        canvas = self.result_canvas
        canvas.delete("all")
        width, height = max(canvas.winfo_width(), 1), max(canvas.winfo_height(), 1)
        canvas.create_text(16, 16, text=self.result_message, anchor="nw", font=("TkDefaultFont", 11, "bold"), fill="#263348")
        if self.full_progress is not None:
            total = self.full_progress["cells_total"]
            completed = self.full_progress["cells_completed"]
            percent = completed / total * 100 if total else 0.0
            left, right, top = 35, 35, 72
            bar_width = width - left - right
            canvas.create_text(left, top, text=f"{completed} of {total} frequency/power cells", anchor="w", fill="#263348", font=("TkDefaultFont", 12, "bold"))
            canvas.create_rectangle(left, top + 28, left + bar_width, top + 60, fill="#f2f4f7", outline="#d0d5dd")
            canvas.create_rectangle(left, top + 28, left + bar_width * percent / 100, top + 60, fill="#2563eb", outline="")
            canvas.create_text(left + bar_width / 2, top + 44, text=f"{percent:.1f}%", fill="#172033", font=("TkDefaultFont", 11, "bold"))
            readings = completed * self.full_progress["samples_per_cell"]
            total_readings = total * self.full_progress["samples_per_cell"]
            canvas.create_text(left, top + 88, text=f"Output readings completed: {readings} / {total_readings}", anchor="w", fill="#526072")
            return
        if not self.results:
            return
        top, bottom, left, right = 55, 35, 255, 90
        row_height = max(68, (height - top - bottom) / len(self.results))
        chart_width = width - left - right
        canvas.create_text(left, height - 14, text="0%", fill="#667085")
        for tick in (50, 100, 125, 150):
            x = left + chart_width * tick / 150
            canvas.create_text(x, height - 14, text=f"{tick}%", fill="#667085")
        for index, result in enumerate(self.results):
            row_top = top + row_height * index
            row_bottom = row_top + row_height - 10
            bar_top, bar_bottom = row_top + 18, row_top + 43
            measured = result.get("measured")
            status = str(result.get("status", "PENDING"))
            color = STATUS_COLORS[status]
            samples = result.get("samples", [])
            assert isinstance(samples, list)
            reference = float(result["reference"])
            canvas.create_text(14, row_top + 18, text=str(result["label"]), anchor="w", fill="#172033", font=("TkDefaultFont", 10, "bold"))
            canvas.create_text(14, row_top + 39, text=f"Reference: {reference:.0f} mW", anchor="w", fill="#526072")
            canvas.create_rectangle(left, bar_top, left + chart_width, bar_bottom, fill="#f2f4f7", outline="#d0d5dd")
            for tick in (50, 100, 125, 150):
                x = left + chart_width * tick / 150
                canvas.create_line(x, bar_top, x, bar_bottom, fill="#d0d5dd", dash=(2, 2))
            target_x = left + chart_width * 100 / 150
            canvas.create_line(target_x, bar_top - 5, target_x, bar_bottom + 5, fill="#344054", dash=(4, 3), width=2)
            if measured is None:
                canvas.create_text(left + 8, row_top + 63, text="Waiting for samples", anchor="w", fill="#98a2b3")
            else:
                target_mw = float(measured) if bool(result.get("final")) else float(samples[-1])
                display_mw = float(result.get("display_mw", target_mw))
                display_percent = min(display_mw / reference * 100, 150.0)
                canvas.create_rectangle(left, bar_top, left + chart_width * display_percent / 150, bar_bottom, fill=color, outline="")
                marker_x = left + chart_width * display_percent / 150
                canvas.create_line(marker_x, bar_top - 5, marker_x, bar_bottom + 5, fill="#172033", width=2)
                phase = "Average" if bool(result.get("final")) else f"Sample {len(samples)}/{self.samples.get()}"
                canvas.create_text(left, row_top + 63, text=f"{phase}: {display_mw:.0f} mW · samples: {', '.join(f'{value:.0f}' for value in samples)}", anchor="w", fill=color)
            canvas.create_text(left + chart_width + 12, (bar_top + bar_bottom) / 2, text=status, anchor="w", fill=color, font=("TkDefaultFont", 10, "bold"))


def main() -> None:
    root = Tk()
    VtxTesterGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
