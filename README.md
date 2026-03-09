# Ubisys

The **Ubisys** integration extends [ZHA (Zigbee Home Automation)](https://www.home-assistant.io/integrations/zha) with support for reading and writing the manufacturer-specific device-setup cluster (cluster `0xFC00`, endpoint `0xE8`) found on all Ubisys Zigbee products.

This allows configuration of how each physical input & output on a Ubisys device behaves — which ZCL command is sent when a button is pressed, held, or released — directly from Home Assistant without needing Zigbee2MQTT or any other gateway.

> **Prerequisite:** ZHA must be installed and your Ubisys devices must already be paired to ZHA before using this integration.


## Supported devices

| Model | Inputs | Outputs | Notes |
|---|---|---|---|
| C4 (5504) | 4 | — | In-wall controller |
| S1 (5501) | 1 | 1 relay | In-wall switch |
| S1-R (5601) | 1 | 1 relay | DIN-rail variant of S1 |
| S2 (5502) | 2 | 2 relays | In-wall switch |
| S2-R (5602) | 2 | 2 relays | DIN-rail variant of S2 |
| D1 (5503) | 2 | 1 dimmer | In-wall triac dimmer |
| D1-R (5603) | 2 | 1 dimmer | DIN-rail variant of D1 |
| J1 (5502) | 2 | 1 shutter | In-wall shutter/blind actuator |
| J1-R (5602) | 2 | 1 shutter | DIN-rail variant of J1 |
| LD6 | 3 | 6 channels | 6-channel LED driver |
| R0 (5501) | — | — | Zigbee router only; no setup cluster |

The integration auto-detects the model from the ZCL Basic cluster `ModelIdentifier` attribute and adapts accordingly.


## Sensors

After setup, the integration creates diagnostic sensor entities that attach to the **existing** ZHA device entries (no new devices are created in the device registry). Sensors update automatically every hour and can be refreshed on demand via the [read actions](#read-actions).

| Sensor | Attribute | Devices |
|---|---|---|
| Input configurations | `InputConfigurations` (0x0000) | All devices with inputs |
| Input actions | `InputActions` (0x0001) | All devices with inputs |
| Output channels | `OutputConfigurations` (0x0010) | LD6 only |

The sensor state shows the **count** of entries (e.g. `4` for a C4 with four input configuration entries). Full decoded data is available as state attributes, one key per entry.


## Actions

Actions are split into three groups:

- [Read actions](#read-actions) — read and return the current device configuration as structured data.
- [Write actions](#write-actions) — write configuration back to the device.
- [Preset write action](#preset-write-action) — configure inputs using named, human-friendly presets.

There are multiple `..._raw_...` actions operating on raw bytes stored in the Ubisys attributes.
The best way to understand these it to read a [Technical reference manual](#technical-reference)
for your device and use Ubisys examples provided there.

All actions require a `device_id` that identifies the target Ubisys ZHA device.


### Read actions

#### `ubisys_poc.read_input_configurations`

Reads the `InputConfigurations` attribute (0x0000). Returns one entry per physical input.

**Fields:**

| Field | Required | Description |
|---|---|---|
| `device_id` | yes | The Ubisys ZHA device to read from |

**Response per entry:**

| Field | Type | Description |
|---|---|---|
| `disabled` | bool | The input is electrically disabled |
| `inverted` | bool | Signal polarity is inverted (active-low / normally-closed) |

The response can be edited and passed verbatim to `write_input_configurations`.


#### `ubisys_poc.read_input_actions`

Reads the `InputActions` attribute (0x0001). Each entry maps a physical input transition (pressed, released, held) to a ZCL command the device sends to a specific endpoint and cluster.

**Fields:**

| Field | Required | Description |
|---|---|---|
| `device_id` | yes | The Ubisys ZHA device to read from |

**Response per entry:**

| Field | Type | Description |
|---|---|---|
| `input_index` | int | Zero-based physical input number |
| `manufacturer_specific` | bool | LD6 only: marks manufacturer-specific entries |
| `has_alternate` | bool | This entry is the primary of an alternating pair |
| `is_alternate` | bool | This entry is the alternate of a pair |
| `initial_state` | str | `ignored`, `pressed`, `kept_pressed`, or `released` |
| `final_state` | str | `ignored`, `pressed`, `kept_pressed`, or `released` |
| `source_endpoint` | int | Local ZCL endpoint that originates the command |
| `cluster_id` | int | Target ZCL cluster ID (decimal) |
| `command_template` | list[int] | Raw ZCL frame payload bytes |

The response can be edited and passed verbatim to `write_input_actions`.


#### `ubisys_poc.read_output_configurations`

Reads the `OutputConfigurations` attribute (0x0010). **LD6 only.** Returns exactly 6 entries (one per output channel).

**Fields:**

| Field | Required | Description |
|---|---|---|
| `device_id` | yes | The LD6 ZHA device to read from |

**Response per entry:**

| Field | Type | Description |
|---|---|---|
| `channel_index` | int | Zero-based channel number (0–5) |
| `endpoint` | int | Logical light endpoint; `0` means the channel is unused |
| `function` | str | `MONO`, `CW`, `WW`, `RED`, `GREEN`, `BLUE`, `AMBER`, `TURQUOISE`, `VIOLET`, or `FREE` |
| `raw_flux` | int | Normalised flux as uint8 (0–254); 255 = not applicable |
| `raw_cie_x` | int | CIE 1931 x chromaticity as uint16; 65535 = not applicable |
| `raw_cie_y` | int | CIE 1931 y chromaticity as uint16; 65535 = not applicable |

The response can be edited and passed verbatim to `write_output_configurations`.


#### `ubisys_poc.read_raw_input_configurations`

Reads the `InputConfigurations` attribute (0x0000) and returns undecoded wire bytes.

**Fields:**

| Field | Required | Description |
|---|---|---|
| `device_id` | yes | The Ubisys ZHA device to read from |

**Response:**

`input_configurations` — a list of space-separated hex byte strings, one per physical input.

| Value | Meaning |
|---|---|
| `"00"` | Normal (enabled, active-high) |
| `"40"` | Inverted (active-low / normally-closed) |
| `"80"` | Disabled |

The response can be edited and passed verbatim to `write_raw_input_configurations`.


#### `ubisys_poc.read_raw_input_actions`

Reads the `InputActions` attribute (0x0001) and returns undecoded wire bytes.

**Fields:**

| Field | Required | Description |
|---|---|---|
| `device_id` | yes | The Ubisys ZHA device to read from |

**Response:**

`input_actions` — a list of space-separated hex byte strings, one per action entry (minimum 5 bytes per entry).
Example entry: `"00 0d 01 06 00 02"` (input 0, press, endpoint 1, On/Off cluster, Toggle command).

The response can be edited and passed verbatim to `write_raw_input_actions`.


#### `ubisys_poc.read_raw_output_configurations`

Reads the `OutputConfigurations` attribute (0x0010) and returns undecoded wire bytes. **LD6 only.**

**Fields:**

| Field | Required | Description |
|---|---|---|
| `device_id` | yes | The LD6 ZHA device to read from |

**Response:**

`output_configurations` — a list of space-separated hex byte strings, one per output channel (6 bytes each).
Unused channels appear as `"00 ff ff ff ff ff"`.

The response can be edited and passed verbatim to `write_raw_output_configurations`.


### Write actions

Write actions accept the same structure returned by their corresponding read actions. You can read the current configuration, modify individual fields, and write it back.

> **Beware:** on every write, the **full attribute is replaced** — all entries (multiple inputs and outputs) must be written at once, including those unchanged.

#### `ubisys_poc.write_input_configurations`

Writes the `InputConfigurations` attribute (0x0000).

**Fields:**

| Field | Required | Description |
|---|---|---|
| `device_id` | yes | The Ubisys ZHA device to write to |
| `input_configurations` | yes | List of input configuration entries (see below) |

**`input_configurations` entry fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `disabled` | bool | `false` | Electrically disable this input |
| `inverted` | bool | `false` | Invert polarity (normally-closed) |


#### `ubisys_poc.write_input_actions`

Writes the `InputActions` attribute (0x0001).

**Fields:**

| Field | Required | Description |
|---|---|---|
| `device_id` | yes | The Ubisys ZHA device to write to |
| `input_actions` | yes | List of input action entries (see below) |

**`input_actions` entry fields:**

| Field | Type | Required | Description |
|---|---|---|---|
| `input_index` | int | yes | Physical input index (0-based) |
| `initial_state` | str | yes | `ignored`, `pressed`, `kept_pressed`, or `released` |
| `final_state` | str | yes | `ignored`, `pressed`, `kept_pressed`, or `released` |
| `source_endpoint` | int | yes | Local ZCL endpoint that sends the command |
| `cluster_id` | int | yes | Target ZCL cluster ID (decimal) |
| `manufacturer_specific` | bool | no | LD6 only; default `false` |
| `has_alternate` | bool | no | Primary of an alternating pair; default `false` |
| `is_alternate` | bool | no | Alternate of a pair; default `false` |
| `command_template` | list[int] | no | Raw ZCL frame payload bytes; default `[]` |


#### `ubisys_poc.write_output_configurations`

Writes the `OutputConfigurations` attribute (0x0010). **LD6 only.**

**Fields:**

| Field | Required | Description |
|---|---|---|
| `device_id` | yes | The LD6 ZHA device to write to |
| `output_configurations` | yes | List of output channel entries (see below) |

**`output_configurations` entry fields:**

| Field | Type | Required | Description |
|---|---|---|---|
| `endpoint` | int | yes | Logical light endpoint; `0` for unused |
| `function` | str | yes | `MONO`, `CW`, `WW`, `RED`, `GREEN`, `BLUE`, `AMBER`, `TURQUOISE`, `VIOLET`, or `FREE` |
| `raw_flux` | int | yes | Normalised flux (0–254); `255` = not applicable |
| `raw_cie_x` | int | yes | CIE 1931 x chromaticity (0–65279); `65535` = not applicable |
| `raw_cie_y` | int | yes | CIE 1931 y chromaticity (0–65279); `65535` = not applicable |


#### `ubisys_poc.write_raw_input_configurations`

Writes the `InputConfigurations` attribute from raw hex byte strings.

**Fields:**

| Field | Required | Description |
|---|---|---|
| `device_id` | yes | The Ubisys ZHA device to write to |
| `input_configurations` | yes | List of hex byte strings, one per physical input (e.g. `["00", "40", "80"]`) |


#### `ubisys_poc.write_raw_input_actions`

Writes the `InputActions` attribute from raw hex byte strings.

**Fields:**

| Field | Required | Description |
|---|---|---|
| `device_id` | yes | The Ubisys ZHA device to write to |
| `input_actions` | yes | List of space-separated hex byte strings, one per entry |


#### `ubisys_poc.write_raw_output_configurations`

Writes the `OutputConfigurations` attribute from raw hex byte strings. **LD6 only.**

**Fields:**

| Field | Required | Description |
|---|---|---|
| `device_id` | yes | The LD6 ZHA device to write to |
| `output_configurations` | yes | List of space-separated hex byte strings, one per channel (6 bytes each) |


### Preset write action

#### `ubisys_poc.write_input_actions_preset`

Configures one or more physical inputs using named presets. Under the hood this builds the full `InputActions` list and writes it as a single attribute write, equivalent to calling `write_input_actions` with a hand-crafted list.

**Fields:**

| Field | Required | Description |
|---|---|---|
| `device_id` | yes | The Ubisys ZHA device to write to |
| `presets` | yes | One entry per physical input to configure (see below) |

**`presets` entry fields:**

| Field | Type | Required | Description |
|---|---|---|---|
| `input_index` | int | yes | Physical input index (0-based) |
| `preset` | str | yes | Preset name (see [Presets](#presets) below) |
| `source_endpoint` | int | no | ZCL source endpoint. Auto-detected for all models except LD6 |
| `scene_id` | int | no | Scene ID. Required only for the `scene` and `scene_switch` presets |

For all known device models (C4, S1, S1-R, S2, S2-R, D1, D1-R, J1, J1-R), `source_endpoint` is derived automatically from the device model and `input_index` — the same defaults used by Zigbee2MQTT. For LD6, `source_endpoint` must be provided explicitly because the output channel endpoints are configurable. For C4 with cover presets, the cover endpoint range (5–8) is applied automatically.


## Presets

Presets translate a common physical switch/button behaviour into the set of `InputAction` entries the device needs. All presets use standard ZCL clusters (`On/Off` 0x0006, `Level Control` 0x0008, `Scenes` 0x0005, `Window Covering` 0x0102).

| Preset | Physical inputs used | Zigbee command sent | Note |
|---|---|---|---|
| `toggle` | 1 | Toggle on button press | Useful for wall switches which *return back* the their previous physical position after release
| `toggle_switch` | 1 | Toggle on switch-on **and** toggle again on switch-off | Useful for wall switches which *remain* in their new physical position after switching
| `on_off_switch` | 1 | Turn-on on switch-on, turn-off on switch-off | Useful for wall switches which *remain* in their new physical position after switching
| `on` | 1 | Turn on only (press) |
| `off` | 1 | Turn off only (press) |
| `dimmer_single` | 1 | Short press toggles; hold alternates dim-up / dim-down |
| `dimmer_double` | 2 | `input_index` = up/on, `input_index+1` = down/off |
| `cover` | 2 | `input_index` = open, `input_index+1` = close; short press stops mid-travel |
| `cover_switch` | 2 | `input_index` = open, `input_index+1` = close; any release stops |
| `cover_up` | 1 | Open/raise cover on press |
| `cover_down` | 1 | Close/lower cover on press |
| `scene` | 1 | Recall scene on short press (requires `scene_id`) |
| `scene_switch` | 1 | Recall scene on press (requires `scene_id`) |

> **Dual-input presets:** `dimmer_double`, `cover`, and `cover_switch` generate actions for **two consecutive inputs**: `input_index` (primary / up / open) and `input_index + 1` (secondary / down / close). Both inputs share the same `source_endpoint`.

### Default source endpoints per model

| Model | On/Off / Dimmer / Scene base | Cover base |
|---|---|---|
| C4 | Input 0 → ep 1, Input 1 → ep 2, … | Input 0 → ep 5, Input 1 → ep 6, … |
| S1 / S1-R | ep 2 | ep 2 |
| S2 / S2-R | Input 0 → ep 3, Input 1 → ep 4 | ep 3, ep 4 |
| D1 / D1-R | Input 0 → ep 2, Input 1 → ep 3 | ep 2, ep 3 |
| J1 / J1-R | Input 0 → ep 2, Input 1 → ep 3 | ep 2, ep 3 |
| LD6 | Must supply explicitly | Must supply explicitly |


## Examples

### Configure first input of C4 as a simple toggle

```yaml
action: ubisys_poc.write_input_actions_preset
data:
  device_id: "abc123def456..."
  presets:
    - input_index: 0
      preset: toggle
```

### Configure first input of S2 as an on/off rocker

```yaml
action: ubisys_poc.write_input_actions_preset
data:
  device_id: "xyz789..."
  presets:
    - input_index: 0
      preset: on_off_switch
```

### Configure a C4 with mixed presets

```yaml
action: ubisys_poc.write_input_actions_preset
data:
  device_id: "c4device..."
  presets:
    - input_index: 0
      preset: toggle
    - input_index: 1
      preset: dimmer_single
    - input_index: 2
      preset: scene
      scene_id: 1
    - input_index: 3
      preset: cover
```

### Configure a two-button dimmer (C4 inputs 0 and 1)

```yaml
action: ubisys_poc.write_input_actions_preset
data:
  device_id: "c4device..."
  presets:
    - input_index: 0
      preset: dimmer_double
```

This configures inputs 0 and 1: short press up turns on / dims up; short press down turns off / dims down; hold stops dimming on release.

### Invert an input (normally-closed switch)

```yaml
action: ubisys_poc.write_input_configurations
data:
  device_id: "s2device..."
  input_configurations:
    - inverted: false
    - inverted: true
```

### Disable an input

```yaml
action: ubisys_poc.write_input_configurations
data:
  device_id: "c4device..."
  input_configurations:
    - disabled: false
    - disabled: false
    - disabled: false
    - disabled: true
```

### Read and inspect current input actions

```yaml
action: ubisys_poc.read_input_actions
data:
  device_id: "c4device..."
```

Typical response for a C4 input configured as `toggle` on all inputs:

```yaml
ieee: ...
model: C4 (5504)
input_actions:
  - input_index: 0
    manufacturer_specific: false
    has_alternate: false
    is_alternate: false
    initial_state: released
    final_state: pressed
    source_endpoint: 1
    cluster_id: 6
    command_template:
      - 2
  - input_index: 1
    manufacturer_specific: false
    has_alternate: false
    is_alternate: false
    initial_state: released
    final_state: pressed
    source_endpoint: 2
    cluster_id: 6
    command_template:
      - 2
  - input_index: 2
    manufacturer_specific: false
    has_alternate: false
    is_alternate: false
    initial_state: released
    final_state: pressed
    source_endpoint: 3
    cluster_id: 6
    command_template:
      - 2
  - input_index: 3
    manufacturer_specific: false
    has_alternate: false
    is_alternate: false
    initial_state: released
    final_state: pressed
    source_endpoint: 4
    cluster_id: 6
    command_template:
      - 2
```


## Input action reference

### State names

| Name | Description |
|---|---|
| `ignored` | Don't care — matches any state |
| `pressed` | Active for less than one second |
| `kept_pressed` | Active for more than one second (held) |
| `released` | Inactive |

### Common transition shorthands

These initial/final state combinations cover the most common real-world behaviours:

| Name | initial_state | final_state | has_alternate | Description |
|---|---|---|---|---|
| PRESS | `released` | `pressed` | false | Fires when button is pressed down |
| SHORT_PRESS | `pressed` | `released` | false | Fires on short press release |
| ANY_RELEASE | `ignored` | `released` | false | Fires on any release |
| LONG_PRESS | `pressed` | `kept_pressed` | false | Fires after holding for > 1 s |
| RELEASE_AFTER_LONG | `kept_pressed` | `released` | false | Fires on release after long hold |
| PRESS_AND_KEEP (primary) | `pressed` | `kept_pressed` | true | Primary of alternating pair |
| PRESS_AND_KEEP (alternate) | `pressed` | `kept_pressed` | true | Alternate of pair (`is_alternate: true`) |

### Common cluster IDs and commands

| Cluster | ID (dec) | Command byte | Action |
|---|---|---|---|
| On/Off | 6 | `0x00` | Turn off |
| On/Off | 6 | `0x01` | Turn on |
| On/Off | 6 | `0x02` | Toggle |
| Level Control | 8 | `0x05, 0x00, 0x32` | Move level up |
| Level Control | 8 | `0x05, 0x01, 0x32` | Move level down |
| Level Control | 8 | `0x03` | Stop level change |
| Window Covering | 258 | `0x00` | Open / raise |
| Window Covering | 258 | `0x01` | Close / lower |
| Window Covering | 258 | `0x02` | Stop |
| Scenes | 5 | `0x05, 0x00, 0x00, <scene_id>` | Recall scene |


## Technical reference

### Setup cluster

All ubisys devices (except R0) expose a manufacturer-specific setup cluster at the same address:

| Field | Value |
|---|---|
| Endpoint | `0xE8` (232) |
| Cluster ID | `0xFC00` |
| Manufacturer ID | `0x10F2` |

On C4 and S2 the cluster appears in `in_clusters`. On LD6 it appears in `out_clusters`.

### Attribute IDs

| Attribute | ID | Description |
|---|---|---|
| `InputConfigurations` | `0x0000` | One byte per physical input |
| `InputActions` | `0x0001` | Variable-length array of action entries |
| `OutputConfigurations` | `0x0010` | LD6 only; six entries of 6 bytes each |

### Model identifiers

The integration matches device capabilities using the full model string as reported by the ZCL Basic cluster `ModelIdentifier` attribute. These always include the product code suffix:

| Model | ZCL model string |
|---|---|
| C4 | `C4 (5504)` |
| S1 | `S1 (5501)` |
| S1-R | `S1-R (5601)` |
| S2 | `S2 (5502)` |
| S2-R | `S2-R (5602)` |
| D1 | `D1 (5503)` |
| D1-R | `D1-R (5603)` |
| J1 | `J1 (5502)` |
| J1-R | `J1-R (5602)` |
| LD6 | `LD6` (no suffix) |
| R0 | `R0 (5501)` |


## References

- [Ubisys C4 technical reference](https://www.ubisys.de/downloads/ubisys-c4-technical-reference.pdf)
- [Ubisys S2 technical reference](https://www.ubisys.de/downloads/ubisys-s2-technical-reference.pdf)
- [Ubisys D1 technical reference](https://www.ubisys.de/downloads/ubisys-d1-technical-reference.pdf)
- [Ubisys J1 technical reference](https://www.ubisys.de/downloads/ubisys-j1-technical-reference.pdf)
- [Ubisys LD6 technical reference](https://www.ubisys.de/wp-content/uploads/ubisys-ld6-technical-reference.pdf)
- [Zigbee2MQTT Ubisys device converters](https://github.com/Koenkk/zigbee-herdsman-converters/blob/master/src/devices/ubisys.ts)
- [Zigbee2MQTT Ubisys library](https://github.com/Koenkk/zigbee-herdsman-converters/blob/master/src/lib/ubisys.ts)
- [Zigbee2MQTT C4 documentation](https://www.zigbee2mqtt.io/devices/C4.html)
- [Zigbee2MQTT S1 documentation](https://www.zigbee2mqtt.io/devices/S1.html)
- [Zigbee2MQTT S1-R documentation](https://www.zigbee2mqtt.io/devices/S1-R.html)
- [Zigbee2MQTT S2 documentation](https://www.zigbee2mqtt.io/devices/S2.html)
- [zigpy library](https://github.com/zigpy/zigpy)
- [ZHA integration](https://www.home-assistant.io/integrations/zha)
