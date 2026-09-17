"""Authoritative Space Keys node and command catalog.

Values are taken from the known-good OBC and COMM sources. Some identifiers
have subsystem-specific aliases (for example 0x27 is COMM GSS and OBC GBCN),
so display-name lookup remains distinct from contextual receive decoding.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SatelliteProfile(str, Enum):
    SPACE_KEYS = "Space Keys"
    GRAD_PROJECT = "Grad Project"


@dataclass(frozen=True, slots=True)
class Node:
    name: str
    identifier: int


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    identifier: int
    description: str
    default_data: bytes = b""
    category: str = "General"

    @property
    def display(self) -> str:
        return f"{self.name} (0x{self.identifier:02X})"


NODES = (
    Node("Broadcast", 0x00),
    Node("Core-OBC", 0x01),
    Node("Core-COM", 0x03),
    Node("ADCS", 0x05),
    Node("Payload", 0x07),
    Node("Power / EPS", 0x09),
    Node("Satellite", 0x0B),
    Node("Space Env.", 0x0D),
    Node("Ground Station", 0x11),
)

GRAD_PROJECT_NODES = (
    Node("Broadcast", 0xFF),
    Node("Core-OBC", 0xA1),
    Node("Power / EPS", 0xA2),
    Node("ADCS", 0xA3),
    Node("Payload", 0xA4),
    Node("S-Band", 0xA5),
    Node("Core-COM", 0xA6),  # UHF/HC-12 implementation
    Node("Ground Station", 0xB0),
)


def nodes_for(profile: SatelliteProfile | str) -> tuple[Node, ...]:
    selected = SatelliteProfile(profile)
    return GRAD_PROJECT_NODES if selected is SatelliteProfile.GRAD_PROJECT else NODES


def node_by_name_for(profile: SatelliteProfile | str) -> dict[str, Node]:
    return {node.name: node for node in nodes_for(profile)}


def node_by_id_for(profile: SatelliteProfile | str) -> dict[int, Node]:
    return {node.identifier: node for node in nodes_for(profile)}

NODE_BY_NAME = {node.name: node for node in NODES}
NODE_BY_ID = {node.identifier: node for node in NODES}


SPACE_KEYS_COMMANDS = (
    CommandSpec("PING", 0x00, "Verify that the selected subsystem is online."),
    CommandSpec("INIT", 0x01, "Initialize the COMM-to-ground command session. Start with N(S)=0 and destination Core-COM.", category="Session"),
    CommandSpec("ACK", 0x02, "Positive acknowledgement. Data normally identifies the acknowledged command.", category="Protocol"),
    CommandSpec("NACK", 0x03, "Negative acknowledgement. Data normally identifies the rejected command.", category="Protocol"),
    CommandSpec("GD", 0x04, "Get two bytes from an OBC configuration address.", b"\x00\x00", "Memory"),
    CommandSpec("PD", 0x05, "Put/update configuration data at the selected subsystem.", b"\x00\x00\x00\x00", "Memory"),
    CommandSpec("RD", 0x06, "Read a data stream or device parameter.", b"\x00\x01\x0C", "Memory"),
    CommandSpec("WD", 0x07, "Write a data stream or device parameter.", b"\x00\x05\x00\x00", "Memory"),
    CommandSpec("END", 0x09, "End the current COMM command session and return to standby.", category="Session"),
    CommandSpec("SON", 0x0B, "Switch on the selected subsystem or experiment latch.", b"\x01", "Power"),
    CommandSpec("SOF", 0x0C, "Switch off the selected subsystem or experiment latch.", b"\x01", "Power"),
    CommandSpec("SCAP", 0x0D, "Select the OBC super-capacitor power path.", category="Power"),
    CommandSpec("HRST", 0x0F, "Request a satellite hard-reset pulse.", category="Power"),
    CommandSpec("STIM", 0x11, "Set OBC calendar time as unsigned 64-bit little-endian seconds since 2000-01-01.", b"\x00" * 8, "Time"),
    CommandSpec("GTIM", 0x12, "Get OBC time. Reply command is 0x52.", category="Time"),
    CommandSpec("SM", 0x15, "Set subsystem or satellite mode.", b"\x02", "Modes"),
    CommandSpec("GM", 0x16, "Get subsystem or satellite mode. Reply command is 0x56.", category="Modes"),
    CommandSpec("SSC", 0x17, "Set the COMM synchronization counter from two big-endian bytes.", b"\x00\x00", "Time"),
    CommandSpec("SRBC", 0x1B, "Set the OBC beacon-rate counter.", b"\x00\x0A", "Telemetry"),
    CommandSpec("GOTM", 0x21, "Get the next online telemetry frame/window. Reply command is 0xE1.", category="Telemetry"),
    CommandSpec("GSTM", 0x22, "Get or resume stored telemetry. Reply command is 0xE2.", category="Telemetry"),
    CommandSpec("GOSTM", 0x25, "Get one-shot telemetry from the selected subsystem. Reply normally uses 0xE5.", category="Telemetry"),
    CommandSpec("GSSTM", 0x26, "Get stored telemetry for one selected subsystem.", category="Telemetry"),
    CommandSpec("GSS / GBCN", 0x27, "Get satellite status (COMM GSS) or OBC beacon telemetry (GBCN).", category="Telemetry"),
    CommandSpec("TIMG", 0x28, "Transfer the captured JPEG into Payload storage; Wi-Fi serves the stored image instead of raw HC-12 bytes.", category="Camera"),
    CommandSpec("GIMG", 0x29, "Request or resume the current image; Payload also exposes it over Wi-Fi.", category="Camera"),
    CommandSpec("CIMG", 0x2B, "Capture an image. Optional data: resolution, JPEG quality, flash, effect, signed brightness.", b"\x04\x09\x01\x00\x00", "Camera"),
    CommandSpec("GIFN / SIMGID", 0x2C, "Get image-frame count in COMM context or select an image ID in OBC context.", b"\x00\x00\x00\x00", "Camera"),
    CommandSpec("GCMI", 0x2F, "Get the current OBC camera-memory image ID.", category="Camera"),
    CommandSpec("KS_ON / TSM1", 0x31, "Enable kill-switch/test-mode function 1 (subsystem context dependent).", category="Power"),
    CommandSpec("KS_OFF / TSM2", 0x32, "Disable kill-switch/test-mode function 2 (subsystem context dependent).", category="Power"),
    CommandSpec("GXBID", 0x33, "Legacy XBee PAN-ID query. Core-COM returns NACK in the HC-12 port.", category="Communication"),
    CommandSpec("WRFL", 0x34, "Legacy COMM external-flash write. Core-COM returns NACK in the port.", category="Communication"),
    CommandSpec("RDFL", 0x35, "Legacy COMM external-flash read. Core-COM returns NACK in the port.", b"\x20", "Communication"),
)

GRAD_PROJECT_COMMANDS = (
    CommandSpec("HI", 0x01, "Announce the subsystem presence. The ICD defines no reply.", category="Protocol"),
    CommandSpec("ACK", 0x02, "Positive acknowledgement. Data identifies the acknowledged command.", b"\x01", "Protocol"),
    CommandSpec("NACK", 0x03, "Negative acknowledgement. Data identifies the rejected command.", b"\x01", "Protocol"),
    CommandSpec("PING", 0x04, "Verify that the selected subsystem is online. The request has no data."),
    CommandSpec("STIME", 0x05, "Set OBC time using the ICD eight-byte time field.", b"\x00" * 8, "Time"),
    CommandSpec("SMODE", 0x06, "Set spacecraft mode: 01 initialization, 02 de-tumbling, 03 normal.", b"\x01", "Modes"),
    CommandSpec("GOTLM", 0x07, "Get online telemetry. The telemetry reply command is 0x47.", category="Telemetry"),
    CommandSpec("GSTLM", 0x08, "Get stored telemetry: subsystem address plus two-byte sequence number.", b"\xA2\x00\x01", "Telemetry"),
    CommandSpec("SON", 0x09, "Switch on power line E1 through E9.", b"\xE1", "Power"),
    CommandSpec("SOFF", 0x0A, "Switch off power line E1 through E9.", b"\xE1", "Power"),
    CommandSpec("CIMG", 0x0C, "Capture an image using the payload camera parameters.", b"\x04\x09\x01\x00\x00", "Camera"),
    CommandSpec("DIMG", 0x0D, "Delete the selected two-byte image identifier.", b"\x00\x00", "Camera"),
    CommandSpec("GIMG", 0x0E, "Get image: image ID (2), sequence (4), window (2), all little-endian.", b"\x00" * 8, "Camera"),
)


def commands_for(profile: SatelliteProfile | str) -> tuple[CommandSpec, ...]:
    selected = SatelliteProfile(profile)
    return GRAD_PROJECT_COMMANDS if selected is SatelliteProfile.GRAD_PROJECT else SPACE_KEYS_COMMANDS


def command_by_display_for(profile: SatelliteProfile | str) -> dict[str, CommandSpec]:
    return {command.display: command for command in commands_for(profile)}


def command_by_id_for(profile: SatelliteProfile | str) -> dict[int, CommandSpec]:
    return {command.identifier: command for command in commands_for(profile)}


# Backward-compatible Space Keys aliases for modules that do not select a profile.
COMMANDS = SPACE_KEYS_COMMANDS
COMMAND_BY_DISPLAY = command_by_display_for(SatelliteProfile.SPACE_KEYS)
COMMAND_BY_ID = command_by_id_for(SatelliteProfile.SPACE_KEYS)


REPLY_NAMES = {
    0x30: "IMAGE SIZE",
    0x44: "GD RESPONSE",
    0x46: "RD RESPONSE",
    0x52: "GTIM RESPONSE",
    0x56: "GM RESPONSE",
    0x65: "GOSTM TELEMETRY (Grad / Space Env.)",
    0x69: "GIMG RESPONSE",
    0xE1: "GOTM TELEMETRY",
    0xE2: "GSTM TELEMETRY",
    0xE5: "GOSTM TELEMETRY",
    0xE7: "SATELLITE STATUS / BEACON",
}


def command_name(
    identifier: int,
    source: int | None = None,
    data: bytes = b"",
    profile: SatelliteProfile | str = SatelliteProfile.SPACE_KEYS,
) -> str:
    """Resolve collisions using source/data context without changing wire IDs."""
    identifier &= 0xFF
    selected = SatelliteProfile(profile)
    if selected is SatelliteProfile.GRAD_PROJECT:
        if identifier == 0x47:
            return "GOTLM TELEMETRY"
        if identifier == 0x48:
            return "GSTLM TELEMETRY"
        spec = command_by_id_for(selected).get(identifier)
        return spec.name if spec else f"0x{identifier:02X}"
    if identifier == 0x04 and source in (0x05, 0x07) and data and data[0] in (0x28, 0x29, 0x2B, 0x2C):
        return "PEND"
    if identifier == 0x27:
        return "GBCN" if source == 0x01 else "GSS"
    if identifier == 0x2C:
        return "SIMGID" if source == 0x01 else "GIFN"
    if identifier == 0x31:
        return "TSM1" if source == 0x01 else "KS_ON"
    if identifier == 0x32:
        return "TSM2" if source == 0x01 else "KS_OFF"
    if identifier in REPLY_NAMES:
        return REPLY_NAMES[identifier]
    spec = COMMAND_BY_ID.get(identifier)
    return spec.name if spec else f"0x{identifier:02X}"


CAMERA_RESOLUTIONS = (
    ("QQVGA 160 x 120", 0),
    ("HQVGA 240 x 160", 1),
    ("QVGA 320 x 240", 2),
    ("CIF 352 x 288", 3),
    ("VGA 640 x 480", 4),
    ("SVGA 800 x 600", 5),
    ("XGA 1024 x 768", 6),
    ("SXGA 1280 x 1024", 7),
    ("UXGA 1600 x 1200", 8),
)

CAMERA_EFFECTS = (
    ("No effect", 0),
    ("Negative", 1),
    ("Grayscale", 2),
    ("Red tint", 3),
    ("Green tint", 4),
    ("Blue tint", 5),
    ("Sepia", 6),
)

PAYLOAD_MODES = (
    ("Standby", 0x02),
    ("Image Payload", 0x04),
    ("Relay Communication", 0x07),
)
