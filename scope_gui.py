import os
os.environ["PYQTGRAPH_QT_LIB"] = "PyQt5"

import sys
import time
import numpy as np
import serial
import pyqtgraph as pg
import pyqtgraph.exporters
from PyQt5 import QtCore, QtGui, QtWidgets

# Constants / wire protocol
SERIAL_PORT = '/dev/ttyACM1'  # Adjust if port differs
BAUD_RATE = 115200
SAMPLES_PER_CHAN = 2048
TOTAL_SAMPLES = SAMPLES_PER_CHAN * 2       # interleaved CH1,CH2,CH1,CH2,...
FRAME_BYTES = TOTAL_SAMPLES * 2            # uint16 per sample
SAMPLE_RATE_PER_CHAN = 500_000.0           # 500 kS/s per channel
RECONNECT_INTERVAL_S = 2.0

CHANNEL_COLORS = [
    ("Cyan", "#00FFCC"), ("Magenta", "#FF007F"), ("Yellow", "#FFD500"),
    ("Orange", "#FF8C00"), ("Green", "#39FF14"), ("White", "#F0F0F0"),
]

DARK_STYLESHEET = """
QMainWindow, QWidget { background-color: #1e1f22; color: #e8e8e8; }
QGroupBox {
    border: 1px solid #3a3d41; border-radius: 6px; margin-top: 10px;
    font-weight: bold; color: #cfd2d6;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
QPushButton {
    background-color: #2d2f33; border: 1px solid #44474b; border-radius: 4px;
    padding: 6px; color: #e8e8e8;
}
QPushButton:hover { background-color: #383b40; }
QPushButton:pressed { background-color: #202124; }
QComboBox, QDoubleSpinBox, QSpinBox {
    background-color: #2a2c30; border: 1px solid #44474b; border-radius: 3px;
    padding: 3px; color: #e8e8e8;
}
QLabel { color: #d6d9dc; }
QCheckBox { color: #d6d9dc; }
QStatusBar { background-color: #17181a; color: #b7bbbf; }
QSplitter::handle { background-color: #3a3d41; }
"""


class Channel:
    """Per-channel display state (enable, color, vertical scale/offset)."""

    def __init__(self, name, color_hex):
        self.name = name
        self.enabled = True
        self.color = color_hex
        self.scale = 1.0
        self.offset = 0.0
        self.last_data = np.zeros(SAMPLES_PER_CHAN)


class DualChannelOscilloscope(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pico 2 Dual-Channel Scope & Spectrum Analyzer")
        self.resize(1400, 900)

        # Acquisition / trigger state
        self.is_running = True
        self.single_armed = False
        self.acq_mode = 'Auto'          # Auto / Normal / Single
        self.trigger_level = 1.65
        self.trigger_mode = 'rising'
        self.trigger_channel = 'CH1'
        self.trigger_holdoff_ms = 0.0
        self.pretrigger_pct = 10.0
        self.timebase_window = 1024
        self.last_trigger_time = 0.0
        self.last_valid_disp = (np.zeros(self.timebase_window), np.zeros(self.timebase_window))

        self.persistence_enabled = False
        self.persistence_depth = 8
        self.persistence_buf = []  # list of (ch1, ch2) old frames, newest last

        self.ch1 = Channel("CH1 (ADC0)", CHANNEL_COLORS[0][1])
        self.ch2 = Channel("CH2 (ADC1)", CHANNEL_COLORS[1][1])

        # fps / status tracking
        self.frame_count = 0
        self.fps = 0.0
        self.last_fps_time = time.time()
        self.last_reconnect_attempt = 0.0

        pg.setConfigOptions(antialias=False, useOpenGL=False)

        self.ser = None
        self.connected = False
        self.try_connect(initial=True)

        self.build_ui()
        self.setStyleSheet(DARK_STYLESHEET)
        self.setup_shortcuts()
        self.update_status_bar()

        # Pre-compute DSP Hann window and FFT frequencies
        self.hann_window = np.hanning(SAMPLES_PER_CHAN)
        self.fft_freqs = np.fft.rfftfreq(SAMPLES_PER_CHAN, d=1.0 / SAMPLE_RATE_PER_CHAN)

        self.timer = QtCore.QTimer()
        self.timer.setInterval(33)
        self.timer.timeout.connect(self.update_scope)
        self.timer.start()

    # Serial connection handling (with auto-reconnect)
    def try_connect(self, initial=False):
        try:
            self.ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.2)
            self.connected = True
            if not initial:
                print(f"Reconnected to {SERIAL_PORT}")
        except Exception as e:
            self.connected = False
            self.ser = None
            if initial:
                print(f"Error opening serial port {SERIAL_PORT}: {e}")

    # UI construction
    def build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        outer_layout = QtWidgets.QHBoxLayout(central)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        outer_layout.addWidget(splitter)

        # Left: plots (in a vertical splitter)
        plot_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)

        self.time_plot = pg.PlotWidget(title="Time Domain Waveforms")
        self.time_plot.setYRange(-0.5, 4.0)
        self.time_plot.setLabel('left', 'Voltage', units='V')
        self.time_plot.setLabel('bottom', 'Time', units='s')
        self.time_plot.showGrid(x=True, y=True, alpha=0.3)
        self.time_plot.addLegend()

        self.ch1_curve = self.time_plot.plot(pen=pg.mkPen(self.ch1.color, width=2), name=self.ch1.name)
        self.ch2_curve = self.time_plot.plot(pen=pg.mkPen(self.ch2.color, width=2), name=self.ch2.name)
        self.ch1_curve.setDownsampling(auto=True, method='peak')
        self.ch2_curve.setDownsampling(auto=True, method='peak')
        self.ch1_curve.setClipToView(True)
        self.ch2_curve.setClipToView(True)

        # Persistence (afterglow) trace pool - pre-allocated, hidden until used
        self.persistence_curves = []
        for _ in range(40):  # up to 20 frames x 2 channels
            c = self.time_plot.plot(pen=pg.mkPen((255, 255, 255, 0), width=1))
            c.setZValue(-10)
            self.persistence_curves.append(c)

        self.trig_line = pg.InfiniteLine(
            pos=self.trigger_level, angle=0,
            pen=pg.mkPen('r', style=QtCore.Qt.DashLine), movable=True)
        self.trig_line.sigPositionChanged.connect(self.on_trig_line_dragged)
        self.time_plot.addItem(self.trig_line)

        self.cursor_v1 = pg.InfiniteLine(pos=1.0, angle=0,
                                          pen=pg.mkPen('#FF8C00', style=QtCore.Qt.DotLine), movable=True)
        self.cursor_v2 = pg.InfiniteLine(pos=2.0, angle=0,
                                          pen=pg.mkPen('#FF8C00', style=QtCore.Qt.DotLine), movable=True)
        self.time_plot.addItem(self.cursor_v1)
        self.time_plot.addItem(self.cursor_v2)
        self.cursor_v1.sigPositionChanged.connect(self.update_cursor_delta)
        self.cursor_v2.sigPositionChanged.connect(self.update_cursor_delta)

        plot_splitter.addWidget(self.time_plot)

        self.fft_plot = pg.PlotWidget(title="FFT Spectrum Analyzer (Hann Windowed)")
        self.fft_plot.setLabel('left', 'Magnitude', units='V')
        self.fft_plot.setLabel('bottom', 'Frequency', units='Hz')
        self.fft_plot.setXRange(0, SAMPLE_RATE_PER_CHAN / 2)
        self.fft_plot.showGrid(x=True, y=True, alpha=0.3)
        self.fft_plot.addLegend()
        self.ch1_fft_curve = self.fft_plot.plot(pen=pg.mkPen(self.ch1.color, width=1.5), name="CH1 FFT")
        self.ch2_fft_curve = self.fft_plot.plot(pen=pg.mkPen(self.ch2.color, width=1.5), name="CH2 FFT")
        plot_splitter.addWidget(self.fft_plot)

        plot_splitter.setStretchFactor(0, 3)
        plot_splitter.setStretchFactor(1, 2)
        splitter.addWidget(plot_splitter)

        # Right: sidebar
        sidebar_widget = QtWidgets.QWidget()
        sidebar = QtWidgets.QVBoxLayout(sidebar_widget)
        sidebar_scroll = QtWidgets.QScrollArea()
        sidebar_scroll.setWidget(sidebar_widget)
        sidebar_scroll.setWidgetResizable(True)
        sidebar_scroll.setMinimumWidth(320)
        splitter.addWidget(sidebar_scroll)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)

        sidebar.addWidget(self._build_measurements_group())
        sidebar.addWidget(self._build_acquisition_group())
        sidebar.addWidget(self._build_channel_group())
        sidebar.addWidget(self._build_trigger_group())
        sidebar.addWidget(self._build_persistence_group())
        sidebar.addWidget(self._build_export_group())
        sidebar.addStretch()

        # Status bar
        self.status_conn = QtWidgets.QLabel()
        self.status_fps = QtWidgets.QLabel()
        self.status_rate = QtWidgets.QLabel(f"{SAMPLE_RATE_PER_CHAN/1000:.0f} kS/s/ch")
        sb = self.statusBar()
        sb.addWidget(self.status_conn)
        sb.addPermanentWidget(self.status_fps)
        sb.addPermanentWidget(self.status_rate)

    def _build_measurements_group(self):
        meas_group = QtWidgets.QGroupBox("Live Measurements")

        self.lbl_ch1_vpp = QtWidgets.QLabel("CH1 Vpp: -- V")
        self.lbl_ch1_rms = QtWidgets.QLabel("CH1 RMS: -- V")
        self.lbl_ch1_freq = QtWidgets.QLabel("CH1 Freq: -- Hz")
        self.lbl_ch1_duty = QtWidgets.QLabel("CH1 Duty: -- %")
        self.lbl_ch1_rise = QtWidgets.QLabel("CH1 Rise: -- \u03bcs")

        self.lbl_ch2_vpp = QtWidgets.QLabel("CH2 Vpp: -- V")
        self.lbl_ch2_rms = QtWidgets.QLabel("CH2 RMS: -- V")
        self.lbl_ch2_freq = QtWidgets.QLabel("CH2 Freq: -- Hz")
        self.lbl_ch2_duty = QtWidgets.QLabel("CH2 Duty: -- %")
        self.lbl_ch2_rise = QtWidgets.QLabel("CH2 Rise: -- \u03bcs")

        self.lbl_cursor_dv = QtWidgets.QLabel("Cursor \u0394V: 1.00 V")

        grid = QtWidgets.QGridLayout()
        grid.addWidget(self.lbl_ch1_vpp, 0, 0)
        grid.addWidget(self.lbl_ch1_rms, 0, 1)
        grid.addWidget(self.lbl_ch1_freq, 1, 0)
        grid.addWidget(self.lbl_ch1_duty, 1, 1)
        grid.addWidget(self.lbl_ch1_rise, 2, 0, 1, 2)
        grid.addWidget(self.lbl_ch2_vpp, 3, 0)
        grid.addWidget(self.lbl_ch2_rms, 3, 1)
        grid.addWidget(self.lbl_ch2_freq, 4, 0)
        grid.addWidget(self.lbl_ch2_duty, 4, 1)
        grid.addWidget(self.lbl_ch2_rise, 5, 0, 1, 2)
        grid.addWidget(self.lbl_cursor_dv, 6, 0, 1, 2)
        meas_group.setLayout(grid)
        return meas_group

    def _build_acquisition_group(self):
        acq_group = QtWidgets.QGroupBox("Acquisition Control")
        layout = QtWidgets.QVBoxLayout()

        self.btn_run_stop = QtWidgets.QPushButton("STOP / FREEZE  (Space)")
        self.btn_run_stop.setStyleSheet("background-color: #8B0000; color: white; font-weight: bold;")
        self.btn_run_stop.clicked.connect(self.toggle_run_stop)
        layout.addWidget(self.btn_run_stop)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Mode:"))
        self.combo_acq_mode = QtWidgets.QComboBox()
        self.combo_acq_mode.addItems(["Auto", "Normal", "Single"])
        self.combo_acq_mode.currentTextChanged.connect(self.on_acq_mode_change)
        row.addWidget(self.combo_acq_mode)
        layout.addLayout(row)

        row2 = QtWidgets.QHBoxLayout()
        row2.addWidget(QtWidgets.QLabel("Refresh (ms):"))
        self.spin_refresh = QtWidgets.QSpinBox()
        self.spin_refresh.setRange(10, 500)
        self.spin_refresh.setValue(33)
        self.spin_refresh.valueChanged.connect(self.on_refresh_change)
        row2.addWidget(self.spin_refresh)
        layout.addLayout(row2)

        self.btn_reset_view = QtWidgets.QPushButton("Reset View (Ctrl+R)")
        self.btn_reset_view.clicked.connect(self.reset_view)
        layout.addWidget(self.btn_reset_view)

        acq_group.setLayout(layout)
        return acq_group

    def _build_channel_group(self):
        ch_group = QtWidgets.QGroupBox("Channels")
        layout = QtWidgets.QGridLayout()

        headers = ["", "On", "Color", "Scale", "Offset (V)"]
        for col, h in enumerate(headers):
            layout.addWidget(QtWidgets.QLabel(f"<b>{h}</b>"), 0, col)

        self.chk_ch1 = QtWidgets.QCheckBox()
        self.chk_ch1.setChecked(True)
        self.chk_ch1.stateChanged.connect(lambda s: self.on_channel_toggle(self.ch1, self.ch1_curve, s))
        self.combo_ch1_color = QtWidgets.QComboBox()
        for name, hexval in CHANNEL_COLORS:
            self.combo_ch1_color.addItem(name, hexval)
        self.combo_ch1_color.currentIndexChanged.connect(
            lambda i: self.on_channel_color(self.ch1, self.ch1_curve, self.ch1_fft_curve, i, self.combo_ch1_color))
        self.spin_ch1_scale = QtWidgets.QDoubleSpinBox()
        self.spin_ch1_scale.setRange(0.01, 100.0)
        self.spin_ch1_scale.setSingleStep(0.1)
        self.spin_ch1_scale.setValue(1.0)
        self.spin_ch1_scale.valueChanged.connect(lambda v: setattr(self.ch1, 'scale', v))
        self.spin_ch1_offset = QtWidgets.QDoubleSpinBox()
        self.spin_ch1_offset.setRange(-10.0, 10.0)
        self.spin_ch1_offset.setSingleStep(0.1)
        self.spin_ch1_offset.valueChanged.connect(lambda v: setattr(self.ch1, 'offset', v))

        layout.addWidget(QtWidgets.QLabel("CH1"), 1, 0)
        layout.addWidget(self.chk_ch1, 1, 1)
        layout.addWidget(self.combo_ch1_color, 1, 2)
        layout.addWidget(self.spin_ch1_scale, 1, 3)
        layout.addWidget(self.spin_ch1_offset, 1, 4)

        self.chk_ch2 = QtWidgets.QCheckBox()
        self.chk_ch2.setChecked(True)
        self.chk_ch2.stateChanged.connect(lambda s: self.on_channel_toggle(self.ch2, self.ch2_curve, s))
        self.combo_ch2_color = QtWidgets.QComboBox()
        for i, (name, hexval) in enumerate(CHANNEL_COLORS):
            self.combo_ch2_color.addItem(name, hexval)
        self.combo_ch2_color.setCurrentIndex(1)
        self.combo_ch2_color.currentIndexChanged.connect(
            lambda i: self.on_channel_color(self.ch2, self.ch2_curve, self.ch2_fft_curve, i, self.combo_ch2_color))
        self.spin_ch2_scale = QtWidgets.QDoubleSpinBox()
        self.spin_ch2_scale.setRange(0.01, 100.0)
        self.spin_ch2_scale.setSingleStep(0.1)
        self.spin_ch2_scale.setValue(1.0)
        self.spin_ch2_scale.valueChanged.connect(lambda v: setattr(self.ch2, 'scale', v))
        self.spin_ch2_offset = QtWidgets.QDoubleSpinBox()
        self.spin_ch2_offset.setRange(-10.0, 10.0)
        self.spin_ch2_offset.setSingleStep(0.1)
        self.spin_ch2_offset.valueChanged.connect(lambda v: setattr(self.ch2, 'offset', v))

        layout.addWidget(QtWidgets.QLabel("CH2"), 2, 0)
        layout.addWidget(self.chk_ch2, 2, 1)
        layout.addWidget(self.combo_ch2_color, 2, 2)
        layout.addWidget(self.spin_ch2_scale, 2, 3)
        layout.addWidget(self.spin_ch2_offset, 2, 4)

        ch_group.setLayout(layout)
        return ch_group

    def _build_trigger_group(self):
        trig_group = QtWidgets.QGroupBox("Trigger Settings")
        layout = QtWidgets.QFormLayout()

        self.combo_trig_source = QtWidgets.QComboBox()
        self.combo_trig_source.addItems(["CH1", "CH2"])
        self.combo_trig_source.currentTextChanged.connect(self.on_trig_source_change)

        self.combo_trig_mode = QtWidgets.QComboBox()
        self.combo_trig_mode.addItems(["rising", "falling", "disabled"])
        self.combo_trig_mode.currentTextChanged.connect(self.on_trig_mode_change)

        self.spin_trig_level = QtWidgets.QDoubleSpinBox()
        self.spin_trig_level.setRange(0.0, 3.3)
        self.spin_trig_level.setSingleStep(0.05)
        self.spin_trig_level.setValue(self.trigger_level)
        self.spin_trig_level.valueChanged.connect(self.on_trig_level_change)

        self.spin_holdoff = QtWidgets.QDoubleSpinBox()
        self.spin_holdoff.setRange(0.0, 1000.0)
        self.spin_holdoff.setSingleStep(1.0)
        self.spin_holdoff.setValue(self.trigger_holdoff_ms)
        self.spin_holdoff.valueChanged.connect(lambda v: setattr(self, 'trigger_holdoff_ms', v))

        self.spin_pretrigger = QtWidgets.QSpinBox()
        self.spin_pretrigger.setRange(0, 90)
        self.spin_pretrigger.setValue(int(self.pretrigger_pct))
        self.spin_pretrigger.setSuffix(" %")
        self.spin_pretrigger.valueChanged.connect(lambda v: setattr(self, 'pretrigger_pct', float(v)))

        layout.addRow("Source:", self.combo_trig_source)
        layout.addRow("Slope:", self.combo_trig_mode)
        layout.addRow("Level (V):", self.spin_trig_level)
        layout.addRow("Holdoff (ms):", self.spin_holdoff)
        layout.addRow("Pre-trigger:", self.spin_pretrigger)
        trig_group.setLayout(layout)
        return trig_group

    def _build_persistence_group(self):
        group = QtWidgets.QGroupBox("Persistence / Afterglow")
        layout = QtWidgets.QFormLayout()

        self.chk_persistence = QtWidgets.QCheckBox("Enable")
        self.chk_persistence.stateChanged.connect(self.on_persistence_toggle)
        self.spin_persistence_depth = QtWidgets.QSpinBox()
        self.spin_persistence_depth.setRange(1, 20)
        self.spin_persistence_depth.setValue(self.persistence_depth)
        self.spin_persistence_depth.valueChanged.connect(self.on_persistence_depth_change)

        layout.addRow(self.chk_persistence)
        layout.addRow("Trace depth:", self.spin_persistence_depth)
        group.setLayout(layout)
        return group

    def _build_export_group(self):
        export_group = QtWidgets.QGroupBox("Export Tools")
        layout = QtWidgets.QVBoxLayout()
        self.btn_export_csv = QtWidgets.QPushButton("Export CSV Data (Ctrl+S)")
        self.btn_export_csv.clicked.connect(self.export_csv)
        self.btn_export_png = QtWidgets.QPushButton("Save Screenshot (Ctrl+P)")
        self.btn_export_png.clicked.connect(self.export_png)
        layout.addWidget(self.btn_export_csv)
        layout.addWidget(self.btn_export_png)
        export_group.setLayout(layout)
        return export_group

    def setup_shortcuts(self):
        QtWidgets.QShortcut(QtGui.QKeySequence("Space"), self, activated=self.toggle_run_stop)
        QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+S"), self, activated=self.export_csv)
        QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+P"), self, activated=self.export_png)
        QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+R"), self, activated=self.reset_view)
        QtWidgets.QShortcut(QtGui.QKeySequence("1"), self, activated=lambda: self.chk_ch1.toggle())
        QtWidgets.QShortcut(QtGui.QKeySequence("2"), self, activated=lambda: self.chk_ch2.toggle())

    # UI callbacks
    def toggle_run_stop(self):
        self.is_running = not self.is_running
        self._refresh_run_stop_button()

    def _refresh_run_stop_button(self):
        if self.is_running:
            self.btn_run_stop.setText("STOP / FREEZE  (Space)")
            self.btn_run_stop.setStyleSheet("background-color: #8B0000; color: white; font-weight: bold;")
        else:
            self.btn_run_stop.setText("RUN / LIVE  (Space)")
            self.btn_run_stop.setStyleSheet("background-color: #006400; color: white; font-weight: bold;")

    def on_acq_mode_change(self, mode):
        self.acq_mode = mode
        if mode == 'Single':
            self.single_armed = True
            self.is_running = True
            self._refresh_run_stop_button()

    def on_refresh_change(self, ms):
        self.timer.setInterval(ms)

    def reset_view(self):
        self.time_plot.enableAutoRange()
        self.fft_plot.enableAutoRange()

    def on_trig_source_change(self, src):
        self.trigger_channel = src

    def on_trig_mode_change(self, mode):
        self.trigger_mode = mode

    def on_trig_level_change(self, val):
        self.trigger_level = val
        self.trig_line.setValue(val)

    def on_trig_line_dragged(self, line):
        val = line.value()
        self.trigger_level = val
        self.spin_trig_level.blockSignals(True)
        self.spin_trig_level.setValue(val)
        self.spin_trig_level.blockSignals(False)

    def on_channel_toggle(self, channel, curve, state):
        channel.enabled = bool(state)
        curve.setVisible(channel.enabled)

    def on_channel_color(self, channel, time_curve, fft_curve, index, combo):
        hexval = combo.itemData(index)
        channel.color = hexval
        time_curve.setPen(pg.mkPen(hexval, width=2))
        fft_curve.setPen(pg.mkPen(hexval, width=1.5))

    def on_persistence_toggle(self, state):
        self.persistence_enabled = bool(state)
        if not self.persistence_enabled:
            self.persistence_buf.clear()
            for c in self.persistence_curves:
                c.setData([], [])

    def on_persistence_depth_change(self, val):
        self.persistence_depth = val
        while len(self.persistence_buf) > val:
            self.persistence_buf.pop(0)

    def update_cursor_delta(self):
        dv = abs(self.cursor_v1.value() - self.cursor_v2.value())
        self.lbl_cursor_dv.setText(f"Cursor \u0394V: {dv:.3f} V")

    # Export
    def export_csv(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Data CSV", "", "CSV Files (*.csv)")
        if path:
            t = np.arange(SAMPLES_PER_CHAN) / SAMPLE_RATE_PER_CHAN
            data = np.column_stack((t, self.ch1.last_data, self.ch2.last_data))
            np.savetxt(path, data, delimiter=",", header="Time_s,CH1_V,CH2_V", comments="")
            print(f"Exported dataset to {path}")

    def export_png(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Screenshot", "", "PNG Files (*.png)")
        if path:
            exporter = pg.exporters.ImageExporter(self.centralWidget().grab())
            exporter.export(path)
            print(f"Saved screenshot to {path}")

    # DSP helpers
    def compute_fft(self, signal):
        ac_signal = (signal - np.mean(signal)) * self.hann_window
        fft_vals = np.abs(np.fft.rfft(ac_signal)) / (SAMPLES_PER_CHAN / 2)
        peak_idx = np.argmax(fft_vals[1:]) + 1
        dom_freq = self.fft_freqs[peak_idx]
        return fft_vals, dom_freq

    def compute_measurements(self, signal, dom_freq):
        vpp = float(np.ptp(signal))
        rms = float(np.sqrt(np.mean(np.square(signal))))

        mid = (float(np.max(signal)) + float(np.min(signal))) / 2.0
        duty = float(np.mean(signal > mid)) * 100.0

        rise_us = None
        lo = mid - 0.4 * (vpp if vpp > 1e-6 else 1.0)
        hi = mid + 0.4 * (vpp if vpp > 1e-6 else 1.0)
        rising_idx = np.where((signal[:-1] < lo) & (signal[1:] >= lo))[0]
        for idx in rising_idx:
            hi_candidates = np.where(signal[idx:] >= hi)[0]
            if len(hi_candidates) > 0:
                delta_samples = hi_candidates[0]
                rise_us = (delta_samples / SAMPLE_RATE_PER_CHAN) * 1e6
                break

        return vpp, rms, duty, rise_us

    def format_freq(self, freq_hz):
        return f"{freq_hz/1e3:.2f} kHz" if freq_hz >= 1000 else f"{freq_hz:.1f} Hz"

    # Status bar
    def update_status_bar(self):
        if self.connected:
            self.status_conn.setText(f"\u25CF Connected: {SERIAL_PORT}")
            self.status_conn.setStyleSheet("color: #39FF14;")
        else:
            self.status_conn.setText(f"\u25CF Disconnected (retrying {SERIAL_PORT}...)")
            self.status_conn.setStyleSheet("color: #FF4040;")
        self.status_fps.setText(f"{self.fps:.1f} fps")

    # Persistence drawing
    def draw_persistence(self, t_axis):
        depth = min(self.persistence_depth, len(self.persistence_buf))
        n_curves = len(self.persistence_curves)
        # clear all first
        for c in self.persistence_curves:
            c.setData([], [])
        slot = 0
        for i in range(depth):
            frame_idx = len(self.persistence_buf) - depth + i
            old_ch1, old_ch2 = self.persistence_buf[frame_idx]
            alpha = int(180 * (i + 1) / depth)
            if self.ch1.enabled and slot < n_curves:
                r, g, b, _ = pg.mkColor(self.ch1.color).getRgb()
                self.persistence_curves[slot].setPen(pg.mkPen((r, g, b, alpha), width=1))
                self.persistence_curves[slot].setData(t_axis[:len(old_ch1)], old_ch1)
                slot += 1
            if self.ch2.enabled and slot < n_curves:
                r, g, b, _ = pg.mkColor(self.ch2.color).getRgb()
                self.persistence_curves[slot].setPen(pg.mkPen((r, g, b, alpha), width=1))
                self.persistence_curves[slot].setData(t_axis[:len(old_ch2)], old_ch2)
                slot += 1

    # Main acquisition loop
    def update_scope(self):
        now = time.time()

        if not self.connected:
            if now - self.last_reconnect_attempt > RECONNECT_INTERVAL_S:
                self.last_reconnect_attempt = now
                self.try_connect()
                self.update_status_bar()
            return

        if not self.is_running:
            return

        try:
            if self.ser.in_waiting > 0:
                self.ser.reset_input_buffer()

            self.ser.write(b's')
            self.ser.flush()

            raw_bytes = self.ser.read(FRAME_BYTES)

            if len(raw_bytes) != FRAME_BYTES:
                return  # partial/timeout frame; just try again next tick

            counts = np.frombuffer(raw_bytes, dtype=np.uint16) & 0x0FFF
            ch1_raw = counts[0::2] * (3.3 / 4095.0)
            ch2_raw = counts[1::2] * (3.3 / 4095.0)

            self.ch1.last_data = ch1_raw
            self.ch2.last_data = ch2_raw

        except (serial.SerialException, OSError) as e:
            print(f"Serial error, disconnecting: {e}")
            self.connected = False
            try:
                self.ser.close()
            except Exception:
                pass
            self.update_status_bar()
            return
        except Exception as e:
            print(f"Error reading scope frame: {e}")
            return

        # Measurements
        ch1_fft, ch1_freq = self.compute_fft(ch1_raw)
        ch2_fft, ch2_freq = self.compute_fft(ch2_raw)

        if self.ch1.enabled:
            self.ch1_fft_curve.setData(self.fft_freqs, ch1_fft)
            vpp, rms, duty, rise_us = self.compute_measurements(ch1_raw, ch1_freq)
            self.lbl_ch1_vpp.setText(f"CH1 Vpp: {vpp:.2f} V")
            self.lbl_ch1_rms.setText(f"CH1 RMS: {rms:.2f} V")
            self.lbl_ch1_freq.setText(f"CH1 Freq: {self.format_freq(ch1_freq)}")
            self.lbl_ch1_duty.setText(f"CH1 Duty: {duty:.1f} %")
            self.lbl_ch1_rise.setText(f"CH1 Rise: {rise_us:.2f} \u03bcs" if rise_us else "CH1 Rise: -- \u03bcs")
        else:
            self.ch1_fft_curve.setData([], [])

        if self.ch2.enabled:
            self.ch2_fft_curve.setData(self.fft_freqs, ch2_fft)
            vpp, rms, duty, rise_us = self.compute_measurements(ch2_raw, ch2_freq)
            self.lbl_ch2_vpp.setText(f"CH2 Vpp: {vpp:.2f} V")
            self.lbl_ch2_rms.setText(f"CH2 RMS: {rms:.2f} V")
            self.lbl_ch2_freq.setText(f"CH2 Freq: {self.format_freq(ch2_freq)}")
            self.lbl_ch2_duty.setText(f"CH2 Duty: {duty:.1f} %")
            self.lbl_ch2_rise.setText(f"CH2 Rise: {rise_us:.2f} \u03bcs" if rise_us else "CH2 Rise: -- \u03bcs")
        else:
            self.ch2_fft_curve.setData([], [])

        # Trigger search (software edge triggering)
        trig_sig = ch1_raw if self.trigger_channel == 'CH1' else ch2_raw
        pretrig_samples = int(self.timebase_window * self.pretrigger_pct / 100.0)

        triggered = False
        if self.trigger_mode == 'disabled':
            disp_ch1 = ch1_raw[:self.timebase_window]
            disp_ch2 = ch2_raw[:self.timebase_window]
            triggered = True
        else:
            if self.trigger_mode == 'rising':
                indices = np.where((trig_sig[:-1] < self.trigger_level) & (trig_sig[1:] >= self.trigger_level))[0]
            else:
                indices = np.where((trig_sig[:-1] > self.trigger_level) & (trig_sig[1:] <= self.trigger_level))[0]

            holdoff_ok = (now - self.last_trigger_time) * 1000.0 >= self.trigger_holdoff_ms

            if len(indices) > 0 and holdoff_ok:
                idx = max(0, indices[0] - pretrig_samples)
                disp_ch1 = ch1_raw[idx:idx + self.timebase_window]
                disp_ch2 = ch2_raw[idx:idx + self.timebase_window]
                triggered = True
                self.last_trigger_time = now
            elif self.acq_mode == 'Auto':
                # Free-run fallback: show something even without a trigger match
                disp_ch1 = ch1_raw[:self.timebase_window]
                disp_ch2 = ch2_raw[:self.timebase_window]
                triggered = True
            else:
                # Normal/Single with no trigger match: hold last good frame
                disp_ch1, disp_ch2 = self.last_valid_disp

        if triggered:
            self.last_valid_disp = (disp_ch1, disp_ch2)

        # Single-shot: stop acquisition after the first real trigger match
        if self.acq_mode == 'Single' and self.single_armed and self.trigger_mode != 'disabled':
            if len(indices) > 0:
                self.single_armed = False
                self.is_running = False
                self._refresh_run_stop_button()

        t_axis = np.arange(len(disp_ch1)) / SAMPLE_RATE_PER_CHAN

        disp_ch1_scaled = disp_ch1 * self.ch1.scale + self.ch1.offset
        disp_ch2_scaled = disp_ch2 * self.ch2.scale + self.ch2.offset

        if self.persistence_enabled:
            self.persistence_buf.append((disp_ch1_scaled, disp_ch2_scaled))
            if len(self.persistence_buf) > self.persistence_depth:
                self.persistence_buf.pop(0)
            self.draw_persistence(t_axis)

        if self.ch1.enabled:
            self.ch1_curve.setData(t_axis, disp_ch1_scaled)
        else:
            self.ch1_curve.setData([], [])
        if self.ch2.enabled:
            self.ch2_curve.setData(t_axis, disp_ch2_scaled)
        else:
            self.ch2_curve.setData([], [])

        # fps bookkeeping
        self.frame_count += 1
        if now - self.last_fps_time >= 1.0:
            self.fps = self.frame_count / (now - self.last_fps_time)
            self.frame_count = 0
            self.last_fps_time = now
            self.update_status_bar()

    def closeEvent(self, event):
        if self.ser is not None and self.ser.is_open:
            self.ser.close()


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    window = DualChannelOscilloscope()
    window.show()
    sys.exit(app.exec_())
