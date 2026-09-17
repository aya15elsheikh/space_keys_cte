from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
from queue import Empty, Queue
import random
import re
import struct
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any

from PIL import Image, ImageTk

from catalog import (
    CAMERA_EFFECTS,
    CAMERA_RESOLUTIONS,
    COMMANDS,
    command_by_display_for,
    commands_for,
    command_name,
    SatelliteProfile,
    node_by_id_for,
    node_by_name_for,
    nodes_for,
    PAYLOAD_MODES,
    CommandSpec,
)
from protocol import (
    AX25_PACKET_SIZE,
    AX25RepeatTracker,
    AX25StreamParser,
    CRCAlgorithm,
    GROUND_CALLSIGN,
    GROUND_SOURCE_A7,
    SATELLITE_CALLSIGN,
    SATELLITE_DEST_A7,
    SSPFrame,
    SSPStreamParser,
    build_ax25_i,
    build_ax25_s,
    extract_ax25_frames_from_log,
    extract_ssp_frames_from_log,
    encode_stim_time,
    format_hex,
    hex_bytes,
    obc_time_seconds,
)
from labview_pages import LabviewTelemetryPages
from telemetry import TelemetryRecord, TelemetryStore
from transport import SerialTransport, available_ports
from wifi_client import PayloadWifiClient, normalize_base_url


APP_NAME = "CubeSat GCS"
APP_VERSION = "3.6.3"

NAVY = "#2B305B"
DEEP_NAVY = "#171B3F"
PANEL_NAVY = "#222952"
CREAM = "#F4EED9"
CREAM_2 = "#EDE5CA"
RED = "#FF233E"
BLUE = "#4B43FF"
GREEN = "#38C84A"
AMBER = "#FFB020"
INK = "#111426"
GRAY = "#A9A9AD"
WHITE = "#FFFFFF"


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def app_data_dir() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CubeSatGCS"
    root.mkdir(parents=True, exist_ok=True)
    return root


def operator_dir(kind: str) -> Path:
    path = Path.home() / "Documents" / "CubeSat GCS" / kind
    path.mkdir(parents=True, exist_ok=True)
    return path


class Led(tk.Canvas):
    def __init__(self, master: tk.Misc, text: str, width: int = 82) -> None:
        super().__init__(master, width=width, height=47, bg=CREAM, highlightthickness=0)
        self.create_text(width // 2, 10, text=text, fill=INK, font=("Segoe UI", 8, "bold"))
        self.light = self.create_oval(width // 2 - 8, 23, width // 2 + 8, 39, fill="#D7D7D7", outline="#929292", width=2)

    def set(self, active: bool | None, color: str = GREEN) -> None:
        fill = color if active else (RED if active is False else "#D7D7D7")
        self.itemconfigure(self.light, fill=fill)


class SpaceKeysApp(LabviewTelemetryPages, tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} — Professional Ground Control Station")
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        window_width = min(1600, max(760, screen_width - 40))
        window_height = min(920, max(540, screen_height - 90))
        self.geometry(f"{window_width}x{window_height}+0+0")
        self.minsize(760, 540)
        self.configure(bg=NAVY)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.events: Queue[tuple[str, Any]] = Queue()
        self.transport = SerialTransport(
            lambda value: self.events.put(("serial", value)),
            lambda value: self.events.put(("serial_error", value)),
        )
        self.ssp_parser = SSPStreamParser(CRCAlgorithm.AUTO)
        self.ax25_parser = AX25StreamParser()
        self.ax25_repeat_tracker = AX25RepeatTracker()
        self.telemetry = TelemetryStore()
        self.port_map: dict[str, str] = {}
        self.log_records: list[str] = []
        self.last_tx_raw = b""
        self.last_tx_wire = b""
        self.tx_count = 0
        self.rx_count = 0
        self.echo_count = 0
        self.bad_crc_count = 0
        self.ax25_count = 0
        self.ax25_bad_count = 0
        self.ax25_logical_count = 0
        self.ax25_repeat_count = 0
        self.ax25_tx_sequence = 0
        self.ax25_rx_sequence = 0
        self.demo_ax25_sequence = 0
        self._scheduled: dict[str, dict[str, Any]] = {}
        self._next_schedule_id = 1
        self._image_photo: ImageTk.PhotoImage | None = None
        self._image_photo_secondary: ImageTk.PhotoImage | None = None
        self._header_images: list[ImageTk.PhotoImage] = []
        self._last_image_revision: Any = None
        self._last_image_signature: tuple[Any, ...] | None = None
        self._wifi_download_in_progress = False
        self._wifi_status_in_progress = False
        self._auto_wifi_job: str | None = None
        self._capture_fetch_pending = False
        self._timg_fetch_pending = False
        self._recording = False
        self._recording_path: Path | None = None
        self._recording_file: io.TextIOWrapper | None = None
        self._recording_writer: csv.writer | None = None

        self._make_variables()
        self._lv_init()
        self._configure_styles()
        self._build_menu()
        self._build_status_bar()
        self._build_scroll_shell()
        self._build_header(self.scroll_content)
        self._build_workspace(self.scroll_content)
        # The panes do not report their final requested sizes until Tk has
        # completed an idle layout pass.  Recalculate then so the outer
        # vertical scrollbar can reach controls below the viewport.
        self.after_idle(self._refresh_scroll_extent)
        self._load_settings()
        self._on_satellite_changed(announce=False)
        self._on_mode_changed(announce=False)
        self.refresh_ports()
        self._select_command()
        self._tick_clock()
        self.after(40, self._drain_events)
        self.after(250, self._run_scheduler)
        self._log("READY", f"{APP_NAME} initialized. Select a COM port or enable Demo mode.")

    def _on_mode_changed(self, _event: Any = None, announce: bool = True) -> None:
        """Apply the mission link defaults when the operator changes transport mode."""
        selected_mode = self.mode_var.get()
        ground = selected_mode.startswith("Ground")
        ground_ax25 = selected_mode == "Ground Station / AX.25"
        if ground:
            self.cable_var.set("HC-12 / AX.25" if ground_ax25 else "HC-12 / SSP")
            self.baud_var.set("9600")
            self.ax25_wrap_var.set(ground_ax25)
            # Both antenna modes are GCS uplinks. Native SSP changes only the
            # RF envelope; its SSP source remains the selected profile's GCS.
            self.source_var.set("Ground Station")
            if ground_ax25:
                self.ax25_dest_var.set(self.ax25_dest_var.get().strip().upper() or SATELLITE_CALLSIGN)
                self.ax25_source_var.set(self.ax25_source_var.get().strip().upper() or GROUND_CALLSIGN)
        else:
            self.cable_var.set("USB RS-485")
            self.baud_var.set("115200")
            self.ax25_wrap_var.set(False)
            # Direct SSP accepts either the active profile's Core-OBC or
            # Ground Station address as source. Preserve the selection.
        if hasattr(self, "ssp_parser"):
            self.ssp_parser.reset()
            self.ax25_parser.reset()
            self.ax25_repeat_tracker.reset()
            self.ax25_rx_sequence = 0
        if announce and hasattr(self, "log_records"):
            if ground:
                link = f"HC-12 at 9600 baud with {'legacy AX.25' if ground_ax25 else 'native SSP'}"
            else:
                link = "direct RS-485 SSP at 115200 baud"
            self._log("EVENT", f"Link mode: {link}.")

    def _profile(self) -> SatelliteProfile:
        try:
            return SatelliteProfile(self.satellite_var.get())
        except ValueError:
            self.satellite_var.set(SatelliteProfile.SPACE_KEYS.value)
            return SatelliteProfile.SPACE_KEYS

    def _nodes(self):
        return nodes_for(self._profile())

    def _node_by_name(self):
        return node_by_name_for(self._profile())

    def _node_by_id(self):
        return node_by_id_for(self._profile())

    def _commands(self) -> tuple[CommandSpec, ...]:
        return commands_for(self._profile())

    def _command_by_display(self) -> dict[str, CommandSpec]:
        return command_by_display_for(self._profile())

    def _command_named(self, name: str) -> CommandSpec | None:
        aliases = {
            "STIM": "STIME",
            "SM": "SMODE",
            "GOSTM": "GOTLM",
            "SOF": "SOFF",
        } if self._profile() is SatelliteProfile.GRAD_PROJECT else {}
        wanted = aliases.get(name, name)
        return next((item for item in self._commands() if item.name == wanted), None)

    def _tx_crc(self) -> CRCAlgorithm:
        selected = CRCAlgorithm(self.crc_var.get())
        if selected is CRCAlgorithm.AUTO:
            return CRCAlgorithm.IBM_3740 if self._profile() is SatelliteProfile.GRAD_PROJECT else CRCAlgorithm.CCITT
        return selected

    def _on_satellite_changed(self, _event: Any = None, announce: bool = True) -> None:
        profile = self._profile()
        self.telemetry.profile = profile
        self._lv_set_profile(profile)
        names = tuple(node.name for node in nodes_for(profile))
        for combo_name in ("destination_combo", "source_combo", "schedule_destination_combo"):
            combo = getattr(self, combo_name, None)
            if combo is not None:
                combo.configure(values=names)
        commands = self._commands()
        command_values = tuple(item.display for item in commands)
        for combo_name in ("command_combo", "schedule_command_combo"):
            combo = getattr(self, combo_name, None)
            if combo is not None:
                combo.configure(values=command_values)
        if self.command_var.get() not in command_values:
            self.command_var.set(commands[0].display)
        # The Grad Project ICD explicitly specifies CRC-16/IBM-3740. Space
        # Keys keeps the reflected legacy CRC used by the original satellite.
        self.crc_var.set(
            CRCAlgorithm.IBM_3740.value
            if profile is SatelliteProfile.GRAD_PROJECT
            else CRCAlgorithm.CCITT.value
        )
        if self.destination_var.get() not in names:
            self.destination_var.set("Payload")
        if self.source_var.get() not in names:
            self.source_var.set("Ground Station")
        if hasattr(self, "description_label"):
            self._select_command()
        if announce and hasattr(self, "log_records"):
            self._log(
                "EVENT",
                f"Satellite contract selected: {profile.value}. Telemetry tabs now use the matching layout; AX.25 framing is unchanged.",
            )

    def _make_variables(self) -> None:
        current_time = datetime.now().replace(microsecond=0)
        self.cable_var = tk.StringVar(value="HC-12 / AX.25")
        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value="9600")
        self.connection_var = tk.StringVar(value="DISCONNECTED")
        self.delay_var = tk.IntVar(value=10)
        self.mode_var = tk.StringVar(value="Ground Station / AX.25")
        self.crc_var = tk.StringVar(value=CRCAlgorithm.CCITT.value)
        self.satellite_var = tk.StringVar(value=SatelliteProfile.SPACE_KEYS.value)
        self.demo_var = tk.BooleanVar(value=False)
        self.ax25_wrap_var = tk.BooleanVar(value=True)
        self.ax25_dest_var = tk.StringVar(value=SATELLITE_CALLSIGN)
        self.ax25_source_var = tk.StringVar(value=GROUND_CALLSIGN)
        self.command_var = tk.StringVar(value=COMMANDS[0].display)
        self.destination_var = tk.StringVar(value="Payload")
        self.source_var = tk.StringVar(value="Ground Station")
        self.sequence_var = tk.IntVar(value=0)
        self.data_var = tk.StringVar()
        self.frame_preview_var = tk.StringVar()
        self.custom_var = tk.StringVar()
        self.clock_var = tk.StringVar()
        self.log_filter_var = tk.StringVar(value="All")
        self.wifi_url_var = tk.StringVar(value="http://192.168.4.1")
        self.wifi_state_var = tk.StringVar(value="Not checked")
        self.wifi_auto_var = tk.BooleanVar(value=False)
        self.image_meta_var = tk.StringVar(value="No image loaded")
        self.camera_resolution_var = tk.StringVar(value="VGA 640 x 480")
        self.camera_quality_var = tk.IntVar(value=9)
        self.camera_flash_var = tk.BooleanVar(value=True)
        self.camera_effect_var = tk.StringVar(value="No effect")
        self.camera_brightness_var = tk.IntVar(value=0)
        self.stim_date_var = tk.StringVar(value=current_time.strftime("%Y-%m-%d"))
        self.stim_hour_var = tk.IntVar(value=current_time.hour)
        self.stim_minute_var = tk.IntVar(value=current_time.minute)
        self.stim_second_var = tk.IntVar(value=current_time.second)
        self.stim_epoch_var = tk.StringVar(value=str(obc_time_seconds(current_time)))
        self.stim_bytes_var = tk.StringVar(value=format_hex(encode_stim_time(current_time)))
        self.schedule_time_var = tk.StringVar(value=(datetime.now() + timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S"))
        self.auto_scroll_var = tk.BooleanVar(value=True)
        self.rx_cmd_var = tk.StringVar(value="—")
        self.rx_source_var = tk.StringVar(value="—")
        self.rx_destination_var = tk.StringVar(value="—")
        self.rx_length_var = tk.StringVar(value="0")
        self.rx_crc_var = tk.StringVar(value="—")
        self.rx_data_var = tk.StringVar(value="")
        self.ax25_status_var = tk.StringVar(value="Waiting for frame")
        self.satellite_time_var = tk.StringVar(value="00:00:00.000\nMM/DD/YYYY")
        self.satellite_mode_var = tk.StringVar(value="Standby")
        self.power_source_var = tk.StringVar(value="Unknown")
        self.dissipated_power_var = tk.StringVar(value="—")
        self.status_frame_var = tk.StringVar(value="Waiting for satellite status")
        self.ssp_frame_var = tk.StringVar(value="Waiting for SSP frame")

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=CREAM)
        style.configure("Navy.TFrame", background=NAVY)
        style.configure("Panel.TFrame", background=PANEL_NAVY)
        style.configure("TLabel", background=CREAM, foreground=INK, font=("Segoe UI", 9))
        style.configure("Navy.TLabel", background=NAVY, foreground=WHITE)
        style.configure("RedTitle.TLabel", background=NAVY, foreground=RED, font=("Segoe UI", 15, "bold"))
        style.configure("BlueTitle.TLabel", background=CREAM, foreground=BLUE, font=("Georgia", 14, "bold italic"))
        style.configure("Section.TLabel", background=CREAM, foreground=INK, font=("Segoe UI", 10, "bold"))
        style.configure("Description.TLabel", background=PANEL_NAVY, foreground=RED, font=("Segoe UI", 10, "bold"))
        style.configure("TButton", font=("Segoe UI", 9, "bold"), padding=(8, 5), background=CREAM_2)
        style.map("TButton", background=[("active", WHITE)])
        style.configure("Send.TButton", foreground=RED, font=("Segoe UI", 16, "bold italic"), padding=(18, 7))
        style.configure("Connect.TButton", foreground=WHITE, background="#B20E2D")
        style.map("Connect.TButton", background=[("active", RED)])
        style.configure("TNotebook", background=CREAM, borderwidth=0)
        style.configure("TNotebook.Tab", background=CREAM_2, foreground=RED, font=("Segoe UI", 9, "bold"), padding=(10, 6))
        style.map("TNotebook.Tab", background=[("selected", WHITE)], foreground=[("selected", RED)])
        style.configure("Hidden.TNotebook", background=CREAM, borderwidth=0)
        style.layout("Hidden.TNotebook.Tab", [])
        style.configure("Treeview", background=WHITE, fieldbackground=WHITE, foreground=INK, rowheight=24, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background=CREAM_2, foreground=INK, font=("Segoe UI", 9, "bold"))
        style.configure("TCheckbutton", background=CREAM, foreground=INK)
        style.configure("TRadiobutton", background=CREAM, foreground=INK)
        style.configure("TEntry", fieldbackground=WHITE)
        style.configure("TCombobox", fieldbackground=WHITE)
        style.configure("Vertical.TScrollbar", background=CREAM_2)

    def _build_menu(self) -> None:
        menu = tk.Menu(self)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="Save session log…", command=self.save_log)
        file_menu.add_command(label="Replay a saved log…", command=self.replay_log)
        file_menu.add_separator()
        file_menu.add_command(label="Start telemetry CSV recording", command=self.toggle_recording)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menu.add_cascade(label="File", menu=file_menu)
        connection_menu = tk.Menu(menu, tearoff=False)
        connection_menu.add_command(label="Refresh COM ports", command=self.refresh_ports)
        connection_menu.add_command(label="Connect / Disconnect", command=self.toggle_connection)
        connection_menu.add_separator()
        connection_menu.add_checkbutton(label="Demo mode", variable=self.demo_var)
        menu.add_cascade(label="Connection", menu=connection_menu)
        tools_menu = tk.Menu(menu, tearoff=False)
        tools_menu.add_command(label="Clear parser state", command=self._reset_parsers)
        tools_menu.add_command(label="Check payload Wi-Fi", command=self.check_wifi)
        tools_menu.add_command(label="Download payload image", command=self.download_wifi_image)
        menu.add_cascade(label="Tools", menu=tools_menu)
        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="Operator quick guide", command=lambda: self.main_notebook.select(self.faq_page))
        help_menu.add_command(label="About", command=self.show_about)
        menu.add_cascade(label="Help", menu=help_menu)
        self.configure(menu=menu)

    def _build_scroll_shell(self) -> None:
        """Provide whole-console scrolling while retaining the 1220 px layout."""
        shell = tk.Frame(self, bg=NAVY)
        shell.pack(fill="both", expand=True, padx=3, pady=(1, 2))
        shell.rowconfigure(0, weight=1)
        shell.columnconfigure(0, weight=1)

        self.scroll_canvas = tk.Canvas(shell, bg=NAVY, highlightthickness=0)
        horizontal = ttk.Scrollbar(shell, orient="horizontal", command=self.scroll_canvas.xview)
        vertical = ttk.Scrollbar(shell, orient="vertical", command=self.scroll_canvas.yview)
        self.scroll_canvas.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
        self.scroll_canvas.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")

        self.scroll_content = tk.Frame(self.scroll_canvas, bg=NAVY)
        self._scroll_window = self.scroll_canvas.create_window(
            (0, 0), window=self.scroll_content, anchor="nw"
        )
        self.scroll_content.bind(
            "<Configure>",
            lambda _event: self.scroll_canvas.configure(
                scrollregion=self.scroll_canvas.bbox("all")
            ),
        )
        self.scroll_canvas.bind("<Configure>", self._resize_scroll_content)

    def _resize_scroll_content(self, event: tk.Event) -> None:
        self._apply_scroll_extent(int(event.width), int(event.height))

    def _apply_scroll_extent(self, viewport_width: int, viewport_height: int) -> None:
        """Size the embedded console to both the viewport and its real content."""
        required_height = 680
        if hasattr(self, "workspace") and hasattr(self, "_workspace_panes"):
            pane_height = max(
                (pane.winfo_reqheight() for pane in self._workspace_panes),
                default=0,
            )
            # The workspace begins below the fixed console header.  Its own
            # requested height does not include the pane contents, so using
            # scroll_content.winfo_reqheight() here would silently clip them.
            required_height = max(
                required_height,
                int(self.workspace.winfo_y()) + pane_height + 4,
            )

        content_width = max(1220, viewport_width)
        content_height = max(required_height, viewport_height)
        self.scroll_canvas.itemconfigure(
            self._scroll_window,
            width=content_width,
            height=content_height,
        )
        # Set an explicit region as well as relying on <Configure>.  This keeps
        # the right scrollbar responsive during rapid maximize/restore events.
        self.scroll_canvas.configure(
            scrollregion=(0, 0, content_width, content_height)
        )

    def _refresh_scroll_extent(self) -> None:
        if not self.winfo_exists() or not hasattr(self, "scroll_canvas"):
            return
        self._apply_scroll_extent(
            max(1, self.scroll_canvas.winfo_width()),
            max(1, self.scroll_canvas.winfo_height()),
        )

    def _build_header(self, parent: tk.Misc) -> None:
        header = tk.Frame(parent, bg=NAVY, height=108, highlightbackground="#777B8B", highlightthickness=2)
        self.console_header = header
        header.pack(fill="x", padx=5, pady=(4, 2))
        header.pack_propagate(False)

        controls = tk.Frame(header, bg=NAVY)
        controls.pack(side="left", fill="y", padx=8, pady=6)
        top = tk.Frame(controls, bg=NAVY)
        top.pack(anchor="w")
        ttk.Combobox(top, textvariable=self.cable_var, values=("HC-12 / AX.25", "HC-12 / SSP", "USB RS-485", "NI RS485", "Cable"), width=16, state="readonly").pack(side="left", padx=(0, 5))
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var, width=24, state="readonly")
        self.port_combo.pack(side="left", padx=4)
        ttk.Button(top, text="↻", width=3, command=self.refresh_ports).pack(side="left", padx=2)
        self.connect_button = ttk.Button(top, text="CONNECT", style="Connect.TButton", command=self.toggle_connection)
        self.connect_button.pack(side="left", padx=5)
        bottom = tk.Frame(controls, bg=NAVY)
        bottom.pack(anchor="w", pady=(7, 0))
        mode_combo = ttk.Combobox(
            bottom,
            textvariable=self.mode_var,
            values=("Ground Station / AX.25", "Ground Station / SSP", "Subsystems / RS-485"),
            width=22,
            state="readonly",
        )
        mode_combo.pack(side="left")
        mode_combo.bind("<<ComboboxSelected>>", self._on_mode_changed)
        ttk.Label(bottom, text="Baud", style="Navy.TLabel").pack(side="left", padx=(10, 3))
        ttk.Combobox(bottom, textvariable=self.baud_var, values=("9600", "19200", "57600", "115200", "230400", "460800"), width=8).pack(side="left")
        ttk.Label(bottom, text="Delay", style="Navy.TLabel").pack(side="left", padx=(10, 3))
        tk.Spinbox(bottom, from_=0, to=2000, textvariable=self.delay_var, width=5, bg=CREAM).pack(side="left")
        ttk.Label(bottom, text="ms", style="Navy.TLabel").pack(side="left", padx=(2, 8))
        ttk.Checkbutton(bottom, text="Demo", variable=self.demo_var, style="TCheckbutton").pack(side="left")

        title = tk.Frame(header, bg=NAVY)
        title.pack(side="left", fill="both", expand=True, padx=12)
        tk.Label(title, text=APP_NAME, bg=DEEP_NAVY, fg=RED, font=("Segoe UI", 18, "bold"), padx=24, pady=6, relief="ridge", bd=3).pack(side="left", padx=(0, 10), pady=7)
        tk.Label(title, text="CUBESAT\nGROUND CONTROL STATION", bg=CREAM, fg=RED, font=("Segoe UI", 12, "bold"), padx=18, pady=6, relief="ridge", bd=3).pack(side="left", pady=7)
        tk.Label(title, textvariable=self.clock_var, bg=CREAM, fg=RED, font=("Segoe UI", 11, "bold"), justify="left", padx=14, pady=8, relief="ridge", bd=3).pack(side="left", padx=10, pady=7)

        logos = tk.Frame(header, bg=NAVY)
        logos.pack(side="right", fill="y", padx=8, pady=4)
        self._load_header_logos(logos)

    def _load_header_logos(self, parent: tk.Misc) -> None:
        try:
            screenshot = Image.open(resource_path("assets/labview_reference.png"))
            # Keep the EgSA mark only.  The former Space Keys logo occupied the
            # second crop in the LabVIEW reference and is intentionally omitted.
            crops = ((897, 4, 1052, 130),)
            for box in crops:
                image = screenshot.crop(box)
                image.thumbnail((118, 92), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(image)
                self._header_images.append(photo)
                tk.Label(parent, image=photo, bg=WHITE, relief="ridge", bd=2).pack(side="left", padx=4)
        except Exception:
            tk.Label(parent, text="EgSA", bg=CREAM, fg=INK, font=("Segoe UI", 11, "bold"), padx=24, pady=20).pack()

    def _build_workspace(self, parent: tk.Misc) -> None:
        workspace = tk.PanedWindow(parent, orient="horizontal", bg="#74798C", sashwidth=5, sashrelief="raised", bd=0)
        self.workspace = workspace
        workspace.pack(fill="both", expand=True, padx=5, pady=2)
        left = tk.Frame(workspace, bg=CREAM, width=360)
        center = tk.Frame(workspace, bg=NAVY, width=360)
        right = tk.Frame(workspace, bg=CREAM, width=700)
        self._workspace_panes = (left, center, right)
        workspace.add(left, minsize=315, width=360)
        workspace.add(center, minsize=315, width=360)
        workspace.add(right, minsize=480)
        self._build_command_panel(left)
        self._build_log_panel(center)
        self._build_telemetry_panel(right)

    def _build_command_panel(self, parent: tk.Misc) -> None:
        mode_tabs = ttk.Notebook(parent)
        mode_tabs.pack(fill="x", padx=7, pady=(7, 3))
        direct_tab = ttk.Frame(mode_tabs)
        ground_tab = ttk.Frame(mode_tabs)
        mode_tabs.add(ground_tab, text="GCS (HC-12)")
        mode_tabs.add(direct_tab, text="Subsystems (RS-485)")
        ttk.Label(direct_tab, text="Direct SSP communication through the selected RS-485 cable.").pack(anchor="w", padx=7, pady=5)
        protocol_row = ttk.Frame(ground_tab)
        protocol_row.pack(fill="x", padx=5, pady=(4, 1))
        ttk.Label(protocol_row, text="Antenna protocol", style="Section.TLabel").pack(side="left", padx=(0, 4))
        ttk.Radiobutton(
            protocol_row,
            text="AX.25",
            variable=self.mode_var,
            value="Ground Station / AX.25",
            command=self._on_mode_changed,
        ).pack(side="left")
        ttk.Radiobutton(
            protocol_row,
            text="SSP",
            variable=self.mode_var,
            value="Ground Station / SSP",
            command=self._on_mode_changed,
        ).pack(side="left", padx=(3, 0))
        callsign_row = ttk.Frame(ground_tab)
        callsign_row.pack(fill="x", padx=5, pady=(1, 4))
        ttk.Label(callsign_row, text="AX.25 callsigns — To").pack(side="left", padx=(0, 2))
        ttk.Entry(callsign_row, textvariable=self.ax25_dest_var, width=7).pack(side="left")
        ttk.Label(callsign_row, text="From").pack(side="left", padx=(7, 2))
        ttk.Entry(callsign_row, textvariable=self.ax25_source_var, width=7).pack(side="left")

        builder = ttk.Frame(parent)
        builder.pack(fill="x", padx=11, pady=3)
        ttk.Label(builder, text="Command List", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(builder, text="Destination", style="Section.TLabel").grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.command_combo = ttk.Combobox(builder, textvariable=self.command_var, values=tuple(item.display for item in COMMANDS), state="readonly", width=20)
        self.command_combo.grid(row=1, column=0, sticky="ew")
        self.command_combo.bind("<<ComboboxSelected>>", lambda _event: self._select_command())
        self.destination_combo = ttk.Combobox(builder, textvariable=self.destination_var, values=tuple(node.name for node in nodes_for(SatelliteProfile.SPACE_KEYS)), state="readonly", width=13)
        self.destination_combo.grid(row=1, column=1, sticky="ew", padx=(8, 0))
        ttk.Label(builder, text="Source", style="Section.TLabel").grid(row=2, column=0, sticky="w", pady=(7, 0))
        ttk.Label(builder, text="N(S)", style="Section.TLabel").grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(7, 0))
        self.source_combo = ttk.Combobox(builder, textvariable=self.source_var, values=tuple(node.name for node in nodes_for(SatelliteProfile.SPACE_KEYS)), state="readonly", width=20)
        self.source_combo.grid(row=3, column=0, sticky="ew")
        tk.Spinbox(builder, from_=0, to=5, textvariable=self.sequence_var, width=6, bg=WHITE).grid(row=3, column=1, sticky="w", padx=(8, 0))
        builder.columnconfigure(0, weight=1)

        ttk.Label(parent, text="Command Description", style="Section.TLabel").pack(anchor="w", padx=11, pady=(6, 2))
        self.description_label = ttk.Label(parent, text="", style="Description.TLabel", anchor="w", justify="left", wraplength=330, padding=8)
        self.description_label.pack(fill="x", padx=11)

        ttk.Label(parent, text="SSP Data", style="Section.TLabel").pack(anchor="w", padx=11, pady=(7, 2))
        data_row = ttk.Frame(parent)
        data_row.pack(fill="x", padx=11)
        ttk.Entry(data_row, textvariable=self.data_var).pack(side="left", fill="x", expand=True)
        ttk.Button(data_row, text="Clear", command=lambda: self.data_var.set("")).pack(side="left", padx=(5, 0))

        parameters = ttk.Notebook(parent)
        parameters.pack(fill="x", padx=11, pady=7)
        payload = ttk.Frame(parameters)
        general = ttk.Frame(parameters)
        communication = ttk.Frame(parameters)
        obc = ttk.Frame(parameters)
        adcs = ttk.Frame(parameters)
        parameters.add(payload, text="Payload")
        parameters.add(general, text="General")
        parameters.add(communication, text="Communication")
        parameters.add(obc, text="OBC")
        parameters.add(adcs, text="ADCS")
        self._payload_presets(payload)
        self._general_presets(general)
        self._communication_presets(communication)
        self._obc_presets(obc)
        self._adcs_presets(adcs)

        ttk.Label(parent, text="Generated Frame", style="Section.TLabel").pack(anchor="w", padx=11)
        preview = tk.Entry(parent, textvariable=self.frame_preview_var, state="readonly", readonlybackground=DEEP_NAVY, fg=WHITE, font=("Consolas", 9), relief="sunken", bd=3)
        preview.pack(fill="x", padx=11, pady=(2, 6), ipady=6)
        send_row = ttk.Frame(parent)
        send_row.pack(fill="x", padx=11)
        ttk.Button(send_row, text="➜  SEND", style="Send.TButton", command=self.send_selected).pack(side="left", expand=True)
        ttk.Button(send_row, text="Build", command=self.preview_selected).pack(side="left", padx=(8, 0))

        separator = ttk.Separator(parent, orient="horizontal")
        separator.pack(fill="x", padx=20, pady=10)
        tk.Label(parent, text="Insert Your Custom Frame", bg=CREAM, fg=RED, font=("Segoe UI", 10, "bold")).pack()
        ttk.Entry(parent, textvariable=self.custom_var, font=("Consolas", 9)).pack(fill="x", padx=11, pady=4)
        custom_row = ttk.Frame(parent)
        custom_row.pack(fill="x", padx=11)
        ttk.Button(custom_row, text="Send Custom Frame", command=self.send_custom).pack(side="left", fill="x", expand=True)
        ttk.Button(custom_row, text="Clear Custom Frame", command=lambda: self.custom_var.set("")).pack(side="left", padx=(5, 0), fill="x", expand=True)

        contract_row = ttk.Frame(parent)
        contract_row.pack(fill="x", padx=11, pady=(10, 2))
        ttk.Label(contract_row, text="Satellite contract", style="Section.TLabel").pack(side="left")
        satellite_combo = ttk.Combobox(contract_row, textvariable=self.satellite_var, values=tuple(item.value for item in SatelliteProfile), state="readonly", width=17)
        satellite_combo.pack(side="left", padx=7)
        satellite_combo.bind("<<ComboboxSelected>>", self._on_satellite_changed)
        crc_row = ttk.Frame(parent)
        crc_row.pack(fill="x", padx=11, pady=(2, 4))
        ttk.Label(crc_row, text="SSP CRC", style="Section.TLabel").pack(side="left")
        ttk.Combobox(crc_row, textvariable=self.crc_var, values=tuple(item.value for item in CRCAlgorithm), state="readonly", width=22).pack(side="left", padx=7)
        ttk.Button(crc_row, text="Reset parser", command=self._reset_parsers).pack(side="right")
        tk.Label(parent, text="CubeSat GCS • SSP + AX.25", bg=CREAM, fg=BLUE, font=("Segoe UI", 9, "bold"), relief="ridge", bd=2).pack(fill="x", padx=11, pady=(7, 5))

    def _payload_presets(self, parent: tk.Misc) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=5, pady=6)
        ttk.Label(row, text="Mode").pack(side="left")
        mode = tk.StringVar(value=PAYLOAD_MODES[0][0])
        combo = ttk.Combobox(row, textvariable=mode, values=tuple(x[0] for x in PAYLOAD_MODES), state="readonly", width=20)
        combo.pack(side="left", padx=5)
        ttk.Button(row, text="Set SM", command=lambda: self._apply_preset("SM", bytes((dict(PAYLOAD_MODES)[mode.get()],)), "Payload")).pack(side="left")
        ttk.Button(row, text="CIMG", command=self.prepare_cimg).pack(side="left", padx=4)
        ttk.Button(row, text="GIMG", command=lambda: self._apply_preset("GIMG", b"", "Payload")).pack(side="left")

    def _general_presets(self, parent: tk.Misc) -> None:
        for name in ("PING", "GOSTM", "RD", "WD"):
            ttk.Button(parent, text=name, command=lambda value=name: self._apply_preset(value)).pack(side="left", padx=4, pady=8)

    def _communication_presets(self, parent: tk.Misc) -> None:
        ttk.Label(parent, text="Relay MAC: GCS ID followed by six MAC bytes.").pack(anchor="w", padx=6, pady=(5, 2))
        relay = tk.StringVar(value="01 00 00 00 00 00 00")
        ttk.Entry(parent, textvariable=relay).pack(side="left", fill="x", expand=True, padx=6, pady=5)
        ttk.Button(parent, text="Set WD", command=lambda: self._apply_preset("WD", hex_bytes(relay.get()), "Payload")).pack(side="left", padx=5)

    def _obc_presets(self, parent: tk.Misc) -> None:
        time_box = ttk.LabelFrame(parent, text="Set OBC Time (STIM)")
        time_box.pack(fill="x", padx=5, pady=(5, 3))

        ttk.Label(time_box, text="Date (YYYY-MM-DD)").grid(row=0, column=0, columnspan=2, sticky="w", padx=5, pady=(3, 0))
        ttk.Label(time_box, text="Time (24-hour)").grid(row=0, column=2, columnspan=5, sticky="w", padx=5, pady=(3, 0))
        date_entry = ttk.Entry(time_box, textvariable=self.stim_date_var, width=12)
        date_entry.grid(row=1, column=0, columnspan=2, sticky="ew", padx=5, pady=(0, 4))
        time_spinboxes: list[tk.Spinbox] = []
        for column, (variable, maximum) in enumerate(
            ((self.stim_hour_var, 23), (self.stim_minute_var, 59), (self.stim_second_var, 59)),
            start=2,
        ):
            spinbox = tk.Spinbox(
                time_box,
                from_=0,
                to=maximum,
                textvariable=variable,
                width=3,
                format="%02.0f",
                bg=WHITE,
                command=self._refresh_stim_preview,
            )
            spinbox.grid(row=1, column=column * 2 - 2, sticky="w", pady=(0, 4))
            time_spinboxes.append(spinbox)
            if column < 4:
                ttk.Label(time_box, text=":").grid(row=1, column=column * 2 - 1, sticky="w", pady=(0, 4))

        date_entry.bind("<FocusOut>", lambda _event: self._refresh_stim_preview())
        date_entry.bind("<Return>", lambda _event: self._prepare_stim())
        for spinbox in time_spinboxes:
            spinbox.bind("<FocusOut>", lambda _event: self._refresh_stim_preview())
            spinbox.bind("<Return>", lambda _event: self._prepare_stim())

        ttk.Label(time_box, text="Epoch seconds (from 2000-01-01)").grid(row=2, column=0, columnspan=4, sticky="w", padx=5)
        tk.Label(
            time_box,
            textvariable=self.stim_epoch_var,
            bg=CREAM_2,
            fg=INK,
            relief="sunken",
            bd=2,
            anchor="w",
            font=("Consolas", 8),
        ).grid(row=3, column=0, columnspan=4, sticky="ew", padx=5, pady=(0, 3))
        ttk.Label(time_box, text="STIM bytes (U64 little-endian)").grid(row=4, column=0, columnspan=7, sticky="w", padx=5)
        tk.Label(
            time_box,
            textvariable=self.stim_bytes_var,
            bg=DEEP_NAVY,
            fg=WHITE,
            relief="sunken",
            bd=2,
            anchor="w",
            font=("Consolas", 8),
            padx=4,
        ).grid(row=5, column=0, columnspan=7, sticky="ew", padx=5, pady=(0, 4))

        actions = ttk.Frame(time_box)
        actions.grid(row=6, column=0, columnspan=7, sticky="ew", padx=5, pady=(0, 5))
        ttk.Button(actions, text="Use PC time", command=self._use_pc_time_for_stim).pack(side="left", fill="x", expand=True)
        ttk.Button(actions, text="Build STIM", command=self._prepare_stim).pack(side="left", fill="x", expand=True, padx=(5, 0))
        time_box.columnconfigure(0, weight=1)
        time_box.columnconfigure(1, weight=1)

        quick = ttk.Frame(parent)
        quick.pack(fill="x", padx=4, pady=(0, 3))
        ttk.Button(quick, text="INIT", command=lambda: self._apply_preset("INIT", b"", "Core-OBC")).pack(side="left", padx=2)
        ttk.Button(quick, text="END", command=lambda: self._apply_preset("END", b"", "Core-OBC")).pack(side="left", padx=2)
        ttk.Button(quick, text="GTIM", command=lambda: self._apply_preset("GTIM", b"", "Core-OBC")).pack(side="left", padx=2)
        ttk.Button(quick, text="Satellite status", command=lambda: self._apply_preset("GOSTM", b"", "Core-OBC")).pack(side="left", padx=2)

    def _stim_datetime(self) -> datetime:
        date_value = datetime.strptime(self.stim_date_var.get().strip(), "%Y-%m-%d")
        return date_value.replace(
            hour=int(self.stim_hour_var.get()),
            minute=int(self.stim_minute_var.get()),
            second=int(self.stim_second_var.get()),
        )

    def _refresh_stim_preview(self, show_error: bool = False) -> bytes | None:
        try:
            selected_time = self._stim_datetime()
            data = encode_stim_time(selected_time)
            self.stim_epoch_var.set(str(obc_time_seconds(selected_time)))
            self.stim_bytes_var.set(format_hex(data))
            return data
        except (TypeError, ValueError, tk.TclError) as exc:
            self.stim_epoch_var.set("Invalid date/time")
            self.stim_bytes_var.set("—")
            if show_error:
                messagebox.showerror(
                    "Invalid STIM time",
                    f"Enter a valid date and 24-hour time.\n\n{exc}",
                    parent=self,
                )
            return None

    def _prepare_stim(self) -> None:
        data = self._refresh_stim_preview(show_error=True)
        if data is not None:
            self._apply_preset("STIM", data, "Core-OBC")

    def _use_pc_time_for_stim(self) -> None:
        current_time = datetime.now().replace(microsecond=0)
        self.stim_date_var.set(current_time.strftime("%Y-%m-%d"))
        self.stim_hour_var.set(current_time.hour)
        self.stim_minute_var.set(current_time.minute)
        self.stim_second_var.set(current_time.second)
        self._prepare_stim()

    def _adcs_presets(self, parent: tk.Misc) -> None:
        ttk.Button(parent, text="Read Gyro", command=lambda: self._apply_preset("RD", b"\x00\x01\x0C", "ADCS")).pack(side="left", padx=4, pady=8)
        ttk.Button(parent, text="Read MAG", command=lambda: self._apply_preset("RD", b"\x00\x05\x0C", "ADCS")).pack(side="left", padx=4, pady=8)
        ttk.Button(parent, text="GOSTM", command=lambda: self._apply_preset("GOSTM", b"", "ADCS")).pack(side="left", padx=4, pady=8)

    def _build_log_panel(self, parent: tk.Misc) -> None:
        ttk.Label(parent, text="Session LOG", style="RedTitle.TLabel", anchor="center").pack(fill="x", pady=(7, 4))
        tools = tk.Frame(parent, bg=NAVY)
        tools.pack(fill="x", padx=7)
        ttk.Combobox(tools, textvariable=self.log_filter_var, values=("All", "TX", "RX", "COPY", "EVENT", "ERROR"), width=10, state="readonly").pack(side="left")
        ttk.Checkbutton(tools, text="Auto scroll", variable=self.auto_scroll_var).pack(side="left", padx=7)
        self.record_button = ttk.Button(tools, text="Record CSV", command=self.toggle_recording)
        self.record_button.pack(side="right")
        self.log_text = scrolledtext.ScrolledText(parent, bg=CREAM, fg=INK, insertbackground=INK, font=("Consolas", 9), wrap="word", relief="sunken", bd=4)
        self.log_text.pack(fill="both", expand=True, padx=8, pady=6)
        self.log_text.tag_configure("TX", foreground="#0A5BBA")
        self.log_text.tag_configure("RX", foreground="#075D27")
        self.log_text.tag_configure("COPY", foreground="#777777")
        self.log_text.tag_configure("ERROR", foreground="#B00020", background="#FFE5E8")
        self.log_text.tag_configure("READY", foreground="#6336A6")
        self.log_text.tag_configure("ECHO", foreground="#777777")
        controls = tk.Frame(parent, bg=NAVY)
        controls.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(controls, text="Clear", command=self.clear_log).pack(side="left", fill="x", expand=True)
        ttk.Button(controls, text="Save Log", command=self.save_log).pack(side="left", fill="x", expand=True, padx=5)
        ttk.Button(controls, text="Replay", command=self.replay_log).pack(side="left", fill="x", expand=True)

    def _build_telemetry_panel(self, parent: tk.Misc) -> None:
        tk.Label(parent, text="Subsystems and Experiments Telemetry", bg=DEEP_NAVY, fg=RED, font=("Segoe UI", 15, "bold"), relief="ridge", bd=3, pady=7).pack(fill="x", padx=8, pady=(7, 4))
        self.telemetry_tab_bar = tk.Frame(parent, bg=CREAM)
        self.telemetry_tab_bar.pack(fill="x", padx=5)
        self.main_notebook = ttk.Notebook(parent, style="Hidden.TNotebook")
        self.main_notebook.pack(fill="both", expand=True, padx=5, pady=(0, 5))
        self.pages: dict[str, tk.Widget] = {}
        self.telemetry_tabs: dict[str, tk.Widget] = {}
        main = ttk.Frame(self.main_notebook)
        self.main_notebook.add(main, text="Main Page")
        self.telemetry_tabs["Main Page"] = main
        self._build_main_page(main)
        dedicated_builders = {
            "OBC": self._labview_build_obc_page,
            "EPS": self._labview_build_eps_page,
            "Space Environment": self._labview_build_environment_page,
            "COMM": self._labview_build_comm_page,
        }
        for label in ("OBC", "EPS", "Space Environment", "COMM"):
            page = ttk.Frame(self.main_notebook)
            self.main_notebook.add(page, text=label)
            self.telemetry_tabs[label] = page
            self.pages[label] = page
            dedicated_builders[label](page)
        comm_image = ttk.Frame(self.main_notebook)
        self.main_notebook.add(comm_image, text="Comm IMG Display")
        self.telemetry_tabs["Comm IMG Display"] = comm_image
        self.pages["Comm IMG Display"] = comm_image
        self._labview_build_comm_image_page(comm_image)
        image_data = ttk.Frame(self.main_notebook)
        self.main_notebook.add(image_data, text="IMG Data")
        self.telemetry_tabs["IMG Data"] = image_data
        self.pages["IMG Data"] = image_data
        self._labview_build_image_data_page(image_data)
        camera = ttk.Frame(self.main_notebook)
        self.main_notebook.add(camera, text="CAM PL")
        self.telemetry_tabs["CAM PL"] = camera
        self.pages["Payload"] = camera
        self._labview_build_camera_page(camera)
        adcs = ttk.Frame(self.main_notebook)
        self.main_notebook.add(adcs, text="ADCS")
        self.telemetry_tabs["ADCS"] = adcs
        self.pages["ADCS"] = adcs
        self._labview_build_adcs_page(adcs)
        schedule = ttk.Frame(self.main_notebook)
        self.main_notebook.add(schedule, text="Time Tagged CMDs")
        self.telemetry_tabs["Time Tagged CMDs"] = schedule
        self._build_schedule_page(schedule)
        self.faq_page = ttk.Frame(self.main_notebook)
        self.main_notebook.add(self.faq_page, text="*FAQs*")
        self.telemetry_tabs["*FAQs*"] = self.faq_page
        self._build_faq_page(self.faq_page)
        rows = (
            ("OBC", "EPS", "Space Environment", "Time Tagged CMDs", "*FAQs*"),
            ("Main Page", "COMM", "Comm IMG Display", "IMG Data", "CAM PL", "ADCS"),
        )
        self.telemetry_tab_buttons: dict[str, tk.Button] = {}
        for row_index, names in enumerate(rows):
            row = tk.Frame(self.telemetry_tab_bar, bg=CREAM)
            row.pack(fill="x")
            for column, name in enumerate(names):
                button = tk.Button(row, text=name, bg=CREAM_2, fg=RED, activebackground=WHITE, activeforeground=RED, font=("Segoe UI", 8, "bold"), relief="raised", bd=2, width=1, wraplength=88, padx=1, pady=3, command=lambda value=name: self._select_main_tab(value))
                button.grid(row=0, column=column, sticky="nsew", padx=1, pady=1)
                row.columnconfigure(column, weight=1, uniform=f"tabrow{row_index}")
                self.telemetry_tab_buttons[name] = button
        self.main_notebook.bind("<<NotebookTabChanged>>", self._on_telemetry_tab_changed)
        self._refresh_tab_buttons()

    def _on_telemetry_tab_changed(self, _event: tk.Event) -> None:
        self._refresh_tab_buttons()
        self.after_idle(self._refresh_scroll_extent)

    def _select_main_tab(self, name: str) -> None:
        self.main_notebook.select(self.telemetry_tabs[name])
        self._refresh_tab_buttons()

    def _refresh_tab_buttons(self) -> None:
        if not hasattr(self, "telemetry_tab_buttons"):
            return
        selected = self.main_notebook.select()
        for name, button in self.telemetry_tab_buttons.items():
            active = str(self.telemetry_tabs[name]) == selected
            button.configure(bg=WHITE if active else CREAM_2, relief="sunken" if active else "raised")

    def _build_main_page(self, parent: tk.Misc) -> None:
        split = ttk.Panedwindow(parent, orient="horizontal")
        split.pack(fill="both", expand=True, padx=8, pady=8)
        status = ttk.Frame(split)
        analysis = ttk.Frame(split)
        split.add(status, weight=1)
        split.add(analysis, weight=1)
        ttk.Label(status, text="Satellite Status", style="BlueTitle.TLabel", anchor="center").pack(fill="x", pady=5)
        ttk.Separator(status).pack(fill="x")
        ttk.Label(status, text="Status Frame", style="Section.TLabel").pack(anchor="w", padx=10, pady=(8, 2))
        tk.Label(status, textvariable=self.status_frame_var, bg=DEEP_NAVY, fg=WHITE, font=("Consolas", 8), relief="sunken", bd=2, anchor="w", justify="left", wraplength=300, padx=5, pady=5).pack(fill="x", padx=10)
        details = ttk.Frame(status)
        details.pack(fill="x", padx=10, pady=7)
        for row, (label, variable) in enumerate((("Satellite Time", self.satellite_time_var), ("Power Source", self.power_source_var), ("Satellite Mode", self.satellite_mode_var), ("Dissipated Power", self.dissipated_power_var))):
            ttk.Label(details, text=label, style="Section.TLabel").grid(row=row, column=0, sticky="w", pady=4)
            tk.Label(details, textvariable=variable, bg=CREAM_2, fg=INK, relief="sunken", bd=2, anchor="w", padx=6, width=24, justify="left").grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=4)
        details.columnconfigure(1, weight=1)
        leds = ttk.Frame(status)
        leds.pack(fill="x", padx=4, pady=6)
        self.subsystem_leds: dict[str, Led] = {}
        for name in ("Power", "Comm", "OBC", "ADCS", "Payload", "Space Env."):
            led = Led(leds, name, 64)
            led.pack(side="left", expand=True)
            self.subsystem_leds[name] = led
        ttk.Separator(status).pack(fill="x", padx=10, pady=8)
        ttk.Label(status, text="AX.25 Frame Analysis", style="BlueTitle.TLabel", anchor="center").pack(fill="x")
        tk.Label(status, textvariable=self.ax25_status_var, bg=WHITE, fg=INK, anchor="nw", justify="left", relief="sunken", bd=2, font=("Consolas", 9), padx=6, pady=6).pack(fill="x", padx=10, pady=6)
        counters = ttk.Frame(status)
        counters.pack(fill="x", padx=10)
        self.ax25_counter_label = ttk.Label(counters, text="Physical: 0     Logical: 0     Copies: 0     Rejected: 0")
        self.ax25_counter_label.pack(anchor="w")

        ttk.Label(analysis, text="SSP Frame Analysis", style="BlueTitle.TLabel", anchor="center").pack(fill="x", pady=5)
        ttk.Separator(analysis).pack(fill="x")
        ttk.Label(analysis, text="SSP Received Frame", style="Section.TLabel").pack(anchor="w", padx=12, pady=(7, 2))
        tk.Label(analysis, textvariable=self.ssp_frame_var, bg=DEEP_NAVY, fg=WHITE, font=("Consolas", 8), relief="sunken", bd=2, anchor="w", justify="left", wraplength=330, padx=5, pady=5).pack(fill="x", padx=12)
        form = ttk.Frame(analysis)
        form.pack(fill="x", padx=12, pady=10)
        fields = (
            ("Source", self.rx_source_var), ("Destination", self.rx_destination_var),
            ("RX_CMD", self.rx_cmd_var), ("Data length", self.rx_length_var),
            ("CRC Check", self.rx_crc_var),
        )
        for row, (label, variable) in enumerate(fields):
            ttk.Label(form, text=label, style="Section.TLabel").grid(row=row, column=0, sticky="w", pady=4)
            tk.Label(form, textvariable=variable, bg=WHITE, fg=INK, anchor="w", relief="sunken", bd=2, width=25).grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=4)
        form.columnconfigure(1, weight=1)
        indicator_row = ttk.Frame(analysis)
        indicator_row.pack(fill="x", padx=12)
        self.ack_led = Led(indicator_row, "ACK/NACK", 90)
        self.ack_led.pack(side="left")
        self.crc_led = Led(indicator_row, "CRC", 70)
        self.crc_led.pack(side="left")
        self.crc_led.set(None)
        ttk.Label(analysis, text="Data", style="Section.TLabel").pack(anchor="w", padx=12, pady=(8, 2))
        tk.Label(analysis, textvariable=self.rx_data_var, bg=DEEP_NAVY, fg=WHITE, relief="sunken", bd=3, justify="left", anchor="nw", font=("Consolas", 9), wraplength=390, padx=7, pady=7).pack(fill="both", expand=True, padx=12, pady=(0, 8))

    def _build_key_value_page(self, parent: tk.Misc, title: str) -> None:
        ttk.Label(parent, text=f"{title} Telemetry", style="BlueTitle.TLabel", anchor="center").pack(fill="x", padx=8, pady=8)
        columns = ("parameter", "value")
        tree = ttk.Treeview(parent, columns=columns, show="headings")
        tree.heading("parameter", text="Parameter")
        tree.heading("value", text="Value")
        tree.column("parameter", width=270, anchor="w")
        tree.column("value", width=250, anchor="w")
        tree.pack(fill="both", expand=True, padx=12, pady=8)
        self.pages[f"{title}:tree"] = tree
        footer = ttk.Frame(parent)
        footer.pack(fill="x", padx=12, pady=(0, 8))
        label = ttk.Label(footer, text="Waiting for telemetry")
        label.pack(side="left")
        self.pages[f"{title}:time"] = label
        ttk.Button(footer, text="Request GOSTM", command=lambda value=title: self._request_page_gostm(value)).pack(side="right")

    def _build_image_display(self, parent: tk.Misc, secondary: bool = False) -> None:
        ttk.Label(parent, text="Communication Image Display", style="BlueTitle.TLabel", anchor="center").pack(fill="x", pady=7)
        label = tk.Label(parent, text="No image received", bg=DEEP_NAVY, fg=WHITE, font=("Segoe UI", 12), relief="sunken", bd=3)
        label.pack(fill="both", expand=True, padx=12, pady=8)
        if secondary:
            self.secondary_image_label = label
        ttk.Label(parent, textvariable=self.image_meta_var, anchor="center").pack(fill="x", padx=10, pady=(0, 7))

    def _build_image_data_page(self, parent: tk.Misc) -> None:
        ttk.Label(parent, text="Image Data", style="BlueTitle.TLabel", anchor="center").pack(fill="x", pady=7)
        self.image_data_text = scrolledtext.ScrolledText(parent, bg=DEEP_NAVY, fg=WHITE, font=("Consolas", 9), wrap="word")
        self.image_data_text.pack(fill="both", expand=True, padx=12, pady=8)
        self.image_data_text.insert("end", "No image has been downloaded.\n")
        self.image_data_text.configure(state="disabled")

    def _build_camera_page(self, parent: tk.Misc) -> None:
        top = ttk.Frame(parent)
        top.pack(fill="x", padx=10, pady=8)
        ttk.Label(top, text="Camera Payload", style="BlueTitle.TLabel").pack(side="left")
        ttk.Label(top, textvariable=self.wifi_state_var).pack(side="right")
        controls = ttk.Frame(parent)
        controls.pack(fill="x", padx=12)
        items = (
            ("Resolution", ttk.Combobox(controls, textvariable=self.camera_resolution_var, values=tuple(x[0] for x in CAMERA_RESOLUTIONS), state="readonly", width=22)),
            ("JPEG quality (0–63)", tk.Spinbox(controls, from_=0, to=63, textvariable=self.camera_quality_var, width=7, bg=WHITE)),
            ("Effect", ttk.Combobox(controls, textvariable=self.camera_effect_var, values=tuple(x[0] for x in CAMERA_EFFECTS), state="readonly", width=16)),
            ("Brightness (-2–2)", tk.Spinbox(controls, from_=-2, to=2, textvariable=self.camera_brightness_var, width=7, bg=WHITE)),
        )
        for index, (label, widget) in enumerate(items):
            ttk.Label(controls, text=label, style="Section.TLabel").grid(row=index // 2 * 2, column=index % 2, sticky="w", padx=5, pady=(3, 0))
            widget.grid(row=index // 2 * 2 + 1, column=index % 2, sticky="ew", padx=5, pady=(0, 5))
        controls.columnconfigure(0, weight=1)
        controls.columnconfigure(1, weight=1)
        ttk.Checkbutton(controls, text="Flash", variable=self.camera_flash_var).grid(row=4, column=0, sticky="w", padx=5, pady=3)

        buttons = ttk.Frame(parent)
        buttons.pack(fill="x", padx=12, pady=5)
        ttk.Button(buttons, text="Build CIMG", command=self.prepare_cimg).pack(side="left", expand=True, fill="x")
        ttk.Button(buttons, text="Capture & Fetch", command=self.capture_and_fetch).pack(side="left", expand=True, fill="x", padx=5)
        ttk.Button(buttons, text="GIMG", command=lambda: self._send_named("GIMG", "Payload", b"")).pack(side="left", expand=True, fill="x")

        wifi = ttk.LabelFrame(parent, text="Payload Wi-Fi — SSID AAST")
        wifi.pack(fill="x", padx=12, pady=6)
        ttk.Label(wifi, text="ESP URL").pack(side="left", padx=(7, 3), pady=6)
        ttk.Entry(wifi, textvariable=self.wifi_url_var, width=24).pack(side="left", fill="x", expand=True, pady=6)
        ttk.Button(wifi, text="Check", command=self.check_wifi).pack(side="left", padx=4)
        ttk.Button(wifi, text="Download", command=self.download_wifi_image).pack(side="left", padx=4)
        ttk.Checkbutton(wifi, text="Auto", variable=self.wifi_auto_var, command=self._toggle_auto_wifi).pack(side="left", padx=5)

        self.camera_image_label = tk.Label(parent, text="Connect the laptop to AAST and press Check", bg=DEEP_NAVY, fg=WHITE, font=("Segoe UI", 11), relief="sunken", bd=3)
        self.camera_image_label.pack(fill="both", expand=True, padx=12, pady=7)
        ttk.Label(parent, textvariable=self.image_meta_var, anchor="center").pack(fill="x", padx=10, pady=(0, 7))

    def _build_schedule_page(self, parent: tk.Misc) -> None:
        title = ttk.Frame(parent)
        title.pack(fill="x", padx=14, pady=(9, 3))
        tk.Label(title, text="■", bg=CREAM, fg=NAVY, font=("Segoe UI", 11, "bold")).pack(side="left")
        ttk.Label(title, text="Time tagged commands", style="Section.TLabel", anchor="center").pack(side="left", fill="x", expand=True)
        tk.Label(title, text="■", bg=CREAM, fg=NAVY, font=("Segoe UI", 11, "bold")).pack(side="right")
        status = ttk.Frame(parent)
        status.pack(fill="x", padx=14, pady=4)
        ttk.Button(status, text="Time Tagged Mode", command=lambda: self._apply_preset("SM", b"\x07", "Core-OBC")).grid(row=0, column=0, rowspan=2, padx=5, sticky="nsew")
        ttk.Label(status, text="Time Tagged Time stamp", foreground=RED).grid(row=0, column=1, sticky="w")
        ttk.Entry(status, textvariable=self.schedule_time_var, width=22).grid(row=1, column=1, sticky="ew", padx=(0, 5))
        ttk.Label(status, text="OBC Time", foreground=BLUE).grid(row=0, column=2, sticky="w")
        tk.Label(status, textvariable=self.satellite_time_var, bg="#163F08", fg=WHITE, relief="sunken", bd=2, justify="left", anchor="w", padx=5).grid(row=1, column=2, sticky="ew")
        status.columnconfigure(1, weight=1)
        status.columnconfigure(2, weight=1)
        editor = ttk.Frame(parent)
        editor.pack(fill="x", padx=10, pady=5)
        ttk.Label(editor, text="SSP TX Time Tagged Frame", foreground=RED).grid(row=0, column=0, sticky="w")
        ttk.Label(editor, text="Time tagged cmd", foreground=RED).grid(row=0, column=1, sticky="w")
        ttk.Label(editor, text="Targeted subsystem ID", foreground=RED).grid(row=0, column=2, sticky="w")
        tk.Label(editor, textvariable=self.frame_preview_var, bg=DEEP_NAVY, fg=WHITE, relief="sunken", bd=2, font=("Consolas", 8), anchor="w", padx=4).grid(row=1, column=0, sticky="ew", padx=(0, 5))
        self.schedule_command_combo = ttk.Combobox(editor, textvariable=self.command_var, values=tuple(x.display for x in COMMANDS), state="readonly", width=22)
        self.schedule_command_combo.grid(row=1, column=1, sticky="ew", padx=5)
        self.schedule_destination_combo = ttk.Combobox(editor, textvariable=self.destination_var, values=tuple(x.name for x in nodes_for(SatelliteProfile.SPACE_KEYS)), state="readonly", width=16)
        self.schedule_destination_combo.grid(row=1, column=2, sticky="ew", padx=5)
        ttk.Button(editor, text="Add", command=self.add_schedule).grid(row=1, column=3, padx=5)
        for index in range(3):
            editor.columnconfigure(index, weight=1)
        columns = ("time", "command", "destination", "data", "status")
        self.schedule_tree = ttk.Treeview(parent, columns=columns, show="headings")
        widths = (145, 125, 115, 190, 90)
        for column, width in zip(columns, widths):
            self.schedule_tree.heading(column, text=column.title())
            self.schedule_tree.column(column, width=width, anchor="w")
        self.schedule_tree.pack(fill="both", expand=True, padx=10, pady=6)
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Button(row, text="Remove selected", command=self.remove_schedule).pack(side="left")
        ttk.Button(row, text="Set +10 seconds", command=lambda: self.schedule_time_var.set((datetime.now() + timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S"))).pack(side="right")

    def _build_faq_page(self, parent: tk.Misc) -> None:
        ttk.Label(parent, text="CubeSat GCS Help", style="BlueTitle.TLabel").pack(anchor="w", padx=28, pady=(10, 3))
        questions = {
            "Choose your Question :)": "Select a question above. The operator quick guide remains available below.",
            "How do I connect direct SSP?": "Select Subsystems / RS-485, use 115200 baud, choose the USB/NI RS-485 COM port, then CONNECT. Verify A/B polarity and a common ground.",
            "How do I receive a Wi-Fi image?": "Connect Windows to AAST (password 12345678). Use Capture & Fetch for SM → CIMG ACK → TIMG ACK → HTTP image download from 192.168.4.1.",
            "Why did a command return NACK?": "Read RX_CMD and the NACK data byte in Main Page. Confirm the destination is in the required mode and the command data length matches the command definition.",
            "How do I replay a LabVIEW session?": "Choose File → Replay a saved log. The CTE extracts only hexadecimal byte lines and preserves SSP frames split over multiple lines.",
        }
        faq_choice = tk.StringVar(value=next(iter(questions)))
        chooser = ttk.Combobox(parent, textvariable=faq_choice, values=tuple(questions), state="readonly")
        chooser.pack(fill="x", padx=28, pady=(0, 5))
        answer = tk.StringVar(value=questions[faq_choice.get()])
        chooser.bind("<<ComboboxSelected>>", lambda _event: answer.set(questions[faq_choice.get()]))
        tk.Label(parent, textvariable=answer, bg=WHITE, fg=INK, relief="sunken", bd=2, anchor="nw", justify="left", wraplength=590, padx=14, pady=12).pack(fill="x", padx=28, pady=5)
        photo = self._lv_reference_crop(parent, "FAQs.png", (60, 180, 490, 608), (330, 300))
        if photo:
            photo.pack(pady=4)
        guide = (
            "OPERATOR QUICK GUIDE\n\n"
            "1. Direct subsystem SSP: 115200 baud over RS-485. Ground RF: HC-12 at 9600 baud using either legacy AX.25 or native SSP.\n\n"
            "2. Select the Space Keys or Grad Project contract, then select the SSP CRC compiled into the nodes. Auto detect is useful for historical receive logs.\n\n"
            "3. PING returns ACK. CIMG returns one ACK after capture. TIMG returns one ACK after publishing the framebuffer over Wi-Fi; JPEG bytes do not travel on Serial0.\n\n"
            "4. Save logs and telemetry CSVs from File. Replay accepts LabVIEW logs, including SSP frames split across lines.\n\n"
            "5. RS-485 hardware: /RE GPIO 4, DE GPIO 0, common ground, correct A/B polarity, and 120-ohm termination at the two physical ends only.\n\n"
            "Support: https://spacekeys.net/contact-us\n\n"
            f"{APP_NAME} v{APP_VERSION}"
        )
        text = scrolledtext.ScrolledText(parent, bg=CREAM, fg=INK, font=("Segoe UI", 10), wrap="word", padx=18, pady=15)
        text.pack(fill="both", expand=True, padx=10, pady=10)
        text.insert("1.0", guide)
        text.configure(state="disabled")

    def _build_status_bar(self) -> None:
        bar = tk.Frame(self, bg=DEEP_NAVY, height=27)
        bar.pack(side="bottom", fill="x", padx=5, pady=(0, 4))
        self.status_connection = tk.Label(bar, textvariable=self.connection_var, bg=DEEP_NAVY, fg=RED, font=("Segoe UI", 9, "bold"), padx=8)
        self.status_connection.pack(side="left")
        self.status_counts = tk.Label(bar, text="TX 0  |  RX 0  |  CRC errors 0", bg=DEEP_NAVY, fg=WHITE, font=("Consolas", 9), padx=8)
        self.status_counts.pack(side="left")
        tk.Label(bar, text=f"{APP_NAME} v{APP_VERSION}", bg=DEEP_NAVY, fg="#BFC5DB", font=("Segoe UI", 8), padx=8).pack(side="right")

    def _select_command(self) -> None:
        commands = self._commands()
        spec = self._command_by_display().get(self.command_var.get(), commands[0])
        self.description_label.configure(text=f"{spec.name}: {spec.description}")
        self.data_var.set(format_hex(spec.default_data))
        self.preview_selected(silent=True)

    def _apply_preset(self, name: str, data: bytes | None = None, destination: str | None = None) -> None:
        spec = self._command_named(name)
        if spec is None:
            messagebox.showinfo(
                "Command not in selected contract",
                f"{name} is not defined by the {self._profile().value} command table.",
                parent=self,
            )
            return
        self.command_var.set(spec.display)
        if destination:
            self.destination_var.set(destination)
        self.data_var.set(format_hex(spec.default_data if data is None else data))
        self._select_command()
        if data is not None:
            self.data_var.set(format_hex(data))
            self.preview_selected(silent=True)

    def _current_frame(self) -> tuple[CommandSpec, SSPFrame]:
        spec = self._command_by_display()[self.command_var.get()]
        destination = self._node_by_name()[self.destination_var.get()].identifier
        source = self._node_by_name()[self.source_var.get()].identifier
        data = hex_bytes(self.data_var.get())
        return spec, SSPFrame(destination, source, spec.identifier, data)

    def preview_selected(self, silent: bool = False) -> bytes | None:
        try:
            _, frame = self._current_frame()
            raw = frame.encode(self._tx_crc())
            wire = self._build_wire_frame(raw)
            self.frame_preview_var.set(format_hex(wire))
            return wire
        except Exception as exc:
            self.frame_preview_var.set("Invalid frame")
            if not silent:
                messagebox.showerror("Cannot build frame", str(exc), parent=self)
            return None

    def send_selected(self) -> None:
        try:
            spec, frame = self._current_frame()
        except Exception as exc:
            messagebox.showerror("Cannot build frame", str(exc), parent=self)
            return
        self._send_frame(spec, frame)

    def _ground_ax25_enabled(self) -> bool:
        return self.mode_var.get() == "Ground Station / AX.25"

    def _build_wire_frame(self, ssp_raw: bytes, send_sequence: int | None = None) -> bytes:
        """Wrap an SSP packet in the exact fixed-length mission AX.25 envelope."""
        if not self._ground_ax25_enabled():
            return ssp_raw
        if send_sequence is None:
            try:
                self.ax25_tx_sequence = int(self.sequence_var.get()) % 6
            except (TypeError, ValueError, tk.TclError):
                self.ax25_tx_sequence = 0
            sequence = self.ax25_tx_sequence
        else:
            sequence = int(send_sequence)
        destination = self.ax25_dest_var.get().strip().upper() or SATELLITE_CALLSIGN
        source = self.ax25_source_var.get().strip().upper() or GROUND_CALLSIGN
        if len(destination) != 6 or len(source) != 6:
            raise ValueError("Legacy AX.25 destination and source must each be six ASCII characters")
        return build_ax25_i(
            ssp_raw,
            send_sequence=sequence % 6,
            receive_sequence=self.ax25_rx_sequence % 6,
            destination=destination,
            source=source,
            destination_a7=SATELLITE_DEST_A7,
            source_a7=GROUND_SOURCE_A7,
        )

    def _advance_tx_sequence(self) -> None:
        if self._ground_ax25_enabled():
            self.ax25_tx_sequence = (self.ax25_tx_sequence + 1) % 6
            self.sequence_var.set(self.ax25_tx_sequence)

    def _send_named(self, name: str, destination: str, data: bytes) -> None:
        spec = self._command_named(name)
        if spec is None:
            self._log("ERROR", f"{name} is not defined by the {self._profile().value} command table.")
            return
        frame = SSPFrame(self._node_by_name()[destination].identifier, self._node_by_name()[self.source_var.get()].identifier, spec.identifier, data)
        self._send_frame(spec, frame)

    def _send_frame(self, spec: CommandSpec, frame: SSPFrame) -> None:
        algorithm = self._tx_crc()
        if self._ground_ax25_enabled() and spec.name == "INIT":
            # COMM accepts INIT only at the beginning of the modulo-six window.
            self.ax25_tx_sequence = 0
            self.ax25_rx_sequence = 0
            self.sequence_var.set(0)
            self._log("EVENT", "AX.25 session sequence reset for INIT.")
        ssp_raw = frame.encode(algorithm)
        try:
            wire = self._build_wire_frame(ssp_raw)
        except ValueError as exc:
            messagebox.showerror("Invalid AX.25 address", str(exc), parent=self)
            return
        if not self.transport.connected and not self.demo_var.get():
            messagebox.showwarning("Not connected", "Connect a COM port or enable Demo mode before sending.", parent=self)
            return
        try:
            if self.transport.connected:
                self.transport.tx_delay_ms = max(100 if self._ground_ax25_enabled() else 0, self.delay_var.get())
                self.transport.send(wire)
        except Exception as exc:
            self._log("ERROR", f"Transmit failed: {exc}")
            return
        self.last_tx_raw = ssp_raw
        self.last_tx_wire = wire
        self.tx_count += 1
        self._advance_tx_sequence()
        to_name = self._node_by_id().get(frame.destination)
        from_name = self._node_by_id().get(frame.source)
        self._log("TX", f"Transmitted Frame: {{CMD Name: {spec.name}, From: {from_name.name if from_name else frame.source}, To: {to_name.name if to_name else frame.destination}}}\n{format_hex(wire, 22)}")
        self._update_counts()
        if self.demo_var.get() and not self.transport.connected:
            self._simulate_response(frame)

    def send_custom(self) -> None:
        try:
            raw = hex_bytes(self.custom_var.get())
            decoded = SSPFrame.decode(raw, CRCAlgorithm.AUTO)
        except Exception as exc:
            messagebox.showerror("Invalid custom frame", str(exc), parent=self)
            return
        if not decoded.valid:
            if not messagebox.askyesno("CRC mismatch", "The custom SSP frame has an invalid CRC. Send it anyway?", parent=self):
                return
        if (self._ground_ax25_enabled() and self._profile() is SatelliteProfile.SPACE_KEYS
                and decoded.command == 0x01):
            self.ax25_tx_sequence = 0
            self.ax25_rx_sequence = 0
            self.sequence_var.set(0)
            self._log("EVENT", "AX.25 session sequence reset for custom INIT.")
        try:
            wire = self._build_wire_frame(raw)
        except ValueError as exc:
            messagebox.showerror("Invalid AX.25 address", str(exc), parent=self)
            return
        if self.transport.connected:
            try:
                self.transport.tx_delay_ms = max(100 if self._ground_ax25_enabled() else 0, self.delay_var.get())
                self.transport.send(wire)
            except Exception as exc:
                self._log("ERROR", f"Custom transmit failed: {exc}")
                return
        elif not self.demo_var.get():
            messagebox.showwarning("Not connected", "Connect a COM port or enable Demo mode.", parent=self)
            return
        self.last_tx_raw = raw
        self.last_tx_wire = wire
        self.tx_count += 1
        self._advance_tx_sequence()
        self._log("TX", f"Custom frame\n{format_hex(wire, 22)}")
        self._update_counts()
        if self.demo_var.get() and not self.transport.connected:
            self._simulate_response(decoded)

    def _simulate_response(self, request: SSPFrame) -> None:
        algo = self._tx_crc()
        def queue_response(frame: SSPFrame, delay_ms: int) -> None:
            raw = frame.encode(algo)
            if self._ground_ax25_enabled():
                raw = build_ax25_i(
                    raw,
                    send_sequence=self.demo_ax25_sequence,
                    receive_sequence=self.ax25_tx_sequence,
                    destination=GROUND_CALLSIGN,
                    source=SATELLITE_CALLSIGN,
                    destination_a7=SATELLITE_DEST_A7,
                    source_a7=GROUND_SOURCE_A7,
                )
                self.demo_ax25_sequence = (self.demo_ax25_sequence + 1) % 6
            self.after(delay_ms, lambda value=raw: self.events.put(("serial", value)))

        if self._profile() is SatelliteProfile.GRAD_PROJECT and request.command == 0x0C:
            queue_response(SSPFrame(request.source, request.destination, 0x02, b"\x0C"), 180)
            self.after(250, self._load_demo_image)
            return
        if self._profile() is SatelliteProfile.SPACE_KEYS and request.command == 0x2B:
            pend = SSPFrame(request.source, request.destination, 0x04, b"\x2B")
            ack = SSPFrame(request.source, request.destination, 0x02, b"\x2B")
            queue_response(pend, 180)
            queue_response(ack, 900)
            self.after(950, self._load_demo_image)
            return
        if ((self._profile() is SatelliteProfile.GRAD_PROJECT and request.command == 0x07)
                or (self._profile() is SatelliteProfile.SPACE_KEYS and request.command == 0x25)):
            nodes = self._node_by_name()
            adcs_address = nodes["ADCS"].identifier
            payload_address = nodes["Payload"].identifier
            eps_address = nodes["Power / EPS"].identifier
            if self._profile() is SatelliteProfile.GRAD_PROJECT:
                response_command = 0x47
                if request.destination == adcs_address:
                    data = bytearray(52)
                    data[0] = adcs_address
                    for offset, value in zip((14, 18, 22, 26, 30, 34, 38, 42, 46), (0.1, -0.2, 1.0, 0.42, -0.18, 1.03, 23.4, -8.7, 41.2)):
                        struct.pack_into("<f", data, offset, value + random.uniform(-0.03, 0.03))
                elif request.destination == payload_address:
                    data = bytearray(57)
                    data[0] = payload_address
                    data[14] = 1
                    data[15] = 1
                    data[16:20] = (107878).to_bytes(4, "little")
                elif request.destination == eps_address:
                    data = bytearray(42)
                    data[0] = eps_address
                    for offset, value in zip(range(14, 42, 2), (8200, 420, 0, 0, 0, 0, 7400, 230, 5000, 610, 12000, 320, 0x1F, 27)):
                        data[offset:offset + 2] = int(value).to_bytes(2, "little")
                elif request.destination == nodes["Core-COM"].identifier:
                    data = bytearray(23)
                    data[0] = request.destination
                else:
                    data = bytearray(55)
                    data[0] = request.destination
                response = SSPFrame(request.source, request.destination, response_command, bytes(data))
            elif request.destination == 0x05:
                data = bytearray(50)
                # LabVIEW fixed-point field is a signed Q8.8 value.
                struct.pack_into(">h", data, 19, int(45 * 256))
                for offset, value in zip((23, 27, 31, 35, 39, 43), (0.42, -0.18, 1.03, 23.4, -8.7, 41.2)):
                    struct.pack_into("<f", data, offset, value + random.uniform(-0.03, 0.03))
                response = SSPFrame(request.source, request.destination, 0xE5, bytes(data))
            elif request.destination == 0x07:
                data = bytearray(64)
                data[16] = 0x04
                data[27:31] = (107878).to_bytes(4, "big")
                data[32:34] = (481).to_bytes(2, "big")
                response = SSPFrame(request.source, request.destination, 0xE5, bytes(data))
            elif request.destination == 0x0D:
                response = SSPFrame(request.source, request.destination, 0x65, bytes((25, 0, 27, 0, 28, 0, 18, 0, 0, 0, 2)))
            elif request.destination == 0x09:
                response = SSPFrame(request.source, request.destination, 0xE5, struct.pack("<hhhhhhhh", 7400, -230, 8200, 420, 5000, 610, 27, 31))
            else:
                response = SSPFrame(request.source, request.destination, 0xE5, b"\x00" * 16)
        elif request.command == 0x06 and request.destination == self._node_by_name()["ADCS"].identifier:
            response = SSPFrame(request.source, request.destination, 0x46, struct.pack("<fff", 0.45, -0.22, 1.06))
        else:
            response = SSPFrame(request.source, request.destination, 0x02, bytes((request.command,)))
        queue_response(response, 180)

    def _process_wire_bytes(self, data: bytes) -> None:
        if self._ground_ax25_enabled():
            parsed_ax25 = self.ax25_parser.feed(data)
            for ax in parsed_ax25:
                address_state = "PASS" if ax.address_valid else "FAIL"
                fcs_state = "PASS" if ax.valid_fcs else "FAIL"
                self.ax25_status_var.set(
                    f"From {ax.source}-{ax.source_ssid}  To {ax.destination}-{ax.destination_ssid}\n"
                    f"{ax.control_description}  Address {address_state}  FCS {fcs_state}\n"
                    f"Payload: {format_hex(ax.payload[:32]) if ax.payload else '—'}"
                )
                if not ax.valid_fcs:
                    self.ax25_bad_count += 1
                    self._log("ERROR", f"Rejected legacy AX.25 frame: FCS mismatch\n{format_hex(ax.raw, 22)}")
                    continue
                if not ax.address_valid:
                    self.ax25_bad_count += 1
                    self._log("ERROR", f"Rejected legacy AX.25 frame: unexpected callsigns\n{format_hex(ax.raw[:16])}")
                    continue
                if ax.address_direction == "uplink":
                    self.echo_count += int(ax.raw == self.last_tx_wire)
                    self._log(
                        "ECHO",
                        f"Ground-to-satellite AX.25 {'local echo' if ax.raw == self.last_tx_wire else 'record'} ignored\n"
                        f"{format_hex(ax.raw, 22)}",
                    )
                    continue
                self.ax25_count += 1
                copy_number = self.ax25_repeat_tracker.observe(ax.raw)
                copy_label = f"copy {copy_number}/{self.ax25_repeat_tracker.expected_copies}"
                self.ax25_status_var.set(
                    f"From {ax.source}-{ax.source_ssid}  To {ax.destination}-{ax.destination_ssid}\n"
                    f"{ax.control_description}  {copy_label}  Address {address_state}  FCS {fcs_state}\n"
                    f"Payload: {format_hex(ax.payload[:32]) if ax.payload else '—'}"
                )
                if copy_number > 1:
                    self.ax25_repeat_count += 1
                    self._log(
                        "COPY",
                        f"Repeated physical AX.25 {copy_label}; logical frame already processed\n"
                        f"{format_hex(ax.raw, 22)}",
                    )
                    continue
                self.ax25_logical_count += 1
                if ax.frame_type != "I":
                    self._log(
                        "RX",
                        f"AX.25 {ax.control_description} received ({copy_label})\n"
                        f"{format_hex(ax.raw, 22)}",
                    )
                    continue
                self._log(
                    "RX",
                    f"AX.25 {ax.control_description} received ({copy_label})\n"
                    f"{format_hex(ax.raw, 22)}",
                )
                if ax.payload_error:
                    self.ax25_bad_count += 1
                    self._log("ERROR", f"Rejected AX.25 I frame: {ax.payload_error}\n{format_hex(ax.raw, 22)}")
                    continue
                if ax.send_sequence == self.ax25_rx_sequence:
                    self.ax25_rx_sequence = (self.ax25_rx_sequence + 1) % 6
                elif ax.send_sequence != (self.ax25_rx_sequence - 1) % 6:
                    self._log("ERROR", f"AX.25 receive-window gap: expected N(S)={self.ax25_rx_sequence}, received {ax.send_sequence}")
                    self.ax25_rx_sequence = (int(ax.send_sequence or 0) + 1) % 6
                for frame in self.ssp_parser.feed(ax.payload):
                    self._handle_frame(frame)
            self.ax25_counter_label.configure(
                text=(f"Physical: {self.ax25_count}     Logical: {self.ax25_logical_count}     "
                      f"Copies: {self.ax25_repeat_count}     Rejected: {self.ax25_bad_count}")
            )
            return
        for frame in self.ssp_parser.feed(data):
            self._handle_frame(frame)

    def _handle_frame(self, frame: SSPFrame) -> None:
        if frame.raw == self.last_tx_raw:
            self.echo_count += 1
            self._log("ECHO", f"SSP local echo ignored\n{format_hex(frame.raw, 22)}")
            return
        self.rx_count += 1
        if not frame.valid:
            self.bad_crc_count += 1
        self._update_frame_analysis(frame)
        source = self._node_by_id().get(frame.source)
        command = command_name(frame.command, frame.source, frame.data, self._profile())
        self._log("RX" if frame.valid else "ERROR", f"Received Frame: {{CMD: {command}, From: {source.name if source else f'0x{frame.source:02X}'}, CRC: {frame.crc_label}}}\n{format_hex(frame.raw, 22)}")
        if frame.valid:
            # Space Keys uses CIMG then TIMG. The Grad ICD has no TIMG command,
            # so its CIMG transaction publishes the new framebuffer directly.
            payload_address = self._node_by_name()["Payload"].identifier
            cimg_id = 0x0C if self._profile() is SatelliteProfile.GRAD_PROJECT else 0x2B
            if (self._capture_fetch_pending and frame.source == payload_address and
                    frame.command in (0x02, 0x03) and frame.data[:1] == bytes((cimg_id,))):
                self._capture_fetch_pending = False
                if frame.command == 0x02:
                    if self._profile() is SatelliteProfile.GRAD_PROJECT:
                        self._timg_fetch_pending = False
                        self._log("EVENT", "ICD CIMG acknowledged; downloading the published JPEG over Wi-Fi.")
                        self.wifi_auto_var.set(True)
                        self._toggle_auto_wifi()
                        self.after(250 + max(0, self.delay_var.get()),
                                   lambda: self.download_wifi_image(silent=True))
                    else:
                        self._timg_fetch_pending = True
                        self._log("EVENT", "CIMG acknowledged; sending TIMG to publish the framebuffer over Wi-Fi.")
                        self.after(250 + max(0, self.delay_var.get()),
                                   lambda: self._send_named("TIMG", "Payload", b""))
                else:
                    self._timg_fetch_pending = False
                    self._log("ERROR", "Payload rejected CIMG; TIMG was not sent.")
            elif (self._timg_fetch_pending and frame.source == payload_address and
                  frame.command in (0x02, 0x03) and frame.data[:1] == b"\x28"):
                self._timg_fetch_pending = False
                if frame.command == 0x02:
                    self._log("EVENT", "TIMG acknowledged; downloading the JPEG directly from ESP32-CAM Wi-Fi.")
                    self.wifi_auto_var.set(True)
                    self._toggle_auto_wifi()
                    self.after(250 + max(0, self.delay_var.get()),
                               lambda: self.download_wifi_image(silent=True))
                else:
                    self._log("ERROR", "Payload rejected TIMG; the camera image was not published.")
            record = self.telemetry.add(frame)
            self._update_telemetry(record)
            self._record_telemetry(record)
        self._update_counts()

    def _update_frame_analysis(self, frame: SSPFrame) -> None:
        source = self._node_by_id().get(frame.source)
        destination = self._node_by_id().get(frame.destination)
        command = command_name(frame.command, frame.source, frame.data, self._profile())
        self.rx_source_var.set(source.name if source else f"0x{frame.source:02X}")
        self.rx_destination_var.set(destination.name if destination else f"0x{frame.destination:02X}")
        self.rx_cmd_var.set(command)
        self.rx_length_var.set(str(len(frame.data)))
        self.rx_crc_var.set(frame.crc_label)
        self.rx_data_var.set(format_hex(frame.data, 12) or "—")
        self.ssp_frame_var.set(format_hex(frame.raw, 14))
        self.crc_led.set(frame.valid)
        if frame.command == 0x02:
            self.ack_led.set(True, GREEN)
        elif frame.command == 0x03:
            self.ack_led.set(False)
        elif frame.command == 0x04:
            self.ack_led.set(True, AMBER)
        else:
            self.ack_led.set(None)

    def _update_telemetry(self, record: TelemetryRecord) -> None:
        led_name = {"Power / EPS": "Power", "COMM": "Comm", "Core-COM": "Comm", "Core-OBC": "OBC", "Space Env.": "Space Env."}.get(record.subsystem, record.subsystem)
        if led_name in self.subsystem_leds:
            self.subsystem_leds[led_name].set(True)
            self.after(1800, lambda name=led_name: self.subsystem_leds.get(name) and self.subsystem_leds[name].set(None))
        page_key = {"Power / EPS": "EPS", "Space Env.": "Space Environment", "Core-OBC": "OBC", "Core-COM": "COMM"}.get(record.subsystem, record.subsystem)
        tree = self.pages.get(f"{page_key}:tree")
        if isinstance(tree, ttk.Treeview):
            tree.delete(*tree.get_children())
            for key, value in record.values.items():
                tree.insert("", "end", values=(key, value if value is not None else "—"))
            label = self.pages.get(f"{page_key}:time")
            if isinstance(label, ttk.Label):
                label.configure(text=f"Last update: {record.timestamp:%Y-%m-%d %H:%M:%S.%f}"[:-3])
        if record.subsystem == "Payload" and "Payload mode" in record.values:
            self.wifi_state_var.set(f"Telemetry: {record.values['Payload mode']}")
        if record.subsystem == "Satellite":
            self.status_frame_var.set(format_hex(record.frame.data, 12))
            self.satellite_mode_var.set(str(record.values.get("Satellite mode", "—")))
            self.power_source_var.set(str(record.values.get("Power source", "—")))
            ticks = record.values.get("Status timestamp (low 32-bit OBT)")
            if isinstance(ticks, int):
                total_ms = ticks * 10
                seconds, milliseconds = divmod(total_ms, 1000)
                minutes, seconds = divmod(seconds, 60)
                hours, minutes = divmod(minutes, 60)
                self.satellite_time_var.set(f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}\nOBT")
            voltage = record.values.get("Supply voltage (raw)")
            current = record.values.get("Supply current (raw)")
            if isinstance(voltage, int) and isinstance(current, int):
                self.dissipated_power_var.set(f"V={voltage}  I={current} (raw)")
            for key, led_name in (("Power", "Power"), ("COMM", "Comm"), ("OBC", "OBC"), ("ADCS", "ADCS"), ("Payload", "Payload"), ("Space Env.", "Space Env.")):
                state = record.values.get(key)
                if state in ("ON", "OFF"):
                    self.subsystem_leds[led_name].set(state == "ON")
        self._update_labview_page(record)

    def _request_page_gostm(self, page: str) -> None:
        destination = {"OBC": "Core-OBC", "EPS": "Power / EPS", "Space Environment": "Space Env.", "COMM": "Core-COM", "ADCS": "ADCS"}.get(page, page)
        self._send_named("GOSTM", destination, b"")

    def refresh_ports(self) -> None:
        ports = available_ports()
        self.port_map = {item.display: item.device for item in ports}
        self.port_combo.configure(values=tuple(self.port_map))
        if ports and self.port_var.get() not in self.port_map:
            self.port_var.set(ports[0].display)
        if not ports:
            self.port_var.set("")

    def toggle_connection(self) -> None:
        if self.transport.connected:
            self.transport.disconnect()
            self._set_connection_state(False)
            self._log("EVENT", "Serial connection closed.")
            return
        display = self.port_var.get()
        if display not in self.port_map:
            messagebox.showwarning("No COM port", "Choose a detected COM port first.", parent=self)
            return
        try:
            self.transport.connect(self.port_map[display], int(self.baud_var.get()))
        except Exception as exc:
            messagebox.showerror("Connection failed", str(exc), parent=self)
            self._log("ERROR", f"Cannot open {self.port_map[display]}: {exc}")
            return
        self._set_connection_state(True)
        self._log("EVENT", f"Connected to {self.port_map[display]} at {self.baud_var.get()} baud ({self.cable_var.get()}).")

    def _set_connection_state(self, connected: bool) -> None:
        self.connection_var.set("CONNECTED" if connected else "DISCONNECTED")
        self.status_connection.configure(fg=GREEN if connected else RED)
        self.connect_button.configure(text="DISCONNECT" if connected else "CONNECT")

    def _drain_events(self) -> None:
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == "serial":
                    self._process_wire_bytes(value)
                elif event == "serial_error":
                    self._set_connection_state(False)
                    self._log("ERROR", f"Serial link error: {value}")
                elif event == "wifi_status":
                    self._wifi_status_in_progress = False
                    self._accept_wifi_status(value)
                elif event == "wifi_image":
                    self._accept_wifi_image(*value)
                elif event == "wifi_error":
                    self._wifi_download_in_progress = False
                    self.wifi_state_var.set("Wi-Fi error")
                    self._log("ERROR", value)
        except Empty:
            pass
        self.after(40, self._drain_events)

    def _log(self, kind: str, message: str) -> None:
        timestamp = datetime.now().strftime("%a, %b %d, %Y  %I:%M:%S.%f %p")[:-3]
        block = f"[{timestamp}]  {kind}\n{message}\n{'-' * 58}\n"
        self.log_records.append(block)
        selected = self.log_filter_var.get()
        if selected == "All" or selected == kind or (selected == "EVENT" and kind in ("READY", "ECHO")):
            self.log_text.configure(state="normal")
            self.log_text.insert("end", block, kind)
            if self.auto_scroll_var.get():
                self.log_text.see("end")

    def clear_log(self) -> None:
        self.log_records.clear()
        self.log_text.delete("1.0", "end")

    def save_log(self) -> None:
        default = operator_dir("Logs") / f"LOG_{datetime.now():%Y-%m-%d_%H-%M-%S}_GND_ESK.txt"
        selected = filedialog.asksaveasfilename(parent=self, title="Save session log", initialdir=default.parent, initialfile=default.name, defaultextension=".txt", filetypes=(("Text log", "*.txt"), ("All files", "*.*")))
        if not selected:
            return
        Path(selected).write_text("\n".join(self.log_records), encoding="utf-8")
        self._log("EVENT", f"Session log saved to {selected}")

    def replay_log(self) -> None:
        selected = filedialog.askopenfilename(parent=self, title="Replay LabVIEW/Python log", initialdir=operator_dir("Logs"), filetypes=(("Log files", "*.txt *.log"), ("All files", "*.*")))
        if not selected:
            return
        text = Path(selected).read_text(encoding="utf-8", errors="ignore")
        if self._ground_ax25_enabled():
            ax_frames = [
                frame for frame in extract_ax25_frames_from_log(text)
                if frame.address_direction == "downlink"
            ]
            if ax_frames:
                self._log("EVENT", f"Replaying {len(ax_frames)} legacy AX.25 frames from {selected}")
                for index, ax in enumerate(ax_frames):
                    self.after(index * max(20, self.delay_var.get()), lambda value=ax.raw: self._process_wire_bytes(value))
                return
        # LabVIEW logs contain dates and prose that also look hexadecimal.
        # Extract only byte-only lines so split SSP frames remain contiguous.
        frames = extract_ssp_frames_from_log(text)
        if not frames:
            messagebox.showwarning("No frames", "No complete SSP frames were found in this file.", parent=self)
            return
        self._log("EVENT", f"Replaying {len(frames)} SSP frames from {selected}")
        for index, frame in enumerate(frames):
            self.after(index * max(20, self.delay_var.get()), lambda value=frame: self._handle_frame(value))

    def toggle_recording(self) -> None:
        if self._recording:
            self._stop_recording()
            return
        default = operator_dir("Telemetry") / f"TLM_{datetime.now():%Y-%m-%d_%H-%M-%S}.csv"
        selected = filedialog.asksaveasfilename(parent=self, title="Record telemetry CSV", initialdir=default.parent, initialfile=default.name, defaultextension=".csv", filetypes=(("CSV", "*.csv"),))
        if not selected:
            return
        self._recording_path = Path(selected)
        self._recording_file = self._recording_path.open("w", encoding="utf-8", newline="")
        self._recording_writer = csv.writer(self._recording_file)
        self._recording_writer.writerow(("timestamp", "subsystem", "source", "destination", "command", "crc", "parameter", "value", "raw_frame"))
        self._recording = True
        self.record_button.configure(text="Stop CSV")
        self._log("EVENT", f"Telemetry recording started: {selected}")

    def _stop_recording(self) -> None:
        if self._recording_file:
            self._recording_file.flush()
            self._recording_file.close()
        self._recording_file = None
        self._recording_writer = None
        self._recording = False
        self.record_button.configure(text="Record CSV")
        self._log("EVENT", f"Telemetry recording stopped: {self._recording_path}")

    def _record_telemetry(self, record: TelemetryRecord) -> None:
        if not self._recording_writer:
            return
        common = (record.timestamp.isoformat(timespec="milliseconds"), record.subsystem, record.frame.source, record.frame.destination, record.frame.command, record.frame.crc_label)
        for key, value in record.values.items():
            self._recording_writer.writerow(common + (key, value, format_hex(record.frame.raw)))
        if self._recording_file:
            self._recording_file.flush()

    def prepare_cimg(self) -> None:
        resolution = dict(CAMERA_RESOLUTIONS)[self.camera_resolution_var.get()]
        effect = dict(CAMERA_EFFECTS)[self.camera_effect_var.get()]
        quality = max(0, min(63, self.camera_quality_var.get()))
        brightness = max(-2, min(2, self.camera_brightness_var.get())) & 0xFF
        data = bytes((resolution, quality, int(self.camera_flash_var.get()), effect, brightness))
        self._apply_preset("CIMG", data, "Payload")
        self.main_notebook.select(self.pages["Payload"])

    def capture_and_fetch(self) -> None:
        self.prepare_cimg()
        data = hex_bytes(self.data_var.get())
        # Pause polling while the previous publication is invalidated. It
        # resumes after TIMG ACK, preventing a stale-image download.
        self.wifi_auto_var.set(False)
        self._toggle_auto_wifi()
        self._capture_fetch_pending = True
        self._timg_fetch_pending = False
        mode = b"\x03" if self._profile() is SatelliteProfile.GRAD_PROJECT else b"\x04"
        self._send_named("SM", "Payload", mode)
        self.after(250 + max(0, self.delay_var.get()), lambda: self._send_named("CIMG", "Payload", data))
        workflow = ("normal mode → CIMG ACK → direct Wi-Fi download"
                    if self._profile() is SatelliteProfile.GRAD_PROJECT
                    else "image mode → CIMG ACK → TIMG ACK → direct Wi-Fi download")
        self._log("EVENT", f"Camera workflow started: {workflow}.")

    def check_wifi(self) -> None:
        if self._wifi_status_in_progress or self._wifi_download_in_progress:
            return
        self.wifi_state_var.set("Checking…")
        url = normalize_base_url(self.wifi_url_var.get())
        self.wifi_url_var.set(url)
        self._wifi_status_in_progress = True
        threading.Thread(target=self._wifi_status_worker, args=(url,), daemon=True).start()

    def _wifi_status_worker(self, url: str) -> None:
        status = PayloadWifiClient(url).status()
        self.events.put(("wifi_status", status))

    def _accept_wifi_status(self, status: Any) -> None:
        if not status.reachable:
            self.wifi_state_var.set("AAST not reachable")
            if not self.wifi_auto_var.get():
                self._log("ERROR", f"Payload Wi-Fi is not reachable: {status.error}")
            return
        values = status.values
        ready = bool(values.get("image_ready"))
        capture_ready = bool(values.get("capture_ready"))
        size = values.get("image_size", values.get("file_size", 0))
        clients = values.get("clients", "—")
        revision = values.get("image_revision")
        state = "Image ready" if ready else "Capture ready" if capture_ready else "Waiting"
        self.wifi_state_var.set(f"Online • {state} • {size} bytes • clients {clients}")
        signature = self._wifi_image_signature(values)
        if (ready and
                not self._wifi_download_in_progress and
                (signature != self._last_image_signature or self._image_photo is None)):
            self._last_image_signature = signature
            self._last_image_revision = revision
            self.download_wifi_image(silent=True)

    def download_wifi_image(self, silent: bool = False) -> None:
        if self._wifi_download_in_progress:
            return
        url = normalize_base_url(self.wifi_url_var.get())
        self.wifi_url_var.set(url)
        self.wifi_state_var.set("Downloading image…")
        destination = operator_dir("Images") / f"pic_{datetime.now():%Y-%m-%d_%H-%M-%S}_PL_CTE_ESK.jpg"
        self._wifi_download_in_progress = True
        threading.Thread(target=self._wifi_download_worker, args=(url, destination, silent), daemon=True).start()

    def _wifi_download_worker(self, url: str, destination: Path, silent: bool) -> None:
        try:
            client = PayloadWifiClient(url)
            path, image = client.download(destination)
            status = client.status()
            self.events.put(("wifi_image", (path, image, silent, status)))
        except Exception as exc:
            self.events.put(("wifi_error", f"Image download failed: {exc}"))

    def _accept_wifi_image(self, path: Path, image: Image.Image, silent: bool, status: Any) -> None:
        self._wifi_download_in_progress = False
        if status.reachable:
            self._last_image_signature = self._wifi_image_signature(status.values)
            self._last_image_revision = status.values.get("image_revision")
        self._display_image(image, path)
        self.wifi_state_var.set("Image downloaded")
        self._log("EVENT", f"Payload image saved: {path}")
        if not silent:
            self.main_notebook.select(self.pages["Payload"])

    @staticmethod
    def _wifi_image_signature(values: dict[str, Any]) -> tuple[Any, ...]:
        return (
            values.get("image_revision"),
            values.get("image_size"),
            values.get("file_size"),
            bool(values.get("image_ready")),
            bool(values.get("capture_ready")),
            values.get("image_error"),
        )

    def _display_image(self, image: Image.Image, path: Path | None = None) -> None:
        display = image.copy()
        # CAM PL uses a real preview viewport.  Do not set a tiny explicit
        # Label height: Tk interprets it as pixels after an image is assigned.
        # A VGA capture therefore remains visible at its full 640 x 480 size.
        display.thumbnail((740, 500), Image.Resampling.LANCZOS)
        self._image_photo = ImageTk.PhotoImage(display)
        camera_labels = getattr(self, "_lv_camera_image_labels", ())
        if camera_labels:
            for label in camera_labels:
                label.configure(image=self._image_photo, text="")
        elif hasattr(self, "camera_image_label"):
            self.camera_image_label.configure(image=self._image_photo, text="")
        secondary = image.copy()
        secondary.thumbnail((700, 500), Image.Resampling.LANCZOS)
        self._image_photo_secondary = ImageTk.PhotoImage(secondary)
        self.secondary_image_label.configure(image=self._image_photo_secondary, text="")
        size = path.stat().st_size if path and path.exists() else 0
        self.image_meta_var.set(f"{image.width} × {image.height} px  •  {size:,} bytes  •  {path or 'Demo image'}")
        raw = path.read_bytes() if path and path.exists() else b""
        self._lv_fill_image_grid(raw)
        comm_image_values = self._lv_values.get("Comm IMG Display", {})
        if "Image size" in comm_image_values:
            comm_image_values["Image size"].set(str(size))
        if hasattr(self, "image_data_text"):
            self.image_data_text.configure(state="normal")
            self.image_data_text.delete("1.0", "end")
            self.image_data_text.insert("end", f"File: {path or 'Bundled demonstration image'}\nDimensions: {image.width} x {image.height}\nSize: {size:,} bytes\n\nJPEG DATA (first 4096 bytes)\n{format_hex(raw[:4096], 24) if raw else 'Available after a Wi-Fi download.'}")
            self.image_data_text.configure(state="disabled")

    def _load_demo_image(self) -> None:
        try:
            path = resource_path("assets/demo_payload.jpg")
            image = Image.open(path)
            image.load()
            self._display_image(image, path)
            self.wifi_state_var.set("Demo image ready")
        except Exception as exc:
            self._log("ERROR", f"Cannot load bundled demo image: {exc}")

    def _toggle_auto_wifi(self) -> None:
        if self._auto_wifi_job:
            self.after_cancel(self._auto_wifi_job)
            self._auto_wifi_job = None
        if self.wifi_auto_var.get():
            self._auto_wifi_poll()

    def _auto_wifi_poll(self) -> None:
        if not self.wifi_auto_var.get():
            self._auto_wifi_job = None
            return
        if not self._wifi_download_in_progress:
            self.check_wifi()
        self._auto_wifi_job = self.after(1500, self._auto_wifi_poll)

    def add_schedule(self) -> None:
        try:
            when = datetime.strptime(self.schedule_time_var.get().strip(), "%Y-%m-%d %H:%M:%S")
            data = hex_bytes(self.data_var.get())
        except ValueError as exc:
            messagebox.showerror("Invalid scheduled command", str(exc), parent=self)
            return
        if when <= datetime.now():
            messagebox.showwarning("Time is in the past", "Choose a future local date and time.", parent=self)
            return
        spec = self._command_by_display()[self.command_var.get()]
        identifier = str(self._next_schedule_id)
        self._next_schedule_id += 1
        entry = {"when": when, "spec": spec, "destination": self.destination_var.get(), "source": self.source_var.get(), "data": data, "status": "Armed"}
        self._scheduled[identifier] = entry
        self.schedule_tree.insert("", "end", iid=identifier, values=(when.strftime("%Y-%m-%d %H:%M:%S"), spec.name, entry["destination"], format_hex(data), "Armed"))

    def remove_schedule(self) -> None:
        for identifier in self.schedule_tree.selection():
            self._scheduled.pop(identifier, None)
            self.schedule_tree.delete(identifier)

    def _run_scheduler(self) -> None:
        now = datetime.now()
        for identifier, entry in tuple(self._scheduled.items()):
            if entry["status"] == "Armed" and entry["when"] <= now:
                entry["status"] = "Sent"
                self.schedule_tree.set(identifier, "status", "Sent")
                frame = SSPFrame(self._node_by_name()[entry["destination"]].identifier, self._node_by_name()[entry["source"]].identifier, entry["spec"].identifier, entry["data"])
                self._send_frame(entry["spec"], frame)
        self.after(250, self._run_scheduler)

    def _reset_parsers(self) -> None:
        algorithm = CRCAlgorithm(self.crc_var.get())
        self.ssp_parser = SSPStreamParser(algorithm)
        self.ax25_parser = AX25StreamParser()
        self.ax25_repeat_tracker.reset()
        self.ax25_rx_sequence = 0
        self._log("EVENT", f"Receive parsers reset; SSP CRC mode is {algorithm.value}.")

    def _update_counts(self) -> None:
        self.status_counts.configure(text=f"TX {self.tx_count}  |  RX {self.rx_count}  |  Echo {self.echo_count}  |  CRC errors {self.bad_crc_count}")

    def _tick_clock(self) -> None:
        self.clock_var.set(datetime.now().strftime("%a, %b %d, %Y\n%I:%M:%S %p"))
        self.after(250, self._tick_clock)

    def _load_settings(self) -> None:
        path = app_data_dir() / "settings.json"
        if not path.exists():
            return
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
            for variable, key in ((self.cable_var, "cable"), (self.baud_var, "baud"), (self.mode_var, "mode"), (self.crc_var, "crc"), (self.satellite_var, "satellite"), (self.wifi_url_var, "wifi_url"), (self.source_var, "source"), (self.destination_var, "destination")):
                if key in values:
                    variable.set(values[key])
        except Exception:
            pass

    def _save_settings(self) -> None:
        values = {"cable": self.cable_var.get(), "baud": self.baud_var.get(), "mode": self.mode_var.get(), "crc": self.crc_var.get(), "satellite": self.satellite_var.get(), "wifi_url": self.wifi_url_var.get(), "source": self.source_var.get(), "destination": self.destination_var.get()}
        try:
            (app_data_dir() / "settings.json").write_text(json.dumps(values, indent=2), encoding="utf-8")
        except OSError:
            pass

    def show_about(self) -> None:
        messagebox.showinfo(f"About {APP_NAME}", f"{APP_NAME} v{APP_VERSION}\n\nPython replacement for the EgSA LabVIEW CTE ground station.\n\nSSP • AX.25 • RS-485 • telemetry • ESP32 Wi-Fi image receiver", parent=self)

    def _on_close(self) -> None:
        self._save_settings()
        if self._recording:
            self._stop_recording()
        self.transport.disconnect()
        self.destroy()


def self_test() -> int:
    algorithms = (CRCAlgorithm.CCITT, CRCAlgorithm.IBM)
    for algorithm in algorithms:
        raw = SSPFrame(0x07, 0x01, 0x00).encode(algorithm)
        decoded = SSPFrame.decode(raw, algorithm)
        assert decoded.valid and decoded.destination == 0x07
        parser = SSPStreamParser(algorithm)
        frames = parser.feed(b"noise" + raw[:3]) + parser.feed(raw[3:] + raw)
        assert len(frames) == 2 and all(frame.valid for frame in frames)
    ax_payload = SSPFrame(7, 1, 0).encode(CRCAlgorithm.CCITT)
    ax = build_ax25_i(
        ax_payload,
        send_sequence=0,
        receive_sequence=0,
        destination=SATELLITE_CALLSIGN,
        source=GROUND_CALLSIGN,
        destination_a7=SATELLITE_DEST_A7,
        source_a7=GROUND_SOURCE_A7,
    )
    assert len(ax) == AX25_PACKET_SIZE and ax[0] == 0x7E and ax[-1] == 0x7E
    parser = AX25StreamParser()
    decoded_ax = parser.feed(ax)
    assert len(decoded_ax) == 1 and decoded_ax[0].valid_fcs and decoded_ax[0].payload == ax_payload
    print(f"{APP_NAME} v{APP_VERSION} self-test passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--self-test", action="store_true")
    arguments = parser.parse_args()
    if arguments.self_test:
        return self_test()
    app = SpaceKeysApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
