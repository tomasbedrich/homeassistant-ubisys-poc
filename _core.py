"""Ubisys device setup cluster — models, parser, encoder and capabilities.

This module has zero dependencies on Home Assistant or zigpy and is designed to
be unit-tested in isolation.  It is the foundation for the full ubisys integration.

Supported devices (all use setup cluster 0xFC00 on endpoint 0xE8)
-----------------------------------------------------------------
C4    — 4-input wall switch
S1    — 1-input + 1-relay panel mount
S1-R  — 1-input + 1-relay DIN-rail (relay variant of S1)
S2    — 2-input + 2-relay panel mount
S2-R  — 2-input + 2-relay DIN-rail (relay variant of S2)
D1    — 2-input + 1-channel triac dimmer, panel mount
D1-R  — 2-input + 1-channel triac dimmer, DIN-rail
J1    — 2-input shutter/blind actuator (identical input layout to S2)
J1-R  — 2-input shutter/blind actuator, DIN-rail
LD6   — 6-channel LED driver, up to 3 inputs
R0    — Zigbee router only; no device-setup cluster

References: ubisys product documentation, manufacturer ID 0x10F2.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
import struct

_LOGGER = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# MODELS
# Frozen dataclasses and enums.  No logic, no I/O.
# ─────────────────────────────────────────────────────────────────────────────


class InputState(Enum):
    """Physical input state used in the Transition field of InputActions.

    Docs: §7.7.5.2 / §7.8.5.2 / §6.13.3.1.2 — bits 3-2 (initial) and 1-0 (final).
    """

    IGNORED = 0b00  # Don't care — match any state
    PRESSED = 0b01  # Active for less than one second
    KEPT_PRESSED = 0b10  # Active for more than one second
    RELEASED = 0b11  # Inactive

    def __str__(self) -> str:
        return self.name.lower().replace("_", " ")


class OutputFunction(Enum):
    """Colour primary / function of one output channel on the LD6.

    Docs: §6.13.3.3.1 — bits 3-0 of EndpointAndFunction.
    """

    MONO = 0x0  # M  — monochromatic / dimmable
    CW = 0x1  # CW — cool white (first white)
    WW = 0x2  # WW — warm white  (second white)
    RED = 0x3  # R
    GREEN = 0x4  # G
    BLUE = 0x5  # B
    AMBER = 0x6  # A
    TURQUOISE = 0x7  # T
    VIOLET = 0x8  # V
    FREE = 0x9  # F  — arbitrary / free colour

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class InputConfiguration:
    """Configuration for one physical input.

    Applies to all devices with physical inputs (attr 0x0000 of the setup cluster).

    Wire format: one unsigned8 per input.
        Bit 7 (0x80) — Disable: electrically disables the input when set.
        Bit 6 (0x40) — Invert:  active-low / normally-closed when set.
        Bits 5-0     — Reserved; always zero on read.

    Docs: §7.7.5.1 (S2/S2-R), §6.13.3.1.1 (LD6).
    """

    input_index: int  # Zero-based position in the InputConfigurations array
    disabled: bool  # Bit 7
    inverted: bool  # Bit 6; True → normally-closed / active-low


@dataclass(frozen=True)
class InputAction:
    """One entry in the InputActions attribute array.

    Wire layout (variable length, minimum 5 bytes):
        [0]   InputAndOptions  — inputMask (bits 3-0), flagManufSpecific (bit 4, LD6 only)
        [1]   Transition       — HasAlt(7), Alt(6), InitState(3-2), FinalState(1-0)
        [2]   Endpoint         — source endpoint for the outgoing ZCL frame
        [3:5] ClusterID        — little-endian uint16
        [5:]  CommandTemplate  — raw ZCL frame payload (no leading length byte)

    Docs: §7.7.5.2 (S2), §7.8.5.2 (C4), §6.13.3.1.2 (LD6).
    """

    input_index: int  # Physical input index (bits 3-0 of InputAndOptions)
    manufacturer_specific: (
        bool  # Bit 4 of InputAndOptions (LD6 only; always False on C4/S2)
    )
    has_alternate: bool  # Transition bit 7 — sibling entry alternates with this one
    is_alternate: bool  # Transition bit 6 — this entry is the alternate of a pair
    initial_state: InputState  # Transition bits 3-2
    final_state: InputState  # Transition bits 1-0
    source_endpoint: int  # Local endpoint originating the ZCL command
    cluster_id: int  # ZCL cluster ID
    command_template: bytes  # Raw ZCL frame payload (variable length)


_OUTPUT_CHANNEL_STRUCT = struct.Struct(
    "<BBHH"
)  # EndpointAndFunction, Flux, CIE-x, CIE-y
OUTPUT_CHANNEL_WIRE_LEN: int = _OUTPUT_CHANNEL_STRUCT.size  # 6 bytes

_FLUX_INVALID: int = 0xFF  # Sentinel: flux not applicable for this channel
_CIE_INVALID: int = 0xFFFF  # Sentinel: CIE coordinate not applicable


@dataclass(frozen=True)
class OutputChannel:
    """Configuration for one physical output channel on the LD6.

    LD6 always has exactly 6 channels.  Unused channels have endpoint == 0.

    Wire layout: 6 bytes per channel (struct "<BBHH").
        [0]   EndpointAndFunction — endpoint mask (bits 7-4), function (bits 3-0)
        [1]   Flux (normalised)   — uint8; 0xFF = not applicable
        [2:4] CIE 1931 x          — little-endian uint16; 0xFFFF = not applicable
        [4:6] CIE 1931 y          — little-endian uint16; 0xFFFF = not applicable

    Coordinate conversions (per spec):
        normalised_flux = raw_flux  / 254    (0xFF excluded)
        x               = raw_cie_x / 65536  (0xFFFF excluded)
        y               = raw_cie_y / 65536  (0xFFFF excluded)

    Docs: §6.13.3.3.1 (LD6).
    """

    channel_index: int  # Zero-based position in OutputConfigurations array (0–5)
    endpoint: int  # Logical light endpoint; 0 means this channel is unused
    function: OutputFunction
    raw_flux: int  # 0..254 = valid; 0xFF = not applicable
    raw_cie_x: int  # 0..65279 = valid; 0xFFFF = not applicable
    raw_cie_y: int  # 0..65279 = valid; 0xFFFF = not applicable

    @property
    def is_active(self) -> bool:
        """Return True when this channel is assigned to a logical endpoint."""
        return self.endpoint != 0

    @property
    def flux(self) -> float | None:
        """Normalised luminous flux in [0, 1], or None when not applicable."""
        return None if self.raw_flux == _FLUX_INVALID else self.raw_flux / 254

    @property
    def cie_x(self) -> float | None:
        """CIE 1931 x chromaticity coordinate, or None when not applicable."""
        return None if self.raw_cie_x == _CIE_INVALID else self.raw_cie_x / 65536

    @property
    def cie_y(self) -> float | None:
        """CIE 1931 y chromaticity coordinate, or None when not applicable."""
        return None if self.raw_cie_y == _CIE_INVALID else self.raw_cie_y / 65536


# ─────────────────────────────────────────────────────────────────────────────
# DEVICE CAPABILITIES
# Central table mapping model strings → static device properties.
# Add a row here when a new model is introduced.
# ─────────────────────────────────────────────────────────────────────────────


class UbisysModel(Enum):
    """Known ubisys device models.

    Enum values are the zigbeeModel strings reported by devices in the ZCL
    Basic cluster (model identifier attribute).  These are used as keys in
    _DEVICE_TABLE for model lookup.
    """

    C4 = "C4 (5504)"
    S1 = "S1 (5501)"
    S1_R = "S1-R (5601)"
    S2 = "S2 (5502)"
    S2_R = "S2-R (5602)"
    D1 = "D1 (5503)"
    D1_R = "D1-R (5603)"
    J1 = "J1 (5502)"
    J1_R = "J1-R (5602)"
    LD6 = "LD6"  # device reports bare "LD6" without a product code
    R0 = "R0 (5501)"  # Zigbee router only — no device-setup cluster


@dataclass(frozen=True)
class DeviceCapabilities:
    """Static capabilities of a ubisys device model.

    Acts as the single source of truth for what a given model supports.
    Consumed by both the reader (to decide which attributes to read) and
    the formatter (to label output correctly).
    """

    model: UbisysModel
    input_count: int  # Number of physical inputs (0 = no setup cluster)
    has_input_configurations: bool  # Exposes InputConfigurations (attr 0x0000)
    has_output_configurations: bool  # Exposes OutputConfigurations (attr 0x0010)
    # First client endpoint for on/off/dimmer commands originating from input 0.
    # Subsequent inputs use base + input_index (mirrors Zigbee2MQTT defaults).
    # None means the mapping is non-trivial and source_endpoint must be supplied
    # explicitly (e.g. LD6 with its configurable output channel endpoints).
    source_endpoint_base: int | None = None
    # First client endpoint for cover commands from input 0, when different from
    # source_endpoint_base.  None means cover commands use the same base as all
    # other commands (i.e. no separate cover endpoint range for this model).
    cover_endpoint_base: int | None = None

    @property
    def has_config(self) -> bool:
        """Return True when the device exposes the setup cluster with anything to read."""
        return self.input_count > 0 or self.has_output_configurations

    def resolve_source_endpoint(self, input_index: int, preset: str) -> int | None:
        """Return the source endpoint for an input/preset pair, or None if unknown.

        When the preset routes through the window-covering cluster and this device
        has a separate cover_endpoint_base, that base is used instead of
        source_endpoint_base.  Returns None when the relevant base is None,
        meaning the caller must supply source_endpoint explicitly.
        """
        is_cover = preset in _COVER_PRESET_NAMES
        base = (
            self.cover_endpoint_base
            if (is_cover and self.cover_endpoint_base is not None)
            else self.source_endpoint_base
        )
        return None if base is None else base + input_index


# Preset names that route through the window-covering cluster.
# On C4, these use a separate client endpoint range (5-8) rather than
# the on/off range (1-4).  Kept as a frozenset for O(1) lookup.
_COVER_PRESET_NAMES: frozenset[str] = frozenset(
    {
        "cover",
        "cover_switch",
        "cover_up",
        "cover_down",
    }
)

_DEVICE_TABLE: dict[str, DeviceCapabilities] = {
    # model                  inputs  in_cfg  out_cfg  ep_base
    # C4: on/off client eps 1-4; cover client eps 5-8 (base 5 = 1 + 4 offset).
    UbisysModel.C4.value: DeviceCapabilities(
        UbisysModel.C4, 4, True, False, source_endpoint_base=1, cover_endpoint_base=5
    ),
    # S1 / S1-R: single input; client endpoint 2 hosts the switch cluster.
    UbisysModel.S1.value: DeviceCapabilities(
        UbisysModel.S1, 1, True, False, source_endpoint_base=2
    ),
    UbisysModel.S1_R.value: DeviceCapabilities(
        UbisysModel.S1_R, 1, True, False, source_endpoint_base=2
    ),
    # S2 / S2-R: two inputs; relay client endpoints start at 3.
    UbisysModel.S2.value: DeviceCapabilities(
        UbisysModel.S2, 2, True, False, source_endpoint_base=3
    ),
    UbisysModel.S2_R.value: DeviceCapabilities(
        UbisysModel.S2_R, 2, True, False, source_endpoint_base=3
    ),
    # D1 / D1-R: two inputs; dimmer client endpoint starts at 2.
    UbisysModel.D1.value: DeviceCapabilities(
        UbisysModel.D1, 2, True, False, source_endpoint_base=2
    ),
    UbisysModel.D1_R.value: DeviceCapabilities(
        UbisysModel.D1_R, 2, True, False, source_endpoint_base=2
    ),
    # J1 / J1-R: two inputs; cover client endpoint starts at 2.
    UbisysModel.J1.value: DeviceCapabilities(
        UbisysModel.J1, 2, True, False, source_endpoint_base=2
    ),
    UbisysModel.J1_R.value: DeviceCapabilities(
        UbisysModel.J1_R, 2, True, False, source_endpoint_base=2
    ),
    # LD6: output channel endpoints are configured via OutputConfigurations
    # and are not statically known; source_endpoint must be supplied explicitly.
    UbisysModel.LD6.value: DeviceCapabilities(
        UbisysModel.LD6, 3, True, True, source_endpoint_base=None
    ),
    UbisysModel.R0.value: DeviceCapabilities(
        UbisysModel.R0, 0, False, False, source_endpoint_base=None
    ),
}


def get_capabilities(model: str) -> DeviceCapabilities | None:
    """Return DeviceCapabilities for a model string, or None if unrecognised."""
    return _DEVICE_TABLE.get(model)


# ─────────────────────────────────────────────────────────────────────────────
# PARSER
# Pure functions: primitive Python types → dataclasses.
# No I/O, no external dependencies — directly unit-testable without mocking.
# ─────────────────────────────────────────────────────────────────────────────

_INPUT_ACTIONS_MIN_LEN: int = 5  # Fixed header bytes before CommandTemplate


def parse_input_configurations(raw: list[int]) -> list[InputConfiguration]:
    """Parse raw uint8 values into InputConfiguration objects.

    Args:
        raw: One byte per physical input as returned by the device.

    Returns:
        List of InputConfiguration, index-aligned with the wire array.
    """
    return [
        InputConfiguration(
            input_index=idx,
            disabled=bool(byte & 0x80),
            inverted=bool(byte & 0x40),
        )
        for idx, byte in enumerate(raw)
    ]


def parse_input_actions(raw: list[bytes]) -> list[InputAction]:
    """Parse raw byte strings into InputAction objects.

    Entries shorter than the 5-byte header are skipped; a warning is logged
    for each one.

    Args:
        raw: One bytes object per array element as returned by the device.

    Returns:
        List of InputAction in the original array order.
    """
    actions = []
    for i, entry in enumerate(raw):
        if len(entry) < _INPUT_ACTIONS_MIN_LEN:
            _LOGGER.warning(
                "InputActions entry %d too short (%d bytes), skipping: %s",
                i,
                len(entry),
                entry.hex(),
            )
            continue

        transition = entry[1]
        actions.append(
            InputAction(
                input_index=entry[0] & 0x0F,
                manufacturer_specific=bool(entry[0] & 0x10),
                has_alternate=bool(transition & 0x80),
                is_alternate=bool(transition & 0x40),
                initial_state=InputState((transition >> 2) & 0x03),
                final_state=InputState(transition & 0x03),
                source_endpoint=entry[2],
                cluster_id=entry[3] | (entry[4] << 8),
                command_template=entry[5:],
            )
        )
    return actions


def parse_output_configurations(raw: list[bytes]) -> list[OutputChannel]:
    """Parse raw byte strings into OutputChannel objects (LD6, §6.13.3.3.1).

    Each entry must be exactly OUTPUT_CHANNEL_WIRE_LEN (6) bytes.
    Entries of the wrong length or with an unknown function code are skipped.

    Args:
        raw: One bytes object per channel (always 6 entries on LD6).

    Returns:
        List of OutputChannel in channel order (index 0 = physical output 1).
    """
    channels = []
    for i, entry in enumerate(raw):
        if len(entry) != OUTPUT_CHANNEL_WIRE_LEN:
            _LOGGER.warning(
                "OutputConfigurations entry %d: expected %d bytes, got %d, skipping",
                i,
                OUTPUT_CHANNEL_WIRE_LEN,
                len(entry),
            )
            continue

        ep_fn, flux, cie_x, cie_y = _OUTPUT_CHANNEL_STRUCT.unpack(entry)
        try:
            function = OutputFunction(ep_fn & 0x0F)
        except ValueError:
            _LOGGER.warning(
                "OutputConfigurations entry %d: unknown function code 0x%02x, skipping",
                i,
                ep_fn & 0x0F,
            )
            continue

        channels.append(
            OutputChannel(
                channel_index=i,
                endpoint=(ep_fn >> 4) & 0x0F,
                function=function,
                raw_flux=flux,
                raw_cie_x=cie_x,
                raw_cie_y=cie_y,
            )
        )
    return channels


# ─────────────────────────────────────────────────────────────────────────────
# ENCODERS
# Pure functions: dicts → wire bytes/integers.  Exact reversal of the parsers.
# No I/O, no external dependencies — directly unit-testable without mocking.
# ─────────────────────────────────────────────────────────────────────────────


def encode_input_configuration(d: dict) -> int:
    """Encode an InputConfiguration dict to the uint8 wire value.

    Expected keys: ``disabled`` (bool), ``inverted`` (bool).  Missing keys
    default to ``False`` so a partial dict is safe to pass.

    Returns:
        A single uint8 (bit 7 = disabled, bit 6 = inverted, bits 5-0 = 0).
    """
    return (0x80 if d.get("disabled", False) else 0) | (
        0x40 if d.get("inverted", False) else 0
    )


def encode_input_action(d: dict) -> bytes:
    """Encode an InputAction dict to variable-length wire bytes.

    Expected keys match the output of serialise_input_action:
    ``input_index``, ``manufacturer_specific``, ``has_alternate``,
    ``is_alternate``, ``initial_state`` (lowercase name), ``final_state``
    (lowercase name), ``source_endpoint``, ``cluster_id``, ``command_template``
    (list of ints).

    Returns:
        At least 5 bytes representing the wire entry.

    Raises:
        KeyError: If a required key is missing.
        KeyError: If ``initial_state`` or ``final_state`` is not a valid
            :class:`InputState` name.
    """
    byte0 = (d["input_index"] & 0x0F) | (
        0x10 if d.get("manufacturer_specific", False) else 0
    )
    initial = InputState[d["initial_state"].upper()]
    final = InputState[d["final_state"].upper()]
    byte1 = (
        (0x80 if d.get("has_alternate", False) else 0)
        | (0x40 if d.get("is_alternate", False) else 0)
        | (initial.value << 2)
        | final.value
    )
    cluster_id_bytes = struct.pack("<H", d["cluster_id"])
    command = bytes(d.get("command_template", []))
    return bytes([byte0, byte1, d["source_endpoint"]]) + cluster_id_bytes + command


def encode_output_channel(d: dict) -> bytes:
    """Encode an OutputChannel dict to 6-byte wire format (LD6, §6.13.3.3.1).

    Expected keys match the output of serialise_output_channel:
    ``endpoint``, ``function`` (uppercase name), ``raw_flux``, ``raw_cie_x``,
    ``raw_cie_y``.  ``channel_index`` is ignored on write.

    Returns:
        Exactly 6 bytes (struct ``<BBHH``).

    Raises:
        KeyError: If a required key is missing or ``function`` is an unknown
            :class:`OutputFunction` name.
    """
    function = OutputFunction[d["function"].upper()]
    ep_fn = ((d["endpoint"] & 0x0F) << 4) | (function.value & 0x0F)
    return _OUTPUT_CHANNEL_STRUCT.pack(
        ep_fn, d["raw_flux"], d["raw_cie_x"], d["raw_cie_y"]
    )
