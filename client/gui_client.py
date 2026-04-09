"""
GUI client for the NI Instrument Test gRPC server.

Tabs
----
1. Connection   – gRPC server address + connect / disconnect
2. SMU          – NI-DCPower resource, channel, voltage, current limit
3. Digital      – NI-Digital resource, file browser for pin-map / pattern /
                  levels / timing
4. HRAM         – History RAM trigger type, sample count, cycles filter
5. Run & Log    – TDMS output path, Run / Abort buttons, live log
6. Results      – Site pass/fail table + HRAM failure detail table

Usage
-----
    python gui_client.py
"""

import os
import sys
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from datetime import datetime

import grpc

# ---------------------------------------------------------------------------
# Add generated stubs to path
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_GENERATED = os.path.join(_HERE, "..", "generated")
sys.path.insert(0, _GENERATED)

try:
    import instrument_test_pb2 as pb2
    import instrument_test_pb2_grpc as pb2_grpc

    _STUBS_OK = True
except ModuleNotFoundError:
    _STUBS_OK = False


# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------
BG = "#1e1e2e"
FG = "#cdd6f4"
ACCENT = "#89b4fa"
PASS_COLOR = "#a6e3a1"
FAIL_COLOR = "#f38ba8"
ENTRY_BG = "#313244"
FRAME_BG = "#181825"
BTN_BG = "#313244"
BTN_FG = "#cdd6f4"
BTN_ACTIVE = "#45475a"
LABEL_FG = "#a6adc8"


# ---------------------------------------------------------------------------
# Helper widgets
# ---------------------------------------------------------------------------
def _label(parent, text, **kw):
    return tk.Label(parent, text=text, bg=FRAME_BG, fg=LABEL_FG,
                    font=("Segoe UI", 9), **kw)


def _entry(parent, textvariable=None, width=30, **kw):
    return tk.Entry(parent, textvariable=textvariable, width=width,
                    bg=ENTRY_BG, fg=FG, insertbackground=FG,
                    relief="flat", font=("Segoe UI", 9), **kw)


def _button(parent, text, command, width=12, **kw):
    btn = tk.Button(parent, text=text, command=command, width=width,
                    bg=BTN_BG, fg=BTN_FG, activebackground=BTN_ACTIVE,
                    activeforeground=FG, relief="flat",
                    font=("Segoe UI", 9, "bold"), cursor="hand2", **kw)
    btn.bind("<Enter>", lambda e: btn.config(bg=BTN_ACTIVE))
    btn.bind("<Leave>", lambda e: btn.config(bg=BTN_BG))
    return btn


def _combo(parent, textvariable, values, width=28, **kw):
    return ttk.Combobox(parent, textvariable=textvariable, values=values,
                        width=width, state="readonly", font=("Segoe UI", 9),
                        **kw)


def _sep(parent):
    return ttk.Separator(parent, orient="horizontal")


def _file_row(parent, row, label_text, var, filetypes, pady=2):
    """Row with label, entry, and Browse button for a file path."""
    _label(parent, label_text).grid(row=row, column=0, sticky="e", padx=6, pady=pady)
    e = _entry(parent, textvariable=var, width=42)
    e.grid(row=row, column=1, sticky="ew", padx=4, pady=pady)

    def browse():
        path = filedialog.askopenfilename(filetypes=filetypes)
        if path:
            var.set(path)

    _button(parent, "Browse…", browse, width=8).grid(
        row=row, column=2, sticky="w", padx=4, pady=pady)
    return e


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("NI SMU + Digital Pattern Test  |  gRPC Client")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(860, 640)

        # gRPC channel/stub
        self._channel: grpc.Channel | None = None
        self._stub = None
        self._connected = False

        # Log queue for thread-safe GUI updates
        self._log_queue: queue.Queue = queue.Queue()

        # ---- Declare ALL tk.Var objects first --------------------------------
        self._setup_vars()

        # ---- Build UI --------------------------------------------------------
        self._build_ui()

        # Start log consumer
        self._poll_log()

        if not _STUBS_OK:
            self._log(
                "[WARNING] gRPC stubs not found in 'generated/'. "
                "Run generate_stubs.bat before connecting.",
                color=FAIL_COLOR,
            )

    # =======================================================================
    # Variables
    # =======================================================================
    def _setup_vars(self):
        # Connection
        self.v_host = tk.StringVar(value="localhost")
        self.v_port = tk.StringVar(value="50051")

        # SMU
        self.v_smu_resource = tk.StringVar(value="PXI1Slot2")
        self.v_smu_channel = tk.StringVar(value="0")
        self.v_smu_voltage = tk.StringVar(value="3.3")
        self.v_smu_current_limit = tk.StringVar(value="0.1")
        self.v_smu_voltage_range = tk.StringVar(value="0")
        self.v_smu_current_range = tk.StringVar(value="0")
        self.v_smu_sense = tk.StringVar(value="LOCAL")
        self.v_smu_source_delay = tk.StringVar(value="0.0")
        self.v_smu_output_fn = tk.StringVar(value="DC_VOLTAGE")
        self.v_smu_simulate = tk.BooleanVar(value=False)

        # Digital
        self.v_dig_resource = tk.StringVar(value="PXI1Slot3")
        self.v_dig_pinmap = tk.StringVar()
        self.v_dig_pattern = tk.StringVar()
        self.v_dig_levels = tk.StringVar()
        self.v_dig_timing = tk.StringVar()
        self.v_dig_start_label = tk.StringVar(value="new_pattern")
        self.v_dig_sites = tk.StringVar(value="0")   # comma-separated
        self.v_dig_simulate = tk.BooleanVar(value=False)

        # HRAM
        self.v_hram_trigger = tk.StringVar(value="FIRST_FAILURE")
        self.v_hram_max_samples = tk.StringVar(value="8192")
        self.v_hram_cycles = tk.StringVar(value="FAILED")
        self.v_hram_pretrigger = tk.StringVar(value="0")
        self.v_hram_finite = tk.BooleanVar(value=True)

        # Run
        self.v_tdms_path = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "Desktop",
                               "test_results.tdms"))
        self.v_run_start_label = tk.StringVar()   # overrides dig config
        self.v_timeout = tk.StringVar(value="10")

        # Status bar
        self.v_status = tk.StringVar(value="Disconnected")

    # =======================================================================
    # Build UI
    # =======================================================================
    def _build_ui(self):
        # ---- Top bar: connection status ------------------------------------
        top = tk.Frame(self, bg=BG, pady=4)
        top.pack(fill="x", padx=8)
        tk.Label(top, text="NI Instrument Test", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 14, "bold")).pack(side="left")
        tk.Label(top, textvariable=self.v_status, bg=BG, fg=LABEL_FG,
                 font=("Segoe UI", 9, "italic")).pack(side="right", padx=8)

        # ---- Notebook -------------------------------------------------------
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=ENTRY_BG, foreground=FG,
                        padding=[10, 4], font=("Segoe UI", 9))
        style.map("TNotebook.Tab",
                  background=[("selected", FRAME_BG)],
                  foreground=[("selected", ACCENT)])
        style.configure("Treeview", background=ENTRY_BG, foreground=FG,
                        rowheight=22, fieldbackground=ENTRY_BG,
                        font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background=BTN_BG, foreground=ACCENT,
                        font=("Segoe UI", 9, "bold"))

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=4)

        self._tab_conn = self._make_frame()
        self._tab_smu = self._make_frame()
        self._tab_dig = self._make_frame()
        self._tab_hram = self._make_frame()
        self._tab_run = self._make_frame()
        self._tab_results = self._make_frame()

        nb.add(self._tab_conn, text=" Connection ")
        nb.add(self._tab_smu, text=" SMU ")
        nb.add(self._tab_dig, text=" Digital ")
        nb.add(self._tab_hram, text=" HRAM ")
        nb.add(self._tab_run, text=" Run & Log ")
        nb.add(self._tab_results, text=" Results ")

        self._build_connection_tab()
        self._build_smu_tab()
        self._build_digital_tab()
        self._build_hram_tab()
        self._build_run_tab()
        self._build_results_tab()

    def _make_frame(self):
        f = tk.Frame(self, bg=FRAME_BG)
        f.columnconfigure(1, weight=1)
        return f

    # -----------------------------------------------------------------------
    # Tab: Connection
    # -----------------------------------------------------------------------
    def _build_connection_tab(self):
        p = self._tab_conn
        p.columnconfigure(1, weight=1)

        tk.Label(p, text="gRPC Server", bg=FRAME_BG, fg=ACCENT,
                 font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=12, pady=(14, 6))

        _label(p, "Host:").grid(row=1, column=0, sticky="e", padx=8, pady=4)
        _entry(p, self.v_host, width=30).grid(row=1, column=1, sticky="ew", padx=4, pady=4)

        _label(p, "Port:").grid(row=2, column=0, sticky="e", padx=8, pady=4)
        _entry(p, self.v_port, width=10).grid(row=2, column=1, sticky="w", padx=4, pady=4)

        btn_frame = tk.Frame(p, bg=FRAME_BG)
        btn_frame.grid(row=3, column=0, columnspan=3, pady=10, padx=12, sticky="w")
        self._btn_connect = _button(btn_frame, "Connect", self._connect, width=14)
        self._btn_connect.pack(side="left", padx=4)
        self._btn_disconnect = _button(btn_frame, "Disconnect", self._disconnect,
                                       width=14)
        self._btn_disconnect.pack(side="left", padx=4)
        self._btn_disconnect.config(state="disabled")

        _sep(p).grid(row=4, column=0, columnspan=3, sticky="ew", padx=8, pady=6)

        tk.Label(p, text="Quick Actions", bg=FRAME_BG, fg=ACCENT,
                 font=("Segoe UI", 10, "bold")).grid(
            row=5, column=0, columnspan=3, sticky="w", padx=12, pady=4)

        qa_frame = tk.Frame(p, bg=FRAME_BG)
        qa_frame.grid(row=6, column=0, columnspan=3, sticky="w", padx=12, pady=4)
        _button(qa_frame, "Get Status", self._get_status, width=14).pack(side="left", padx=4)
        _button(qa_frame, "Shutdown Server", self._shutdown_server, width=16).pack(
            side="left", padx=4)

        tk.Label(p, text="Apply All Settings", bg=FRAME_BG, fg=ACCENT,
                 font=("Segoe UI", 10, "bold")).grid(
            row=7, column=0, columnspan=3, sticky="w", padx=12, pady=(12, 4))

        seq_frame = tk.Frame(p, bg=FRAME_BG)
        seq_frame.grid(row=8, column=0, columnspan=3, sticky="w", padx=12)
        _button(seq_frame, "Send SMU Config", self._send_smu_config, width=18).pack(
            side="left", padx=4)
        _button(seq_frame, "Send Digital Config", self._send_digital_config, width=18).pack(
            side="left", padx=4)
        _button(seq_frame, "Send HRAM Config", self._send_hram_config, width=18).pack(
            side="left", padx=4)

    # -----------------------------------------------------------------------
    # Tab: SMU
    # -----------------------------------------------------------------------
    def _build_smu_tab(self):
        p = self._tab_smu
        p.columnconfigure(1, weight=1)

        tk.Label(p, text="NI-DCPower (SMU) Configuration", bg=FRAME_BG, fg=ACCENT,
                 font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=12, pady=(14, 6))

        rows = [
            ("Resource Name:", self.v_smu_resource),
            ("Channel:", self.v_smu_channel),
            ("Voltage Level (V):", self.v_smu_voltage),
            ("Current Limit (A):", self.v_smu_current_limit),
            ("Voltage Range (V, 0=auto):", self.v_smu_voltage_range),
            ("Current Range (A, 0=auto):", self.v_smu_current_range),
            ("Source Delay (s):", self.v_smu_source_delay),
        ]
        for i, (lbl, var) in enumerate(rows, start=1):
            _label(p, lbl).grid(row=i, column=0, sticky="e", padx=8, pady=3)
            _entry(p, var, width=24).grid(row=i, column=1, sticky="w", padx=4, pady=3)

        r = len(rows) + 1
        _label(p, "Sense:").grid(row=r, column=0, sticky="e", padx=8, pady=3)
        _combo(p, self.v_smu_sense, ["LOCAL", "REMOTE"], width=14).grid(
            row=r, column=1, sticky="w", padx=4, pady=3)

        r += 1
        _label(p, "Output Function:").grid(row=r, column=0, sticky="e", padx=8, pady=3)
        _combo(p, self.v_smu_output_fn, ["DC_VOLTAGE", "DC_CURRENT"], width=14).grid(
            row=r, column=1, sticky="w", padx=4, pady=3)

        r += 1
        ck = tk.Checkbutton(p, text="Simulate (no hardware)", variable=self.v_smu_simulate,
                             bg=FRAME_BG, fg=FG, selectcolor=ENTRY_BG,
                             activebackground=FRAME_BG, activeforeground=FG,
                             font=("Segoe UI", 9))
        ck.grid(row=r, column=1, sticky="w", padx=4, pady=3)

        r += 1
        _sep(p).grid(row=r, column=0, columnspan=3, sticky="ew", padx=8, pady=8)

        r += 1
        _button(p, "Send SMU Config", self._send_smu_config, width=20).grid(
            row=r, column=1, sticky="w", padx=4)

    # -----------------------------------------------------------------------
    # Tab: Digital
    # -----------------------------------------------------------------------
    def _build_digital_tab(self):
        p = self._tab_dig
        p.columnconfigure(1, weight=1)

        tk.Label(p, text="NI-Digital Pattern Instrument Configuration",
                 bg=FRAME_BG, fg=ACCENT, font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=12, pady=(14, 6))

        _label(p, "Resource Name:").grid(row=1, column=0, sticky="e", padx=8, pady=3)
        _entry(p, self.v_dig_resource, width=24).grid(
            row=1, column=1, sticky="w", padx=4, pady=3)

        _label(p, "Active Sites\n(e.g. 0,1,2):").grid(
            row=2, column=0, sticky="e", padx=8, pady=3)
        _entry(p, self.v_dig_sites, width=24).grid(
            row=2, column=1, sticky="w", padx=4, pady=3)

        _label(p, "Pattern Start Label:").grid(row=3, column=0, sticky="e", padx=8, pady=3)
        _entry(p, self.v_dig_start_label, width=24).grid(
            row=3, column=1, sticky="w", padx=4, pady=3)

        file_types_pinmap = [("Pin Map", "*.pinmap"), ("All files", "*.*")]
        file_types_pat = [("Digital Pattern", "*.digipat"), ("All files", "*.*")]
        file_types_lvl = [("Levels", "*.digilevels"), ("All files", "*.*")]
        file_types_tmg = [("Timing", "*.digitiming"), ("All files", "*.*")]

        _file_row(p, 4, "Pin Map File:", self.v_dig_pinmap, file_types_pinmap)
        _file_row(p, 5, "Pattern File:", self.v_dig_pattern, file_types_pat)
        _file_row(p, 6, "Levels File:", self.v_dig_levels, file_types_lvl)
        _file_row(p, 7, "Timing File:", self.v_dig_timing, file_types_tmg)

        ck = tk.Checkbutton(p, text="Simulate (no hardware)",
                            variable=self.v_dig_simulate,
                            bg=FRAME_BG, fg=FG, selectcolor=ENTRY_BG,
                            activebackground=FRAME_BG, activeforeground=FG,
                            font=("Segoe UI", 9))
        ck.grid(row=8, column=1, sticky="w", padx=4, pady=3)

        _sep(p).grid(row=9, column=0, columnspan=3, sticky="ew", padx=8, pady=8)

        _button(p, "Send Digital Config", self._send_digital_config, width=22).grid(
            row=10, column=1, sticky="w", padx=4)

    # -----------------------------------------------------------------------
    # Tab: HRAM
    # -----------------------------------------------------------------------
    def _build_hram_tab(self):
        p = self._tab_hram
        p.columnconfigure(1, weight=1)

        tk.Label(p, text="History RAM (HRAM) Configuration", bg=FRAME_BG, fg=ACCENT,
                 font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=12, pady=(14, 6))

        _label(p, "Trigger Type:").grid(row=1, column=0, sticky="e", padx=8, pady=4)
        _combo(p, self.v_hram_trigger,
               ["FIRST_FAILURE", "CYCLE_NUMBER", "PATTERN_LABEL"], width=20).grid(
            row=1, column=1, sticky="w", padx=4, pady=4)

        _label(p, "Max Samples / Site:").grid(row=2, column=0, sticky="e", padx=8, pady=4)
        _entry(p, self.v_hram_max_samples, width=12).grid(
            row=2, column=1, sticky="w", padx=4, pady=4)

        _label(p, "Cycles to Acquire:").grid(row=3, column=0, sticky="e", padx=8, pady=4)
        _combo(p, self.v_hram_cycles, ["FAILED", "ALL"], width=12).grid(
            row=3, column=1, sticky="w", padx=4, pady=4)

        _label(p, "Pre-trigger Samples:").grid(row=4, column=0, sticky="e", padx=8, pady=4)
        _entry(p, self.v_hram_pretrigger, width=10).grid(
            row=4, column=1, sticky="w", padx=4, pady=4)

        ck = tk.Checkbutton(p, text="Finite number of samples",
                            variable=self.v_hram_finite,
                            bg=FRAME_BG, fg=FG, selectcolor=ENTRY_BG,
                            activebackground=FRAME_BG, activeforeground=FG,
                            font=("Segoe UI", 9))
        ck.grid(row=5, column=1, sticky="w", padx=4, pady=4)

        _sep(p).grid(row=6, column=0, columnspan=3, sticky="ew", padx=8, pady=8)

        _button(p, "Send HRAM Config", self._send_hram_config, width=20).grid(
            row=7, column=1, sticky="w", padx=4)

    # -----------------------------------------------------------------------
    # Tab: Run & Log
    # -----------------------------------------------------------------------
    def _build_run_tab(self):
        p = self._tab_run
        p.columnconfigure(1, weight=1)
        p.rowconfigure(4, weight=1)

        tk.Label(p, text="Test Execution", bg=FRAME_BG, fg=ACCENT,
                 font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=12, pady=(14, 6))

        # TDMS output path
        _file_row(p, 1, "TDMS Output File:",
                  self.v_tdms_path,
                  [("TDMS", "*.tdms"), ("All", "*.*")])

        _label(p, "Override Start Label\n(leave blank to use Digital tab):").grid(
            row=2, column=0, sticky="e", padx=8, pady=4)
        _entry(p, self.v_run_start_label, width=24).grid(
            row=2, column=1, sticky="w", padx=4, pady=4)

        _label(p, "Timeout (s):").grid(row=3, column=0, sticky="e", padx=8, pady=4)
        _entry(p, self.v_timeout, width=10).grid(
            row=3, column=1, sticky="w", padx=4, pady=4)

        # Buttons
        btn_frame = tk.Frame(p, bg=FRAME_BG)
        btn_frame.grid(row=4, column=0, columnspan=3, sticky="w", padx=12, pady=8)

        self._btn_run = _button(btn_frame, "▶ Run Test", self._run_test, width=16)
        self._btn_run.config(bg="#1e6e35", activebackground="#28a745")
        self._btn_run.pack(side="left", padx=4)

        self._btn_abort = _button(btn_frame, "⏹ Abort", self._abort_test, width=12)
        self._btn_abort.config(bg="#6e1e1e", activebackground="#dc3545")
        self._btn_abort.pack(side="left", padx=4)

        _button(btn_frame, "Clear Log", self._clear_log, width=12).pack(
            side="left", padx=4)

        # Log area
        _sep(p).grid(row=5, column=0, columnspan=3, sticky="ew", padx=8, pady=4)
        tk.Label(p, text="Log", bg=FRAME_BG, fg=ACCENT,
                 font=("Segoe UI", 9, "bold")).grid(
            row=6, column=0, columnspan=3, sticky="w", padx=12)

        self._log_text = scrolledtext.ScrolledText(
            p, bg="#0d0d14", fg=FG, insertbackground=FG,
            font=("Consolas", 9), relief="flat", state="disabled", wrap="word")
        self._log_text.grid(row=7, column=0, columnspan=3, sticky="nsew",
                            padx=8, pady=(4, 8))
        self._log_text.tag_config("error", foreground=FAIL_COLOR)
        self._log_text.tag_config("pass", foreground=PASS_COLOR)
        self._log_text.tag_config("warn", foreground="#fab387")
        self._log_text.tag_config("info", foreground=FG)
        self._log_text.tag_config("accent", foreground=ACCENT)
        p.rowconfigure(7, weight=1)

    # -----------------------------------------------------------------------
    # Tab: Results
    # -----------------------------------------------------------------------
    def _build_results_tab(self):
        p = self._tab_results
        p.columnconfigure(0, weight=1)
        p.rowconfigure(3, weight=1)

        tk.Label(p, text="Test Results", bg=FRAME_BG, fg=ACCENT,
                 font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=(14, 6))

        # ---- Overall result banner -----------------------------------------
        self._result_banner = tk.Label(p, text="No result yet",
                                       bg=ENTRY_BG, fg=FG,
                                       font=("Segoe UI", 14, "bold"),
                                       relief="flat", pady=8)
        self._result_banner.grid(row=1, column=0, sticky="ew", padx=8, pady=4)

        # ---- Site pass/fail ------------------------------------------------
        spf_frame = tk.LabelFrame(p, text="Site Pass / Fail", bg=FRAME_BG, fg=ACCENT,
                                  font=("Segoe UI", 9, "bold"))
        spf_frame.grid(row=2, column=0, sticky="ew", padx=8, pady=4)

        self._site_tree = ttk.Treeview(spf_frame, columns=("site", "result"),
                                       show="headings", height=4)
        self._site_tree.heading("site", text="Site")
        self._site_tree.heading("result", text="Result")
        self._site_tree.column("site", width=80, anchor="center")
        self._site_tree.column("result", width=120, anchor="center")
        self._site_tree.pack(fill="x", padx=4, pady=4)

        # ---- Failure detail -------------------------------------------------
        fail_frame = tk.LabelFrame(p, text="HRAM Failure Detail", bg=FRAME_BG,
                                   fg=ACCENT, font=("Segoe UI", 9, "bold"))
        fail_frame.grid(row=3, column=0, sticky="nsew", padx=8, pady=4)
        fail_frame.columnconfigure(0, weight=1)
        fail_frame.rowconfigure(0, weight=1)

        fail_cols = ("site", "cycle", "vector", "pattern", "timeset",
                     "pin", "expected", "actual", "pass")
        self._fail_tree = ttk.Treeview(fail_frame, columns=fail_cols,
                                       show="headings")
        col_cfg = {
            "site":     ("Site",     60),
            "cycle":    ("Cycle",    80),
            "vector":   ("Vector",   80),
            "pattern":  ("Pattern",  120),
            "timeset":  ("Time Set", 100),
            "pin":      ("Pin",      100),
            "expected": ("Expected", 80),
            "actual":   ("Actual",   80),
            "pass":     ("Pass?",    60),
        }
        for col, (hdr, w) in col_cfg.items():
            self._fail_tree.heading(col, text=hdr)
            self._fail_tree.column(col, width=w, anchor="center")

        scroll_y = ttk.Scrollbar(fail_frame, orient="vertical",
                                 command=self._fail_tree.yview)
        scroll_x = ttk.Scrollbar(fail_frame, orient="horizontal",
                                 command=self._fail_tree.xview)
        self._fail_tree.configure(yscrollcommand=scroll_y.set,
                                  xscrollcommand=scroll_x.set)
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")
        self._fail_tree.grid(row=0, column=0, sticky="nsew")

        _button(p, "Export CSV", self._export_csv, width=14).grid(
            row=4, column=0, sticky="e", padx=8, pady=6)

    # =======================================================================
    # gRPC Connection
    # =======================================================================
    def _connect(self):
        if not _STUBS_OK:
            messagebox.showerror(
                "Missing stubs",
                "gRPC stubs not found in 'generated/'.\n"
                "Run generate_stubs.bat first.")
            return
        addr = f"{self.v_host.get()}:{self.v_port.get()}"
        try:
            self._channel = grpc.insecure_channel(addr)
            self._stub = pb2_grpc.InstrumentTestServiceStub(self._channel)
            # Quick connectivity probe
            grpc.channel_ready_future(self._channel).result(timeout=5)
            self._connected = True
            self.v_status.set(f"Connected  →  {addr}")
            self._btn_connect.config(state="disabled")
            self._btn_disconnect.config(state="normal")
            self._log(f"[Connected] {addr}", color=PASS_COLOR)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[Connection failed] {exc}", color=FAIL_COLOR)
            self._channel = None
            self._stub = None

    def _disconnect(self):
        if self._channel:
            self._channel.close()
            self._channel = None
            self._stub = None
        self._connected = False
        self.v_status.set("Disconnected")
        self._btn_connect.config(state="normal")
        self._btn_disconnect.config(state="disabled")
        self._log("[Disconnected]", color="warn")

    def _require_connection(self) -> bool:
        if not self._connected or self._stub is None:
            messagebox.showwarning("Not Connected", "Connect to the gRPC server first.")
            return False
        return True

    # =======================================================================
    # Config senders
    # =======================================================================
    def _send_smu_config(self):
        if not self._require_connection():
            return
        try:
            cfg = pb2.SMUConfig(
                resource_name=self.v_smu_resource.get(),
                channel=self.v_smu_channel.get(),
                voltage_level=float(self.v_smu_voltage.get()),
                current_limit=float(self.v_smu_current_limit.get()),
                voltage_level_range=float(self.v_smu_voltage_range.get()),
                current_limit_range=float(self.v_smu_current_range.get()),
                sense=self.v_smu_sense.get(),
                source_delay=float(self.v_smu_source_delay.get()),
                output_function=self.v_smu_output_fn.get(),
                simulate=self.v_smu_simulate.get(),
            )
            resp = self._stub.ConfigureSMU(cfg)
            self._handle_status(resp, "SMU")
        except Exception as exc:  # noqa: BLE001
            self._log(f"[SMU config error] {exc}", color=FAIL_COLOR)

    def _send_digital_config(self):
        if not self._require_connection():
            return
        try:
            sites_raw = self.v_dig_sites.get().strip()
            sites = []
            if sites_raw:
                for s in sites_raw.split(","):
                    s = s.strip()
                    if s.isdigit():
                        sites.append(int(s))

            cfg = pb2.DigitalConfig(
                resource_name=self.v_dig_resource.get(),
                pin_map_file=self.v_dig_pinmap.get(),
                pattern_file=self.v_dig_pattern.get(),
                levels_file=self.v_dig_levels.get(),
                timing_file=self.v_dig_timing.get(),
                start_label=self.v_dig_start_label.get(),
                sites=sites,
                simulate=self.v_dig_simulate.get(),
            )
            resp = self._stub.ConfigureDigital(cfg)
            self._handle_status(resp, "Digital")
        except Exception as exc:  # noqa: BLE001
            self._log(f"[Digital config error] {exc}", color=FAIL_COLOR)

    def _send_hram_config(self):
        if not self._require_connection():
            return
        try:
            cfg = pb2.HRAMConfig(
                trigger_type=self.v_hram_trigger.get(),
                max_samples_per_site=int(self.v_hram_max_samples.get()),
                cycles_to_acquire=self.v_hram_cycles.get(),
                pretrigger_samples=int(self.v_hram_pretrigger.get()),
                number_of_samples_finite=self.v_hram_finite.get(),
            )
            resp = self._stub.ConfigureHRAM(cfg)
            self._handle_status(resp, "HRAM")
        except Exception as exc:  # noqa: BLE001
            self._log(f"[HRAM config error] {exc}", color=FAIL_COLOR)

    # =======================================================================
    # Test execution
    # =======================================================================
    def _run_test(self):
        if not self._require_connection():
            return
        self._log("─" * 60, color="accent")
        self._log(f"[Run] {datetime.now().strftime('%H:%M:%S')}  starting test…",
                  color="accent")
        threading.Thread(target=self._run_test_thread, daemon=True).start()

    def _run_test_thread(self):
        try:
            req = pb2.RunTestRequest(
                tdms_log_file=self.v_tdms_path.get(),
                start_label=self.v_run_start_label.get(),
                timeout=float(self.v_timeout.get()),
            )
            resp: pb2.RunTestResponse = self._stub.RunTest(req)

            if not resp.success:
                self._log(f"[RunTest ERROR] {resp.message}", color=FAIL_COLOR)
                return

            result_str = "PASS" if resp.test_passed else "FAIL"
            color = PASS_COLOR if resp.test_passed else FAIL_COLOR
            self._log(f"[Result] {result_str} — {resp.message}", color=color)
            self._log(f"         Total failures: {resp.total_failures}")
            if resp.tdms_file_path:
                self._log(f"         TDMS: {resp.tdms_file_path}", color="accent")

            self._update_results_tab(resp)

        except Exception as exc:  # noqa: BLE001
            self._log(f"[RunTest exception] {exc}", color=FAIL_COLOR)

    def _abort_test(self):
        if not self._require_connection():
            return
        try:
            resp = self._stub.AbortTest(pb2.Empty())
            self._log(f"[Abort] {resp.message}", color="warn")
        except Exception as exc:  # noqa: BLE001
            self._log(f"[Abort error] {exc}", color=FAIL_COLOR)

    # =======================================================================
    # Results tab update
    # =======================================================================
    def _update_results_tab(self, resp: "pb2.RunTestResponse"):
        def _update():
            # Banner
            if resp.test_passed:
                self._result_banner.config(
                    text="⬛  PASS", bg="#1a3d26", fg=PASS_COLOR)
            else:
                self._result_banner.config(
                    text=f"⬛  FAIL   ({resp.total_failures} failures)",
                    bg="#3d1a1a", fg=FAIL_COLOR)

            # Site tree
            for item in self._site_tree.get_children():
                self._site_tree.delete(item)
            for site_num, passed in sorted(resp.site_pass_fail.items()):
                tag = "pass" if passed else "fail"
                self._site_tree.insert("", "end", values=(
                    f"site{site_num}", "PASS" if passed else "FAIL"), tags=(tag,))
            self._site_tree.tag_configure("pass", foreground=PASS_COLOR)
            self._site_tree.tag_configure("fail", foreground=FAIL_COLOR)

            # Failure tree
            for item in self._fail_tree.get_children():
                self._fail_tree.delete(item)

            for failure in resp.failures:
                site = failure.site_number
                cycle = failure.cycle_number
                vector = failure.vector_number
                pattern = failure.pattern_name
                timeset = failure.time_set_name

                # Expand per-pin rows
                pin_count = max(len(failure.pin_names),
                                len(failure.expected_states),
                                len(failure.actual_states),
                                len(failure.per_pin_pass_fail), 1)
                for i in range(pin_count):
                    pin = failure.pin_names[i] if i < len(failure.pin_names) else ""
                    exp = failure.expected_states[i] if i < len(failure.expected_states) else ""
                    act = failure.actual_states[i] if i < len(failure.actual_states) else ""
                    ppf = failure.per_pin_pass_fail[i] if i < len(failure.per_pin_pass_fail) else None
                    pass_str = "PASS" if ppf else "FAIL" if ppf is not None else ""
                    tag = "pass" if ppf else "fail"
                    self._fail_tree.insert("", "end", values=(
                        site, cycle, vector, pattern, timeset, pin, exp, act, pass_str
                    ), tags=(tag,))

            self._fail_tree.tag_configure("pass", foreground=PASS_COLOR)
            self._fail_tree.tag_configure("fail", foreground=FAIL_COLOR)

        self.after(0, _update)

    # =======================================================================
    # Misc actions
    # =======================================================================
    def _get_status(self):
        if not self._require_connection():
            return
        try:
            status = self._stub.GetStatus(pb2.Empty())
            self._log(
                f"[Status] SMU init={status.smu_initialized} "
                f"({status.smu_resource}  {status.configured_voltage:.4f} V) | "
                f"Digital init={status.digital_initialized} "
                f"({status.digital_resource}) | "
                f"HRAM trigger={status.hram_trigger_type}",
                color="accent",
            )
        except Exception as exc:  # noqa: BLE001
            self._log(f"[GetStatus error] {exc}", color=FAIL_COLOR)

    def _shutdown_server(self):
        if not self._require_connection():
            return
        if not messagebox.askyesno("Shutdown",
                                   "Send Shutdown to the server?\n"
                                   "This will close all instrument sessions."):
            return
        try:
            resp = self._stub.Shutdown(pb2.Empty())
            self._handle_status(resp, "Shutdown")
        except Exception as exc:  # noqa: BLE001
            self._log(f"[Shutdown error] {exc}", color=FAIL_COLOR)

    def _handle_status(self, resp: "pb2.StatusResponse", label=""):
        if resp.success:
            self._log(f"[{label}] {resp.message}", color=PASS_COLOR)
        else:
            self._log(f"[{label} ERROR] {resp.message}", color=FAIL_COLOR)

    def _export_csv(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("All", "*.*")],
            title="Export Failures as CSV",
        )
        if not path:
            return
        try:
            import csv
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Site", "Cycle", "Vector", "Pattern",
                                 "Time Set", "Pin", "Expected", "Actual", "Pass?"])
                for item in self._fail_tree.get_children():
                    writer.writerow(self._fail_tree.item(item)["values"])
            self._log(f"[Export] CSV written to {path}", color=PASS_COLOR)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[Export error] {exc}", color=FAIL_COLOR)

    # =======================================================================
    # Logging helpers (thread-safe)
    # =======================================================================
    def _log(self, msg: str, color: str = "info"):
        """Queue a log message for consumption on the main thread."""
        self._log_queue.put((msg, color))

    def _poll_log(self):
        """Drain the log queue and append to the scrolled text widget."""
        try:
            while True:
                msg, color = self._log_queue.get_nowait()
                self._log_text.config(state="normal")
                self._log_text.insert("end", msg + "\n", color)
                self._log_text.see("end")
                self._log_text.config(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll_log)

    def _clear_log(self):
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.config(state="disabled")


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app = App()
    app.mainloop()
