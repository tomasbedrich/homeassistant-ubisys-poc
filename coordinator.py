"""Data update coordinator and ZHA I/O layer for Ubisys devices."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
import struct

from zha.application.gateway import Device, Gateway
import zigpy.types as zigpy_t
import zigpy.zcl
from zigpy.zcl import foundation as zcl_foundation

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ._core import (
    DeviceCapabilities,
    InputAction,
    InputConfiguration,
    OutputChannel,
    get_capabilities,
    parse_input_actions,
    parse_input_configurations,
    parse_output_configurations,
)
from .const import (
    ATTR_INPUT_ACTIONS,
    ATTR_INPUT_CONFIGURATIONS,
    ATTR_OUTPUT_CONFIGURATIONS,
    DOMAIN,
    UBISYS_SETUP_CLUSTER_ID,
    UBISYS_SETUP_ENDPOINT_ID,
)

_LOGGER = logging.getLogger(__name__)

_UPDATE_INTERVAL = timedelta(hours=1)


# ─────────────────────────────────────────────────────────────────────────────
# READER
# Thin async I/O layer — the only code that touches zigpy internals.
# Uses read_attributes_raw to bypass zigpy's schema lookup, which would fail
# for manufacturer-specific clusters with no registered attribute definitions.
# ─────────────────────────────────────────────────────────────────────────────


async def _read_array_attr(cluster: zigpy.zcl.Cluster, attr_id: int) -> list:
    """Read one array attribute from a zigpy cluster and return its elements.

    Raises:
        ValueError: On transport error, unexpected response shape, or non-SUCCESS status.
    """
    result = await cluster.read_attributes_raw([attr_id])

    if not isinstance(result[0], list) or not result[0]:
        raise ValueError(f"Unexpected response for attr 0x{attr_id:04x}: {result!r}")

    rec = result[0][0]
    if rec.status.name != "SUCCESS":
        raise ValueError(f"Attr 0x{attr_id:04x} read status: {rec.status}")

    entries = rec.value.value
    if not isinstance(entries, list):
        raise TypeError(
            f"Expected list for attr 0x{attr_id:04x}, got {type(entries).__name__}"
        )
    return entries


async def read_input_configurations(
    cluster: zigpy.zcl.Cluster,
) -> list[InputConfiguration]:
    """Read and parse InputConfigurations (attr 0x0000) from a setup cluster."""
    raw = await _read_array_attr(cluster, ATTR_INPUT_CONFIGURATIONS)
    # zigpy returns data8 objects (bytes-like, not int-like) for ZCL type 0x08.
    return parse_input_configurations([bytes(e)[0] for e in raw])


async def read_input_actions(cluster: zigpy.zcl.Cluster) -> list[InputAction]:
    """Read and parse InputActions (attr 0x0001) from a setup cluster."""
    raw = await _read_array_attr(cluster, ATTR_INPUT_ACTIONS)
    return parse_input_actions([bytes(e) for e in raw])


async def read_output_configurations(
    cluster: zigpy.zcl.Cluster,
) -> list[OutputChannel]:
    """Read and parse OutputConfigurations (attr 0x0010) from a setup cluster (LD6)."""
    raw = await _read_array_attr(cluster, ATTR_OUTPUT_CONFIGURATIONS)
    return parse_output_configurations([bytes(e) for e in raw])


# ─────────────────────────────────────────────────────────────────────────────
# WRITER
# Thin async I/O layer for writing attributes to the setup cluster.
# Parallel to the READER section above.
# ─────────────────────────────────────────────────────────────────────────────


async def _write_array_attr(
    cluster: zigpy.zcl.Cluster,
    attr_id: int,
    element_type: int,
    elements: list,
) -> None:
    """Write one array attribute to a zigpy cluster.

    Builds the ZCL Array value: element_type byte + count (uint16 LE) +
    serialized elements.  For octet_str elements (type 0x41) each element
    is prefixed with its 1-byte length.  For data8 elements (type 0x08)
    each element is a single byte.

    Calls write_attributes_raw with manufacturer_code=None per FINDINGS.md.
    Passing UBISYS_MANUFACTURER_ID causes the device to reject the write with
    UNSUPPORTED_ATTRIBUTE (0x86) — the manufacturer bit must be absent on writes.

    Raises:
        ValueError: On transport error or non-SUCCESS write response status.
    """
    count = len(elements)
    payload = bytes([element_type]) + struct.pack("<H", count)
    for element in elements:
        if isinstance(element, (bytes, bytearray)):
            payload += bytes([len(element)]) + bytes(element)
        else:
            payload += bytes([int(element) & 0xFF])
    _LOGGER.debug(
        "write_array_attr attr=0x%04x element_type=0x%02x count=%d payload=%s",
        attr_id,
        element_type,
        count,
        payload.hex(),
    )
    # Build a proper foundation.Attribute with a hand-serialized ZCL Array value.
    # TypeValue.serialize() = type_byte + value.serialize(), so zigpy_t.Bytes wraps
    # the array payload bytes and serializes them verbatim.
    tv = zcl_foundation.TypeValue(type=0x48, value=zigpy_t.Bytes(payload))
    attr_record = zcl_foundation.Attribute(attrid=attr_id, value=tv)
    # manufacturer_code=None — DO NOT pass UBISYS_MANUFACTURER_ID here.
    # Reads on cluster 0xFC00 require the manufacturer bit in the ZCL header,
    # but writes are rejected with UNSUPPORTED_ATTRIBUTE when that bit is set.
    # (Same behaviour observed in the z2m ecosystem with this cluster.)
    result = await cluster.write_attributes_raw(
        [attr_record],
        manufacturer_code=None,
    )
    _LOGGER.debug("write_array_attr attr=0x%04x response: %r", attr_id, result)
    if result and isinstance(result[0], list):
        for rec in result[0]:
            if rec.status.name != "SUCCESS":
                raise ValueError(f"Write attr 0x{attr_id:04x} failed: {rec.status}")


async def write_input_configurations(
    cluster: zigpy.zcl.Cluster, values: list[int]
) -> None:
    """Write InputConfigurations (attr 0x0000) to a setup cluster.

    Args:
        cluster: The ubisys setup cluster to write to.
        values: One uint8 per physical input (as produced by
            encode_input_configuration).
    """
    await _write_array_attr(cluster, ATTR_INPUT_CONFIGURATIONS, 0x08, values)


async def write_input_actions(cluster: zigpy.zcl.Cluster, entries: list[bytes]) -> None:
    """Write InputActions (attr 0x0001) to a setup cluster.

    Args:
        cluster: The ubisys setup cluster to write to.
        entries: One bytes object per action entry (as produced by
            encode_input_action).
    """
    await _write_array_attr(cluster, ATTR_INPUT_ACTIONS, 0x41, entries)


async def write_output_configurations(
    cluster: zigpy.zcl.Cluster, entries: list[bytes]
) -> None:
    """Write OutputConfigurations (attr 0x0010) to a setup cluster (LD6).

    Args:
        cluster: The ubisys setup cluster to write to.
        entries: One 6-byte object per channel (as produced by
            encode_output_channel).
    """
    await _write_array_attr(cluster, ATTR_OUTPUT_CONFIGURATIONS, 0x41, entries)


# ─────────────────────────────────────────────────────────────────────────────
# SCANNER / DEVICE LOOKUP
# ZHA discovery — the only code that depends on the HA gateway.
# ─────────────────────────────────────────────────────────────────────────────


def get_setup_cluster(dev: Device) -> zigpy.zcl.Cluster | None:
    """Return the ubisys setup cluster from a ZHA device's zigpy device, or None.

    Checks in_clusters first, then out_clusters.  Some firmware versions
    register the manufacturer-specific setup cluster as an output cluster.
    """
    endpoint = dev.device.endpoints.get(UBISYS_SETUP_ENDPOINT_ID)
    if endpoint is None:
        available = sorted(dev.device.endpoints.keys())
        _LOGGER.warning(
            "[%s] ep 0x%02x not found; available endpoints: %s",
            dev.ieee,
            UBISYS_SETUP_ENDPOINT_ID,
            [f"0x{e:02x}" for e in available],
        )
        return None
    cluster: zigpy.zcl.Cluster | None = endpoint.in_clusters.get(
        UBISYS_SETUP_CLUSTER_ID
    ) or endpoint.out_clusters.get(UBISYS_SETUP_CLUSTER_ID)
    if cluster is None:
        all_clusters = sorted(
            [f"0x{c:04x}(in)" for c in endpoint.in_clusters]
            + [f"0x{c:04x}(out)" for c in endpoint.out_clusters]
        )
        _LOGGER.warning(
            "[%s] cluster 0x%04x not in ep 0x%02x; available: %s",
            dev.ieee,
            UBISYS_SETUP_CLUSTER_ID,
            UBISYS_SETUP_ENDPOINT_ID,
            all_clusters,
        )
    return cluster


def find_device_by_ieee(gateway: Gateway, ieee_str: str) -> Device | None:
    """Return the ZHA device matching the given IEEE address string, or None."""
    ieee_str_norm = ieee_str.lower()
    for dev in gateway.devices.values():
        if str(dev.ieee).lower() == ieee_str_norm:
            return dev
    return None


# ─────────────────────────────────────────────────────────────────────────────
# DATA MODEL
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class UbisysDeviceData:
    """Parsed configuration data for a single Ubisys device."""

    input_configurations: list[InputConfiguration] | None = field(default=None)
    input_actions: list[InputAction] = field(default_factory=list)
    output_configurations: list[OutputChannel] | None = field(default=None)


# ─────────────────────────────────────────────────────────────────────────────
# COORDINATOR
# ─────────────────────────────────────────────────────────────────────────────


class UbisysDataUpdateCoordinator(DataUpdateCoordinator[UbisysDeviceData]):
    """Coordinator that periodically reads configuration from one Ubisys device."""

    def __init__(
        self,
        hass: HomeAssistant,
        dev: Device,
        cluster: zigpy.zcl.Cluster,
        caps: DeviceCapabilities,
    ) -> None:
        """Initialize the coordinator."""
        self._dev = dev
        self._cluster = cluster
        self._caps = caps
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {dev.ieee}",
            update_interval=_UPDATE_INTERVAL,
        )

    @property
    def device(self) -> Device:
        """Return the associated ZHA device."""
        return self._dev

    @property
    def capabilities(self) -> DeviceCapabilities:
        """Return the device capabilities."""
        return self._caps

    async def _async_update_data(self) -> UbisysDeviceData:
        """Read current configuration from the device over ZCL."""
        data = UbisysDeviceData()
        try:
            if self._caps.has_input_configurations:
                data.input_configurations = await read_input_configurations(
                    self._cluster
                )
            data.input_actions = await read_input_actions(self._cluster)
            if self._caps.has_output_configurations:
                data.output_configurations = await read_output_configurations(
                    self._cluster
                )
        except Exception as err:
            raise UpdateFailed(
                f"Error reading configuration from {self._dev.ieee}: {err}"
            ) from err
        return data


def create_coordinators(
    hass: HomeAssistant,
    gateway: Gateway,
    manufacturer_id: int,
) -> list[UbisysDataUpdateCoordinator]:
    """Create one coordinator per recognized Ubisys device found in the gateway."""
    coordinators: list[UbisysDataUpdateCoordinator] = []
    for dev in gateway.devices.values():
        if dev.manufacturer_code != manufacturer_id:
            continue

        caps = get_capabilities(dev.model or "")
        if caps is None or not caps.has_config:
            _LOGGER.debug(
                "[%s] model %r unrecognised or no config cluster", dev.ieee, dev.model
            )
            continue

        cluster = get_setup_cluster(dev)
        if cluster is None:
            continue

        coordinators.append(UbisysDataUpdateCoordinator(hass, dev, cluster, caps))
        _LOGGER.debug("[%s] coordinator created for model %s", dev.ieee, dev.model)

    return coordinators
