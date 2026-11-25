"""Runtime coordinator: owns the TTP session and the mirrored control state.

The integration keeps exactly one TTP session to the Tesira. Everything -
initial reads, subscriptions, polling of attributes Tesira can't push, writes,
preset recalls and re-discovery - goes over it. Opening sessions is the
expensive part for the Tesira's Telnet server (~2.5 s each; concurrent opens
time out), not the bytes on the wire.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
import logging

from homeassistant.core import HomeAssistant, callback

from .const import (
    BLOCK_KINDS,
    CHANNEL_KINDS,
    DYNAMICS_CONTROLS,
    FAULT_POLL_INTERVAL,
    GAIN_MAX,
    GAIN_MIN,
    GAIN_STEP,
    LEVEL_MAX,
    LEVEL_MIN,
    LEVEL_STEP,
    METER_RATE_MS,
    RATE_KINDS,
    RATE_MS_MAX,
    RATE_MS_MIN,
    RECONCILE_INTERVAL,
    SIGNAL_RATE_MS,
)
from .ttp import TesiraTTP, idx_str, join_cmd, parse_array

_LOGGER = logging.getLogger(__name__)


def is_true(raw: str | None) -> bool | None:
    """Parse a TTP boolean ("true"/"false", "1"/"0"); None if unknown."""
    if raw is None:
        return None
    return raw.strip().strip('"').lower() in ("true", "1")


@dataclass(frozen=True)
class ControlSpec:
    """One HA entity mapped to a Tesira (block, attribute, index-tuple)."""

    platform: str  # number | switch | sensor | binary_sensor
    block: str
    attr: str
    idx: tuple[int, ...]
    name: str
    icon: str | None = None
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    mode: str | None = None
    state_class: str | None = None
    rate_ms: int | None = None
    subscribable: bool = True  # False -> re-read by the reconcile loop
    device_class: str | None = None
    # On means the raw attribute is false (a dynamics block's `bypass`).
    invert: bool = False
    enabled_default: bool = True
    # The value arrives in the block's packed `levels` array, not per channel.
    packed: bool = False
    # Computed in the coordinator: on if any of these tokens is on.
    sources: tuple[str, ...] = ()

    @property
    def token(self) -> str:
        return f"{self.block}|{self.attr}|{'_'.join(str(i) for i in self.idx)}"

    @property
    def key(self) -> str:
        base = f"{self.block}_{self.attr}"
        if not self.idx:  # index-less attribute (bypass) or a computed entity
            return base
        return f"{base}_{'_'.join(str(i) for i in self.idx)}"

    @property
    def computed(self) -> bool:
        return bool(self.sources)

    @property
    def get_cmd(self) -> str:
        return join_cmd(self.block, "get", self.attr, idx_str(self.idx))


@dataclass(frozen=True)
class Read:
    """One ``get`` and the token(s) its reply fills."""

    cmd: str
    tokens: tuple[str, ...]
    packed: bool = False  # reply is an array, one element per token
    polled: bool = False  # re-read on every reconcile pass


@dataclass(frozen=True)
class Subscription:
    block: str
    attr: str
    idx: tuple[int, ...]
    token: str
    rate_ms: int | None


def _level(block: str, attr: str, idx: tuple[int, ...], name: str) -> ControlSpec:
    return ControlSpec(
        "number", block, attr, idx, name,
        icon="mdi:tune-vertical", unit="dB",
        minimum=LEVEL_MIN, maximum=LEVEL_MAX, step=LEVEL_STEP, mode="slider",
    )


def _switch(block: str, attr: str, idx: tuple[int, ...], name: str,
            icon: str = "mdi:volume-mute") -> ControlSpec:
    return ControlSpec("switch", block, attr, idx, name, icon=icon)


class DesignError(Exception):
    """The configured design list is malformed."""


def _check_int(tag: str, blk: dict, field: str, lo: int, hi: int) -> None:
    v = blk.get(field)
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise DesignError(f"block {tag!r}: '{field}' must be {lo}..{hi}")


def _validate_presets(blk: dict) -> None:
    presets = blk.get("presets")
    if not isinstance(presets, list) or not presets:
        raise DesignError("'presets' must be a non-empty list")
    seen: set[object] = set()
    for i, p in enumerate(presets):
        if not isinstance(p, dict):
            raise DesignError(f"preset {i} is not an object")
        pid, name = p.get("id"), p.get("name")
        if pid is None and name is None:
            raise DesignError(f"preset {i} needs an 'id' or a 'name'")
        if pid is not None and (
            not isinstance(pid, int) or isinstance(pid, bool) or pid < 0
        ):
            raise DesignError(f"preset {i}: 'id' must be a non-negative integer")
        if name is not None and (not isinstance(name, str) or not name):
            raise DesignError(f"preset {i}: 'name' must be a non-empty string")
        ident = pid if pid is not None else name
        if ident in seen:
            raise DesignError(f"duplicate preset {ident!r}")
        seen.add(ident)


def validate_design(design: object) -> list[dict]:
    """Type/shape-check a user-supplied design list; raise DesignError if bad."""
    if not isinstance(design, list) or not design:
        raise DesignError("design must be a non-empty list of block objects")
    seen: set[str] = set()
    have_presets = False
    out: list[dict] = []
    for i, blk in enumerate(design):
        if not isinstance(blk, dict):
            raise DesignError(f"block {i} is not an object")
        tag, kind = blk.get("tag"), blk.get("kind")
        if kind not in BLOCK_KINDS:
            raise DesignError(
                f"block {tag or i!r}: kind must be one of {sorted(BLOCK_KINDS)}"
            )
        if kind == "presets":  # not a DSP block; no tag needed
            if have_presets:
                raise DesignError("only one 'presets' entry is allowed")
            have_presets = True
            _validate_presets(blk)
            out.append(blk)
            continue
        if not isinstance(tag, str) or not tag:
            raise DesignError(f"block {i} is missing a string 'tag'")
        if tag in seen:
            raise DesignError(f"duplicate tag {tag!r}")
        seen.add(tag)
        if kind in CHANNEL_KINDS:
            _check_int(tag, blk, "channels", 1, 64)
        elif kind == "dynamics":
            controls = blk.get("controls", [])
            if not isinstance(controls, list) or len(set(controls)) != len(controls):
                raise DesignError(f"block {tag!r}: 'controls' must be a list")
            for c in controls:
                if c not in DYNAMICS_CONTROLS:
                    raise DesignError(
                        f"block {tag!r}: control {c!r} is not one of "
                        f"{sorted(DYNAMICS_CONTROLS)}"
                    )
        else:  # mixers
            for f in ("inputs", "outputs"):
                _check_int(tag, blk, f, 1, 64)
        if "rate_ms" in blk:
            if kind not in RATE_KINDS:
                raise DesignError(
                    f"block {tag!r}: 'rate_ms' only applies to {sorted(RATE_KINDS)}"
                )
            _check_int(tag, blk, "rate_ms", RATE_MS_MIN, RATE_MS_MAX)
        out.append(blk)
    return out


def build_specs(design: list[dict]) -> list[ControlSpec]:
    """Expand a validated design list into the full entity list."""
    specs: list[ControlSpec] = []
    for blk in design:
        tag, kind = blk.get("tag"), blk["kind"]
        if kind == "presets":
            continue  # buttons, see build_presets
        if kind in ("aecinput", "input"):
            for ch in range(1, blk["channels"] + 1):
                specs.append(ControlSpec(
                    "number", tag, "gain", (ch,), f"{tag} ch{ch} gain",
                    icon="mdi:microphone-settings", unit="dB",
                    minimum=GAIN_MIN, maximum=GAIN_MAX, step=GAIN_STEP, mode="box",
                    subscribable=False,  # AEC/Mic-Line input gain isn't subscribable
                ))
                specs.append(_switch(
                    tag, "phantomPower", (ch,), f"{tag} ch{ch} phantom power",
                    icon="mdi:flash",
                ))
        elif kind == "meter":
            rate = blk.get("rate_ms", METER_RATE_MS)
            for ch in range(1, blk["channels"] + 1):
                specs.append(ControlSpec(
                    "sensor", tag, "level", (ch,), f"{tag} ch{ch} level",
                    icon="mdi:sine-wave", unit="dB", state_class="measurement",
                    rate_ms=rate, packed=True,
                ))
        elif kind == "signalpresent":
            rate = blk.get("rate_ms", SIGNAL_RATE_MS)
            chans = [
                ControlSpec(
                    "binary_sensor", tag, "present", (ch,), f"{tag} ch{ch} present",
                    device_class="sound", rate_ms=rate, enabled_default=False,
                )
                for ch in range(1, blk["channels"] + 1)
            ]
            specs.extend(chans)
            specs.append(ControlSpec(
                "binary_sensor", tag, "present", (), f"{tag} present",
                device_class="sound", sources=tuple(c.token for c in chans),
            ))
        elif kind == "dynamics":
            specs.append(ControlSpec(
                "switch", tag, "bypass", (), f"{tag} active",
                icon="mdi:auto-fix", subscribable=False, invert=True,
            ))
            for ctl in blk.get("controls", []):
                label, lo, hi, step, unit, icon = DYNAMICS_CONTROLS[ctl]
                specs.append(ControlSpec(
                    "number", tag, ctl, (), f"{tag} {label}",
                    icon=icon, unit=unit, minimum=lo, maximum=hi, step=step,
                    mode="box", subscribable=False,
                ))
        elif kind == "level":
            for ch in range(1, blk["channels"] + 1):
                specs.append(_level(tag, "level", (ch,), f"{tag} ch{ch} level"))
                specs.append(_switch(tag, "mute", (ch,), f"{tag} ch{ch} mute"))
        elif kind == "mute":
            for ch in range(1, blk["channels"] + 1):
                specs.append(_switch(tag, "mute", (ch,), f"{tag} ch{ch} mute"))
        else:  # standardmixer | matrixmixer
            n_in, n_out = blk["inputs"], blk["outputs"]
            for i in range(1, n_in + 1):
                specs.append(_level(tag, "inputLevel", (i,), f"{tag} in{i} level"))
                specs.append(_switch(tag, "inputMute", (i,), f"{tag} in{i} mute"))
            for o in range(1, n_out + 1):
                lbl = f"{tag} out{o} level" if n_out > 1 else f"{tag} output level"
                specs.append(_level(tag, "outputLevel", (o,), lbl))
                mlbl = f"{tag} out{o} mute" if n_out > 1 else f"{tag} output mute"
                specs.append(_switch(tag, "outputMute", (o,), mlbl))
            for i in range(1, n_in + 1):
                for o in range(1, n_out + 1):
                    if kind == "standardmixer":
                        specs.append(_switch(
                            tag, "crosspoint", (i, o), f"{tag} route {i}→{o}",
                            icon="mdi:call-split",
                        ))
                    else:
                        specs.append(_level(
                            tag, "crosspointLevel", (i, o),
                            f"{tag} crosspoint {i}→{o} level",
                        ))
                        specs.append(_switch(
                            tag, "crosspointLevelState", (i, o),
                            f"{tag} crosspoint {i}→{o} on", icon="mdi:call-split",
                        ))
    return specs


def build_presets(design: list[dict]) -> list[dict]:
    """The {id?, name?} presets listed in a validated design."""
    return [
        dict(p)
        for blk in design
        if blk.get("kind") == "presets"
        for p in blk["presets"]
    ]


def preset_command(preset: dict) -> str:
    """``DEVICE recallPreset <id>``, or by name when only a name is given."""
    if preset.get("id") is not None:
        return f"DEVICE recallPreset {preset['id']}"
    return f'DEVICE recallPresetByName "{preset["name"]}"'


def build_io(
    specs: Iterable[ControlSpec],
) -> tuple[list[Read], list[Subscription], dict[str, tuple[str, ...]]]:
    """Plan the session traffic for a spec list.

    Returns the reads (done on connect; polled ones also every reconcile
    pass), the subscriptions, and the fan-out map from a packed publish token
    to its per-channel tokens. Each meter block gets ONE ``levels``
    subscription instead of one per channel; its array is fanned out to the
    usual ``<tag>|level|<ch>`` tokens so entity unique IDs don't change.
    """
    reads: list[Read] = []
    subs: list[Subscription] = []
    fanout: dict[str, tuple[str, ...]] = {}
    packed: dict[str, list[ControlSpec]] = {}
    for spec in specs:
        if spec.computed:
            continue
        if spec.packed:
            packed.setdefault(spec.block, []).append(spec)
            continue
        reads.append(Read(spec.get_cmd, (spec.token,), polled=not spec.subscribable))
        if spec.subscribable:
            subs.append(Subscription(
                spec.block, spec.attr, spec.idx, spec.token, spec.rate_ms
            ))
    for block, chans in packed.items():
        chans.sort(key=lambda s: s.idx)
        tokens = tuple(s.token for s in chans)
        token = f"{block}|levels|"
        reads.append(Read(join_cmd(block, "get", "levels"), tokens, packed=True))
        subs.append(Subscription(block, "levels", (), token, chans[0].rate_ms))
        fanout[token] = tokens
    return reads, subs, fanout


class TesiraForte:
    """Holds the live TTP connection and the current value of every control."""

    def __init__(
        self, hass: HomeAssistant, host: str, port: int, design: list[dict]
    ) -> None:
        self.hass = hass
        self.host = host
        self.port = port
        self.specs = build_specs(design)
        self.presets = build_presets(design)
        self.serial: str | None = None
        self.firmware: str | None = None
        self.available = False
        self.fault_active = False
        self.fault_detail = "OK"
        self.data: dict[str, str] = {}
        self._listeners: set[Callable[[], None]] = set()
        self._fault_task: asyncio.Task | None = None
        self._reconcile_task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._reads, self._subs, self._fanout = build_io(self.specs)
        self._polled = [r for r in self._reads if r.polled]
        self._aggs_by_source: dict[str, list[ControlSpec]] = {}
        for spec in self.specs:
            for src in spec.sources:
                self._aggs_by_source.setdefault(src, []).append(spec)
        self.ttp = TesiraTTP(
            host, port,
            on_connect=self._on_ttp_connect,
            on_disconnect=self._on_ttp_disconnect,
            publish_cb=self._on_publish,
            fault_cb=self._on_fault,
        )

    # -- setup / teardown --------------------------------------------
    async def async_setup(self) -> None:
        await self.ttp.start()
        # _on_ttp_connect fetches serial/firmware then flips _ready.
        await asyncio.wait_for(self._ready.wait(), timeout=20)
        _LOGGER.info("Tesira Forte SN=%s firmware=%s", self.serial, self.firmware)

    async def async_close(self) -> None:
        for task in (self._fault_task, self._reconcile_task):
            if task:
                task.cancel()
        await self.ttp.stop()

    # -- listeners --------------------------------------------------
    @callback
    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(cb)
        return lambda: self._listeners.discard(cb)

    @callback
    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    # -- value store -------------------------------------------------
    @callback
    def _store(self, tokens: tuple[str, ...], value: str, packed: bool) -> None:
        if packed:
            for token, element in zip(tokens, parse_array(value), strict=False):
                self.data[token] = element
        else:
            self.data[tokens[0]] = value
        self._update_aggregates(tokens)

    @callback
    def _update_aggregates(self, tokens: Iterable[str]) -> None:
        aggs = {a for t in tokens for a in self._aggs_by_source.get(t, ())}
        for agg in aggs:
            known = [
                is_true(self.data[s]) for s in agg.sources if s in self.data
            ]
            if known:
                self.data[agg.token] = "true" if any(known) else "false"

    # -- TTP callbacks -------------------------------------------
    async def _on_ttp_connect(self) -> None:
        # Nothing else is writing yet, so these replies are unambiguous.
        self.serial = await self.ttp.get_value("DEVICE get serialNumber")
        self.firmware = await self.ttp.get_value("DEVICE get version")
        self._ready.set()
        # 1) Pull the current value of everything with plain gets. These are
        #    serialised request/response and dead reliable; do them before any
        #    subscription so there's no async publish traffic to race with.
        for read in self._reads:
            await self._read(read)
            await asyncio.sleep(0.01)
        # 2) Subscribe the attributes that support it, for ongoing changes.
        for sub in self._subs:
            _LOGGER.debug("subscribe %s (rate %s)", sub.token, sub.rate_ms)
            await self.ttp.subscribe(
                sub.block, sub.attr, sub.idx, sub.token, sub.rate_ms
            )
            await asyncio.sleep(0.01)
        self.available = True
        self._notify()
        if self._fault_task is None or self._fault_task.done():
            self._fault_task = self.hass.async_create_background_task(
                self._fault_loop(), name="tesira-fault-poll"
            )
        if self._reconcile_task is None or self._reconcile_task.done():
            self._reconcile_task = self.hass.async_create_background_task(
                self._reconcile_loop(), name="tesira-reconcile"
            )

    async def _read(
        self, read: Read, attempts: int = 3, timeout: float = 12
    ) -> None:
        for attempt in range(1, attempts + 1):
            try:
                value = await self.ttp.get_value(read.cmd, timeout=timeout)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("%s (try %d): %s", read.cmd, attempt, err)
                if attempt < attempts:
                    await asyncio.sleep(0.2)
                continue
            self._store(read.tokens, value, read.packed)
            _LOGGER.debug("%s -> %r", read.cmd, value)
            return

    async def _poll_once(self) -> None:
        """Re-read the attributes Tesira can't push, and backfill any value
        still missing (a get that lost a race during startup)."""
        targets = list(self._polled)
        targets += [
            r for r in self._reads
            if not r.polled and any(t not in self.data for t in r.tokens)
        ]
        for read in targets:
            if not self.ttp.connected:
                return
            await self._read(read)
            await asyncio.sleep(0.05)
        self._notify()

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(RECONCILE_INTERVAL)
            await self._poll_once()

    @callback
    def _on_ttp_disconnect(self) -> None:
        self.available = False
        self._notify()

    @callback
    def _on_publish(self, token: str, value: str) -> None:
        if (tokens := self._fanout.get(token)) is not None:
            self._store(tokens, value, packed=True)
        else:
            self._store((token,), value, packed=False)
        self._notify()

    @callback
    def _on_fault(self, entries: list[tuple[int, str]]) -> None:
        bad = [txt for _fid, txt in entries if txt and "no fault" not in txt.lower()]
        self.fault_active = bool(bad)
        self.fault_detail = "; ".join(bad) if bad else "OK"
        self._notify()

    async def _fault_loop(self) -> None:
        while True:
            try:
                await self.ttp.send("DEVICE get activeFaultList")
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("fault poll send failed: %s", err)
            await asyncio.sleep(FAULT_POLL_INTERVAL)

    # -- writes ------------------------------------------------
    async def async_set(self, spec: ControlSpec, value: str) -> None:
        await self.ttp.set_value(spec.block, spec.attr, spec.idx, value)
        if not spec.subscribable:
            # No publish will come back; read it back so the UI agrees now.
            await asyncio.sleep(0.15)
            await self._read(
                Read(spec.get_cmd, (spec.token,)), attempts=1, timeout=5
            )
            self._notify()

    async def async_recall_preset(self, preset: dict) -> None:
        """Recall a device preset, then refresh what subscriptions don't push."""
        await self.ttp.command(preset_command(preset), timeout=10)
        self.hass.async_create_background_task(
            self._poll_once(), name="tesira-preset-refresh"
        )
