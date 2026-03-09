# ubisys PoC — Implementation Findings

Non-obvious discoveries from working with real hardware and the zigpy/ZHA stack.
Code-visible facts (wire formats, model names, cluster IDs) are documented in `_core.py` — this file covers only *why* things are the way they are.

---

## Zigbee / ZHA access layer

### `read_attributes_raw` is the only viable entry point

`cluster.read_attributes([attr_id])` fails with a bare `KeyError` for every attribute on a manufacturer-specific cluster that has no zigpy attribute schema registered.  `read_attributes_raw` bypasses schema lookup entirely.

Response shape (fixed, always check defensively):
```
result[0]          → list of ReadAttrRecord objects
result[0][0]       → first record
result[0][0].status.name  → "SUCCESS" (string, not an enum comparison)
result[0][0].value.value  → the actual array (Python list)
```

### Array element types are NOT plain Python scalars

zigpy deserialises ZCL typed fields into custom wrappers:

| ZCL type | zigpy type | Correct extraction |
|---|---|---|
| `uint8` / `data8` (0x08) | `data8` — bytes-like, **not** `int`-like | `bytes(e)[0]` |
| variable octets (`octet_str`) | `octet_str` — bytes-like | `bytes(e)` |

`int(e)` on a `data8` raises `TypeError`.  This applies to every element of `InputConfigurations` (one `data8` per input) and every element of `InputActions` / `OutputConfigurations` (`octet_str`).

### Manufacturer-specific attribute writes

The write path is `cluster.write_attributes_raw(...)` but the `manufacturer_code` kwarg must be `None`:
```python
await cluster.write_attributes_raw(
    [attr_record],
    manufacturer_code=None,  # NOT UBISYS_MANUFACTURER_ID
)
```

**Reads require the manufacturer bit; writes reject it.**  Passing `manufacturer_code=UBISYS_MANUFACTURER_ID` causes the ZCL header to have the manufacturer-specific bit set, and the device responds with `UNSUPPORTED_ATTRIBUTE (0x86)`.  Omitting it (or passing `None`) succeeds.  The same asymmetry was observed in the z2m ecosystem with cluster `0xFC00`.

### ZCL Array serialization for unregistered attributes

For attributes on manufacturer-specific clusters with no registered zigpy schema, the `foundation.Attribute` object must be constructed manually.  `zigpy.types.Bytes` works as the value wrapper — its `.serialize()` returns the raw bytes verbatim, which is exactly what `TypeValue.serialize()` calls:

```python
import zigpy.types as zigpy_t

payload = bytes([element_type]) + struct.pack("<H", count) + ...  # ZCL Array wire bytes
tv = zcl_foundation.TypeValue(type=0x48, value=zigpy_t.Bytes(payload))
attr_record = zcl_foundation.Attribute(attrid=attr_id, value=tv)
result = await cluster.write_attributes_raw([attr_record], manufacturer_code=None)
```

`TypeValue.serialize()` emits `type_byte + value.serialize()`, so `zigpy_t.Bytes` injects the pre-built array payload verbatim without needing a custom wrapper class.  Confirmed working for `InputActions` (attr `0x0001`) with `Status.SUCCESS` response (2026-03-08).

---

## Device model strings

### Always include the product code suffix

Devices report their model in the ZCL Basic cluster as `"S2 (5502)"`, not `"S2"`.  The suffix is part of the official product number.  `UbisysModel` enum values are intentionally the full strings so that dict lookup works directly on `dev.model`.

### J1 and S2 coincidentally share product code 5502

- `S2` → `"S2 (5502)"`
- `J1` → `"J1 (5502)"`

The full model strings differ, so there is no dict collision.  Relevant only if you ever try to match on the numeric code alone.

### LD6 is the only device without a product code suffix

`dev.model == "LD6"` exactly — no parenthesised code.

---

## Endpoint and cluster topology

All known ubisys devices use the same setup endpoint and cluster:

| Field | Value |
|---|---|
| Endpoint | `0xE8` (232) |
| Cluster | `0xFC00` |

The ubisys documentation for LD6 (§6.13) suggests cluster `0xFC02` — this is **wrong**.  The physical device exposes `0xFC00` on `0xE8`, identical to all other models.

### LD6 registers the setup cluster in `out_clusters`, not `in_clusters`

On S2 and C4, `endpoint.in_clusters[0xFC00]` works.  On LD6, `in_clusters` does not contain `0xFC00`; it appears in `out_clusters`.  `get_setup_cluster()` checks both.

---

## `InputActions` wire format subtleties

### `CommandTemplate` is the full ZCL frame payload excluding ZCL header

Bytes 5+ of each entry are sent verbatim as the ZCL frame payload when the action fires.  For On/Off cluster commands this is a single byte (`\x00` Off, `\x01` On, `\x02` Toggle).  There is no leading length byte in the stored template.

### Bit 4 of `InputAndOptions` is LD6-only

Bit 4 (`manufacturer_specific` flag) distinguishes LD6-specific managed-input actions from standard ZCL commands.  On C4 and S2 this bit is always 0.

---

## HA integration architecture

### `device_info` is silently ignored unless loaded via a config entry

This is the most expensive lesson learned.  HA only processes `device_info` on an entity if **both** conditions are met:
1. The platform is loaded via a **config entry** (`async_setup_entry`)
2. The entity has a `unique_id`

If the platform is loaded via `discovery.async_load_platform` (the legacy YAML path), `device_info` is never read — entities are created but float unattached.  Manual workarounds via `entity_registry.async_update_entity(device_id=...)` in `async_added_to_hass` technically work but are a hack.

**The correct solution**: add a minimal config flow (`config_flow.py` + `"config_flow": true` in `manifest.json`) and switch the platform entry point from `async_setup_platform` to `async_setup_entry`.  Even a no-input confirm-only flow (single `async_step_user` that immediately creates the entry) is sufficient.

### Linking to an existing ZHA device, not creating a new one

Return this exact `DeviceInfo` from the entity's `device_info` property:

```python
DeviceInfo(
    connections={(CONNECTION_ZIGBEE, str(dev.ieee))},
    identifiers={("zha", str(dev.ieee))},
    ...
)
```

The `identifiers` key `("zha", ieee_str)` is what ZHA writes to the device registry for every ZHA device.  HA matches by identifier, so returning it from an external entity causes the entity to be attached to the **existing** ZHA device entry rather than creating a duplicate.  The `connections` entry alone is not sufficient — `identifiers` must match.

### `device_info` must be a `@property`, not `_attr_device_info`

Setting `self._attr_device_info = ...` in `__init__` is not supported — the instance attribute is not read by HA's entity base class.  Only the `@property def device_info(self)` form works.

### `EVENT_HOMEASSISTANT_STARTED` deferral is required even inside `async_setup_entry`

`async_setup_entry` is called during integration setup, which happens before ZHA finishes initialising its device list.  Calling `get_zha_gateway(hass)` at setup time may succeed (ZHA is loaded) but `gateway.devices` may still be empty.  Deferring entity creation to `EVENT_HOMEASSISTANT_STARTED` ensures all ZHA devices are fully initialised before the scan runs.

---

## HA entity conventions

### Entity names: use `translation_key` + `strings.json`, not `_attr_name`

Hard-coding `_attr_name = "Input actions"` works but makes the name non-translatable.  The correct pattern:

```python
_attr_translation_key = "input_actions"  # in the entity class
```

```json
// strings.json
{
  "entity": {
    "sensor": {
      "input_actions": { "name": "Input actions" }
    }
  }
}
```

HA resolves the display name from `strings.json` at runtime.  Both approaches produce the same UI result but only the `translation_key` path is picked up by the translation pipeline.

### `native_value` should be numeric; units go in `native_unit_of_measurement`

Do not embed unit words in the state string (`"8 actions"`).  HA treats the state as an opaque string and cannot sort, graph, or threshold-alert on it.  Instead:

```python
_attr_native_value = len(actions)           # int
_attr_native_unit_of_measurement = "actions"  # displayed alongside
```

This makes the value machine-parseable and enables `SensorStateClass.MEASUREMENT` semantics.

### `EntityCategory.CONFIG` places entities in the Configuration section

Setting `_attr_entity_category = EntityCategory.CONFIG` on a sensor moves it under the "Configuration" section of the device detail page in the UI, which is the correct location for read-only device configuration data.

### ZHA-style unique IDs

ZHA formats entity unique IDs as `{ieee}_{endpoint}_{cluster}_{attribute_id}`.  Following this pattern for ubisys-specific entities keeps IDs derivable from Zigbee addressing and avoids collisions with ZHA's own entities:

```python
f"{dev.ieee}_{UBISYS_SETUP_ENDPOINT_ID}_{UBISYS_SETUP_CLUSTER_ID}_{ATTR_INPUT_ACTIONS}"
```

| Situation | Level |
|---|---|
| Device found / count | `info` |
| Full device report (the multi-line table) | `info` |
| Device has no config (R0) | `info` |
| Unrecognised model string | `warning` |
| Cluster / endpoint not found (unexpected) | `warning` |
| Attribute read failed | `error` |

`config/configuration.yaml` has `custom_components.ubisys_poc: debug` so debug messages are visible during development.

---

## Test architecture

`config/test_ubisys_poc.py` loads `_core.py` via `importlib.util.spec_from_file_location` to avoid importing `__init__.py` (which pulls in HA).  This keeps the test suite runnable without a live HA instance.

Run tests:
```
python -m pytest config/test_ubisys_poc.py -v
```

Deploy to local HA:
```
./deploy_ubisys.sh
```
