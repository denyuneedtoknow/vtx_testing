#!/usr/bin/env python3
"""PySide6 presentation layer for VTX Tester.

The legacy Tkinter GUI remains in ``vtx_tester_gui.py`` while this UI is
validated.  All RF work still happens in the existing command-line tools.
"""
from __future__ import annotations

import csv
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean

from PySide6.QtCore import Qt, QSettings, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QButtonGroup, QComboBox, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QSpinBox, QStackedWidget, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QSizePolicy, QScrollArea)

from measurement_path import load_measurement_path, offset_db_at_frequency, validate_sensor_frequencies
from vtx_a1_measure import format_vtx
from vtx_msp_probe import MSP_VTX_CONFIG, MspClient, MspError, open_serial, parse_vtx_config, read_vtx_power_levels, resolve_port
from vtx_power_logger import NrpzError, NrpzSensor, watts_to_millwatts
from vtx_power_table_measure import DIRECT_FREQUENCY_SWEEP_PLAN_FILENAMES, POWER_TABLE_PLAN_FILENAMES, load_direct_frequency_sweep_plan, load_power_table_plan, measurement_points, measurement_power_levels
from vtx_three_point_test import PLAN_DIRECTORY, VTX_PLAN_FILENAMES, load_plan, power_status, switch_and_confirm, wait_for_stable_power

ROOT = Path(__file__).resolve().parent
LOG_DIRECTORY = ROOT / "logs"
FULL_ONLY_VTX = {"5.8.test"}
GUI_VTX_OPTIONS = sorted({*VTX_PLAN_FILENAMES, *FULL_ONLY_VTX})
POWER_SELECTIONS = {"All": "all", "25 mW": "25", "500 mW": "500", "2 W": "2000", "2.5 W": "2500", "3 W": "3000", "4 W": "4000", "5 W": "5000"}

PALETTES = {
    "light": {"bg":"#F8FAFC", "surface":"#FFFFFF", "alt":"#F1F5F9", "text":"#0F172A", "muted":"#64748B", "border":"#E2E8F0", "primary":"#3B82F6", "ok":"#22C55E", "low":"#F59E0B", "fail":"#EF4444", "pending":"#94A3B8", "soft":"#ECFDF3"},
    "dark": {"bg":"#0F172A", "surface":"#1E293B", "alt":"#243449", "text":"#F8FAFC", "muted":"#94A3B8", "border":"#334155", "primary":"#60A5FA", "ok":"#22C55E", "low":"#FBBF24", "fail":"#F87171", "pending":"#64748B", "soft":"#103B2B"},
}


class ComparisonChart(QWidget):
    def __init__(self, gui: "VtxTesterQt") -> None:
        super().__init__(); self.gui = gui; self.setMinimumHeight(250)

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        p, g = QPainter(self), self.gui.palette
        p.fillRect(self.rect(), QColor(g["surface"])); p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QColor(g["text"])); p.drawText(16, 25, self.gui.result_message)
        if self.gui.full_progress:
            done, total, samples = self.gui.full_progress
            ratio = done / total if total else 0
            p.drawText(35, 72, f"{done} of {total} frequency/power cells")
            p.setPen(QPen(QColor(g["border"]))); p.setBrush(QColor(g["alt"])); p.drawRoundedRect(35, 85, self.width()-70, 28, 8, 8)
            p.setPen(Qt.NoPen); p.setBrush(QColor(g["primary"])); p.drawRoundedRect(35, 85, (self.width()-70)*ratio, 28, 8, 8)
            p.setPen(QColor(g["muted"])); p.drawText(35, 140, f"Output readings completed: {done * samples} / {total * samples}"); return
        if not self.gui.results: return
        rows, left, right, top = len(self.gui.results), 245, 98, 48
        row_h, chart_w = max(70, (self.height()-top-18)/rows), max(1, self.width()-left-right)
        max_percent = max(150, max((float(r.get("measured", 0))/float(r["reference"])*100 for r in self.gui.results), default=0))
        for index, r in enumerate(self.gui.results):
            y, reference, status = top + row_h*index, float(r["reference"]), str(r.get("status", "PENDING"))
            colors = {"OK":g["ok"], "LOW":g["low"], "FAIL":g["fail"], "MEASURING":g["primary"], "PENDING":g["pending"], "N/A":g["muted"]}; color = colors[status]
            p.setPen(QColor(g["text"])); p.drawText(16, int(y+18), str(r["label"])); p.setPen(QColor(g["muted"])); p.drawText(16, int(y+39), f"Reference: {reference:.0f} mW")
            p.setPen(QPen(QColor(g["border"]))); p.setBrush(QColor(g["alt"])); p.drawRoundedRect(left, int(y+8), chart_w, 24, 7, 7)
            marker = left + chart_w*100/max_percent; p.setPen(QPen(QColor(g["text"]), 2, Qt.DashLine)); p.drawLine(int(marker), int(y+3), int(marker), int(y+37))
            if r.get("measured") is not None:
                value = float(r.get("display_mw", r["measured"])); x = left + chart_w*value/reference*100/max_percent
                p.setPen(Qt.NoPen); p.setBrush(QColor(color)); p.drawRoundedRect(left, int(y+8), int(x-left), 24, 7, 7)
                samples = ", ".join(f"{v:.0f}" for v in r.get("samples", [])); p.setPen(QColor(color)); p.drawText(left, int(y+57), f"Average: {value:.0f} mW · samples: {samples}")
            else: p.setPen(QColor(g["pending"])); p.drawText(left, int(y+57), "Waiting for samples")
            p.setPen(QColor(color)); p.drawText(left+chart_w+12, int(y+25), status)


class StatusTabs(QTabWidget):
    """Position a compact status message immediately after the tab labels."""
    def set_status_widget(self, widget: QLabel) -> None:
        self.status_widget = widget
        widget.setParent(self)
        widget.show()
        self._position_status()

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._position_status()

    def _position_status(self) -> None:
        if hasattr(self, "status_widget"):
            self.status_widget.adjustSize()
            tab_height = self.tabBar().height()
            self.status_widget.move(self.tabBar().width() + 12, max(0, (tab_height - self.status_widget.height()) // 2))
            self.status_widget.raise_()


class VtxTesterQt(QMainWindow):
    def __init__(self) -> None:
        super().__init__(); self.settings = QSettings("Foxeer", "VTXTester"); self.theme = self.settings.value("theme", "light")
        self.palette = PALETTES[self.theme if self.theme in PALETTES else "light"]; self.process = None; self.run_active = self.stop_requested = self.safety_reset_in_progress = self.safety_reset_confirmed = self.run_failed = False
        self.events: queue.Queue[tuple[str, str]] = queue.Queue(); self.output_path: Path | None = None; self.current_frequency = None; self.results: list[dict] = []; self.full_progress: tuple[int, int, int] | None = None
        self.result_message = "Run a three-point test or open its CSV to see reference comparison."; self._build(); self._apply_theme()
        self.timer = QTimer(self); self.timer.timeout.connect(self._drain_events); self.timer.start(80)

    def _build(self) -> None:
        self.setWindowTitle("VTX Tester"); self.resize(1120, 720); self.setMinimumSize(980, 650)
        root = QWidget(); self.setCentralWidget(root); layout = QVBoxLayout(root); layout.setContentsMargins(24, 8, 24, 14); layout.setSpacing(6)
        header = QHBoxLayout(); header.setContentsMargins(0, 0, 0, 0); title = QLabel("VTX Tester"); title.setObjectName("title"); header.addWidget(title); header.addStretch(); self.theme_button = QPushButton(); self.theme_button.setObjectName("themeToggle"); self.theme_button.setFixedSize(96, 36); self.theme_button.clicked.connect(self._toggle_theme); header.addWidget(self.theme_button); layout.addLayout(header)
        panel = QFrame(); panel.setObjectName("panel"); panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum); grid = QGridLayout(panel); grid.setContentsMargins(18, 14, 18, 14); grid.setHorizontalSpacing(10); grid.setVerticalSpacing(10)
        grid.addWidget(QLabel("VTX"), 0, 0); self.vtx = QComboBox(); self.vtx.setFixedWidth(132); self.vtx.addItems(GUI_VTX_OPTIONS); self.vtx.setCurrentText("5.8"); self.vtx.currentTextChanged.connect(self._sync_mode); grid.addWidget(self.vtx, 0, 1)
        com_label = QLabel("COM port (optional)"); com_label.setMinimumWidth(142); grid.addWidget(com_label, 0, 2); self.port = QLineEdit(); self.port.setFixedWidth(220); grid.addWidget(self.port, 0, 3)
        readings_label = QLabel("Output readings per point/cell"); readings_label.setMinimumWidth(230); grid.addWidget(readings_label, 0, 4); self.samples = QComboBox(); self.samples.setFixedWidth(78); self.samples.addItems([str(value) for value in range(1, 31)]); self.samples.setCurrentText("3"); grid.addWidget(self.samples, 0, 5)
        self.sound = QPushButton("🔔︎"); self.sound.setObjectName("soundButton"); self.sound.setFixedWidth(44); self.sound.setCheckable(True); self.sound.setChecked(True); self.sound.clicked.connect(lambda: self.sound.setText("🔔︎" if self.sound.isChecked() else "🔕︎")); grid.addWidget(self.sound, 0, 7); grid.setColumnStretch(6, 1)
        self.mode_group = QButtonGroup(self); modes = QHBoxLayout(); self.mode_buttons = {}
        for key, text in (("three", "Three-point test"), ("full", "Full power table"), ("manual", "Manual frequency switch")):
            button = QPushButton(text); button.setCheckable(True); button.setFixedHeight(38); button.setObjectName("modeCard"); button.clicked.connect(lambda checked, value=key: self._set_mode(value)); self.mode_group.addButton(button); self.mode_buttons[key] = button; modes.addWidget(button)
        self.mode_buttons["three"].setChecked(True); modes.addStretch(); grid.addLayout(modes, 1, 0, 1, 8)
        self.option_stack = QStackedWidget(); self.option_stack.setStyleSheet("QStackedWidget { background: transparent; }"); blank = QWidget(); self.full_options = QWidget(); self.full_options.setObjectName("fullOptions"); self.full_options.setStyleSheet("QWidget#fullOptions { background: transparent; }"); f = QHBoxLayout(self.full_options); f.setContentsMargins(0,0,0,0); f.addWidget(QLabel("Measure power")); self.full_power = QComboBox(); self.full_power.setFixedWidth(118); f.addWidget(self.full_power); f.addWidget(QLabel("Stabilization delay, s")); self.settle = QSpinBox(); self.settle.setFixedWidth(92); self.settle.setRange(0,120); self.settle.setValue(3); f.addWidget(self.settle); f.addStretch()
        self.manual_options = QWidget(); self.manual_options.setObjectName("manualOptions"); self.manual_options.setStyleSheet("QWidget#manualOptions { background: transparent; }"); m = QHBoxLayout(self.manual_options); m.setContentsMargins(0,0,0,0); m.addWidget(QLabel("Frequency, MHz")); self.frequency = QComboBox(); self.frequency.setMinimumWidth(220); m.addWidget(self.frequency); m.addWidget(QLabel("Power, mW")); self.power = QComboBox(); self.power.setFixedWidth(100); m.addWidget(self.power); m.addStretch(); self.option_stack.addWidget(blank); self.option_stack.addWidget(self.full_options); self.option_stack.addWidget(self.manual_options); self.option_stack.setMaximumHeight(0); grid.addWidget(self.option_stack, 2, 0, 1, 8)
        self.status = QLabel(); self.status.setObjectName("status"); self.status.setAlignment(Qt.AlignLeft | Qt.AlignVCenter); self.status.setContentsMargins(10, 3, 10, 3)
        action_row = QWidget(); action_row.setObjectName("actionRow"); action_row.setStyleSheet("QWidget#actionRow { background: transparent; }"); action_grid = QGridLayout(action_row); action_grid.setContentsMargins(0, 0, 0, 0); action_grid.setColumnStretch(0, 1); action_grid.setColumnStretch(1, 1); action_grid.setColumnStretch(2, 1)
        actions = QWidget(); actions.setObjectName("actionButtons"); actions.setStyleSheet("QWidget#actionButtons { background: transparent; }"); actions_layout = QHBoxLayout(actions); actions_layout.setContentsMargins(0, 0, 0, 0); self.start_button = QPushButton("Start test"); self.start_button.setFixedHeight(36); self.start_button.setObjectName("primary"); self.start_button.clicked.connect(self.start); self.stop_button = QPushButton("Stop"); self.stop_button.setFixedHeight(36); self.stop_button.setEnabled(False); self.stop_button.clicked.connect(self.stop); actions_layout.addWidget(self.start_button); actions_layout.addWidget(self.stop_button); action_grid.addWidget(actions, 0, 0, alignment=Qt.AlignLeft); action_grid.addWidget(self.status, 0, 1, alignment=Qt.AlignHCenter); grid.addWidget(action_row,3,0,1,8); layout.addWidget(panel)
        tabs = QTabWidget(); result = QWidget(); rv = QVBoxLayout(result); rv.setContentsMargins(12, 12, 12, 12); rv.setSpacing(8); self.steps = QTableWidget(0,2); self.steps.setHorizontalHeaderLabels(["Step","Result"]); self.steps.verticalHeader().setVisible(False); self.steps.verticalHeader().setDefaultSectionSize(20); self.steps.horizontalHeader().setFixedHeight(24); self.steps.horizontalHeader().setStretchLastSection(True); self.steps.setMinimumHeight(145); self.steps.setMaximumHeight(185); rv.addWidget(self.steps); self.chart = ComparisonChart(self); rv.addWidget(self.chart, 1); tabs.addTab(result,"Result summary")
        log = QWidget(); lv = QVBoxLayout(log); self.log = QTableWidget(0,1); self.log.setHorizontalHeaderLabels(["Output"]); self.log.horizontalHeader().setStretchLastSection(True); lv.addWidget(self.log); tabs.addTab(log,"Live test log"); layout.addWidget(tabs,1); self._set_status("Ready. Choose a test and press Start test."); self._sync_mode()

    def _apply_theme(self) -> None:
        p = self.palette; self.theme_button.setText(("☾  Dark" if self.theme == "dark" else "☀  Light")); self.setStyleSheet(f"""QWidget{{background:{p['bg']};color:{p['text']};font:14px 'Segoe UI';}} QLabel{{background:transparent;}} #title{{font-size:22px;font-weight:700;}} #panel,QTabWidget::pane{{background:{p['surface']};border:1px solid {p['border']};border-radius:12px;}} QPushButton,QLineEdit,QComboBox,QSpinBox{{min-height:32px;border:1px solid {p['border']};border-radius:9px;background:{p['surface']};padding:0 10px;}} QPushButton:hover{{border-color:{p['primary']};}} #themeToggle,#primary{{min-height:32px;max-height:32px;}} #primary{{background:{p['primary']};color:white;font-weight:700;}} #primary:hover{{background:#2563EB;}} #primary:pressed{{background:#1D4ED8;}} #primary:disabled{{background:{p['pending']};border-color:{p['border']};color:{p['surface']};}} #modeCard{{min-height:36px;max-height:36px;background:{p['surface']};text-align:left;padding-left:14px;}} #modeCard:checked{{background:{p['alt']};border:2px solid {p['primary']};}} QSpinBox::up-button,QSpinBox::down-button{{width:22px;border-left:1px solid {p['border']};background:{p['alt']};}} QSpinBox::up-arrow{{border-left:5px solid transparent;border-right:5px solid transparent;border-bottom:7px solid {p['text']};}} QSpinBox::down-arrow{{border-left:5px solid transparent;border-right:5px solid transparent;border-top:7px solid {p['text']};}} QTabBar::tab{{padding:8px 16px;background:{p['alt']};border:1px solid {p['border']};}} QTabBar::tab:selected{{color:{p['primary']};background:{p['surface']};}} QTableWidget{{background:{p['surface']};border:1px solid {p['border']};gridline-color:{p['border']};}} QHeaderView::section{{background:{p['alt']};padding:8px;border:0;}} #status{{padding:6px 12px;border-radius:8px;}}"""); self.chart.update(); self._set_status(self.status.text())

    def _toggle_theme(self) -> None: self.theme = "dark" if self.theme == "light" else "light"; self.palette = PALETTES[self.theme]; self.settings.setValue("theme", self.theme); self._apply_theme()
    def _play_completion_sound(self) -> None:
        if self.sound.isChecked():
            QApplication.beep()
    def _set_status(self, text: str) -> None:
        self.status.setText(text); self.steps.horizontalHeader().setStyleSheet("QHeaderView::section { padding: 3px 8px; }"); self.steps.horizontalHeader().setFixedHeight(self.steps.fontMetrics().height() + 8); p=self.palette; low=text.lower(); color = p["fail"] if any(x in low for x in ("error","warning","cannot","not confirmed")) else p["primary"] if any(x in low for x in ("running","switching","stopping","returning")) else p["ok"] if "completed" in low else p["pending"]
        self.status.setStyleSheet(f"background:{p['soft'] if color == p['ok'] else p['alt']}; border:1px solid {color}; color:{p['text']}; font-weight:600; padding:3px 10px;")
        self.status.setFixedHeight(self.status.fontMetrics().height() + 12)
    def _set_mode(self, mode: str) -> None:
        self.mode_buttons[mode].setChecked(True); self.option_stack.setCurrentIndex({"three":0,"full":1,"manual":2}[mode]); self.option_stack.setMaximumHeight(0 if mode == "three" else 48); self.start_button.setText("Switch VTX" if mode=="manual" else "Start test"); self._sync_mode()
    def _mode(self) -> str: return next(key for key, button in self.mode_buttons.items() if button.isChecked())
    def _sync_mode(self) -> None:
        full_only=self.vtx.currentText() in FULL_ONLY_VTX; self.mode_buttons["three"].setEnabled(not full_only)
        if full_only and self._mode()=="three": self._set_mode("full"); return
        vtx=self.vtx.currentText(); full=("5 W",) if vtx in {"7.2", "8.5"} else (("All","25 mW","500 mW","3 W","5 W") if vtx=="5.8.test" else ("All","25 mW","2 W","4 W")); powers=("25","2500","5000") if vtx in {"7.2", "8.5"} else (("25","500","3000","5000") if vtx=="5.8.test" else ("25","2000","4000")); self.full_power.clear(); self.full_power.addItems(full); self.power.clear(); self.power.addItems(powers)
        if self._mode()=="manual": self._sync_manual_frequencies()
    def _sync_manual_frequencies(self) -> None:
        try:
            v=self.vtx.currentText(); plan=load_direct_frequency_sweep_plan(PLAN_DIRECTORY/DIRECT_FREQUENCY_SWEEP_PLAN_FILENAMES[v]) if v in DIRECT_FREQUENCY_SWEEP_PLAN_FILENAMES else (load_power_table_plan(PLAN_DIRECTORY/POWER_TABLE_PLAN_FILENAMES[v]) if v in POWER_TABLE_PLAN_FILENAMES else load_plan(PLAN_DIRECTORY/VTX_PLAN_FILENAMES[v])); values=[f"{b}{i} · {freq} MHz" for b,fs in plan["_profile"]["bands"].items() for i,freq in enumerate(fs,1) if isinstance(freq,int) and freq>0]
        except (OSError, ValueError, KeyError, TypeError): values=[]
        self.frequency.clear(); self.frequency.addItems(values)
    def _tool_command(self, executable: str, script: str) -> list[str]:
        if not getattr(sys,"frozen",False): return [sys.executable,"-u",str(ROOT/script)]
        target=Path(sys.executable).resolve().parent.parent/executable/f"{executable}{'.exe' if sys.platform=='win32' else ''}"; return [str(target)] if target.is_file() else (_ for _ in ()).throw(ValueError(f"Packaged tool was not found: {target}"))
    def _command(self) -> list[str]:
        vtx, mode, stamp=self.vtx.currentText(),self._mode(),datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if mode=="three": self.output_path=LOG_DIRECTORY/f"gui-three-point-{vtx}-{stamp}.csv"; command=[*self._tool_command("VTX-Three-Point-Test","vtx_three_point_test.py"),"--vtx",vtx,"--samples",self.samples.currentText(),"--csv",str(self.output_path)]
        else: self.output_path=LOG_DIRECTORY/f"gui-power-table-{vtx}-{stamp}.csv"; command=[*self._tool_command("VTX-Power-Table","vtx_power_table_measure.py"),"--vtx","5.8.all" if vtx=="5.8" else vtx,"--samples",self.samples.currentText(),"--csv",str(self.output_path),"--power",POWER_SELECTIONS[self.full_power.currentText()],"--settle-time",str(self.settle.value())]
        if self.port.text().strip(): command.extend(["--port",self.port.text().strip()])
        return command
    def start(self) -> None:
        if self.run_active or self.process: return
        if self._mode()=="manual": self._switch_manual(); return
        try: command=self._command()
        except ValueError as exc: QMessageBox.critical(self,"Invalid settings",str(exc)); return
        self.log.setRowCount(0); self.steps.setRowCount(0); self.results=[]; self.run_active=True; self.stop_requested=self.run_failed=self.safety_reset_in_progress=self.safety_reset_confirmed=False; self.full_progress=None
        if self._mode()=="three": self._prepare_preview()
        else: self._prepare_full_progress()
        self._set_status("Test is running. Key switching and measurement steps will appear below."); self.chart.update(); self._line("$ "+" ".join(command)); self.start_button.setEnabled(False); self.stop_button.setEnabled(True); threading.Thread(target=self._run_process,args=(command,),daemon=True).start()
    def _prepare_preview(self) -> None:
        try: self.results=[{"label":f"{x['band']}{x['channel']} · {x['frequency_mhz']} MHz","frequency":int(x["frequency_mhz"]),"reference":float(x["reference_output_mw"]),"samples":[],"status":"PENDING"} for x in load_plan(PLAN_DIRECTORY/VTX_PLAN_FILENAMES[self.vtx.currentText()])["points"]]; self.chart.setMinimumHeight(70 + 72 * len(self.results)); self.result_message="Live measurement: every new sample and final average are shown below."
        except (OSError,ValueError,KeyError): self.results=[]
    def _prepare_full_progress(self) -> None:
        try:
            v="5.8.all" if self.vtx.currentText()=="5.8" else self.vtx.currentText(); direct=v in DIRECT_FREQUENCY_SWEEP_PLAN_FILENAMES; plan=(load_direct_frequency_sweep_plan if direct else load_power_table_plan)(PLAN_DIRECTORY/(DIRECT_FREQUENCY_SWEEP_PLAN_FILENAMES[v] if direct else POWER_TABLE_PLAN_FILENAMES[v])); selected=[x for x in measurement_power_levels(plan["_profile"]) if POWER_SELECTIONS[self.full_power.currentText()] == "all" or x[1]==int(POWER_SELECTIONS[self.full_power.currentText()])]; self.full_progress=(0,len(measurement_points(plan["_profile"]))*len(selected),int(self.samples.currentText())); self.result_message="Full-table progress"
        except (OSError,ValueError,KeyError): self.full_progress=None
    def _run_process(self, command: list[str]) -> None:
        try:
            env=os.environ.copy(); env["PYTHONIOENCODING"]="utf-8"; self.process=subprocess.Popen(command,cwd=ROOT,text=True,encoding="utf-8",errors="replace",env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,bufsize=1,creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform=="win32" else 0)
            for line in self.process.stdout or []: self.events.put(("log",line.rstrip()))
            self.events.put(("finished",str(self.process.wait())))
        except OSError as exc: self.events.put(("error",str(exc)))
    def _line(self, text: str) -> None: row=self.log.rowCount(); self.log.insertRow(row); self.log.setItem(row,0,QTableWidgetItem(text)); self.log.scrollToBottom()
    def _step(self, step: str, detail: str) -> None:
        row=self.steps.rowCount(); self.steps.insertRow(row); self.steps.setRowHeight(row, 20); self.steps.setItem(row,0,QTableWidgetItem(step)); result=QTableWidgetItem(detail); result.setToolTip(detail); self.steps.setItem(row,1,result); self.steps.scrollToBottom()
    def _record_progress(self,line:str) -> None:
        if line.startswith("Setting "):
            match=re.search(r"\((\d+) MHz\)",line); self.current_frequency=int(match.group(1)) if match else None; self._step("Switching",line.removeprefix("Setting "))
        elif line.startswith("Sample "):
            match=re.fullmatch(r"Sample (\d+): ([0-9.]+) mW(?: \([-0-9.]+ dBm\))?",line)
            if match and self.current_frequency:
                for r in self.results:
                    if r["frequency"]==self.current_frequency: r["samples"].append(float(match.group(2))); r["measured"]=fmean(r["samples"]); r["status"]="MEASURING"; self._step("Sample",f"{match.group(1)}/{self.samples.currentText()}: {float(match.group(2)):.0f} mW"); break
        elif line.startswith("VTX output (path-corrected):") and self.current_frequency:
            for r in self.results:
                if r["frequency"]==self.current_frequency and r.get("measured") is not None: r["status"]=power_status(float(r["measured"]),float(r["reference"]),{"green_min":90.,"yellow_min":80.}); break
            self._step("Measured",line.removeprefix("VTX output (path-corrected):").strip())
        elif line.startswith("Returning VTX"): self.safety_reset_in_progress=True; self._step("Safety",line)
        elif line.startswith("Final VTX:"): self.safety_reset_in_progress=False; self.safety_reset_confirmed=True; self._step("Completed",line.removeprefix("Final VTX:"))
        elif line.startswith("Error:"): self.run_failed=True; self._step("Error",line.removeprefix("Error:"))
        elif self.full_progress and re.match(r"^\s{2}\S+\s+\(\d+ MHz\)\s+\d+ mW",line): done,total,samples=self.full_progress; self.full_progress=(min(done+1,total),total,samples); self._set_status(f"Full-table test is running: {(done+1)/total*100:.1f}% complete.")
        self.chart.update()
    def stop(self) -> None:
        if not self.run_active or QMessageBox.question(self,"Stop measurement","Stop the test and wait while the VTX is returned to 25 mW?") != QMessageBox.Yes: return
        self.stop_requested=True; self.stop_button.setEnabled(False); self._set_status("Stopping test safely and returning VTX to 25 mW…"); self._step("Stopping","Waiting for the VTX safety reset to complete.")
        if self.process and self.process.poll() is None:
            try: self.process.send_signal(signal.CTRL_BREAK_EVENT if sys.platform=="win32" else signal.SIGINT)
            except OSError as exc: self.events.put(("error",str(exc)))
    def _drain_events(self) -> None:
        try:
            while True:
                kind,value=self.events.get_nowait()
                if kind=="log": self._line(value); self._record_progress(value)
                elif kind=="finished":
                    self.process=None; self.run_active=False; self.start_button.setEnabled(True); self.stop_button.setEnabled(False)
                    if self.stop_requested: self._set_status("Test stopped safely. VTX was confirmed at 25 mW." if self.safety_reset_confirmed else "Test stopped, but 25 mW was not confirmed. Check the VTX and technical log.")
                    elif value!="0": self.run_failed=True; self._set_status("Test stopped with an error. Partial results, if any, are shown below.")
                    else: self._set_status("Test completed. Compare the values and statuses with the reference below."); self._play_completion_sound()
                    if self.output_path and self.output_path.exists(): self._load_result(self.output_path)
                elif kind=="error": self.process=None; self.run_active=False; self.run_failed=True; self.start_button.setEnabled(True); self.stop_button.setEnabled(False); self._set_status("Action failed. See the technical log for details."); self._line("Error: "+value); QMessageBox.critical(self,"VTX Tester",value)
                elif kind=="manual": self._line(value); self._step("Active VTX",value.removeprefix("VTX active: ")); self._set_status("Frequency and power were set. Measuring output now.")
                elif kind=="manual_sample": self._record_progress(f"Sample {value.replace('|', ': ', 1)}")
                elif kind=="manual_done":
                    if self.results: self.results[0]["measured"]=float(value); self.results[0]["status"]="N/A"
                    self.run_active=False; self.start_button.setEnabled(True); self.stop_button.setEnabled(False); self._set_status("Manual measurement completed. The VTX remains at the selected frequency and power."); self._play_completion_sound(); self.chart.update()
        except queue.Empty: pass
    def _load_result(self,path:Path)->None:
        if "power-table" in path.name: self.results=[]; self.result_message=f"Full table saved: {path.name}."; self.chart.update(); return
        try:
            plan=load_plan(PLAN_DIRECTORY/VTX_PLAN_FILENAMES[self.vtx.currentText()]); values=defaultdict(list)
            with path.open(encoding="utf-8-sig",newline="") as source:
                for row in csv.DictReader(source): values[int(row["frequency_mhz"])].append(float(row["vtx_output_mw"]))
            self.results=[]
            for x in plan["points"]:
                f=int(x["frequency_mhz"])
                if values[f]: avg=fmean(values[f]); ref=float(x["reference_output_mw"]); self.results.append({"label":f"{x['band']}{x['channel']} · {f} MHz","frequency":f,"reference":ref,"samples":values[f],"measured":avg,"status":power_status(avg,ref,plan["_reference_thresholds_percent"])})
            self.chart.setMinimumHeight(70 + 72 * len(self.results)); self.result_message=f"Reference comparison: {path.name}"
        except (OSError,ValueError,KeyError,csv.Error) as exc: self.results=[]; self.result_message=f"Cannot read results: {exc}"
        self.chart.update()
    def _switch_manual(self) -> None:
        match=re.search(r"(\d+) MHz$",self.frequency.currentText())
        if not match: QMessageBox.critical(self,"Invalid manual setting","Choose a frequency from the VTX channel list."); return
        frequency,power=int(match.group(1)),int(self.power.currentText())
        try: validate_sensor_frequencies(load_measurement_path(ROOT/"configs"/"measurement_path_control.json"),[frequency])
        except ValueError as exc: QMessageBox.critical(self,"Invalid manual setting",str(exc)); return
        self.results=[{"label":self.frequency.currentText(),"frequency":frequency,"reference":float(power),"samples":[],"status":"MEASURING"}]; self.current_frequency=frequency; self.run_active=True; self.start_button.setEnabled(False); self.stop_button.setEnabled(True); self._set_status("Switching VTX, waiting for stable output, then measuring samples."); self._line(f"Switching directly to {frequency} MHz / {power} mW…"); self.chart.update(); threading.Thread(target=self._manual_worker,args=(frequency,power,int(self.samples.currentText())),daemon=True).start()
    def _manual_worker(self, frequency:int, power:int, sample_count:int)->None:
        sensor=None
        try:
            port=resolve_port(self.port.text().strip() or None,115200,2.0)
            with open_serial(port,115200,2.0) as serial_port:
                client=MspClient(serial_port,2.0); current=parse_vtx_config(client.request(MSP_VTX_CONFIG)); levels=read_vtx_power_levels(client,int(current.get("vtx_table_power_levels") or 0)); matching=[int(level["index"]) for level in levels if int(level["value"])==power]
                if not matching: raise MspError(f"Flight Controller VTX table has no {power} mW power level.")
                updated=switch_and_confirm(client,current,band=1,channel=1,frequency_mhz=frequency,power_index=matching[0],settle_time_s=3.0,direct_frequency=True)
            self.events.put(("manual","VTX active: "+format_vtx(updated))); path=load_measurement_path(ROOT/"configs"/"measurement_path_control.json"); sensor=NrpzSensor(); sensor.open("*"); sensor.configure(frequency*1_000_000,16); sensor.set_level_offset(offset_db_at_frequency(path,frequency)); wait_for_stable_power(sensor,timeout_ms=5_000,sample_interval_s=.5,required_samples=3,tolerance_percent=2.0,max_wait_s=15.0); readings=[]
            for index in range(1,sample_count+1):
                value=watts_to_millwatts(sensor.read_watts(5_000)); readings.append(value); self.events.put(("manual_sample",f"{index}|{value:.2f}"));
                if index<sample_count: time.sleep(.5)
            self.events.put(("manual_done",f"{fmean(readings):.2f}"))
        except Exception as exc: self.events.put(("error",str(exc)))
        finally:
            if sensor:
                try: sensor.close()
                except NrpzError: pass


def main() -> None:
    app=QApplication(sys.argv); app.setStyle("Fusion"); window=VtxTesterQt(); window.show(); sys.exit(app.exec())
if __name__ == "__main__": main()
