"""Telemetry decoding and bounded history for CubeSat GCS."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import struct
from typing import Any

from catalog import SatelliteProfile, command_name, node_by_id_for
from protocol import SSPFrame, format_hex


SATELLITE_MODES = {
    0: "Standby",
    1: "ADCS experiment",
    2: "Communication experiment",
    3: "OBC experiment",
    4: "Power experiment",
    5: "Imaging payload experiment",
    6: "Space-environment experiment",
    7: "Integrated operation",
}

COMM_MODES = {
    1: "Initialization",
    2: "Standby",
    3: "Communication session",
    4: "Image",
    5: "Telemetry download",
    6: "Emergency",
}

# The current OBC contract uses the first six lengths.  Early payload firmware
# returned a 55-byte payload body plus the same 9-byte secondary header (64
# bytes total); real LabVIEW session logs still contain those frames.
SECONDARY_HEADER_LENGTHS = {20, 33, 42, 50, 55, 63, 64}


def _f32le(data: bytes, index: int) -> float | None:
    if index + 4 > len(data):
        return None
    return round(struct.unpack_from("<f", data, index)[0], 5)


def _fixed_i16be(data: bytes, index: int) -> float | None:
    if index + 2 > len(data):
        return None
    return round(struct.unpack_from(">h", data, index)[0] / 256.0, 4)


def _u16le(data: bytes, index: int) -> int | None:
    return int.from_bytes(data[index:index + 2], "little") if index + 2 <= len(data) else None


def _u16be(data: bytes, index: int) -> int | None:
    return int.from_bytes(data[index:index + 2], "big") if index + 2 <= len(data) else None


def _u32be(data: bytes, index: int) -> int | None:
    return int.from_bytes(data[index:index + 4], "big") if index + 4 <= len(data) else None


def _u32le(data: bytes, index: int) -> int | None:
    return int.from_bytes(data[index:index + 4], "little") if index + 4 <= len(data) else None


def _s32le(data: bytes, index: int) -> int | None:
    return int.from_bytes(data[index:index + 4], "little", signed=True) if index + 4 <= len(data) else None


def _u64le(data: bytes, index: int) -> int | None:
    return int.from_bytes(data[index:index + 8], "little") if index + 8 <= len(data) else None


def _grad_header(data: bytes) -> dict[str, Any]:
    return {
        "Subsystem address": f"0x{data[0]:02X}",
        "Mode": data[1],
        "OBC time": _u64le(data, 2),
        "RTC": _u32le(data, 10),
    }


def has_secondary_header(data: bytes) -> bool:
    if len(data) < 9 or len(data) not in SECONDARY_HEADER_LENGTHS:
        return False
    subsystem = data[0] & 0x1F
    # Satellite-status records historically used subsystem id zero in their
    # secondary header.  Treat it as a valid header only for the 20-byte
    # satellite/environment packet size.
    return (subsystem in node_by_id_for(SatelliteProfile.SPACE_KEYS) or (len(data) == 20 and subsystem == 0)) and (data[0] >> 5) <= 7


def decode_secondary_header(data: bytes) -> dict[str, Any]:
    if not has_secondary_header(data):
        return {}
    subsystem_id = data[0] & 0x1F
    mode = data[0] >> 5
    timestamp = int.from_bytes(data[1:9], "little")
    node = node_by_id_for(SatelliteProfile.SPACE_KEYS).get(subsystem_id)
    return {
        "Header subsystem": node.name if node else ("Satellite" if subsystem_id == 0 else f"0x{subsystem_id:02X}"),
        "Header satellite mode": SATELLITE_MODES.get(mode, f"Mode {mode}"),
        "OBT raw (0.01 s ticks)": timestamp,
        "OBT seconds": round(timestamp / 100.0, 2),
    }


def _with_header(data: bytes, values: dict[str, Any]) -> dict[str, Any]:
    header = decode_secondary_header(data)
    return header | values if header else values


def decode_adcs(data: bytes, profile: SatelliteProfile = SatelliteProfile.SPACE_KEYS) -> dict[str, Any]:
    if len(data) == 12:
        return {
            "Vector X": _f32le(data, 0),
            "Vector Y": _f32le(data, 4),
            "Vector Z": _f32le(data, 8),
        }
    if profile is SatelliteProfile.GRAD_PROJECT and len(data) >= 52:
        return _grad_header(data) | {
            "Accelerometer X": _f32le(data, 14), "Accelerometer Y": _f32le(data, 18), "Accelerometer Z": _f32le(data, 22),
            "Gyroscope X": _f32le(data, 26), "Gyroscope Y": _f32le(data, 30), "Gyroscope Z": _f32le(data, 34),
            "Magnetometer X": _f32le(data, 38), "Magnetometer Y": _f32le(data, 42), "Magnetometer Z": _f32le(data, 46),
            "Gyroscope X (deg/s)": _f32le(data, 26), "Gyroscope Y (deg/s)": _f32le(data, 30), "Gyroscope Z (deg/s)": _f32le(data, 34),
            "Magnetometer X (uT)": _f32le(data, 38), "Magnetometer Y (uT)": _f32le(data, 42), "Magnetometer Z (uT)": _f32le(data, 46),
            "Reaction wheel speed (rpm)": _u16le(data, 50),
            "Telemetry layout": "Grad ICD v3.1 Table 13",
        }
    if len(data) < 49:
        return _with_header(data, {f"Byte {index:02d}": value for index, value in enumerate(data)})
    values = {
        "Reaction wheel speed (rpm)": _fixed_i16be(data, 19),
        "MT command": _fixed_i16be(data, 21),
        "Gyroscope X (deg/s)": _f32le(data, 23),
        "Gyroscope Y (deg/s)": _f32le(data, 27),
        "Gyroscope Z (deg/s)": _f32le(data, 31),
        "Magnetometer X (uT)": _f32le(data, 35),
        "Magnetometer Y (uT)": _f32le(data, 39),
        "Magnetometer Z (uT)": _f32le(data, 43),
        "MPU status": "FAULT" if data[47] else "OK",
        "Magnetometer status": "FAULT" if data[48] else "OK",
    }
    return _with_header(data, values)


def decode_payload(data: bytes, profile: SatelliteProfile = SatelliteProfile.SPACE_KEYS) -> dict[str, Any]:
    if len(data) == 12:
        image_size = _u32le(data, 8)
        return {
            "ESP camera status": f"0x{data[0]:02X}",
            "Frame buffer": "Ready" if data[1] else "Empty",
            "Resolution index": data[2],
            "JPEG quality": data[3],
            "Flash": "On" if data[4] else "Off",
            "Special effect": data[5],
            "Brightness": int.from_bytes(data[6:7], "little", signed=True),
            "Transfer count": data[7],
            "Image size (bytes)": image_size,
        }
    if profile is SatelliteProfile.GRAD_PROJECT and len(data) >= 57:
        return _grad_header(data) | {
            "Payload mode": {0x02: "Standby", 0x04: "Image Payload", 0x07: "Relay Communication"}.get(data[1], f"0x{data[1]:02X}"),
            "Images count": data[14],
            "Image 1 number": data[15],
            "Image 1 size (bytes)": _u32le(data, 16),
            "Image 1 parameters": int.from_bytes(data[20:28], "little"),
            "Image 2 number": data[28],
            "Image 2 size (bytes)": _u32le(data, 29),
            "Image 2 parameters": int.from_bytes(data[33:41], "little"),
            "Image transfer rate": _u16le(data, 41),
            "RF power": _u16le(data, 43),
            "Total image frames": _u16le(data, 45),
            "Transmitted image frames": _u16le(data, 47),
            "Ground stations in relay": data[49],
            "Relay mode": data[50],
            "Relay total RX frames": _u16le(data, 51),
            "Relay correct RX frames": _u16le(data, 53),
            "Relay total TX frames": _u16le(data, 55),
            "Cached image size (bytes)": _u32le(data, 16),
            "Last image segment": _u16le(data, 47),
            "SPI ACK": 0,
            "Image error": 0,
            "Telemetry layout": "Grad ICD v3.1 Table 15",
            "Wi-Fi endpoint": "http://192.168.4.1/image",
        }
    if len(data) == 64:
        # Old-satellite ICD Table 15 uses hexadecimal byte locations across
        # the complete 64-byte data field.  Keep this layout separate from the
        # current STM32/ESP32 record: the marker bytes and two MAC addresses
        # are part of the historical Payload telemetry contract.
        legacy_modes = {
            0: "Standby",
            2: "Standby",
            3: "Communication session",
            4: "Imaging mode",
        }
        mode_id = data[0] >> 5
        image_size = _u32le(data, 0x1B)
        image_error = _u16be(data, 0x23)
        values = {
            "Payload mode": legacy_modes.get(mode_id, f"Mode {mode_id}"),
            "Legacy time (seconds)": _u16be(data, 0x07),
            "Legacy debugging word": _u16be(data, 0x09),
            "Capture parameters": format_hex(data[0x12:0x1A]),
            "Image size (bytes)": image_size,
            "Cached image size (bytes)": image_size,
            "SPI ACK": _u16be(data, 0x20),
            "Image error / SPI NACK": image_error,
            "Image error": image_error,
            "Last image segment": None,
            "MAC address A": ":".join(f"{value:02X}" for value in data[0x26:0x2E]),
            "MAC address B": ":".join(f"{value:02X}" for value in data[0x2F:0x37]),
            "Reserved": format_hex(data[0x37:]),
            "Payload telemetry layout": "Original satellite ICD (64 bytes)",
        }
        return _with_header(data, values)
    if len(data) >= 37:
        modes = {0x02: "Standby", 0x04: "Image Payload", 0x07: "Relay Communication"}
        values = {
            "Payload mode": modes.get(data[16], f"0x{data[16]:02X}"),
            "SPI ACK": data[26],
            "Cached image size (bytes)": _u32be(data, 27),
            "Last image segment": _u16be(data, 32),
            "Image error": _u16be(data, 35),
            "Payload telemetry layout": "STM32/ESP32 current (55 bytes)",
            "Wi-Fi endpoint": "http://192.168.4.1/image",
        }
        return _with_header(data, values)
    return _with_header(data, {f"Byte {index:02d}": value for index, value in enumerate(data)})


def decode_environment(data: bytes) -> dict[str, Any]:
    body = data[9:] if has_secondary_header(data) else data
    names = (
        "Radiation low", "Radiation high", "Temperature 1 L", "Temperature 1 H",
        "Temperature 2 L", "Temperature 2 H", "Magnetic field L", "Magnetic field H",
        "Thermal flag", "Magnetic flag", "Radiation flag",
    )
    values = {names[i] if i < len(names) else f"Byte {i:02d}": value for i, value in enumerate(body)}
    if len(body) >= 8:
        values.update({
            "Radiation count": int.from_bytes(body[0:2], "little"),
            "Temperature sensor 1": int.from_bytes(body[2:4], "little", signed=True),
            "Temperature sensor 2": int.from_bytes(body[4:6], "little", signed=True),
            "Magnetic field raw": int.from_bytes(body[6:8], "little", signed=True),
        })
    return _with_header(data, values)


def decode_power(data: bytes, profile: SatelliteProfile = SatelliteProfile.SPACE_KEYS) -> dict[str, Any]:
    if profile is SatelliteProfile.GRAD_PROJECT and len(data) >= 42:
        valid = _u16le(data, 36) or 0
        saturated = _u16le(data, 38) or 0
        switch_controls = _u16le(data, 34) or 0
        measurements = (
            ("OBC voltage (mV)", 14, 0),
            ("OBC current (mA)", 16, 1),
            ("COMM voltage (mV)", 18, 2),
            ("COMM current (mA)", 20, 3),
            ("Payload voltage (mV)", 22, 4),
            ("Payload current (mA)", 24, 5),
            ("ADCS voltage (mV)", 26, 6),
            ("ADCS current (mA)", 28, 7),
            ("Battery voltage (mV)", 30, 8),
            ("Battery current (mA)", 32, 9),
        )
        values = _grad_header(data)
        for label, offset, bit in measurements:
            raw_value = _u16le(data, offset)
            values[label] = raw_value if valid & (1 << bit) else None
            if not (valid & (1 << bit)):
                state = "Calibration required"
            elif saturated & (1 << bit):
                state = "ADC saturated"
            else:
                state = "Valid"
            values[f"{label} status"] = state
        values.update({
            "OBC control pin": "HIGH" if switch_controls & 0x0001 else "LOW",
            "COMM control pin": "HIGH" if switch_controls & 0x0002 else "LOW",
            "Payload control pin": "HIGH" if switch_controls & 0x0004 else "LOW",
            "ADCS 5V control pin": "HIGH" if switch_controls & 0x0008 else "LOW",
            "ADCS 12V control pin": "HIGH" if switch_controls & 0x0010 else "LOW",
            "Switch control bitmap": f"0x{switch_controls:04X}",
            "Measurement valid bitmap": f"0x{valid:04X}",
            "ADC saturation bitmap": f"0x{saturated:04X}",
            "Current calibration": "Configured" if (valid & 0x02AA) == 0x02AA else "Required",
            "Sample counter": _u16le(data, 40),
            "Telemetry layout": "Grad Power board 42-byte hardware mapping",
        })
        return values
    body = data[9:] if has_secondary_header(data) else data
    labels = (
        "3.3 V rail current", "5 V rail voltage", "5 V rail current", "3.3 V rail voltage",
        "12 V rail current", "12 V rail voltage", "Battery voltage", "Battery current",
        "Solar-array voltage", "Battery temperature 2", "Solar-array current", "Battery temperature 1",
    )
    values: dict[str, Any] = {}
    for item, label in enumerate(labels):
        value = _u16le(body, item * 2)
        if value is not None:
            values[f"{label} (raw)"] = value
    # LabVIEW labels these channels as "Real" values.  They are the exact
    # 12-bit ADC words placed in telemetry by the original OBC; calibration
    # belongs to the electrical test configuration, so the CTE does not invent
    # a board-specific gain/offset.
    display_keys = (
        ("Real I_3v3", 0), ("Real V_5V", 1), ("Real I_5V", 2),
        ("Real V_3v3", 3), ("Real I_12V", 4), ("Real V_12V", 5),
        ("Real V_BAT", 6), ("Real I_BAT", 7), ("Real V_SA", 8),
        ("Real T2_BAT", 9), ("Real I_SA", 10), ("Real T1_BAT", 11),
    )
    for label, index in display_keys:
        value = _u16le(body, index * 2)
        if value is not None:
            values[label] = value
    for index in range(len(labels) * 2, len(body)):
        values[f"Status byte {index - len(labels) * 2}"] = f"0x{body[index]:02X}"
    return _with_header(data, values)


def decode_comm(data: bytes, profile: SatelliteProfile = SatelliteProfile.SPACE_KEYS) -> dict[str, Any]:
    if profile is SatelliteProfile.GRAD_PROJECT and len(data) >= 23:
        return _grad_header(data) | {
            "Core-COM mode": COMM_MODES.get(data[1], f"Mode {data[1]}"),
            "Core-COM address": data[0],
            "Total received frames": _u16le(data, 14),
            "Correct received frames": _u16le(data, 16),
            "Last received command": command_name(data[18], profile=profile),
            "Communication rate": _u16le(data, 19),
            "RF output power": _u16le(data, 21),
            "Radio": "HC-12 / UHF",
            "Telemetry layout": "Grad ICD v3.1 Table 14",
        }
    body = data[9:] if has_secondary_header(data) and len(data) == 63 else data
    values: dict[str, Any] = {}
    if body:
        values["Core-COM mode"] = COMM_MODES.get(body[0] >> 5, f"Mode {body[0] >> 5}")
        values["Core-COM address"] = body[0] & 0x1F
    if len(body) >= 11:
        values["Synchronization counter"] = _u16le(body, 7)
        values["OBC UART failure count"] = _u16le(body, 9)
    if len(body) >= 12:
        values["TIMG status"] = f"0x{body[11]:02X}"
    commands = (
        "INIT", "PING", "SM", "SSC", "SON", "SOF", "PD", "WD", "KS_ON", "KS_OFF",
        "STIM", "GD", "RD", "HRST", "GTIM", "GM", "GOTM", "GSTM", "GOSTM", "GIMG", "GIFN",
    )
    for offset, name in enumerate(commands):
        index = 12 + offset * 2
        if index + 1 >= len(body):
            break
        values[f"{name} received"] = body[index]
        values[f"{name} delivered"] = body[index + 1]
    return _with_header(data, values)


def decode_obc(data: bytes, profile: SatelliteProfile = SatelliteProfile.SPACE_KEYS) -> dict[str, Any]:
    if profile is SatelliteProfile.GRAD_PROJECT and len(data) >= 55:
        return _grad_header(data) | {
            "Satellite mode": SATELLITE_MODES.get(data[1], f"Mode {data[1]}"),
            "Longitude": _s32le(data, 14),
            "Latitude": _s32le(data, 18),
            "Velocity North": _u32le(data, 22),
            "Velocity East": _u32le(data, 26),
            "Velocity Down": _u32le(data, 30),
            "iTOW": _u32le(data, 34),
            "Total received SSP frames": _u16le(data, 38),
            "Total transmitted SSP frames": _u16le(data, 40),
            "Last executed command": command_name(data[42], profile=profile),
            "EPS SRecords (index 13)": _u16le(data, 43),
            "EPS SRecords (index 14, ICD duplicate)": _u16le(data, 45),
            "OBC SRecords": _u16le(data, 47),
            "ADCS SRecords": _u16le(data, 49),
            "COMM SRecords": _u16le(data, 51),
            "Payload SRecords": _u16le(data, 53),
            "Telemetry layout": "Grad ICD v3.1 Table 12",
        }
    body = data[9:] if has_secondary_header(data) else data
    values: dict[str, Any] = {}
    if len(body) >= 24:
        values.update({
            "Satellite mode": SATELLITE_MODES.get(body[0], f"Mode {body[0]}"),
            "OBC activity bitmap": f"0x{body[1]:02X}",
            "Last executed command": command_name(body[2]),
            "COMM retry count": body[3],
            "COMM response command": command_name(body[4]),
            "COMM error type": body[5] & 0x7F,
            "COMM active bus": "B" if body[5] & 0x80 else "A",
            "ADCS retry count": body[6],
            "ADCS response command": command_name(body[7]),
            "ADCS error type": body[8] & 0x7F,
            "ADCS active bus": "B" if body[8] & 0x80 else "A",
            "Payload retry count": body[9],
            "Payload response command": command_name(body[10]),
            "Payload error type": body[11] & 0x7F,
            "Payload active bus": "B" if body[11] & 0x80 else "A",
            "Space Env. retry count": body[12],
            "Space Env. response command": command_name(body[13]),
            "Space Env. error type": body[14] & 0x7F,
            "Space Env. active bus": "B" if body[14] & 0x80 else "A",
            "Power switch bitmap": f"0x{(_u16le(body, 15) or 0):04X}",
            "OBC flags": f"0x{body[17]:02X}",
            "OBC current (raw)": body[18],
            "OBC temperature (raw)": body[19],
            "Timed command past due": body[20],
            "Inserted timed command past due": body[21],
            "Last executed timed command": command_name(body[22]),
            "Timed command execution count": body[23],
        })
        switch_bits = _u16le(body, 15) or 0
        # Latch order is defined by pins_mapping.h.  These are the five states
        # presented by the LabVIEW OBC page.
        values.update({
            "PL 5V": "ON" if switch_bits & (1 << 0) else "OFF",
            "Space Env. 5V": "ON" if switch_bits & (1 << 2) else "OFF",
            "ADCS 5V": "ON" if switch_bits & (1 << 6) else "OFF",
            "Space Env. 3.3V": "ON" if switch_bits & (1 << 3) else "OFF",
            "ADCS 3.3V": "ON" if switch_bits & (1 << 5) else "OFF",
            "Latch reading": switch_bits,
        })
    if len(body) >= 28:
        values["System restart count"] = _u16le(body, 24)
        values["Watchdog reset count"] = _u16le(body, 26)
    if len(body) > 28:
        values["Watchdog reset OBT bytes available"] = format_hex(body[28:])
    return _with_header(data, values)


def decode_satellite_status(data: bytes) -> dict[str, Any]:
    body = data[9:] if has_secondary_header(data) else data
    values: dict[str, Any] = {}
    if len(body) >= 11:
        subsystem_status = body[5]
        power_source = {1: "Solar array", 2: "Battery"}.get(body[6], f"0x{body[6]:02X}")
        values.update({
            "Status timestamp (low 32-bit OBT)": _u32le(body, 0),
            "Satellite mode": SATELLITE_MODES.get(body[4], f"Mode {body[4]}"),
            "Subsystem status bitmap": f"0x{subsystem_status:02X}",
            "Power": "ON" if subsystem_status & 0x80 else "OFF",
            "COMM": "ON" if subsystem_status & 0x40 else "OFF",
            "OBC": "ON" if subsystem_status & 0x20 else "OFF",
            "ADCS": "ON" if subsystem_status & 0x10 else "OFF",
            "Payload": "ON" if subsystem_status & 0x08 else "OFF",
            "Space Env.": "ON" if subsystem_status & 0x04 else "OFF",
            "Power source": power_source,
            "Supply voltage (raw)": _u16le(body, 7),
            "Supply current (raw)": _u16le(body, 9),
        })
    else:
        values["Raw status"] = format_hex(body)
    return _with_header(data, values)


def _embedded_subsystem(data: bytes) -> int | None:
    return (data[0] & 0x1F) if has_secondary_header(data) else None


def decode_frame(frame: SSPFrame, profile: SatelliteProfile = SatelliteProfile.SPACE_KEYS) -> tuple[str, dict[str, Any]]:
    profile = SatelliteProfile(profile)
    nodes_by_id = node_by_id_for(profile)
    outer_node = nodes_by_id.get(frame.source)
    outer_name = outer_node.name if outer_node else f"Node 0x{frame.source:02X}"
    name = command_name(frame.command, frame.source, frame.data, profile)

    if name in ("ACK", "NACK", "PEND"):
        return outer_name, {
            "Response": name,
            "For command": command_name(frame.data[0], profile=profile) if frame.data else "—",
        }

    embedded = _embedded_subsystem(frame.data)
    source = embedded if embedded is not None and frame.command in (0xE1, 0xE2, 0xE5, 0xE7) else frame.source
    node = nodes_by_id.get(source)
    subsystem = node.name if node else outer_name

    if frame.command == 0x27 or (frame.command == 0xE7 and (source in (0x00, 0x01, 0x0B) or len(frame.data) == 20)):
        return "Satellite", decode_satellite_status(frame.data)
    if profile is SatelliteProfile.GRAD_PROJECT:
        if source == 0xA1 and frame.command in (0x47, 0x48): return "Core-OBC", decode_obc(frame.data, profile)
        if source == 0xA2 and frame.command in (0x47, 0x48): return "Power / EPS", decode_power(frame.data, profile)
        if source == 0xA3 and frame.command in (0x47, 0x48): return "ADCS", decode_adcs(frame.data, profile)
        if source == 0xA4 and frame.command in (0x47, 0x48): return "Payload", decode_payload(frame.data, profile)
        if source in (0xA5, 0xA6) and frame.command in (0x47, 0x48): return "Core-COM", decode_comm(frame.data, profile)
    if source == 0x01 and frame.command in (0xE1, 0xE2, 0xE5):
        return "Core-OBC", decode_obc(frame.data, profile)
    if source == 0x03 and frame.command in (0xE1, 0xE2, 0xE5):
        return "Core-COM", decode_comm(frame.data, profile)
    if source == 0x05 and frame.command in (0x46, 0xE1, 0xE2, 0xE5):
        return "ADCS", decode_adcs(frame.data, profile)
    if source == 0x07 and frame.command in (0xE1, 0xE2, 0xE5):
        return "Payload", decode_payload(frame.data, profile)
    if source == 0x0D and frame.command in (0x65, 0xE1, 0xE2, 0xE5):
        return "Space Env.", decode_environment(frame.data)
    if source == 0x09 and frame.command in (0xE1, 0xE2, 0xE5):
        return "Power / EPS", decode_power(frame.data, profile)
    return subsystem, {
        "Command": name,
        "Data length": len(frame.data),
        "Raw data": format_hex(frame.data),
    }


@dataclass(slots=True)
class TelemetryRecord:
    timestamp: datetime
    subsystem: str
    values: dict[str, Any]
    frame: SSPFrame


@dataclass
class TelemetryStore:
    limit: int = 1000
    profile: SatelliteProfile = SatelliteProfile.SPACE_KEYS
    records: deque[TelemetryRecord] = field(init=False)
    latest: dict[str, TelemetryRecord] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.records = deque(maxlen=self.limit)

    def add(self, frame: SSPFrame) -> TelemetryRecord:
        subsystem, values = decode_frame(frame, self.profile)
        record = TelemetryRecord(datetime.now(), subsystem, values, frame)
        self.records.append(record)
        self.latest[subsystem] = record
        return record
