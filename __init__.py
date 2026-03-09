"""Ubisys integration — registers services and wires up config entries.

Services (registered in async_setup, shared across all config entries):
    ubisys_poc.read_input_configurations      — reads attr 0x0000, returns parsed data
    ubisys_poc.read_input_actions             — reads attr 0x0001, returns parsed data
    ubisys_poc.read_output_configurations     — reads attr 0x0010 (LD6 only)
    ubisys_poc.read_raw_input_configurations  — raw hex bytes
    ubisys_poc.read_raw_input_actions         — raw hex bytes
    ubisys_poc.read_raw_output_configurations — raw hex bytes (LD6 only)
    ubisys_poc.write_input_configurations     — write attr 0x0000
    ubisys_poc.write_input_actions            — write attr 0x0001
    ubisys_poc.write_output_configurations    — write attr 0x0010 (LD6 only)
    ubisys_poc.write_raw_input_configurations — write from raw hex bytes
    ubisys_poc.write_raw_input_actions        — write from raw hex bytes
    ubisys_poc.write_raw_output_configurations — write from raw hex bytes (LD6 only)
    ubisys_poc.write_input_actions_preset     — write attr 0x0001 using a named preset

Each read service requires a `device_id` (HA device registry ID) and returns a
structured dict. Write services return {"written": true} on success.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.components.zha.helpers import get_zha_gateway
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType

from ._core import (
    InputAction,
    InputConfiguration,
    OutputChannel,
    encode_input_action,
    encode_input_configuration,
    encode_output_channel,
    get_capabilities,
)
from ._presets import Preset
from .const import (
    ATTR_INPUT_ACTIONS,
    ATTR_INPUT_CONFIGURATIONS,
    ATTR_OUTPUT_CONFIGURATIONS,
    DOMAIN,
    SERVICE_READ_INPUT_ACTIONS,
    SERVICE_READ_INPUT_CONFIGURATIONS,
    SERVICE_READ_OUTPUT_CONFIGURATIONS,
    SERVICE_READ_RAW_INPUT_ACTIONS,
    SERVICE_READ_RAW_INPUT_CONFIGURATIONS,
    SERVICE_READ_RAW_OUTPUT_CONFIGURATIONS,
    SERVICE_WRITE_INPUT_ACTIONS,
    SERVICE_WRITE_INPUT_ACTIONS_PRESET,
    SERVICE_WRITE_INPUT_CONFIGURATIONS,
    SERVICE_WRITE_OUTPUT_CONFIGURATIONS,
    SERVICE_WRITE_RAW_INPUT_ACTIONS,
    SERVICE_WRITE_RAW_INPUT_CONFIGURATIONS,
    SERVICE_WRITE_RAW_OUTPUT_CONFIGURATIONS,
    UBISYS_MANUFACTURER_ID,
    UBISYS_SETUP_CLUSTER_ID,
)
from .coordinator import (
    UbisysDataUpdateCoordinator,
    _read_array_attr,
    create_coordinators,
    find_device_by_ieee,
    get_setup_cluster,
    read_input_actions,
    read_input_configurations,
    read_output_configurations,
    write_input_actions,
    write_input_configurations,
    write_output_configurations,
)

if TYPE_CHECKING:
    from zha.application.gateway import Device
    import zigpy.zcl

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

type UbisysPocConfigEntry = ConfigEntry[list[UbisysDataUpdateCoordinator]]

_SERVICE_SCHEMA = vol.Schema({vol.Required("device_id"): str})

_WRITE_INPUT_CONFIGURATIONS_SCHEMA = _SERVICE_SCHEMA.extend(
    {
        vol.Required("input_configurations"): [
            vol.Schema(
                {
                    vol.Optional("input_index"): int,
                    vol.Optional("disabled", default=False): bool,
                    vol.Optional("inverted", default=False): bool,
                }
            )
        ]
    }
)

_WRITE_INPUT_ACTIONS_SCHEMA = _SERVICE_SCHEMA.extend(
    {
        vol.Required("input_actions"): [
            vol.Schema(
                {
                    vol.Required("input_index"): int,
                    vol.Optional("manufacturer_specific", default=False): bool,
                    vol.Optional("has_alternate", default=False): bool,
                    vol.Optional("is_alternate", default=False): bool,
                    vol.Required("initial_state"): str,
                    vol.Required("final_state"): str,
                    vol.Required("source_endpoint"): int,
                    vol.Required("cluster_id"): int,
                    vol.Optional("command_template", default=list): [int],
                }
            )
        ]
    }
)

_WRITE_OUTPUT_CONFIGURATIONS_SCHEMA = _SERVICE_SCHEMA.extend(
    {
        vol.Required("output_configurations"): [
            vol.Schema(
                {
                    vol.Optional("channel_index"): int,
                    vol.Required("endpoint"): int,
                    vol.Required("function"): str,
                    vol.Required("raw_flux"): int,
                    vol.Required("raw_cie_x"): int,
                    vol.Required("raw_cie_y"): int,
                }
            )
        ]
    }
)

_WRITE_RAW_INPUT_CONFIGURATIONS_SCHEMA = _SERVICE_SCHEMA.extend(
    {vol.Required("input_configurations"): [str]}
)

_WRITE_RAW_INPUT_ACTIONS_SCHEMA = _SERVICE_SCHEMA.extend(
    {vol.Required("input_actions"): [str]}
)

_WRITE_RAW_OUTPUT_CONFIGURATIONS_SCHEMA = _SERVICE_SCHEMA.extend(
    {vol.Required("output_configurations"): [str]}
)

_WRITE_INPUT_ACTIONS_PRESET_SCHEMA = _SERVICE_SCHEMA.extend(
    {
        vol.Required("presets"): [
            vol.Schema(
                {
                    vol.Required("input_index"): int,
                    vol.Required("preset"): vol.In(Preset.names()),
                    # Optional: auto-assigned from device model when omitted.
                    # See DeviceCapabilities.source_endpoint_base.
                    vol.Optional("source_endpoint"): int,
                    vol.Optional("scene_id"): int,
                }
            )
        ]
    }
)


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────


def _ieee_from_device_id(hass: HomeAssistant, device_id: str) -> str:
    """Resolve an HA device registry ID to a ZHA IEEE address string.

    Raises:
        ServiceValidationError: When the device entry is not found or has no ZHA identifier.
    """
    registry = dr.async_get(hass)
    device_entry = registry.async_get(device_id)
    if device_entry is None:
        raise ServiceValidationError(
            f"Device not found in registry: {device_id}",
            translation_domain=DOMAIN,
            translation_key="device_not_found",
        )
    for domain, identifier in device_entry.identifiers:
        if domain == "zha":
            return str(identifier)
    raise ServiceValidationError(
        f"Device {device_id} has no ZHA identifier",
        translation_domain=DOMAIN,
        translation_key="not_a_zha_device",
    )


def _resolve_cluster(
    hass: HomeAssistant, ieee_str: str
) -> tuple[Device, zigpy.zcl.Cluster]:
    """Look up the ZHA device and return (dev, cluster).

    Raises:
        ServiceValidationError: When the gateway, device, or cluster cannot be found.
    """
    try:
        gateway = get_zha_gateway(hass)
    except ValueError as err:
        raise ServiceValidationError(
            "ZHA gateway not available",
            translation_domain=DOMAIN,
            translation_key="zha_not_available",
        ) from err

    dev = find_device_by_ieee(gateway, ieee_str)
    if dev is None:
        raise ServiceValidationError(
            f"Device not found: {ieee_str}",
            translation_domain=DOMAIN,
            translation_key="device_not_found",
        )

    cluster = get_setup_cluster(dev)
    if cluster is None:
        raise ServiceValidationError(
            f"Setup cluster 0x{UBISYS_SETUP_CLUSTER_ID:04x} not found on {ieee_str}",
            translation_domain=DOMAIN,
            translation_key="no_setup_cluster",
        )
    return dev, cluster


# ─────────────────────────────────────────────────────────────────────────────
# SERIALISERS
# Convert parsed dataclasses to plain dicts for service responses.
# The shape is intentionally symmetric with what future write services
# will accept — users can copy the response, edit values, and pass it back.
# ─────────────────────────────────────────────────────────────────────────────


def _serialise_input_configuration(cfg: InputConfiguration) -> dict[str, Any]:
    """Serialise an InputConfiguration to a plain dict."""
    return {
        "input_index": cfg.input_index,
        "disabled": cfg.disabled,
        "inverted": cfg.inverted,
    }


def _serialise_input_action(action: InputAction) -> dict[str, Any]:
    """Serialise an InputAction to a plain dict.

    `initial_state` and `final_state` use the enum name in lower-case
    (e.g. ``"pressed"``, ``"released"``).  `command_template` is a list of
    integers so it round-trips through YAML without quoting ambiguity and
    can be passed back verbatim to the future write service.
    """
    return {
        "input_index": action.input_index,
        "manufacturer_specific": action.manufacturer_specific,
        "has_alternate": action.has_alternate,
        "is_alternate": action.is_alternate,
        "initial_state": action.initial_state.name.lower(),
        "final_state": action.final_state.name.lower(),
        "source_endpoint": action.source_endpoint,
        "cluster_id": action.cluster_id,
        "command_template": list(action.command_template),
    }


def _serialise_output_channel(ch: OutputChannel) -> dict[str, Any]:
    """Serialise an OutputChannel to a plain dict.

    Raw integer values are used (not normalised floats) so the write service
    can pass them back to the device without loss of precision.  The `function`
    field uses the enum name (e.g. ``"MONO"``, ``"RED"``).
    """
    return {
        "channel_index": ch.channel_index,
        "endpoint": ch.endpoint,
        "function": ch.function.name,
        "raw_flux": ch.raw_flux,
        "raw_cie_x": ch.raw_cie_x,
        "raw_cie_y": ch.raw_cie_y,
    }


# ─────────────────────────────────────────────────────────────────────────────
# HOME ASSISTANT WIRING
# ─────────────────────────────────────────────────────────────────────────────


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register Ubisys service actions."""

    async def _refresh_coordinator(ieee_str: str) -> None:
        """Request a data refresh on the coordinator matching the given IEEE address."""
        ieee_lower = ieee_str.lower()
        for entry in hass.config_entries.async_entries(DOMAIN):
            for coordinator in entry.runtime_data:
                if str(coordinator.device.ieee).lower() == ieee_lower:
                    await coordinator.async_request_refresh()
                    return

    async def handle_read_input_configurations(call: ServiceCall) -> ServiceResponse:
        """Read InputConfigurations (attr 0x0000) from the given device."""
        ieee_str = _ieee_from_device_id(hass, call.data["device_id"])
        dev, cluster = _resolve_cluster(hass, ieee_str)

        caps = get_capabilities(dev.model or "")
        if caps is not None and not caps.has_input_configurations:
            raise ServiceValidationError(
                f"{dev.model} does not expose InputConfigurations",
                translation_domain=DOMAIN,
                translation_key="no_input_configurations",
            )

        configs = await read_input_configurations(cluster)
        _LOGGER.debug(
            "[%s] read_input_configurations: %d entries", ieee_str, len(configs)
        )
        return {
            "ieee": ieee_str,
            "model": dev.model,
            "input_configurations": [
                _serialise_input_configuration(c) for c in configs
            ],
        }

    async def handle_read_input_actions(call: ServiceCall) -> ServiceResponse:
        """Read InputActions (attr 0x0001) from the given device."""
        ieee_str = _ieee_from_device_id(hass, call.data["device_id"])
        dev, cluster = _resolve_cluster(hass, ieee_str)

        actions = await read_input_actions(cluster)
        _LOGGER.debug("[%s] read_input_actions: %d entries", ieee_str, len(actions))
        return {
            "ieee": ieee_str,
            "model": dev.model,
            "input_actions": [_serialise_input_action(a) for a in actions],
        }

    async def handle_read_output_configurations(call: ServiceCall) -> ServiceResponse:
        """Read OutputConfigurations (attr 0x0010) from the given device (LD6)."""
        ieee_str = _ieee_from_device_id(hass, call.data["device_id"])
        dev, cluster = _resolve_cluster(hass, ieee_str)

        caps = get_capabilities(dev.model or "")
        if caps is not None and not caps.has_output_configurations:
            raise ServiceValidationError(
                f"{dev.model} does not expose OutputConfigurations",
                translation_domain=DOMAIN,
                translation_key="no_output_configurations",
            )

        channels = await read_output_configurations(cluster)
        _LOGGER.debug(
            "[%s] read_output_configurations: %d channels", ieee_str, len(channels)
        )
        return {
            "ieee": ieee_str,
            "model": dev.model,
            "output_configurations": [_serialise_output_channel(ch) for ch in channels],
        }

    async def handle_read_raw_input_configurations(
        call: ServiceCall,
    ) -> ServiceResponse:
        """Read InputConfigurations (attr 0x0000) as a list of hex bytes."""
        ieee_str = _ieee_from_device_id(hass, call.data["device_id"])
        dev, cluster = _resolve_cluster(hass, ieee_str)

        raw = await _read_array_attr(cluster, ATTR_INPUT_CONFIGURATIONS)
        return {
            "ieee": ieee_str,
            "model": dev.model,
            # One space-separated hex byte per physical input, e.g. "00" / "40" / "80".
            "input_configurations": [
                " ".join(f"{b:02x}" for b in bytes(e)) for e in raw
            ],
        }

    async def handle_read_raw_input_actions(call: ServiceCall) -> ServiceResponse:
        """Read InputActions (attr 0x0001) as a list of hex strings."""
        ieee_str = _ieee_from_device_id(hass, call.data["device_id"])
        dev, cluster = _resolve_cluster(hass, ieee_str)

        raw = await _read_array_attr(cluster, ATTR_INPUT_ACTIONS)
        return {
            "ieee": ieee_str,
            "model": dev.model,
            # One space-separated hex string per action entry (variable length).
            "input_actions": [" ".join(f"{b:02x}" for b in bytes(e)) for e in raw],
        }

    async def handle_read_raw_output_configurations(
        call: ServiceCall,
    ) -> ServiceResponse:
        """Read OutputConfigurations (attr 0x0010) as a list of hex strings (LD6)."""
        ieee_str = _ieee_from_device_id(hass, call.data["device_id"])
        dev, cluster = _resolve_cluster(hass, ieee_str)

        raw = await _read_array_attr(cluster, ATTR_OUTPUT_CONFIGURATIONS)
        return {
            "ieee": ieee_str,
            "model": dev.model,
            # One space-separated hex string per channel (6 bytes: ep_fn, flux, cie_x, cie_y).
            "output_configurations": [
                " ".join(f"{b:02x}" for b in bytes(e)) for e in raw
            ],
        }

    async def handle_write_input_configurations(call: ServiceCall) -> ServiceResponse:
        """Write InputConfigurations (attr 0x0000) to the given device."""
        ieee_str = _ieee_from_device_id(hass, call.data["device_id"])
        dev, cluster = _resolve_cluster(hass, ieee_str)

        caps = get_capabilities(dev.model or "")
        if caps is not None and not caps.has_input_configurations:
            raise ServiceValidationError(
                f"{dev.model} does not expose InputConfigurations",
                translation_domain=DOMAIN,
                translation_key="no_input_configurations",
            )

        try:
            cfgs = call.data["input_configurations"]
            if all("input_index" in c for c in cfgs):
                cfgs = sorted(cfgs, key=lambda c: c["input_index"])
            values = [encode_input_configuration(d) for d in cfgs]
        except (KeyError, ValueError) as err:
            raise ServiceValidationError(
                f"Invalid input_configurations entry: {err}",
                translation_domain=DOMAIN,
                translation_key="invalid_write_data",
            ) from err

        await write_input_configurations(cluster, values)
        _LOGGER.debug(
            "[%s] write_input_configurations: %d entries written", ieee_str, len(values)
        )
        await _refresh_coordinator(ieee_str)
        return {"ieee": ieee_str, "model": dev.model, "written": True}

    async def handle_write_input_actions(call: ServiceCall) -> ServiceResponse:
        """Write InputActions (attr 0x0001) to the given device."""
        ieee_str = _ieee_from_device_id(hass, call.data["device_id"])
        dev, cluster = _resolve_cluster(hass, ieee_str)

        try:
            entries = [encode_input_action(d) for d in call.data["input_actions"]]
        except (KeyError, ValueError) as err:
            raise ServiceValidationError(
                f"Invalid input_actions entry: {err}",
                translation_domain=DOMAIN,
                translation_key="invalid_write_data",
            ) from err

        await write_input_actions(cluster, entries)
        _LOGGER.debug(
            "[%s] write_input_actions: %d entries written", ieee_str, len(entries)
        )
        await _refresh_coordinator(ieee_str)
        return {"ieee": ieee_str, "model": dev.model, "written": True}

    async def handle_write_output_configurations(call: ServiceCall) -> ServiceResponse:
        """Write OutputConfigurations (attr 0x0010) to the given device (LD6)."""
        ieee_str = _ieee_from_device_id(hass, call.data["device_id"])
        dev, cluster = _resolve_cluster(hass, ieee_str)

        caps = get_capabilities(dev.model or "")
        if caps is not None and not caps.has_output_configurations:
            raise ServiceValidationError(
                f"{dev.model} does not expose OutputConfigurations",
                translation_domain=DOMAIN,
                translation_key="no_output_configurations",
            )

        try:
            channels = call.data["output_configurations"]
            if all("channel_index" in c for c in channels):
                channels = sorted(channels, key=lambda c: c["channel_index"])
            entries = [encode_output_channel(d) for d in channels]
        except (KeyError, ValueError) as err:
            raise ServiceValidationError(
                f"Invalid output_configurations entry: {err}",
                translation_domain=DOMAIN,
                translation_key="invalid_write_data",
            ) from err

        await write_output_configurations(cluster, entries)
        _LOGGER.debug(
            "[%s] write_output_configurations: %d channels written",
            ieee_str,
            len(entries),
        )
        await _refresh_coordinator(ieee_str)
        return {"ieee": ieee_str, "model": dev.model, "written": True}

    async def handle_write_raw_input_configurations(
        call: ServiceCall,
    ) -> ServiceResponse:
        """Write InputConfigurations (attr 0x0000) from raw hex byte strings."""
        ieee_str = _ieee_from_device_id(hass, call.data["device_id"])
        dev, cluster = _resolve_cluster(hass, ieee_str)

        try:
            values = [int(s.strip(), 16) for s in call.data["input_configurations"]]
        except ValueError as err:
            raise ServiceValidationError(
                f"Invalid hex value in input_configurations: {err}",
                translation_domain=DOMAIN,
                translation_key="invalid_write_data",
            ) from err

        await write_input_configurations(cluster, values)
        _LOGGER.debug(
            "[%s] write_raw_input_configurations: %d entries written",
            ieee_str,
            len(values),
        )
        await _refresh_coordinator(ieee_str)
        return {"ieee": ieee_str, "model": dev.model, "written": True}

    async def handle_write_raw_input_actions(call: ServiceCall) -> ServiceResponse:
        """Write InputActions (attr 0x0001) from raw hex byte strings."""
        ieee_str = _ieee_from_device_id(hass, call.data["device_id"])
        dev, cluster = _resolve_cluster(hass, ieee_str)

        try:
            entries = [
                bytes.fromhex(s.replace(" ", "")) for s in call.data["input_actions"]
            ]
        except ValueError as err:
            raise ServiceValidationError(
                f"Invalid hex value in input_actions: {err}",
                translation_domain=DOMAIN,
                translation_key="invalid_write_data",
            ) from err

        await write_input_actions(cluster, entries)
        _LOGGER.debug(
            "[%s] write_raw_input_actions: %d entries written", ieee_str, len(entries)
        )
        await _refresh_coordinator(ieee_str)
        return {"ieee": ieee_str, "model": dev.model, "written": True}

    async def handle_write_raw_output_configurations(
        call: ServiceCall,
    ) -> ServiceResponse:
        """Write OutputConfigurations (attr 0x0010) from raw hex byte strings (LD6)."""
        ieee_str = _ieee_from_device_id(hass, call.data["device_id"])
        dev, cluster = _resolve_cluster(hass, ieee_str)

        caps = get_capabilities(dev.model or "")
        if caps is not None and not caps.has_output_configurations:
            raise ServiceValidationError(
                f"{dev.model} does not expose OutputConfigurations",
                translation_domain=DOMAIN,
                translation_key="no_output_configurations",
            )

        try:
            entries = [
                bytes.fromhex(s.replace(" ", ""))
                for s in call.data["output_configurations"]
            ]
        except ValueError as err:
            raise ServiceValidationError(
                f"Invalid hex value in output_configurations: {err}",
                translation_domain=DOMAIN,
                translation_key="invalid_write_data",
            ) from err

        await write_output_configurations(cluster, entries)
        _LOGGER.debug(
            "[%s] write_raw_output_configurations: %d channels written",
            ieee_str,
            len(entries),
        )
        await _refresh_coordinator(ieee_str)
        return {"ieee": ieee_str, "model": dev.model, "written": True}

    async def handle_write_input_actions_preset(call: ServiceCall) -> ServiceResponse:
        """Write InputActions (attr 0x0001) using named presets for all inputs at once.

        Each entry in `presets` specifies one physical input's preset assignment.
        All entries are combined and written as a single replacement of the full
        InputActions attribute — identical to calling write_input_actions with
        a hand-crafted list.
        """
        ieee_str = _ieee_from_device_id(hass, call.data["device_id"])
        dev, cluster = _resolve_cluster(hass, ieee_str)
        caps = get_capabilities(dev.model or "")

        try:
            all_entries = []
            for p in call.data["presets"]:
                preset_name = p["preset"]
                input_index = p["input_index"]

                if "source_endpoint" in p:
                    source_ep = p["source_endpoint"]
                elif caps is not None:
                    source_ep = caps.resolve_source_endpoint(input_index, preset_name)
                    if source_ep is None:
                        raise ServiceValidationError(
                            f"`source_endpoint` is required for {dev.model} "
                            "because its endpoint layout is not statically known",
                            translation_domain=DOMAIN,
                            translation_key="source_endpoint_required",
                        )
                else:
                    raise ServiceValidationError(
                        f"`source_endpoint` is required for {dev.model} "
                        "because its endpoint layout is not statically known",
                        translation_domain=DOMAIN,
                        translation_key="source_endpoint_required",
                    )

                actions = Preset.get(preset_name).build(
                    input_index,
                    source_ep,
                    scene_id=p.get("scene_id"),
                )
                all_entries.extend(encode_input_action(d) for d in actions)
        except ServiceValidationError:
            raise
        except (KeyError, ValueError) as err:
            raise ServiceValidationError(
                f"Invalid preset configuration: {err}",
                translation_domain=DOMAIN,
                translation_key="invalid_write_data",
            ) from err

        await write_input_actions(cluster, all_entries)
        _LOGGER.debug(
            "[%s] write_input_actions_preset: %d entries written",
            ieee_str,
            len(all_entries),
        )
        await _refresh_coordinator(ieee_str)
        return {"ieee": ieee_str, "model": dev.model, "written": True}

    hass.services.async_register(
        DOMAIN,
        SERVICE_READ_INPUT_CONFIGURATIONS,
        handle_read_input_configurations,
        schema=_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_READ_INPUT_ACTIONS,
        handle_read_input_actions,
        schema=_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_READ_OUTPUT_CONFIGURATIONS,
        handle_read_output_configurations,
        schema=_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_READ_RAW_INPUT_CONFIGURATIONS,
        handle_read_raw_input_configurations,
        schema=_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_READ_RAW_INPUT_ACTIONS,
        handle_read_raw_input_actions,
        schema=_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_READ_RAW_OUTPUT_CONFIGURATIONS,
        handle_read_raw_output_configurations,
        schema=_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_WRITE_INPUT_CONFIGURATIONS,
        handle_write_input_configurations,
        schema=_WRITE_INPUT_CONFIGURATIONS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_WRITE_INPUT_ACTIONS,
        handle_write_input_actions,
        schema=_WRITE_INPUT_ACTIONS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_WRITE_OUTPUT_CONFIGURATIONS,
        handle_write_output_configurations,
        schema=_WRITE_OUTPUT_CONFIGURATIONS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_WRITE_RAW_INPUT_CONFIGURATIONS,
        handle_write_raw_input_configurations,
        schema=_WRITE_RAW_INPUT_CONFIGURATIONS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_WRITE_RAW_INPUT_ACTIONS,
        handle_write_raw_input_actions,
        schema=_WRITE_RAW_INPUT_ACTIONS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_WRITE_RAW_OUTPUT_CONFIGURATIONS,
        handle_write_raw_output_configurations,
        schema=_WRITE_RAW_OUTPUT_CONFIGURATIONS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_WRITE_INPUT_ACTIONS_PRESET,
        handle_write_input_actions_preset,
        schema=_WRITE_INPUT_ACTIONS_PRESET_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: UbisysPocConfigEntry) -> bool:
    """Set up Ubisys from a config entry."""
    try:
        gateway = get_zha_gateway(hass)
    except ValueError as err:
        # ZHA is listed as a dependency so this should not happen in normal use,
        # but guard defensively in case ZHA failed to set up.
        _LOGGER.error("ZHA gateway not available; cannot set up Ubisys integration")
        raise RuntimeError("ZHA gateway not available") from err

    coordinators = create_coordinators(hass, gateway, UBISYS_MANUFACTURER_ID)

    if not coordinators:
        _LOGGER.debug("No recognised Ubisys devices found in ZHA")

    # Perform the first data read for all devices in parallel.
    for coordinator in coordinators:
        await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinators

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: UbisysPocConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
