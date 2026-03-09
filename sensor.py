"""Ubisys PoC — diagnostic sensors for input configurations, input actions and output channels.

One sensor entity is created per Ubisys device:
  - UbisysInputConfigSensor   — reflects InputConfiguration entries (attr 0x0000)
  - UbisysInputActionSensor   — reflects InputAction entries (attr 0x0001)
  - UbisysOutputChannelSensor — reflects OutputChannel entries (attr 0x0010, LD6 only)

Each entity attaches to the existing ZHA device registry entry via DeviceInfo
identifiers so no duplicate device is created in the device registry.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_ZIGBEE, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ._core import InputAction, InputConfiguration, OutputChannel
from .const import (
    ATTR_INPUT_ACTIONS,
    ATTR_INPUT_CONFIGURATIONS,
    ATTR_OUTPUT_CONFIGURATIONS,
    UBISYS_SETUP_CLUSTER_ID,
    UBISYS_SETUP_ENDPOINT_ID,
)
from .coordinator import UbisysDataUpdateCoordinator

_CLUSTER_NAMES: dict[int, str] = {
    0x0003: "Identify",
    0x0004: "Groups",
    0x0005: "Scenes",
    0x0006: "On/Off",
    0x0008: "Level Control",
    0x0102: "Window Covering",
    0x0300: "Color Control",
    0xFC02: "Managed Input (mfr)",
}

_ONOFF_COMMANDS: dict[bytes, str] = {
    b"\x00": "Off",
    b"\x01": "On",
    b"\x02": "Toggle",
}


def _fmt_command(action: InputAction) -> str:
    if not action.command_template:
        return "<empty>"
    if action.cluster_id == 0x0006:
        if label := _ONOFF_COMMANDS.get(action.command_template):
            return label
    return action.command_template.hex(" ")


def _summarise_input_configuration(cfg: InputConfiguration) -> str:
    """Return a one-line description of an input's configuration."""
    if cfg.disabled:
        return "disabled"
    return "NC (active-low)" if cfg.inverted else "NO (active-high)"


def _summarise_input_action(action: InputAction) -> str:
    """Return a one-line description of a single input action."""
    cluster_label = _CLUSTER_NAMES.get(action.cluster_id, f"0x{action.cluster_id:04x}")
    return (
        f"{action.initial_state}\u2192{action.final_state}:"
        f" ep{action.source_endpoint} {cluster_label} {_fmt_command(action)}"
    )


def _summarise_output_channel(ch: OutputChannel) -> str:
    """Return a one-line description of one LD6 output channel."""
    if not ch.is_active:
        return "unused"
    parts = [f"{ch.function} ep{ch.endpoint}"]
    if ch.flux is not None:
        parts.append(f"flux={ch.flux:.3f}")
    if ch.cie_x is not None and ch.cie_y is not None:
        parts.append(f"CIE({ch.cie_x:.4f},{ch.cie_y:.4f})")
    return " ".join(parts)


if TYPE_CHECKING:
    from . import UbisysPocConfigEntry

# ZHA-style unique_id: {ieee}_{endpoint}_{cluster}_{attr}
# Matches the pattern ZHA uses internally for its own entity unique IDs.
_EP = UBISYS_SETUP_ENDPOINT_ID
_CL = UBISYS_SETUP_CLUSTER_ID


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UbisysPocConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Ubisys sensors from a config entry."""
    entities: list[SensorEntity] = []
    for coordinator in entry.runtime_data:
        caps = coordinator.capabilities
        data = coordinator.data

        if caps.has_input_configurations and data.input_configurations is not None:
            entities.append(UbisysInputConfigSensor(coordinator))
        entities.append(UbisysInputActionSensor(coordinator))
        if caps.has_output_configurations and data.output_configurations is not None:
            entities.append(UbisysOutputChannelSensor(coordinator))

    async_add_entities(entities)


class _UbisysBaseSensor(CoordinatorEntity[UbisysDataUpdateCoordinator], SensorEntity):
    """Base class providing device linkage and coordinator wiring for all Ubisys sensors."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def device_info(self) -> DeviceInfo:
        """Link to the existing ZHA device registry entry."""
        dev = self.coordinator.device
        return DeviceInfo(
            connections={(CONNECTION_ZIGBEE, str(dev.ieee))},
            identifiers={("zha", str(dev.ieee))},
            manufacturer=dev.manufacturer,
            model=dev.model,
            name=dev.name or str(dev.ieee),
        )


class UbisysInputConfigSensor(_UbisysBaseSensor):
    """Sensor summarising all InputConfiguration entries (attr 0x0000)."""

    _attr_translation_key = "input_configuration"
    _attr_native_unit_of_measurement = "inputs"

    def __init__(self, coordinator: UbisysDataUpdateCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator)
        dev = coordinator.device
        self._attr_unique_id = f"{dev.ieee}_{_EP}_{_CL}_{ATTR_INPUT_CONFIGURATIONS}"

    @property
    def native_value(self) -> int | None:
        """Return the number of input configuration entries."""
        configs = self.coordinator.data.input_configurations
        return len(configs) if configs is not None else None

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """Return per-input configuration detail."""
        configs = self.coordinator.data.input_configurations
        if not configs:
            return {}
        return {
            f"in[{cfg.input_index}]": _summarise_input_configuration(cfg)
            for cfg in configs
        }


class UbisysInputActionSensor(_UbisysBaseSensor):
    """Sensor summarising all InputAction entries (attr 0x0001)."""

    _attr_translation_key = "input_actions"
    _attr_native_unit_of_measurement = "actions"

    def __init__(self, coordinator: UbisysDataUpdateCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator)
        dev = coordinator.device
        self._attr_unique_id = f"{dev.ieee}_{_EP}_{_CL}_{ATTR_INPUT_ACTIONS}"

    @property
    def native_value(self) -> int:
        """Return the number of input action entries."""
        return len(self.coordinator.data.input_actions)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """Return per-action detail, keyed by action index."""
        return {
            f"action[{idx}]": f"in[{action.input_index}] {_summarise_input_action(action)}"
            for idx, action in enumerate(self.coordinator.data.input_actions)
        }


class UbisysOutputChannelSensor(_UbisysBaseSensor):
    """Sensor summarising all OutputChannel entries (attr 0x0010, LD6 only)."""

    _attr_translation_key = "output_channels"
    _attr_native_unit_of_measurement = "channels"

    def __init__(self, coordinator: UbisysDataUpdateCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator)
        dev = coordinator.device
        self._attr_unique_id = f"{dev.ieee}_{_EP}_{_CL}_{ATTR_OUTPUT_CONFIGURATIONS}"

    @property
    def native_value(self) -> int | None:
        """Return the number of output channel entries."""
        channels = self.coordinator.data.output_configurations
        return len(channels) if channels is not None else None

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """Return per-channel detail."""
        channels = self.coordinator.data.output_configurations
        if not channels:
            return {}
        return {
            f"ch[{ch.channel_index}]": _summarise_output_channel(ch) for ch in channels
        }
