"""Space Keys SSP and legacy fixed-length AX.25 wire protocols.

SSP packet::

    C0 destination source command length data... crc_lo crc_hi C0

The flight COMM software does not use shifted amateur-radio AX.25 addresses or
an AX.25 PID byte. Its RF envelope is a fixed 254-byte mission format::

    7E | destination[6] | dest A7 | source[6] | source A7 | control
       | SSP (I frames only) | AA padding | crc_lo | crc_hi | 7E

The outer CRC covers bytes 1..250 and is the same reflected legacy CCITT
calculation used by the known-good MSP430 implementation. Fixed-length parsing
is essential because either CRC byte may legitimately equal 0x7E.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import re
import time
from typing import Iterable


FEND = 0xC0
AX25_FLAG = 0x7E
AX25_PADDING = 0xAA
AX25_PACKET_SIZE = 254
AX25_CONTROL_INDEX = 15
AX25_DATA_INDEX = 16
AX25_CRC_LOW_INDEX = 251
AX25_CRC_HIGH_INDEX = 252
AX25_WINDOW_SIZE = 6
AX25_MAX_SSP_SIZE = AX25_CRC_LOW_INDEX - AX25_DATA_INDEX

SATELLITE_CALLSIGN = "ESKQUB"
GROUND_CALLSIGN = "EGYGCS"
SATELLITE_DEST_A7 = 2
SATELLITE_UI_A7 = 4
GROUND_SOURCE_A7 = 3
GROUND_UI_A7 = 5
GROUND_DEST_A7 = 2
SATELLITE_SOURCE_A7 = 3

# The OBC RTC conversion uses RTC year 0 (calendar year 2000) as its base and
# cmdSTim passes the received U64 directly to OBTSynchronize(), which stores it
# in seconds.  Keep this explicit so the operator UI and flight code use the
# same epoch and unit.
OBC_TIME_EPOCH = datetime(2000, 1, 1)


def obc_time_seconds(value: datetime) -> int:
    """Return whole mission seconds since 2000-01-01 for an OBC calendar time."""
    if value.tzinfo is not None:
        raise ValueError("OBC calendar time must not include a timezone")
    delta = value - OBC_TIME_EPOCH
    seconds = delta.days * 86_400 + delta.seconds
    if seconds < 0:
        raise ValueError("OBC time cannot be earlier than 2000-01-01 00:00:00")
    return seconds


def encode_stim_time(value: datetime) -> bytes:
    """Encode a STIM data field as the firmware's little-endian U64 seconds."""
    return obc_time_seconds(value).to_bytes(8, "little", signed=False)


class CRCAlgorithm(str, Enum):
    CCITT = "CCITT (legacy reflected)"
    IBM = "IBM / ARC"
    IBM_3740 = "CRC-16/IBM-3740 (ICD)"
    AUTO = "Auto detect"


class SequenceDisposition(str, Enum):
    """Result of checking an I-frame N(S) against the modulo-six window."""

    ACCEPT = "accept"
    DUPLICATE = "duplicate"
    GAP = "gap"
    INVALID = "invalid"


def classify_i_sequence(received: int | None, expected: int) -> SequenceDisposition:
    """Mirror the COMM receive-window rules without time-based heuristics.

    The known-good COMM accepts only ``N(S) == V(R)``, ignores a repeated
    ``N(S) == V(R)-1`` copy, and discards every other sequence value.  Both
    state variables roll over at six rather than the three-bit AX.25 limit.
    """
    if received is None or not 0 <= received < AX25_WINDOW_SIZE:
        return SequenceDisposition.INVALID
    expected %= AX25_WINDOW_SIZE
    if received == expected:
        return SequenceDisposition.ACCEPT
    if received == (expected - 1) % AX25_WINDOW_SIZE:
        return SequenceDisposition.DUPLICATE
    return SequenceDisposition.GAP


def crc16_reflected(data: bytes, algorithm: CRCAlgorithm | str) -> int:
    """Calculate a supported SSP CRC (the name is retained for compatibility)."""
    algorithm = CRCAlgorithm(algorithm)
    if algorithm is CRCAlgorithm.AUTO:
        algorithm = CRCAlgorithm.CCITT
    if algorithm is CRCAlgorithm.IBM_3740:
        crc = 0xFFFF
        for value in data:
            crc ^= value << 8
            for _ in range(8):
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
        return crc
    if algorithm is CRCAlgorithm.IBM:
        crc, polynomial = 0x0000, 0xA001
    else:
        crc, polynomial = 0xFFFF, 0x8408
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ polynomial if crc & 1 else crc >> 1
    return crc & 0xFFFF


def hex_bytes(value: str | bytes | bytearray | Iterable[int]) -> bytes:
    """Parse a forgiving operator-entered hexadecimal byte string."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if not isinstance(value, str):
        return bytes(value)
    cleaned = value.strip()
    if not cleaned:
        return b""
    tokens = [token for token in re.split(r"[\s,;:_-]+", cleaned) if token]
    result = bytearray()
    for token in tokens:
        token = token.lower().removeprefix("0x")
        if not re.fullmatch(r"[0-9a-f]+", token):
            raise ValueError(f"Invalid hexadecimal value: {token!r}")
        if len(token) > 2:
            if len(token) % 2:
                token = "0" + token
            result.extend(bytes.fromhex(token))
        else:
            result.append(int(token, 16))
    return bytes(result)


def format_hex(data: bytes | Iterable[int], columns: int = 0) -> str:
    values = [f"{int(value) & 0xFF:02X}" for value in data]
    if not columns:
        return " ".join(values)
    return "\n".join(" ".join(values[i : i + columns]) for i in range(0, len(values), columns))


@dataclass(slots=True)
class SSPFrame:
    destination: int
    source: int
    command: int
    data: bytes = b""
    received_crc: int | None = None
    matched_crc: CRCAlgorithm | None = None
    raw: bytes = b""
    error: str = ""

    @property
    def valid(self) -> bool:
        return not self.error and self.matched_crc is not None

    @property
    def crc_label(self) -> str:
        return self.matched_crc.value if self.matched_crc else "INVALID"

    def encode(self, algorithm: CRCAlgorithm | str = CRCAlgorithm.CCITT) -> bytes:
        if not all(0 <= value <= 0xFF for value in (self.destination, self.source, self.command)):
            raise ValueError("SSP address and command values must be bytes")
        if len(self.data) > 0xFF:
            raise ValueError("SSP payload cannot exceed 255 bytes")
        body = bytes((self.destination, self.source, self.command, len(self.data))) + self.data
        crc = crc16_reflected(body, algorithm)
        return bytes((FEND,)) + body + crc.to_bytes(2, "little") + bytes((FEND,))

    @classmethod
    def decode(
        cls,
        raw: bytes,
        algorithm: CRCAlgorithm | str = CRCAlgorithm.AUTO,
    ) -> "SSPFrame":
        if len(raw) < 8:
            raise ValueError("SSP frame is shorter than its 8-byte overhead")
        if raw[0] != FEND or raw[-1] != FEND:
            raise ValueError("SSP frame delimiters are invalid")
        expected = raw[4] + 8
        if len(raw) != expected:
            raise ValueError(f"SSP length field expects {expected} bytes, received {len(raw)}")
        payload = raw[5:-3]
        received = int.from_bytes(raw[-3:-1], "little")
        body = raw[1:-3]
        wanted = CRCAlgorithm(algorithm)
        algorithms = (CRCAlgorithm.CCITT, CRCAlgorithm.IBM, CRCAlgorithm.IBM_3740) if wanted is CRCAlgorithm.AUTO else (wanted,)
        match = next((item for item in algorithms if crc16_reflected(body, item) == received), None)
        error = "" if match else "CRC mismatch"
        return cls(raw[1], raw[2], raw[3], payload, received, match, bytes(raw), error)


@dataclass(slots=True)
class ParseStats:
    accepted: int = 0
    crc_errors: int = 0
    framing_errors: int = 0
    discarded_bytes: int = 0


class SSPStreamParser:
    """Length-driven SSP stream parser that survives split/back-to-back frames."""

    def __init__(self, algorithm: CRCAlgorithm | str = CRCAlgorithm.AUTO, max_payload: int = 255):
        self.algorithm = CRCAlgorithm(algorithm)
        self.max_payload = max_payload
        self.buffer = bytearray()
        self.stats = ParseStats()

    def reset(self) -> None:
        self.buffer.clear()
    def feed(self, chunk: bytes | bytearray) -> list[SSPFrame]:
        self.buffer.extend(chunk)
        frames: list[SSPFrame] = []
        while self.buffer:
            try:
                start = self.buffer.index(FEND)
            except ValueError:
                self.stats.discarded_bytes += len(self.buffer)
                self.buffer.clear()
                break
            if start:
                self.stats.discarded_bytes += start
                del self.buffer[:start]
            if len(self.buffer) < 5:
                break
            if self.buffer[4] > self.max_payload:
                self.stats.framing_errors += 1
                del self.buffer[0]
                continue
            expected = self.buffer[4] + 8
            if len(self.buffer) < expected:
                break
            candidate = bytes(self.buffer[:expected])
            if candidate[-1] != FEND:
                self.stats.framing_errors += 1
                del self.buffer[0]
                continue
            del self.buffer[:expected]
            try:
                frame = SSPFrame.decode(candidate, self.algorithm)
            except ValueError:
                self.stats.framing_errors += 1
                continue
            if frame.valid:
                self.stats.accepted += 1
            else:
                self.stats.crc_errors += 1
            frames.append(frame)
        return frames


def extract_ssp_frames_from_log(text: str) -> list[SSPFrame]:
    """Extract complete SSP frames from LabVIEW/Python session prose.

    Only lines made entirely of hexadecimal byte tokens are fed to the stream
    parser. This avoids treating dates, command names, and timestamps as data,
    while preserving frames split across two or more log lines.
    """
    parser = SSPStreamParser(CRCAlgorithm.AUTO)
    frames: list[SSPFrame] = []
    byte_line = re.compile(r"^\s*(?:(?:0x)?[0-9a-fA-F]{1,2}\s+)+(?:0x)?[0-9a-fA-F]{1,2}\s*$")
    for line in text.splitlines():
        if byte_line.fullmatch(line):
            frames.extend(parser.feed(hex_bytes(line)))
    return frames


def _raw_callsign(value: str) -> bytes:
    encoded = value.upper().strip().encode("ascii")
    if len(encoded) != 6:
        raise ValueError("Legacy AX.25 callsigns must contain exactly six ASCII characters")
    return encoded


def _validate_sequence(value: int, label: str) -> int:
    if not 0 <= value < AX25_WINDOW_SIZE:
        raise ValueError(f"{label} must be from 0 to {AX25_WINDOW_SIZE - 1}")
    return value


def build_i_control(send_sequence: int, receive_sequence: int = 0) -> int:
    ns = _validate_sequence(send_sequence, "N(S)")
    nr = _validate_sequence(receive_sequence, "N(R)")
    return (ns << 1) | (nr << 5)


def build_s_control(supervisory: str | int = "RR", receive_sequence: int = 0) -> int:
    kinds = {"RR": 0, "RNR": 1, "REJ": 2, "SREJ": 3}
    if isinstance(supervisory, str):
        try:
            kind = kinds[supervisory.upper()]
        except KeyError as exc:
            raise ValueError(f"Unknown supervisory type: {supervisory}") from exc
    else:
        kind = int(supervisory)
    if not 0 <= kind <= 3:
        raise ValueError("Supervisory type must be RR, RNR, REJ, SREJ, or 0..3")
    nr = _validate_sequence(receive_sequence, "N(R)")
    # The known-good firmware defines F_BIT_MASK as 0x01. Preserve it.
    return 0x01 | (kind << 2) | (nr << 5)


def build_u_control(unnumbered: str | int = "UI") -> int:
    kinds = {"SABME": 0x6C, "DISC": 0x40, "DM": 0x04, "UA": 0x60, "UI": 0x00, "TEST": 0xE0}
    if isinstance(unnumbered, str):
        try:
            kind = kinds[unnumbered.upper()]
        except KeyError as exc:
            raise ValueError(f"Unknown unnumbered type: {unnumbered}") from exc
    else:
        kind = int(unnumbered) & 0xEC
    control = 0x13 | kind
    if kind == kinds["DM"]:
        control |= 0x01
    return control & 0xFF


def build_legacy_ax25(
    payload: bytes = b"",
    *,
    destination: str = SATELLITE_CALLSIGN,
    source: str = GROUND_CALLSIGN,
    destination_a7: int = SATELLITE_DEST_A7,
    source_a7: int = GROUND_SOURCE_A7,
    control: int = 0,
) -> bytes:
    """Build the exact 254-byte envelope used by the flight COMM software."""
    if not 0 <= destination_a7 <= 0xFF or not 0 <= source_a7 <= 0xFF:
        raise ValueError("Legacy A7 address values must be bytes")
    if not 0 <= control <= 0xFF:
        raise ValueError("AX.25 control value must be a byte")
    if len(payload) > AX25_MAX_SSP_SIZE:
        raise ValueError(f"AX.25 payload cannot exceed {AX25_MAX_SSP_SIZE} bytes")

    frame = bytearray((AX25_PADDING,) * AX25_PACKET_SIZE)
    frame[0] = AX25_FLAG
    frame[1:7] = _raw_callsign(destination)
    frame[7] = destination_a7
    frame[8:14] = _raw_callsign(source)
    frame[14] = source_a7
    frame[AX25_CONTROL_INDEX] = control
    if payload:
        frame[AX25_DATA_INDEX : AX25_DATA_INDEX + len(payload)] = payload
    crc = crc16_reflected(bytes(frame[1:AX25_CRC_LOW_INDEX]), CRCAlgorithm.CCITT)
    frame[AX25_CRC_LOW_INDEX] = crc & 0xFF
    frame[AX25_CRC_HIGH_INDEX] = crc >> 8
    frame[-1] = AX25_FLAG
    return bytes(frame)


def build_ax25_i(
    payload: bytes,
    send_sequence: int = 0,
    receive_sequence: int = 0,
    *,
    destination: str = SATELLITE_CALLSIGN,
    source: str = GROUND_CALLSIGN,
    destination_a7: int = SATELLITE_DEST_A7,
    source_a7: int = GROUND_SOURCE_A7,
) -> bytes:
    return build_legacy_ax25(
        payload,
        destination=destination,
        source=source,
        destination_a7=destination_a7,
        source_a7=source_a7,
        control=build_i_control(send_sequence, receive_sequence),
    )


def build_ax25_s(
    supervisory: str | int = "RR",
    receive_sequence: int = 0,
    *,
    destination: str = SATELLITE_CALLSIGN,
    source: str = GROUND_CALLSIGN,
) -> bytes:
    return build_legacy_ax25(
        destination=destination,
        source=source,
        destination_a7=SATELLITE_DEST_A7,
        source_a7=GROUND_SOURCE_A7,
        control=build_s_control(supervisory, receive_sequence),
    )


def build_ax25_u(
    unnumbered: str | int = "UI",
    *,
    destination: str = SATELLITE_CALLSIGN,
    source: str = GROUND_CALLSIGN,
    ui_addresses: bool = False,
) -> bytes:
    return build_legacy_ax25(
        destination=destination,
        source=source,
        destination_a7=SATELLITE_UI_A7 if ui_addresses else SATELLITE_DEST_A7,
        source_a7=GROUND_UI_A7 if ui_addresses else GROUND_SOURCE_A7,
        control=build_u_control(unnumbered),
    )


def build_ax25_ui(
    payload: bytes,
    destination: str = SATELLITE_CALLSIGN,
    source: str = GROUND_CALLSIGN,
    destination_ssid: int = SATELLITE_DEST_A7,
    source_ssid: int = GROUND_SOURCE_A7,
    *,
    send_sequence: int = 0,
    receive_sequence: int = 0,
) -> bytes:
    """Compatibility name for callers of CTE 2.x; builds a legacy I frame."""
    return build_ax25_i(
        payload,
        send_sequence,
        receive_sequence,
        destination=destination,
        source=source,
        destination_a7=destination_ssid,
        source_a7=source_ssid,
    )


_S_NAMES = {0: "RR", 1: "RNR", 2: "REJ", 3: "SREJ"}
_U_NAMES = {0x6C: "SABME", 0x40: "DISC", 0x04: "DM", 0x60: "UA", 0x00: "UI", 0xE0: "TEST"}


@dataclass(slots=True)
class AX25Frame:
    destination: str
    destination_ssid: int
    source: str
    source_ssid: int
    control: int
    payload: bytes
    valid_fcs: bool
    raw: bytes = field(repr=False)
    received_fcs: int = 0
    calculated_fcs: int = 0
    payload_error: str = ""

    @property
    def frame_type(self) -> str:
        if (self.control & 0x01) == 0:
            return "I"
        if (self.control & 0x02) == 0:
            return "S"
        return "U"

    @property
    def send_sequence(self) -> int | None:
        return (self.control >> 1) & 0x07 if self.frame_type == "I" else None

    @property
    def receive_sequence(self) -> int | None:
        return (self.control >> 5) & 0x07 if self.frame_type in ("I", "S") else None

    @property
    def supervisory_name(self) -> str | None:
        return _S_NAMES[(self.control >> 2) & 0x03] if self.frame_type == "S" else None

    @property
    def unnumbered_name(self) -> str | None:
        return _U_NAMES.get(self.control & 0xEC, f"M=0x{self.control & 0xEC:02X}") if self.frame_type == "U" else None

    @property
    def control_description(self) -> str:
        if self.frame_type == "I":
            return f"I  N(S)={self.send_sequence}  N(R)={self.receive_sequence}"
        if self.frame_type == "S":
            return f"S/{self.supervisory_name}  N(R)={self.receive_sequence}"
        return f"U/{self.unnumbered_name}"

    @property
    def address_valid(self) -> bool:
        return self.address_direction is not None

    @property
    def address_direction(self) -> str | None:
        """Return the mission link direction when callsigns *and* A7 bytes match."""
        if (
            self.destination == SATELLITE_CALLSIGN
            and self.source == GROUND_CALLSIGN
            and self.destination_ssid in (SATELLITE_DEST_A7, SATELLITE_UI_A7)
            and self.source_ssid in (GROUND_SOURCE_A7, GROUND_UI_A7)
        ):
            return "uplink"
        if (
            self.destination == GROUND_CALLSIGN
            and self.source == SATELLITE_CALLSIGN
            and self.destination_ssid in (GROUND_DEST_A7, GROUND_UI_A7)
            and self.source_ssid in (SATELLITE_SOURCE_A7, SATELLITE_UI_A7)
        ):
            return "downlink"
        return None

    @property
    def pid(self) -> None:
        """The legacy mission envelope has no PID byte."""
        return None


def decode_ax25_ui(raw: bytes) -> AX25Frame:
    """Decode one fixed 254-byte legacy envelope (compatibility function name)."""
    if len(raw) != AX25_PACKET_SIZE:
        raise ValueError(f"Legacy AX.25 frame must be {AX25_PACKET_SIZE} bytes, received {len(raw)}")
    if raw[0] != AX25_FLAG or raw[-1] != AX25_FLAG:
        raise ValueError("Legacy AX.25 frame flags are invalid")

    try:
        destination = raw[1:7].decode("ascii")
        source = raw[8:14].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("Legacy AX.25 callsign contains non-ASCII bytes") from exc

    received = int.from_bytes(raw[AX25_CRC_LOW_INDEX:AX25_CRC_HIGH_INDEX + 1], "little")
    calculated = crc16_reflected(raw[1:AX25_CRC_LOW_INDEX], CRCAlgorithm.CCITT)
    control = raw[AX25_CONTROL_INDEX]
    payload = b""
    payload_error = ""

    if (control & 0x01) == 0:
        segment = raw[AX25_DATA_INDEX:AX25_CRC_LOW_INDEX]
        if len(segment) < 5 or segment[0] != FEND:
            payload_error = "I frame does not begin with SSP FEND"
        else:
            expected = segment[4] + 8
            if expected > AX25_MAX_SSP_SIZE:
                payload_error = f"SSP length exceeds {AX25_MAX_SSP_SIZE}-byte AX.25 data area"
            else:
                payload = bytes(segment[:expected])
                if len(payload) != expected or payload[-1] != FEND:
                    payload_error = "Embedded SSP frame is incomplete or has no trailing FEND"

    return AX25Frame(
        destination=destination,
        destination_ssid=raw[7],
        source=source,
        source_ssid=raw[14],
        control=control,
        payload=payload,
        valid_fcs=calculated == received,
        raw=bytes(raw),
        received_fcs=received,
        calculated_fcs=calculated,
        payload_error=payload_error,
    )


class AX25StreamParser:
    """Fixed-length parser; delimiter bytes inside CRC never split a frame."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.stats = ParseStats()

    def reset(self) -> None:
        self.buffer.clear()

    def feed(self, chunk: bytes | bytearray) -> list[AX25Frame]:
        self.buffer.extend(chunk)
        result: list[AX25Frame] = []
        while self.buffer:
            try:
                start = self.buffer.index(AX25_FLAG)
            except ValueError:
                self.stats.discarded_bytes += len(self.buffer)
                self.buffer.clear()
                break
            if start:
                self.stats.discarded_bytes += start
                del self.buffer[:start]
            if len(self.buffer) < AX25_PACKET_SIZE:
                break
            candidate = bytes(self.buffer[:AX25_PACKET_SIZE])
            if candidate[-1] != AX25_FLAG:
                self.stats.framing_errors += 1
                del self.buffer[0]
                continue
            del self.buffer[:AX25_PACKET_SIZE]
            try:
                frame = decode_ax25_ui(candidate)
            except ValueError:
                self.stats.framing_errors += 1
                continue
            if frame.valid_fcs:
                self.stats.accepted += 1
            else:
                self.stats.crc_errors += 1
            result.append(frame)
        if len(self.buffer) > AX25_PACKET_SIZE * 16:
            self.stats.discarded_bytes += len(self.buffer) - 1
            del self.buffer[:-1]
        return result


class AX25RepeatTracker:
    """Identify consecutive physical copies of one logical AX.25 frame.

    The original satellite transmits each prepared frame three times.  The
    tracker does not discard bytes or depend on sequence numbers; it compares
    the complete, FCS-protected frame so an RNR, telemetry I-frame, and final
    RR remain three distinct logical frames even when they arrive in one UART
    burst.
    """

    def __init__(self, window_seconds: float = 2.0, expected_copies: int = 3) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if expected_copies < 1:
            raise ValueError("expected_copies must be positive")
        self.window_seconds = float(window_seconds)
        self.expected_copies = int(expected_copies)
        self._last_raw = b""
        self._last_time = 0.0
        self._copy_number = 0

    def reset(self) -> None:
        self._last_raw = b""
        self._last_time = 0.0
        self._copy_number = 0

    def observe(self, raw: bytes, now: float | None = None) -> int:
        """Return one for a new logical frame, otherwise its repeat number."""
        timestamp = time.monotonic() if now is None else float(now)
        encoded = bytes(raw)
        if encoded == self._last_raw and timestamp - self._last_time <= self.window_seconds:
            self._copy_number += 1
        else:
            self._last_raw = encoded
            self._copy_number = 1
        self._last_time = timestamp
        return self._copy_number


def extract_ax25_frames_from_log(text: str) -> list[AX25Frame]:
    """Extract full fixed-length AX.25 frames from LabVIEW/Python logs.

    Historical LabVIEW logs commonly concatenate all three copies of every
    downlink frame on one hexadecimal line.  Only byte-only lines beginning a
    frame, or continuing a partial frame, are accepted so timestamps and SSP
    logs containing an incidental ``7E`` byte cannot corrupt replay.
    """
    parser = AX25StreamParser()
    frames: list[AX25Frame] = []
    byte_line = re.compile(r"^\s*(?:(?:0x)?[0-9a-fA-F]{1,2}\s+)+(?:0x)?[0-9a-fA-F]{1,2}\s*$")
    for line in text.splitlines():
        if not byte_line.fullmatch(line):
            continue
        raw = hex_bytes(line)
        if parser.buffer or (raw and raw[0] == AX25_FLAG):
            frames.extend(parser.feed(raw))
    return frames
