"""Constants for the Ubisys integration."""

DOMAIN = "ubisys_poc"

# Hardware addresses — the same across all known ubisys devices.
UBISYS_SETUP_ENDPOINT_ID: int = 0xE8  # 232
UBISYS_SETUP_CLUSTER_ID: int = 0xFC00
UBISYS_MANUFACTURER_ID: int = 0x10F2

# Attribute IDs within the setup cluster (cluster 0xFC00).
ATTR_INPUT_CONFIGURATIONS: int = 0x0000
ATTR_INPUT_ACTIONS: int = 0x0001
ATTR_OUTPUT_CONFIGURATIONS: int = 0x0010

SERVICE_READ_INPUT_CONFIGURATIONS = "read_input_configurations"
SERVICE_READ_INPUT_ACTIONS = "read_input_actions"
SERVICE_READ_OUTPUT_CONFIGURATIONS = "read_output_configurations"
SERVICE_READ_RAW_INPUT_CONFIGURATIONS = "read_raw_input_configurations"
SERVICE_READ_RAW_INPUT_ACTIONS = "read_raw_input_actions"
SERVICE_READ_RAW_OUTPUT_CONFIGURATIONS = "read_raw_output_configurations"

SERVICE_WRITE_INPUT_CONFIGURATIONS = "write_input_configurations"
SERVICE_WRITE_INPUT_ACTIONS = "write_input_actions"
SERVICE_WRITE_OUTPUT_CONFIGURATIONS = "write_output_configurations"
SERVICE_WRITE_RAW_INPUT_CONFIGURATIONS = "write_raw_input_configurations"
SERVICE_WRITE_RAW_INPUT_ACTIONS = "write_raw_input_actions"
SERVICE_WRITE_RAW_OUTPUT_CONFIGURATIONS = "write_raw_output_configurations"
SERVICE_WRITE_INPUT_ACTIONS_PRESET = "write_input_actions_preset"
