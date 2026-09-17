"""LabVIEW-compatible telemetry pages used by the Python CTE.

The widgets in this module deliberately mirror the operator-facing grouping of
the original CTE while keeping the protocol decoder and transport independent.
Every displayed field is backed by a named telemetry value; unknown bytes stay
visible in the raw strip and are never silently reinterpreted.
"""

from __future__ import annotations

import math
from pathlib import Path
import sys
import tkinter as tk
from tkinter import ttk
from typing import Any

from catalog import CAMERA_EFFECTS, CAMERA_RESOLUTIONS, SatelliteProfile
from PIL import Image, ImageTk


CREAM = "#F4EED9"
CREAM_2 = "#EDE5CA"
NAVY = "#2B305B"
DEEP_NAVY = "#171B3F"
PANEL_NAVY = "#222952"
RED = "#FF233E"
BLUE = "#4B43FF"
GREEN = "#38C84A"
AMBER = "#FFB020"
INK = "#111426"
WHITE = "#FFFFFF"


class LabviewTelemetryPages:
    """Mixin containing the mission telemetry views and their update routing."""

    def _lv_init(self) -> None:
        self._lv_values: dict[str, dict[str, tk.StringVar]] = {}
        self._lv_raw: dict[str, tk.StringVar] = {}
        self._lv_updated: dict[str, tk.StringVar] = {}
        self._lv_leds: dict[str, tuple[tk.Canvas, int]] = {}
        self._lv_comm_tree: ttk.Treeview | None = None
        self._lv_rw_canvas: tk.Canvas | None = None
        self._lv_rw_needle: int | None = None
        self._lv_rw_value_text: int | None = None
        self._lv_img_complete: tuple[tk.Canvas, int] | None = None
        self._lv_image_grid: ttk.Treeview | None = None
        self._lv_photos: list[ImageTk.PhotoImage] = []
        self._lv_profile_views: dict[str, tuple[ttk.Frame, ttk.Frame]] = {}
        self._lv_active_profile: SatelliteProfile | None = None
        self._lv_camera_image_labels: list[tk.Label] = []

    def _lv_var(self, page: str, key: str, default: str = "0") -> tk.StringVar:
        values = self._lv_values.setdefault(page, {})
        if key not in values:
            values[key] = tk.StringVar(value=default)
        return values[key]

    @staticmethod
    def _lv_scroll_page(parent: tk.Misc) -> ttk.Frame:
        canvas = tk.Canvas(parent, bg=CREAM, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        inner.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        return inner

    def _lv_profile_pages(self, parent: tk.Misc, page_name: str) -> tuple[ttk.Frame, ttk.Frame]:
        """Create independently switchable Space Keys and Grad telemetry views."""
        space_host = ttk.Frame(parent)
        grad_host = ttk.Frame(parent)
        self._lv_profile_views[page_name] = (space_host, grad_host)
        space_host.pack(fill="both", expand=True)
        return self._lv_scroll_page(space_host), self._lv_scroll_page(grad_host)

    def _lv_set_profile(self, profile: SatelliteProfile) -> None:
        """Show the telemetry widgets that match the selected wire contract."""
        profile = SatelliteProfile(profile)
        show_grad = profile is SatelliteProfile.GRAD_PROJECT
        for space_host, grad_host in self._lv_profile_views.values():
            space_host.pack_forget()
            grad_host.pack_forget()
            (grad_host if show_grad else space_host).pack(fill="both", expand=True)

        if profile != self._lv_active_profile:
            waiting = f"Waiting for {profile.value} telemetry"
            for variable in self._lv_raw.values():
                variable.set(waiting)
            for variable in self._lv_updated.values():
                variable.set("Waiting for telemetry")
            for values in self._lv_values.values():
                for variable in values.values():
                    variable.set("—")
            for identity in self._lv_leds:
                self._lv_set_led(identity, None)
            if self._lv_comm_tree:
                for command in self._lv_comm_tree.get_children():
                    self._lv_comm_tree.item(command, values=(0, 0))
            self._lv_active_profile = profile

        refresh = getattr(self, "_refresh_scroll_extent", None)
        if callable(refresh):
            self.after_idle(refresh)

    @staticmethod
    def _lv_title(parent: tk.Misc, text: str) -> None:
        row = tk.Frame(parent, bg=CREAM)
        row.pack(fill="x", padx=14, pady=(10, 5))
        tk.Label(row, text="■", bg=CREAM, fg=RED, font=("Segoe UI", 12, "bold")).pack(side="left")
        tk.Label(row, text=text, bg=CREAM, fg=NAVY, font=("Segoe UI", 12, "bold")).pack(side="left", expand=True)
        tk.Label(row, text="■", bg=CREAM, fg=RED, font=("Segoe UI", 12, "bold")).pack(side="right")
        ttk.Separator(parent).pack(fill="x", padx=35, pady=(0, 4))

    def _lv_reference_crop(self, parent: tk.Misc, filename: str,
                           box: tuple[int, int, int, int], size: tuple[int, int]) -> tk.Label | None:
        """Load only a hardware photograph from a supplied LabVIEW reference."""
        packaged = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets" / "cte_reference" / filename
        source = Path(__file__).resolve().parent.parent / "CTE Images" / filename
        path = packaged if packaged.exists() else source
        try:
            image = Image.open(path).crop(box)
            image.thumbnail(size, Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(image)
            self._lv_photos.append(photo)
            return tk.Label(parent, image=photo, bg=CREAM, relief="ridge", bd=2)
        except Exception:
            return None

    def _lv_raw_strip(self, parent: tk.Misc, page: str, label: str) -> None:
        self._lv_raw.setdefault(page, tk.StringVar(value="Waiting for telemetry"))
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=16, pady=(4, 6))
        ttk.Label(row, text=label, style="Section.TLabel").pack(anchor="w")
        tk.Label(
            row, textvariable=self._lv_raw[page], bg=CREAM_2, fg=INK,
            font=("Consolas", 8), anchor="w", justify="left", relief="sunken",
            bd=3, padx=6, pady=7, wraplength=610,
        ).pack(fill="x", pady=(2, 0))

    def _lv_field(self, parent: tk.Misc, page: str, key: str, label: str | None = None,
                  row: int = 0, column: int = 0, width: int = 13,
                  default: str = "0", color: str = INK) -> None:
        box = ttk.Frame(parent)
        box.grid(row=row, column=column, sticky="ew", padx=5, pady=4)
        ttk.Label(box, text=label or key, style="Section.TLabel", wraplength=145).pack(anchor="w")
        tk.Label(
            box, textvariable=self._lv_var(page, key, default), bg=CREAM_2, fg=color,
            relief="sunken", bd=2, anchor="w", padx=5, width=width,
            font=("Segoe UI", 9, "bold"),
        ).pack(fill="x")

    def _lv_led(self, parent: tk.Misc, identity: str, label: str, row: int, column: int) -> None:
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=column, padx=5, pady=4, sticky="n")
        ttk.Label(frame, text=label, style="Section.TLabel").pack()
        canvas = tk.Canvas(frame, width=28, height=25, bg=CREAM, highlightthickness=0)
        item = canvas.create_oval(5, 3, 23, 21, fill="#D5D5D5", outline="#777777", width=2)
        canvas.pack()
        self._lv_leds[identity] = (canvas, item)

    def _lv_set_led(self, identity: str, state: bool | None, active_color: str = GREEN) -> None:
        led = self._lv_leds.get(identity)
        if led:
            led[0].itemconfigure(led[1], fill=active_color if state else (RED if state is False else "#D5D5D5"))

    def _lv_footer(self, parent: tk.Misc, page: str, destination: str) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=16, pady=(3, 10))
        self._lv_updated.setdefault(page, tk.StringVar(value="Waiting for telemetry"))
        ttk.Label(row, textvariable=self._lv_updated[page]).pack(side="left")
        ttk.Button(row, text="Request GOSTM", command=lambda: self._send_named("GOSTM", destination, b"")).pack(side="right")

    def _lv_grad_header(self, parent: tk.Misc, page: str, title: str, table: str) -> None:
        self._lv_title(parent, title)
        tk.Label(
            parent,
            text=f"Grad Project telemetry • ICD v3.1 {table} • little-endian fields",
            bg=DEEP_NAVY,
            fg=WHITE,
            font=("Segoe UI", 9, "bold"),
            padx=8,
            pady=6,
        ).pack(fill="x", padx=16, pady=(2, 5))
        self._lv_raw_strip(parent, page, "Received telemetry frame")
        header = ttk.LabelFrame(parent, text="Telemetry Header (indices 0–3)")
        header.pack(fill="x", padx=16, pady=6)
        for index, (key, label) in enumerate((
            ("Subsystem address", "Subsystem address"),
            ("Mode", "Mode"),
            ("OBC time", "OBC time (u64)"),
            ("RTC", "RTC (u32)"),
        )):
            self._lv_field(header, page, key, label, 0, index, 15, "—")
            header.columnconfigure(index, weight=1)

    def _lv_grad_group(
        self,
        parent: tk.Misc,
        page: str,
        title: str,
        fields: tuple[tuple[str, str], ...],
        columns: int = 3,
    ) -> None:
        group = ttk.LabelFrame(parent, text=title)
        group.pack(fill="x", padx=16, pady=6)
        for index, (key, label) in enumerate(fields):
            self._lv_field(
                group,
                page,
                key,
                label,
                index // columns,
                index % columns,
                18,
                "—",
            )
        for column in range(columns):
            group.columnconfigure(column, weight=1)

    def _lv_build_grad_obc_page(self, page: tk.Misc) -> None:
        self._lv_grad_header(page, "OBC", "Grad Project — On Board Computer Telemetry", "Table 12")
        self._lv_grad_group(page, "OBC", "Navigation", (
            ("Satellite mode", "Satellite mode"),
            ("Longitude", "Longitude (s32)"),
            ("Latitude", "Latitude (s32)"),
            ("Velocity North", "Velocity North (u32)"),
            ("Velocity East", "Velocity East (u32)"),
            ("Velocity Down", "Velocity Down (u32)"),
            ("iTOW", "iTOW (u32)"),
        ))
        self._lv_grad_group(page, "OBC", "SSP Communication", (
            ("Total received SSP frames", "Total received SSP frames"),
            ("Total transmitted SSP frames", "Total transmitted SSP frames"),
            ("Last executed command", "Last executed command"),
        ))
        self._lv_grad_group(page, "OBC", "Subsystem Stored Records", (
            ("EPS SRecords (index 13)", "EPS SRecords — index 13"),
            ("EPS SRecords (index 14, ICD duplicate)", "EPS SRecords — index 14"),
            ("OBC SRecords", "OBC SRecords"),
            ("ADCS SRecords", "ADCS SRecords"),
            ("COMM SRecords", "COMM SRecords"),
            ("Payload SRecords", "Payload SRecords"),
        ))
        self._lv_footer(page, "OBC", "Core-OBC")

    def _lv_build_grad_eps_page(self, page: tk.Misc) -> None:
        self._lv_grad_header(page, "EPS", "Grad Project — Electrical Power Telemetry", "Table 11 hardware extension")
        self._lv_grad_group(page, "EPS", "Switched Subsystem Rails", (
            ("OBC voltage (mV)", "OBC voltage (mV)"),
            ("OBC current (mA)", "OBC current (mA)"),
            ("COMM voltage (mV)", "COMM voltage (mV)"),
            ("COMM current (mA)", "COMM current (mA)"),
            ("Payload voltage (mV)", "Payload voltage (mV)"),
            ("Payload current (mA)", "Payload current (mA)"),
            ("ADCS voltage (mV)", "ADCS voltage (mV)"),
            ("ADCS current (mA)", "ADCS current (mA)"),
        ), columns=4)
        self._lv_grad_group(page, "EPS", "Battery", (
            ("Battery voltage (mV)", "Battery voltage (mV)"),
            ("Battery current (mA)", "Battery current (mA)"),
            ("Current calibration", "Current calibration"),
        ))
        self._lv_grad_group(page, "EPS", "Power Control Pin Levels", (
            ("OBC control pin", "OBC control"),
            ("COMM control pin", "COMM control"),
            ("Payload control pin", "Payload control"),
            ("ADCS 5V control pin", "ADCS 5V control"),
            ("ADCS 12V control pin", "ADCS 12V control"),
        ), columns=5)
        self._lv_grad_group(page, "EPS", "Acquisition Status", (
            ("Switch control bitmap", "Control bitmap"),
            ("Measurement valid bitmap", "Valid bitmap"),
            ("ADC saturation bitmap", "Saturation bitmap"),
            ("Sample counter", "Sample counter"),
        ), columns=4)
        self._lv_footer(page, "EPS", "Power / EPS")

    def _lv_build_grad_adcs_page(self, page: tk.Misc) -> None:
        self._lv_grad_header(page, "ADCS", "Grad Project — ADCS Telemetry", "Table 13")
        self._lv_grad_group(page, "ADCS", "Accelerometer", (
            ("Accelerometer X", "Accelerometer X"),
            ("Accelerometer Y", "Accelerometer Y"),
            ("Accelerometer Z", "Accelerometer Z"),
        ))
        self._lv_grad_group(page, "ADCS", "Gyroscope", (
            ("Gyroscope X", "Gyroscope X"),
            ("Gyroscope Y", "Gyroscope Y"),
            ("Gyroscope Z", "Gyroscope Z"),
        ))
        self._lv_grad_group(page, "ADCS", "Magnetometer", (
            ("Magnetometer X", "Magnetometer X"),
            ("Magnetometer Y", "Magnetometer Y"),
            ("Magnetometer Z", "Magnetometer Z"),
        ))
        self._lv_grad_group(page, "ADCS", "Actuator", (
            ("Reaction wheel speed (rpm)", "Reaction wheel speed (RPM)"),
        ), columns=1)
        self._lv_footer(page, "ADCS", "ADCS")

    def _lv_build_grad_comm_page(self, page: tk.Misc) -> None:
        self._lv_grad_header(page, "COMM", "Grad Project — Communication Telemetry", "Table 14")
        self._lv_grad_group(page, "COMM", "Communication Status", (
            ("Core-COM mode", "Communication mode"),
            ("Core-COM address", "Communication address"),
            ("Radio", "Radio"),
            ("Total received frames", "Total received frames"),
            ("Correct received frames", "Correct received frames"),
            ("Last received command", "Last received command"),
            ("Communication rate", "Communication rate"),
            ("RF output power", "RF output power"),
        ))
        self._lv_footer(page, "COMM", "Core-COM")

    def _lv_build_grad_environment_page(self, page: tk.Misc) -> None:
        self._lv_title(page, "Grad Project — Space Environment")
        tk.Label(
            page,
            text=(
                "The Grad Project ICD v3.1 telemetry contract does not define a "
                "Space Environment subsystem record. No Space Keys byte offsets "
                "are applied while Grad Project is selected."
            ),
            bg=CREAM_2,
            fg=INK,
            relief="sunken",
            bd=2,
            justify="left",
            wraplength=620,
            padx=18,
            pady=18,
        ).pack(fill="x", padx=24, pady=20)

    def _lv_build_grad_payload_page(self, page: tk.Misc) -> None:
        self._lv_grad_header(page, "Payload", "Grad Project — Payload Telemetry", "Table 15")
        self._lv_grad_group(page, "Payload", "Images", (
            ("Payload mode", "Payload mode"),
            ("Images count", "Images count"),
            ("Image 1 number", "Image 1 number"),
            ("Image 1 size (bytes)", "Image 1 size (bytes)"),
            ("Image 1 parameters", "Image 1 parameters (u64)"),
            ("Image 2 number", "Image 2 number"),
            ("Image 2 size (bytes)", "Image 2 size (bytes)"),
            ("Image 2 parameters", "Image 2 parameters (u64)"),
        ))
        self._lv_grad_group(page, "Payload", "Image Transmission", (
            ("Image transfer rate", "Image transfer rate"),
            ("RF power", "RF power"),
            ("Total image frames", "Total image frames"),
            ("Transmitted image frames", "Transmitted image frames"),
        ), columns=2)
        self._lv_grad_group(page, "Payload", "Relay Communication", (
            ("Ground stations in relay", "Ground stations in relay"),
            ("Relay mode", "Relay mode"),
            ("Relay total RX frames", "Relay total RX frames"),
            ("Relay correct RX frames", "Relay correct RX frames"),
            ("Relay total TX frames", "Relay total TX frames"),
        ))

        wifi = ttk.LabelFrame(page, text="Payload Wi-Fi — SSID AAST")
        wifi.pack(fill="x", padx=16, pady=6)
        ttk.Label(wifi, text="ESP URL").grid(row=0, column=0, padx=5, pady=4)
        ttk.Entry(wifi, textvariable=self.wifi_url_var, width=24).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(wifi, text="Check", command=self.check_wifi).grid(row=0, column=2, padx=4)
        ttk.Button(wifi, text="Download", command=self.download_wifi_image).grid(row=0, column=3, padx=4)
        ttk.Checkbutton(wifi, text="Auto", variable=self.wifi_auto_var, command=self._toggle_auto_wifi).grid(row=0, column=4, padx=5)
        wifi.columnconfigure(1, weight=1)
        buttons = ttk.Frame(page)
        buttons.pack(fill="x", padx=16, pady=4)
        ttk.Button(buttons, text="Build CIMG", command=self.prepare_cimg).pack(side="left", expand=True, fill="x")
        ttk.Button(buttons, text="Capture & Fetch", command=self.capture_and_fetch).pack(side="left", expand=True, fill="x", padx=5)
        ttk.Button(buttons, text="CLR IMG", command=self._clear_image_views).pack(side="left", expand=True, fill="x")
        preview = tk.Frame(page, bg="#D4D4D4", height=420, relief="sunken", bd=3)
        preview.pack(fill="x", padx=16, pady=6)
        preview.pack_propagate(False)
        image_label = tk.Label(
            preview,
            text="Connect the laptop to AAST and press Check",
            bg="#D4D4D4",
            fg=INK,
            font=("Segoe UI", 11),
            anchor="center",
        )
        image_label.pack(fill="both", expand=True)
        self._lv_camera_image_labels.append(image_label)
        ttk.Label(page, textvariable=self.wifi_state_var, anchor="center").pack(fill="x")
        ttk.Label(page, textvariable=self.image_meta_var, anchor="center").pack(fill="x", padx=10, pady=(0, 7))
        self._lv_footer(page, "Payload", "Payload")

    def _labview_build_obc_page(self, parent: tk.Misc) -> None:
        page, grad_page = self._lv_profile_pages(parent, "OBC")
        self._lv_title(page, "On Board Computer Subsystem")
        self._lv_raw_strip(page, "OBC", "OBC TLM")

        upper = ttk.Frame(page)
        upper.pack(fill="x", padx=12, pady=4)
        power = ttk.LabelFrame(upper, text="pwrSwStatus")
        power.grid(row=0, column=0, sticky="nsew", padx=4)
        for index, (key, label) in enumerate((
            ("PL 5V", "PL_5V"), ("Space Env. 5V", "SEnv_5V"),
            ("ADCS 5V", "ADCS_5V"), ("Space Env. 3.3V", "SEnv_3v3"),
            ("ADCS 3.3V", "ADCS_3v3"),
        )):
            self._lv_led(power, f"obc:{key}", label, index // 3, index % 3)
        self._lv_field(power, "OBC", "Latch reading", row=2, column=0, width=10)

        serial = ttk.LabelFrame(upper, text="Serial Communication status")
        serial.grid(row=0, column=1, sticky="nsew", padx=4)
        fields = (
            ("Last executed command", "LastExCmd"),
            ("COMM response command", "commResponseCode"),
            ("COMM retry count", "commErrorCount"),
            ("COMM error type", "commErrorType"),
        )
        for index, (key, label) in enumerate(fields):
            self._lv_field(serial, "OBC", key, label, index // 2, index % 2, 15, "—")

        sensors = ttk.LabelFrame(upper, text="OBC Sensors")
        sensors.grid(row=0, column=2, sticky="nsew", padx=4)
        self._lv_field(sensors, "OBC", "OBC temperature (raw)", "OBC temperature", 0, 0, 13)
        self._lv_field(sensors, "OBC", "OBC current (raw)", "OBC current", 1, 0, 13)
        for column in range(3):
            upper.columnconfigure(column, weight=1)

        mode = ttk.Frame(page)
        mode.pack(fill="x", padx=16, pady=6)
        self._lv_field(mode, "OBC", "Satellite mode", row=0, column=0, width=18, default="Standby")
        self._lv_field(mode, "OBC", "OBT seconds", "OBC Time (s)", 0, 1, 18)
        self._lv_field(mode, "OBC", "OBC activity bitmap", "Activity bitmap", 0, 2, 14, "0x00")
        for column in range(3):
            mode.columnconfigure(column, weight=1)

        timed = ttk.LabelFrame(page, text="Time Tagged CMD TLM")
        timed.pack(fill="x", padx=16, pady=6)
        for index, (key, label) in enumerate((
            ("Inserted timed command past due", "Inserted time cmd past due"),
            ("Timed command past due", "Timed cmds past due"),
            ("Last executed timed command", "Last executed timed cmd"),
            ("Timed command execution count", "TimedCmdExeCount"),
        )):
            self._lv_field(timed, "OBC", key, label, 0, index, 14)
            timed.columnconfigure(index, weight=1)
        self._lv_footer(page, "OBC", "Core-OBC")
        self._lv_build_grad_obc_page(grad_page)

    def _labview_build_eps_page(self, parent: tk.Misc) -> None:
        page, grad_page = self._lv_profile_pages(parent, "EPS")
        self._lv_title(page, "Electrical Power Subsystem")
        self._lv_raw_strip(page, "EPS", "EPS telemetry")
        volts = ttk.LabelFrame(page, text="Voltage Reading")
        volts.pack(fill="x", padx=22, pady=(8, 7))
        for index, key in enumerate(("Real V_SA", "Real V_BAT", "Real V_12V", "Real V_5V", "Real V_3v3")):
            self._lv_field(volts, "EPS", key, row=0, column=index, width=10)
            volts.columnconfigure(index, weight=1)
        currents = ttk.LabelFrame(page, text="Current Sense")
        currents.pack(fill="x", padx=22, pady=7)
        for index, key in enumerate(("Real I_SA", "Real I_BAT", "Real I_12V", "Real I_5V", "Real I_3v3")):
            self._lv_field(currents, "EPS", key, row=0, column=index, width=10)
            currents.columnconfigure(index, weight=1)
        temps = ttk.LabelFrame(page, text="Temperature Sensor")
        temps.pack(fill="x", padx=80, pady=18)
        self._lv_field(temps, "EPS", "Real T1_BAT", "Real T1_BAT", 0, 0, 16)
        self._lv_field(temps, "EPS", "Real T2_BAT", "Real T2_BAT", 0, 1, 16)
        temps.columnconfigure(0, weight=1)
        temps.columnconfigure(1, weight=1)
        ttk.Label(page, text="Values are exact telemetry ADC words; apply the board calibration used by your electrical setup.", foreground="#555577").pack(pady=4)
        self._lv_footer(page, "EPS", "Power / EPS")
        self._lv_build_grad_eps_page(grad_page)

    def _labview_build_environment_page(self, parent: tk.Misc) -> None:
        page, grad_page = self._lv_profile_pages(parent, "Space Environment")
        self._lv_title(page, "Space Environment Subsystem")
        self._lv_raw_strip(page, "Space Environment", "Space Environment TLM")
        values = ttk.LabelFrame(page, text="Environment Sensors")
        values.pack(fill="x", padx=20, pady=18)
        for index, (key, label) in enumerate((
            ("Radiation count", "Radiation count"),
            ("Temperature sensor 1", "Temperature 1"),
            ("Temperature sensor 2", "Temperature 2"),
            ("Magnetic field raw", "Magnetic field"),
        )):
            self._lv_field(values, "Space Environment", key, label, index // 2, index % 2, 20)
            values.columnconfigure(index % 2, weight=1)
        flags = ttk.LabelFrame(page, text="Sensor Status")
        flags.pack(fill="x", padx=80, pady=10)
        for index, (key, label) in enumerate((
            ("Thermal flag", "Thermal"), ("Magnetic flag", "Magnetic"), ("Radiation flag", "Radiation"),
        )):
            self._lv_field(flags, "Space Environment", key, label, 0, index, 14)
            flags.columnconfigure(index, weight=1)
        self._lv_footer(page, "Space Environment", "Space Env.")
        self._lv_build_grad_environment_page(grad_page)

    def _labview_build_comm_page(self, parent: tk.Misc) -> None:
        page, grad_page = self._lv_profile_pages(parent, "COMM")
        self._lv_title(page, "Communication Subsystem")
        self._lv_raw_strip(page, "COMM", "COMM telemetry")
        body = ttk.Frame(page)
        body.pack(fill="both", expand=True, padx=14, pady=4)
        common = ttk.LabelFrame(body, text="Common Section")
        common.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        for index, (key, label) in enumerate((
            ("Core-COM address", "Core_COMM Address"),
            ("Synchronization counter", "Sync Counter"),
            ("Core-COM mode", "Mode"),
            ("OBC UART failure count", "OBC_UART Failure Counter"),
            ("TIMG status", "TIMG Status"),
        )):
            self._lv_field(common, "COMM", key, label, index, 0, 19, "—")
        ttk.Button(common, text="Reset N(S)", command=self._reset_ax25_sequences).grid(row=6, column=0, padx=6, pady=10, sticky="ew")

        report = ttk.LabelFrame(body, text="Communication Session Reporting Section")
        report.grid(row=0, column=1, sticky="nsew")
        tree = ttk.Treeview(report, columns=("received", "delivered"), show="tree headings", height=16)
        tree.heading("#0", text="CMD")
        tree.heading("received", text="Received")
        tree.heading("delivered", text="Delivered")
        tree.column("#0", width=80, anchor="w")
        tree.column("received", width=72, anchor="center")
        tree.column("delivered", width=72, anchor="center")
        commands = ("INIT", "PING", "SM", "SSC", "SON", "SOF", "PD", "WD", "KS_ON", "KS_OFF", "STIM", "GD", "RD", "HRST", "GTIM", "GM", "GOTM", "GSTM", "GOSTM", "GIMG", "GIFN")
        for command in commands:
            tree.insert("", "end", iid=command, text=command, values=(0, 0))
        scroll = ttk.Scrollbar(report, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self._lv_comm_tree = tree
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)
        self._lv_footer(page, "COMM", "Core-COM")
        self._lv_build_grad_comm_page(grad_page)

    def _reset_ax25_sequences(self) -> None:
        self.ax25_tx_sequence = 0
        self.ax25_rx_sequence = 0
        self.sequence_var.set(0)
        self._log("EVENT", "Communication N(S) and N(R) counters reset to zero.")

    def _labview_build_adcs_page(self, parent: tk.Misc) -> None:
        page, grad_page = self._lv_profile_pages(parent, "ADCS")
        self._lv_title(page, "Attitude Determination and Control Subsystem")
        self._lv_raw_strip(page, "ADCS", "ADCS TLM")
        lower = tk.Frame(page, bg="#7D86A5", relief="sunken", bd=2)
        lower.pack(fill="both", expand=True, padx=20, pady=8)
        magnet = tk.LabelFrame(lower, text="Magnetometer (µTesla)", bg="#7D86A5", fg=NAVY, font=("Segoe UI", 10, "bold"))
        magnet.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        for index, key in enumerate(("Magnetometer X (uT)", "Magnetometer Y (uT)", "Magnetometer Z (uT)")):
            label = ("Bx", "By", "Bz")[index]
            tk.Label(magnet, text=label, bg="#7D86A5", fg=RED, font=("Segoe UI", 9, "italic")).grid(row=0, column=index, sticky="w", padx=4)
            tk.Label(magnet, textvariable=self._lv_var("ADCS", key), bg=CREAM_2, fg=INK, relief="sunken", bd=2, width=10, anchor="w").grid(row=1, column=index, padx=4)
        gyro = tk.LabelFrame(lower, text="Gyro (Degrees per second)", bg="#7D86A5", fg=NAVY, font=("Segoe UI", 10, "bold"))
        gyro.grid(row=1, column=0, sticky="nsew", padx=10, pady=10)
        for index, key in enumerate(("Gyroscope X (deg/s)", "Gyroscope Y (deg/s)", "Gyroscope Z (deg/s)")):
            label = ("wx", "wy", "wz")[index]
            tk.Label(gyro, text=label, bg="#7D86A5", fg=RED, font=("Segoe UI", 9, "italic")).grid(row=0, column=index, sticky="w", padx=4)
            tk.Label(gyro, textvariable=self._lv_var("ADCS", key), bg=CREAM_2, fg=INK, relief="sunken", bd=2, width=10, anchor="w").grid(row=1, column=index, padx=4)
        actuators = tk.Frame(lower, bg="#7D86A5")
        actuators.grid(row=2, column=0, sticky="ew", padx=8, pady=8)
        ttk.Button(actuators, text="MT OFF", command=lambda: self._send_named("WD", "ADCS", bytes((0x08, 0x00, 0x00)))).pack(side="left", expand=True, padx=6)
        ttk.Button(actuators, text="Thruster OFF", command=lambda: self._send_named("WD", "ADCS", bytes((0x06, 0x00, 0x00)))).pack(side="left", expand=True, padx=6)

        wheel = tk.LabelFrame(lower, text="Reaction Wheel — Speed (RPM)", bg="#7D86A5", fg=NAVY, font=("Segoe UI", 10, "bold"))
        wheel.grid(row=0, column=1, rowspan=3, sticky="nsew", padx=10, pady=10)
        canvas = tk.Canvas(wheel, width=250, height=170, bg=CREAM, highlightthickness=2, highlightbackground="#999999")
        canvas.pack(padx=8, pady=8)
        canvas.create_arc(30, 35, 220, 220, start=25, extent=130, style="arc", width=13, outline="#29E000")
        canvas.create_arc(30, 35, 220, 220, start=25, extent=42, style="arc", width=13, outline="#FF2020")
        canvas.create_text(25, 130, text="-200", font=("Segoe UI", 9, "bold"))
        canvas.create_text(225, 130, text="200", font=("Segoe UI", 9, "bold"))
        self._lv_rw_value_text = canvas.create_text(125, 155, text="0 rpm", font=("Segoe UI", 11, "bold"))
        self._lv_rw_canvas = canvas
        self._lv_rw_needle = canvas.create_line(125, 125, 125, 63, width=4, fill=RED, arrow="last")
        mt_row = ttk.Frame(wheel)
        mt_row.pack(fill="x", padx=8, pady=(0, 8))
        self._lv_field(mt_row, "ADCS", "MT command", "MT command", 0, 0, 15)
        mt_row.columnconfigure(0, weight=1)
        lower.columnconfigure(0, weight=2)
        lower.columnconfigure(1, weight=1)
        self._lv_footer(page, "ADCS", "ADCS")
        self._lv_build_grad_adcs_page(grad_page)

    def _labview_build_comm_image_page(self, parent: tk.Misc) -> None:
        page = self._lv_scroll_page(parent)
        self._lv_title(page, "Image data Received using RF")
        top = ttk.Frame(page)
        top.pack(fill="both", expand=True, padx=14, pady=6)
        secondary_preview = tk.Frame(top, bg="#D4D4D4", height=440, width=500, relief="sunken", bd=2)
        secondary_preview.grid(row=0, column=0, rowspan=5, sticky="nsew", padx=(0, 10))
        secondary_preview.grid_propagate(False)
        self.secondary_image_label = tk.Label(secondary_preview, text="No RF/Wi-Fi image received", bg="#D4D4D4", fg=INK, font=("Segoe UI", 11), anchor="center")
        self.secondary_image_label.pack(fill="both", expand=True)
        for row, (key, label) in enumerate((
            ("Image size", "Image Size"), ("SPI ACK", "SPI ACK"),
            ("Last frame counter", "Last Frame Counter"), ("Last Tx N(S)", "Last Tx N(S) Value"),
        )):
            self._lv_field(top, "Comm IMG Display", key, label, row, 1, 17)
        self._lv_led(top, "comm_img:complete", "IMG_Complete", 4, 1)
        top.columnconfigure(0, weight=2)
        top.columnconfigure(1, weight=1)
        top.rowconfigure(0, weight=1)
        buttons = ttk.Frame(page)
        buttons.pack(fill="x", padx=14, pady=5)
        ttk.Button(buttons, text="Auto GIMG (RF Mode only)", command=lambda: self._send_named("GIMG", "Core-COM", b"")).pack(side="left", expand=True, fill="x")
        ttk.Button(buttons, text="Save Image", command=self.download_wifi_image).pack(side="left", expand=True, fill="x", padx=5)
        ttk.Button(buttons, text="CLR IMG", command=self._clear_image_views).pack(side="left", expand=True, fill="x")
        ttk.Label(page, text="SSP phase: RF frame reconstruction is displayed when GIMG/GIFN frames are received; AX.25 changes are intentionally deferred.", foreground="#555577", wraplength=600).pack(padx=16, pady=5)
        ttk.Label(page, textvariable=self.image_meta_var, anchor="center").pack(fill="x", padx=10, pady=(0, 7))

    def _labview_build_image_data_page(self, parent: tk.Misc) -> None:
        page = self._lv_scroll_page(parent)
        self._lv_title(page, "Image Frames Received using RF")
        frame = ttk.Frame(page)
        frame.pack(fill="both", expand=True, padx=14, pady=6)
        tree = ttk.Treeview(frame, columns=tuple(f"b{i}" for i in range(8)), show="tree headings", height=18)
        tree.heading("#0", text="Frame")
        tree.column("#0", width=82, anchor="center")
        for index in range(8):
            tree.heading(f"b{index}", text=f"+{index}")
            tree.column(f"b{index}", width=55, anchor="center")
        yscroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        xscroll = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self._lv_image_grid = tree
        complete = ttk.Frame(page)
        complete.pack(fill="x", padx=14, pady=(3, 10))
        self._lv_led(complete, "image_data:complete", "Comm IMG Complete?", 0, 0)
        ttk.Label(complete, text="Rows show the downloaded image bytes in the same frame-oriented form as the LabVIEW 2D array.").grid(row=0, column=1, sticky="w", padx=12)

    def _labview_build_camera_page(self, parent: tk.Misc) -> None:
        page, grad_page = self._lv_profile_pages(parent, "Payload")
        self._lv_title(page, "Camera Payload Subsystem")
        self._lv_raw_strip(page, "Payload", "Payload Telemetry")
        status = ttk.Frame(page)
        status.pack(fill="x", padx=14, pady=3)
        for index, (key, label) in enumerate((
            ("SPI ACK", "SPI ACK"), ("Cached image size (bytes)", "Image Size"),
            ("Image error", "Image error"), ("Last image segment", "Last Frame Counter"),
            ("Payload mode", "Mode"),
        )):
            self._lv_field(status, "Payload", key, label, index // 3, index % 3, 16, "—")
            status.columnconfigure(index % 3, weight=1)

        wifi = ttk.LabelFrame(page, text="Payload Wi-Fi — SSID AAST")
        wifi.pack(fill="x", padx=14, pady=5)
        ttk.Label(wifi, text="ESP URL").grid(row=0, column=0, padx=5, pady=4)
        ttk.Entry(wifi, textvariable=self.wifi_url_var, width=24).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(wifi, text="Check", command=self.check_wifi).grid(row=0, column=2, padx=4)
        ttk.Button(wifi, text="Download", command=self.download_wifi_image).grid(row=0, column=3, padx=4)
        ttk.Checkbutton(wifi, text="Auto", variable=self.wifi_auto_var, command=self._toggle_auto_wifi).grid(row=0, column=4, padx=5)
        wifi.columnconfigure(1, weight=1)
        settings = ttk.Frame(wifi)
        settings.grid(row=1, column=0, columnspan=5, sticky="ew", padx=4, pady=3)
        ttk.Combobox(settings, textvariable=self.camera_resolution_var, values=tuple(x[0] for x in CAMERA_RESOLUTIONS), state="readonly", width=18).pack(side="left", padx=3)
        tk.Spinbox(settings, from_=0, to=63, textvariable=self.camera_quality_var, width=5, bg=WHITE).pack(side="left", padx=3)
        ttk.Combobox(settings, textvariable=self.camera_effect_var, values=tuple(x[0] for x in CAMERA_EFFECTS), state="readonly", width=13).pack(side="left", padx=3)
        tk.Spinbox(settings, from_=-2, to=2, textvariable=self.camera_brightness_var, width=4, bg=WHITE).pack(side="left", padx=3)
        ttk.Checkbutton(settings, text="Flash", variable=self.camera_flash_var).pack(side="left", padx=3)
        buttons = ttk.Frame(page)
        buttons.pack(fill="x", padx=14, pady=4)
        ttk.Button(buttons, text="Build CIMG", command=self.prepare_cimg).pack(side="left", expand=True, fill="x")
        ttk.Button(buttons, text="Capture & Fetch", command=self.capture_and_fetch).pack(side="left", expand=True, fill="x", padx=5)
        ttk.Button(buttons, text="CLR IMG", command=self._clear_image_views).pack(side="left", expand=True, fill="x")
        preview = tk.Frame(page, bg="#D4D4D4", height=520, relief="sunken", bd=3)
        preview.pack(fill="x", padx=14, pady=6)
        preview.pack_propagate(False)
        self.camera_image_label = tk.Label(preview, text="Connect the laptop to AAST and press Check", bg="#D4D4D4", fg=INK, font=("Segoe UI", 11), anchor="center")
        self.camera_image_label.pack(fill="both", expand=True)
        self._lv_camera_image_labels.append(self.camera_image_label)
        ttk.Label(page, textvariable=self.wifi_state_var, anchor="center").pack(fill="x")
        ttk.Label(page, textvariable=self.image_meta_var, anchor="center").pack(fill="x", padx=10, pady=(0, 7))
        self._lv_footer(page, "Payload", "Payload")
        self._lv_build_grad_payload_page(grad_page)

    def _clear_image_views(self) -> None:
        self._image_photo = None
        self._image_photo_secondary = None
        for label in self._lv_camera_image_labels:
            label.configure(image="", text="No image loaded")
        if hasattr(self, "secondary_image_label"):
            self.secondary_image_label.configure(image="", text="No RF/Wi-Fi image received")
        if self._lv_image_grid:
            self._lv_image_grid.delete(*self._lv_image_grid.get_children())
        self.image_meta_var.set("No image loaded")
        self._lv_set_led("image_data:complete", None)
        self._lv_set_led("comm_img:complete", None)

    def _lv_fill_image_grid(self, raw: bytes) -> None:
        if not self._lv_image_grid:
            return
        self._lv_image_grid.delete(*self._lv_image_grid.get_children())
        for row_index in range(0, min(len(raw), 4096), 8):
            chunk = raw[row_index:row_index + 8]
            values = [f"x{value:02X}" for value in chunk] + [""] * (8 - len(chunk))
            self._lv_image_grid.insert("", "end", text=f"Frame-{row_index // 8}", values=values)
        self._lv_set_led("image_data:complete", bool(raw))
        self._lv_set_led("comm_img:complete", bool(raw))

    def _lv_set_reaction_wheel(self, value: Any) -> None:
        if not self._lv_rw_canvas or self._lv_rw_needle is None:
            return
        try:
            rpm = max(-200.0, min(200.0, float(value)))
        except (TypeError, ValueError):
            return
        angle = math.radians(90.0 - rpm * 65.0 / 200.0)
        end_x = 125 + 62 * math.cos(angle)
        end_y = 125 - 62 * math.sin(angle)
        self._lv_rw_canvas.coords(self._lv_rw_needle, 125, 125, end_x, end_y)
        if self._lv_rw_value_text is not None:
            self._lv_rw_canvas.itemconfigure(self._lv_rw_value_text, text=f"{rpm:g} rpm")

    def _update_labview_page(self, record: Any) -> None:
        if record.frame.command not in (0x27, 0x46, 0x65, 0xE1, 0xE2, 0xE5, 0xE7):
            return
        page = {
            "Power / EPS": "EPS", "Space Env.": "Space Environment",
            "Core-OBC": "OBC", "Core-COM": "COMM",
        }.get(record.subsystem, record.subsystem)
        if page in self._lv_raw:
            payload = record.frame.data
            lines = []
            for start in range(0, len(payload), 16):
                lines.append(" ".join(f"x{value:02X}" for value in payload[start:start + 16]))
            self._lv_raw[page].set("\n".join(lines) or "—")
        if page in self._lv_updated:
            self._lv_updated[page].set(f"Last update: {record.timestamp:%Y-%m-%d %H:%M:%S.%f}"[:-3])
        variables = self._lv_values.get(page, {})
        for key, value in record.values.items():
            if key in variables:
                variables[key].set("—" if value is None else str(value))
        if page == "OBC":
            for key in ("PL 5V", "Space Env. 5V", "ADCS 5V", "Space Env. 3.3V", "ADCS 3.3V"):
                state = record.values.get(key)
                self._lv_set_led(f"obc:{key}", None if state is None else state == "ON")
        elif page == "COMM" and self._lv_comm_tree:
            for command in self._lv_comm_tree.get_children():
                received = record.values.get(f"{command} received")
                delivered = record.values.get(f"{command} delivered")
                if received is not None or delivered is not None:
                    self._lv_comm_tree.item(command, values=(received or 0, delivered or 0))
            self._lv_var("Comm IMG Display", "Last Tx N(S)").set(str(self.ax25_tx_sequence))
        elif page == "ADCS":
            self._lv_set_reaction_wheel(record.values.get("Reaction wheel speed (rpm)"))
        elif page == "Payload":
            aliases = {
                "Cached image size (bytes)": "Image size",
                "Last image segment": "Last frame counter",
                "SPI ACK": "SPI ACK",
            }
            comm_values = self._lv_values.get("Comm IMG Display", {})
            for source_key, target_key in aliases.items():
                if target_key in comm_values and source_key in record.values:
                    value = record.values[source_key]
                    comm_values[target_key].set("—" if value is None else str(value))
