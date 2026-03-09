"""Preset configurations for common Ubisys input action patterns.

Each preset is implemented as a `Preset` subclass with a `build()` method
that returns a list of InputAction dicts accepted by encode_input_action().

Transition byte reference (bits 7-0):
    7  — has_alternate (part of an alternating pair, this is the primary)
    6  — is_alternate  (this entry is the alternate of a pair)
    3-2 — initial InputState: IGNORED=00 PRESSED=01 KEPT_PRESSED=10 RELEASED=11
    1-0 — final   InputState: IGNORED=00 PRESSED=01 KEPT_PRESSED=10 RELEASED=11

Named transition shorthands (transition byte in parentheses):
    PRESS              → initial=RELEASED,       final=PRESSED       (0x0D)
    SHORT_PRESS        → initial=PRESSED,        final=RELEASED      (0x07)
    ANY_RELEASE        → initial=IGNORED,        final=RELEASED      (0x03)
    LONG_PRESS         → initial=PRESSED,        final=KEPT_PRESSED  (0x06)
    RELEASE_AFTER_LONG → initial=KEPT_PRESSED,   final=RELEASED      (0x0B)
    PRESS_AND_KEEP     → initial=PRESSED,        final=KEPT_PRESSED,
                          has_alternate=True                          (0x86)
    ALT_PRESS_AND_KEEP → initial=PRESSED,        final=KEPT_PRESSED,
                          has_alternate=True, is_alternate=True       (0xC6)

Dual-input presets (COVER, COVER_SWITCH, DIMMER_DOUBLE):
    These generate actions for TWO consecutive physical inputs: input_index
    (primary/up/open) and input_index+1 (secondary/down/close).  Both inputs
    share the same source_endpoint.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ._core import InputState

_CLUSTER_ON_OFF = 0x0006
_CLUSTER_SCENES = 0x0005
_CLUSTER_LEVEL_CONTROL = 0x0008
_CLUSTER_WINDOW_COVERING = 0x0102


def _action(
    input_index: int,
    initial: InputState,
    final: InputState,
    source_endpoint: int,
    cluster_id: int,
    command_template: list[int],
    *,
    has_alternate: bool = False,
    is_alternate: bool = False,
) -> dict:
    """Build one InputAction dict in the format expected by encode_input_action."""
    return {
        "input_index": input_index,
        "manufacturer_specific": False,
        "has_alternate": has_alternate,
        "is_alternate": is_alternate,
        "initial_state": initial.name.lower(),
        "final_state": final.name.lower(),
        "source_endpoint": source_endpoint,
        "cluster_id": cluster_id,
        "command_template": command_template,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PRESET BUILDERS
# One concrete class per preset.  Each implements build() and returns a list
# of InputAction dicts ready for encode_input_action().
# ─────────────────────────────────────────────────────────────────────────────


class Preset(ABC):
    """Abstract base class for a named Ubisys input action preset builder.

    Concrete subclasses register themselves by passing ``name=`` in the class
    statement.  Use ``Preset.names()`` to list all registered names (e.g. for
    a select schema) and ``Preset.get(name)`` to retrieve an instance by name.
    """

    _registry: dict[str, Preset] = {}

    def __init_subclass__(cls, name: str = "", **kwargs: object) -> None:
        """Register the subclass under its preset name."""
        super().__init_subclass__(**kwargs)
        if name:
            Preset._registry[name] = cls()

    @classmethod
    def get(cls, name: str) -> Preset:
        """Return the preset instance for the given name.

        Raises:
            ValueError: When name is not a known preset.
        """
        try:
            return cls._registry[name]
        except KeyError:
            raise ValueError(f"Unknown preset: {name!r}") from None

    @classmethod
    def names(cls) -> list[str]:
        """Return all registered preset names in registration order."""
        return list(cls._registry)

    @abstractmethod
    def build(
        self,
        input_index: int,
        source_endpoint: int,
        **kwargs: object,
    ) -> list[dict]:
        """Return InputAction dicts for this preset."""


class _Toggle(Preset, name="toggle"):
    """Single tap toggles the light (fires on button press down)."""

    def build(
        self, input_index: int, source_endpoint: int, **kwargs: object
    ) -> list[dict]:
        """Build actions for the toggle preset."""
        return [
            _action(
                input_index,
                InputState.RELEASED,
                InputState.PRESSED,
                source_endpoint,
                _CLUSTER_ON_OFF,
                [0x02],
            ),
        ]


class _ToggleSwitch(Preset, name="toggle_switch"):
    """Toggle on press AND again on any release (rocker/switch behaviour)."""

    def build(
        self, input_index: int, source_endpoint: int, **kwargs: object
    ) -> list[dict]:
        """Build actions for the toggle_switch preset."""
        return [
            _action(
                input_index,
                InputState.RELEASED,
                InputState.PRESSED,
                source_endpoint,
                _CLUSTER_ON_OFF,
                [0x02],
            ),
            _action(
                input_index,
                InputState.IGNORED,
                InputState.RELEASED,
                source_endpoint,
                _CLUSTER_ON_OFF,
                [0x02],
            ),
        ]


class _OnOffSwitch(Preset, name="on_off_switch"):
    """ON while held, OFF on any release (momentary switch behaviour)."""

    def build(
        self, input_index: int, source_endpoint: int, **kwargs: object
    ) -> list[dict]:
        """Build actions for the on_off_switch preset."""
        return [
            _action(
                input_index,
                InputState.RELEASED,
                InputState.PRESSED,
                source_endpoint,
                _CLUSTER_ON_OFF,
                [0x01],
            ),
            _action(
                input_index,
                InputState.IGNORED,
                InputState.RELEASED,
                source_endpoint,
                _CLUSTER_ON_OFF,
                [0x00],
            ),
        ]


class _On(Preset, name="on"):
    """Unconditionally turns the light on when pressed."""

    def build(
        self, input_index: int, source_endpoint: int, **kwargs: object
    ) -> list[dict]:
        """Build actions for the on preset."""
        return [
            _action(
                input_index,
                InputState.RELEASED,
                InputState.PRESSED,
                source_endpoint,
                _CLUSTER_ON_OFF,
                [0x01],
            ),
        ]


class _Off(Preset, name="off"):
    """Unconditionally turns the light off when pressed."""

    def build(
        self, input_index: int, source_endpoint: int, **kwargs: object
    ) -> list[dict]:
        """Build actions for the off preset."""
        return [
            _action(
                input_index,
                InputState.RELEASED,
                InputState.PRESSED,
                source_endpoint,
                _CLUSTER_ON_OFF,
                [0x00],
            ),
        ]


class _DimmerSingle(Preset, name="dimmer_single"):
    """Single-button dimmer: short press toggles; hold alternates dim-up/down."""

    def build(
        self, input_index: int, source_endpoint: int, **kwargs: object
    ) -> list[dict]:
        """Build actions for the dimmer_single preset."""
        return [
            # Short press — toggle on/off.
            _action(
                input_index,
                InputState.PRESSED,
                InputState.RELEASED,
                source_endpoint,
                _CLUSTER_ON_OFF,
                [0x02],
            ),
            # Hold — dim up (primary of alternating pair).
            _action(
                input_index,
                InputState.PRESSED,
                InputState.KEPT_PRESSED,
                source_endpoint,
                _CLUSTER_LEVEL_CONTROL,
                [0x05, 0x00, 0x32],
                has_alternate=True,
            ),
            # Hold again — dim down (alternate of pair).
            _action(
                input_index,
                InputState.PRESSED,
                InputState.KEPT_PRESSED,
                source_endpoint,
                _CLUSTER_LEVEL_CONTROL,
                [0x05, 0x01, 0x32],
                has_alternate=True,
                is_alternate=True,
            ),
            # Release after hold — stop dimming.
            _action(
                input_index,
                InputState.KEPT_PRESSED,
                InputState.RELEASED,
                source_endpoint,
                _CLUSTER_LEVEL_CONTROL,
                [0x03],
            ),
        ]


class _DimmerDouble(Preset, name="dimmer_double"):
    """Two-button dimmer: input_index = up/on button, input_index+1 = down/off."""

    def build(
        self, input_index: int, source_endpoint: int, **kwargs: object
    ) -> list[dict]:
        """Build actions for the dimmer_double preset."""
        return [
            # Up button — short press: turn on.
            _action(
                input_index,
                InputState.PRESSED,
                InputState.RELEASED,
                source_endpoint,
                _CLUSTER_ON_OFF,
                [0x01],
            ),
            # Up button — hold: dim up.
            _action(
                input_index,
                InputState.PRESSED,
                InputState.KEPT_PRESSED,
                source_endpoint,
                _CLUSTER_LEVEL_CONTROL,
                [0x05, 0x00, 0x32],
            ),
            # Up button — release after hold: stop.
            _action(
                input_index,
                InputState.KEPT_PRESSED,
                InputState.RELEASED,
                source_endpoint,
                _CLUSTER_LEVEL_CONTROL,
                [0x03],
            ),
            # Down button — short press: turn off.
            _action(
                input_index + 1,
                InputState.PRESSED,
                InputState.RELEASED,
                source_endpoint,
                _CLUSTER_ON_OFF,
                [0x00],
            ),
            # Down button — hold: dim down.
            _action(
                input_index + 1,
                InputState.PRESSED,
                InputState.KEPT_PRESSED,
                source_endpoint,
                _CLUSTER_LEVEL_CONTROL,
                [0x05, 0x01, 0x32],
            ),
            # Down button — release after hold: stop.
            _action(
                input_index + 1,
                InputState.KEPT_PRESSED,
                InputState.RELEASED,
                source_endpoint,
                _CLUSTER_LEVEL_CONTROL,
                [0x03],
            ),
        ]


class _Cover(Preset, name="cover"):
    """Two-button cover: input_index = open, input_index+1 = close; short release stops mid-travel."""

    def build(
        self, input_index: int, source_endpoint: int, **kwargs: object
    ) -> list[dict]:
        """Build actions for the cover preset."""
        return [
            _action(
                input_index,
                InputState.RELEASED,
                InputState.PRESSED,
                source_endpoint,
                _CLUSTER_WINDOW_COVERING,
                [0x00],
            ),  # Open
            _action(
                input_index,
                InputState.PRESSED,
                InputState.RELEASED,
                source_endpoint,
                _CLUSTER_WINDOW_COVERING,
                [0x02],
            ),  # Stop
            _action(
                input_index + 1,
                InputState.RELEASED,
                InputState.PRESSED,
                source_endpoint,
                _CLUSTER_WINDOW_COVERING,
                [0x01],
            ),  # Close
            _action(
                input_index + 1,
                InputState.PRESSED,
                InputState.RELEASED,
                source_endpoint,
                _CLUSTER_WINDOW_COVERING,
                [0x02],
            ),  # Stop
        ]


class _CoverSwitch(Preset, name="cover_switch"):
    """Two-button cover: any release (short or long) stops mid-travel."""

    def build(
        self, input_index: int, source_endpoint: int, **kwargs: object
    ) -> list[dict]:
        """Build actions for the cover_switch preset."""
        return [
            _action(
                input_index,
                InputState.RELEASED,
                InputState.PRESSED,
                source_endpoint,
                _CLUSTER_WINDOW_COVERING,
                [0x00],
            ),  # Open
            _action(
                input_index,
                InputState.IGNORED,
                InputState.RELEASED,
                source_endpoint,
                _CLUSTER_WINDOW_COVERING,
                [0x02],
            ),  # Stop on any release
            _action(
                input_index + 1,
                InputState.RELEASED,
                InputState.PRESSED,
                source_endpoint,
                _CLUSTER_WINDOW_COVERING,
                [0x01],
            ),  # Close
            _action(
                input_index + 1,
                InputState.IGNORED,
                InputState.RELEASED,
                source_endpoint,
                _CLUSTER_WINDOW_COVERING,
                [0x02],
            ),  # Stop on any release
        ]


class _CoverUp(Preset, name="cover_up"):
    """Single button that opens/raises the cover when pressed."""

    def build(
        self, input_index: int, source_endpoint: int, **kwargs: object
    ) -> list[dict]:
        """Build actions for the cover_up preset."""
        return [
            _action(
                input_index,
                InputState.RELEASED,
                InputState.PRESSED,
                source_endpoint,
                _CLUSTER_WINDOW_COVERING,
                [0x00],
            ),
        ]


class _CoverDown(Preset, name="cover_down"):
    """Single button that closes/lowers the cover when pressed."""

    def build(
        self, input_index: int, source_endpoint: int, **kwargs: object
    ) -> list[dict]:
        """Build actions for the cover_down preset."""
        return [
            _action(
                input_index,
                InputState.RELEASED,
                InputState.PRESSED,
                source_endpoint,
                _CLUSTER_WINDOW_COVERING,
                [0x01],
            ),
        ]


class _Scene(Preset, name="scene"):
    """Recall a scene on short-press release."""

    def build(
        self, input_index: int, source_endpoint: int, **kwargs: object
    ) -> list[dict]:
        """Build actions for the scene preset."""
        try:
            scene_id = int(kwargs.get("scene_id"))
        except ValueError as e:
            raise ValueError("scene_id must be an integer for the scene preset") from e
        if scene_id is None:
            raise ValueError("scene_id is required for the scene preset")
        return [
            _action(
                input_index,
                InputState.PRESSED,
                InputState.RELEASED,
                source_endpoint,
                _CLUSTER_SCENES,
                [0x05, 0x00, 0x00, scene_id & 0xFF],
            ),
        ]


class _SceneSwitch(Preset, name="scene_switch"):
    """Recall a scene immediately on button press."""

    def build(
        self, input_index: int, source_endpoint: int, **kwargs: object
    ) -> list[dict]:
        """Build actions for the scene_switch preset."""
        try:
            scene_id = int(kwargs.get("scene_id"))
        except ValueError as e:
            raise ValueError(
                "scene_id must be an integer for the scene_switch preset"
            ) from e
        if scene_id is None:
            raise ValueError("scene_id is required for the scene_switch preset")
        return [
            _action(
                input_index,
                InputState.RELEASED,
                InputState.PRESSED,
                source_endpoint,
                _CLUSTER_SCENES,
                [0x05, 0x00, 0x00, scene_id & 0xFF],
            ),
        ]
