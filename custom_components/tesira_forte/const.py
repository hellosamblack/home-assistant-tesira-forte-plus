"""Constants for the Biamp Tesira integration."""

from __future__ import annotations

DOMAIN = "tesira_forte"

DEFAULT_PORT = 23

CONF_DESIGN = "design"

# Tesira fader / level range (dB). Matches the inputMinLevel/inputMaxLevel a
# Standard/Matrix Mixer reports; also the range of a Level Control block.
LEVEL_MIN = -100.0
LEVEL_MAX = 12.0
LEVEL_STEP = 0.5

# Analog mic pre-gain (dB) on an AEC Input / Mic-Line Input block: discrete
# 6 dB steps, 0..66.
GAIN_MIN = 0
GAIN_MAX = 66
GAIN_STEP = 6

# DEVICE activeFaultList poll interval (also the TTP keepalive).
FAULT_POLL_INTERVAL = 15

# AudioMeter subscription push rate (ms). A design block can override it with
# "rate_ms" (RATE_MS_MIN..RATE_MS_MAX).
METER_RATE_MS = 500
# Signal Present meters push only on change; the rate is still required.
SIGNAL_RATE_MS = 1000
RATE_MS_MIN = 100
RATE_MS_MAX = 60000

# Seconds between re-reads of the attributes Tesira can't push (dynamics bypass
# and parameters, AEC/Mic-Line input gain).
RECONCILE_INTERVAL = 30

# Optional "controls" of a dynamics block (Noise Gate, Ducker, Compressor ...):
# attribute -> (entity label, min, max, step, unit, icon). None of them is subscribable on
# FW 5.7, so all are polled. Release is capped at 3000 ms: the unit rejected a
# Noise Gate configured with 10000 ms.
DYNAMICS_CONTROLS: dict[str, tuple[str, float, float, float, str, str]] = {
    "threshold": ("threshold", -80.0, 0.0, 1.0, "dB", "mdi:arrow-collapse-down"),
    "releaseTime": ("release time", 1.0, 3000.0, 10.0, "ms", "mdi:timer-sand"),
    "duckingLevel": ("ducking level", -100.0, 0.0, 1.0, "dB", "mdi:volume-low"),
}

# Supported DSP block kinds. The config flow auto-discovers the running design
# over TTP (SESSION get aliases + per-block attribute probing; see discovery.py)
# and the user reviews it; it can also be edited by hand or re-pulled from the
# options flow, or seeded from YAML. Each entry: {"tag": <instanceTag>, "kind":
# <one of these>, ...size fields}.
#
#   aecinput / input   channels          -> per ch: gain (number), phantomPower (switch)
#   meter              channels          -> per ch: level (sensor, dB), fed by
#                                           one packed `levels` subscription
#   signalpresent      channels          -> per ch: present (binary_sensor, off by
#                                           default) + one "<tag> present" (any ch)
#   dynamics           [controls]        -> "<tag> active" switch (on = bypass
#                                           false) + optional polled numbers
#   presets            presets           -> one button per {"id"/"name"}
#   level              channels          -> per ch: level (number), mute (switch)
#   mute               channels          -> per ch: mute (switch)
#   standardmixer      inputs, outputs   -> in/out level+mute, crosspoint (switch)
#   matrixmixer        inputs, outputs   -> in/out level+mute, per-crosspoint
#                                           level (number) + crosspointLevelState (switch)
BLOCK_KINDS = frozenset(
    {
        "aecinput", "input", "meter", "signalpresent", "level", "mute",
        "standardmixer", "matrixmixer", "dynamics", "presets",
    }
)
# Kinds sized by "channels".
CHANNEL_KINDS = frozenset({"aecinput", "input", "meter", "signalpresent", "level", "mute"})
# Kinds that subscribe and so accept a per-block "rate_ms".
RATE_KINDS = frozenset({"meter", "signalpresent"})

# Fallback only. The config flow discovers the real design from the device;
# this generic 3-block list is used just to seed the review step (or an entry
# with no design) if discovery is skipped or the device returns nothing. The
# tags are Tesira's stock default block names.
DEFAULT_DESIGN: list[dict] = [
    {"tag": "AecInput1", "kind": "aecinput", "channels": 2},
    {"tag": "AudioMeter1", "kind": "meter", "channels": 2},
    {"tag": "Mixer1", "kind": "matrixmixer", "inputs": 4, "outputs": 4},
]
