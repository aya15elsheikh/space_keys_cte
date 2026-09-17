"""Threaded serial transport with deterministic delivery.

The HC-12 RF link uses the same UART stream as the ground-station AX.25
envelope.  A complete legacy envelope is 254 bytes, so at 9,600 baud it takes
roughly 265 ms to leave the radio.  The flight COMM software deliberately
repeats downlink frames; this transport therefore supports an explicit copy
count and inter-copy guard instead of making the application concatenate
frames blindly.
"""

from __future__ import annotations

from dataclasses import dataclass
from queue import Queue, Empty
import threading
import time
from typing import Callable

import serial
from serial.tools import list_ports


@dataclass(frozen=True, slots=True)
class PortInfo:
    device: str
    description: str
    hwid: str

    @property
    def display(self) -> str:
        return f"{self.device} — {self.description}" if self.description else self.device


@dataclass(frozen=True, slots=True)
class _TxItem:
    """One queued write and the post-write guard before the next item."""

    payload: bytes
    gap_ms: float


def available_ports() -> list[PortInfo]:
    return [PortInfo(item.device, item.description, item.hwid) for item in sorted(list_ports.comports(), key=lambda p: p.device)]


class SerialTransport:
    def __init__(self, on_bytes: Callable[[bytes], None], on_error: Callable[[str], None]) -> None:
        self.on_bytes = on_bytes
        self.on_error = on_error
        self.serial: serial.Serial | None = None
        self._stop = threading.Event()
        self._write_queue: Queue[_TxItem] = Queue()
        self._thread: threading.Thread | None = None
        self.tx_delay_ms = 10

    @property
    def connected(self) -> bool:
        return bool(self.serial and self.serial.is_open)

    def connect(self, port: str, baudrate: int = 115200, timeout: float = 0.05) -> None:
        self.disconnect()
        self.serial = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout,
            write_timeout=2,
        )
        self.serial.reset_input_buffer()
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, name="SpaceKeysSerial", daemon=True)
        self._thread.start()

    def disconnect(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.5)
        self._thread = None
        if self.serial:
            try:
                self.serial.close()
            except serial.SerialException:
                pass
        self.serial = None
        while True:
            try:
                self._write_queue.get_nowait()
            except Empty:
                break

    def send(self, payload: bytes, copies: int = 1, repeat_gap_ms: float | None = None) -> None:
        """Queue one frame, or a bounded number of complete repeated frames.

        ``copies`` is primarily for the legacy HC-12/AX.25 path, where the
        receiver expects repeated frames to improve the chance of reception.
        The bytes are queued as separate writes so each copy is flushed before
        the guard interval.  Direct RS-485 callers should keep the default of
        one copy.
        """
        if not self.connected:
            raise RuntimeError("Serial port is not connected")
        if copies < 1 or copies > 3:
            raise ValueError("copies must be between 1 and 3")
        encoded = bytes(payload)
        if not encoded:
            raise ValueError("payload must contain at least one byte")
        gap = self.tx_delay_ms if repeat_gap_ms is None else float(repeat_gap_ms)
        if gap < 0 or gap > 60_000:
            raise ValueError("repeat_gap_ms must be between 0 and 60000")
        for _ in range(copies):
            self._write_queue.put(_TxItem(encoded, gap))

    def _worker(self) -> None:
        serial_port = self.serial
        assert serial_port is not None
        try:
            while not self._stop.is_set():
                try:
                    item = self._write_queue.get_nowait()
                except Empty:
                    item = None
                if item is not None:
                    written = serial_port.write(item.payload)
                    if written != len(item.payload):
                        raise serial.SerialTimeoutException(
                            f"serial write incomplete ({written}/{len(item.payload)} bytes)"
                        )
                    # flush() waits until pyserial has handed the bytes to the
                    # operating-system driver.  At 9,600 baud this is also the
                    # natural HC-12 airtime guard before the next copy.
                    serial_port.flush()
                    if item.gap_ms:
                        time.sleep(item.gap_ms / 1000)
                waiting = serial_port.in_waiting
                incoming = serial_port.read(waiting or 1)
                if incoming:
                    self.on_bytes(incoming)
        except (serial.SerialException, OSError) as exc:
            if not self._stop.is_set():
                self.on_error(str(exc))
        finally:
            try:
                serial_port.close()
            except (serial.SerialException, OSError):
                pass
