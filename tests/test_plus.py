"""Tests for the "plus" features: packed meters, signal present, dynamics,
presets, design validation and discovery (no Home Assistant or device)."""
import asyncio

import pytest

from conftest import load

ttp = load("ttp")
coord = load("coordinator")
disc = load("discovery")


# -- publish parsing -------------------------------------------------------
def _publishes(lines):
    got = []
    client = ttp.TesiraTTP("127.0.0.1", publish_cb=lambda t, v: got.append((t, v)))
    for line in lines:
        client._dispatch(line)
    return got


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('! "NvidiaRawSig|present|1" false', ("NvidiaRawSig|present|1", "false")),
        ('! "MainLvl|level|1" -20.000000', ("MainLvl|level|1", "-20.000000")),
        ('! "x|label|1" "Hello world"', ("x|label|1", '"Hello world"')),
        (
            '! "NvidiaNormPk|levels|" [-100.000000 -99.5]',
            ("NvidiaNormPk|levels|", "[-100.000000 -99.5]"),
        ),
        # verbose-mode labelling
        ('! "publishToken":"tok" "value":false', ("tok", "false")),
        ('! "publishToken":"lv" "value":[-1.0 -2.0]', ("lv", "[-1.0 -2.0]")),
    ],
)
def test_publish_re(line, expected):
    assert _publishes([line]) == [expected]


def test_get_levels_reply_is_not_a_fault_list():
    faults = []
    client = ttp.TesiraTTP("127.0.0.1", fault_cb=faults.append)
    client._dispatch("+OK [-100.000000 -100.000000]")
    assert faults == []


def test_parse_array():
    assert ttp.parse_array("[-100.000000 -99.980003]") == ["-100.000000", "-99.980003"]
    assert ttp.parse_array(" [1  2\t3] ") == ["1", "2", "3"]


def test_join_cmd_drops_empty_index():
    assert ttp.join_cmd("VocalGate", "get", "bypass", ttp.idx_str(())) == "VocalGate get bypass"
    assert ttp.join_cmd("M", "set", "level", "1 2", "-3.00") == "M set level 1 2 -3.00"


@pytest.mark.asyncio
async def test_subscribe_line_formats():
    sent = []
    client = ttp.TesiraTTP("127.0.0.1")

    async def fake_send(line):
        sent.append(line)

    client._send_raw = fake_send
    await client.subscribe("Pk", "levels", (), "Pk|levels|", 500)
    await client.subscribe("Sig", "present", (1,), "Sig|present|1", 1000)
    await client.subscribe("Lvl", "mute", (2,), "Lvl|mute|2")
    assert sent == [
        'Pk subscribe levels "Pk|levels|" 500',
        'Sig subscribe present 1 "Sig|present|1" 1000',
        'Lvl subscribe mute 2 "Lvl|mute|2"',
    ]


# -- design validation ------------------------------------------------------
GOOD = [
    {"tag": "ShieldLvl", "kind": "level", "channels": 2},
    {"tag": "NvidiaNormRms", "kind": "meter", "channels": 2, "rate_ms": 1000},
    {"tag": "NvidiaRawSig", "kind": "signalpresent", "channels": 2},
    {"tag": "VocalGate", "kind": "dynamics"},
    {"tag": "WiiMDuck", "kind": "dynamics", "controls": ["threshold", "duckingLevel"]},
    {"kind": "presets", "presets": [{"id": 1001, "name": "Karaoke default"}, {"name": "Party"}]},
]


def test_validate_accepts_new_kinds():
    assert coord.validate_design(GOOD) == GOOD


@pytest.mark.parametrize(
    "bad",
    [
        [{"tag": "M", "kind": "meter", "channels": 2, "rate_ms": 50}],
        [{"tag": "M", "kind": "meter", "channels": 2, "rate_ms": "500"}],
        [{"tag": "L", "kind": "level", "channels": 2, "rate_ms": 500}],
        [{"tag": "S", "kind": "signalpresent"}],
        [{"tag": "G", "kind": "dynamics", "controls": ["attackTime"]}],
        [{"tag": "G", "kind": "dynamics", "controls": "threshold"}],
        [{"tag": "G", "kind": "dynamics", "controls": ["threshold", "threshold"]}],
        [{"kind": "presets", "presets": []}],
        [{"kind": "presets", "presets": [{}]}],
        [{"kind": "presets", "presets": [{"id": -1}]}],
        [{"kind": "presets", "presets": [{"id": 1}, {"id": 1}]}],
        [{"kind": "presets", "presets": [{"id": 1}]}, {"kind": "presets", "presets": [{"id": 2}]}],
        [{"kind": "dynamics"}],
    ],
)
def test_validate_rejects(bad):
    with pytest.raises(coord.DesignError):
        coord.validate_design(bad)


# -- build_specs ------------------------------------------------------------
def _by_key(specs):
    return {s.key: s for s in specs}


def test_signalpresent_specs():
    specs = _by_key(coord.build_specs([GOOD[2]]))
    assert set(specs) == {
        "NvidiaRawSig_present_1", "NvidiaRawSig_present_2", "NvidiaRawSig_present",
    }
    ch1 = specs["NvidiaRawSig_present_1"]
    assert (ch1.platform, ch1.device_class, ch1.rate_ms) == ("binary_sensor", "sound", 1000)
    assert ch1.enabled_default is False and ch1.subscribable
    anyp = specs["NvidiaRawSig_present"]
    assert anyp.name == "NvidiaRawSig present" and anyp.enabled_default
    assert anyp.sources == ("NvidiaRawSig|present|1", "NvidiaRawSig|present|2")


def test_dynamics_specs():
    specs = _by_key(coord.build_specs([GOOD[4]]))
    sw = specs["WiiMDuck_bypass"]
    assert (sw.platform, sw.name, sw.invert, sw.subscribable) == (
        "switch", "WiiMDuck active", True, False,
    )
    assert sw.get_cmd == "WiiMDuck get bypass"
    thr = specs["WiiMDuck_threshold"]
    assert (thr.platform, thr.minimum, thr.maximum, thr.unit) == ("number", -80, 0, "dB")
    duck = specs["WiiMDuck_duckingLevel"]
    assert (duck.minimum, duck.maximum, duck.subscribable) == (-100, 0, False)
    assert "WiiMDuck_releaseTime" not in specs
    rel = _by_key(coord.build_specs(
        [{"tag": "G", "kind": "dynamics", "controls": ["releaseTime"]}]
    ))["G_releaseTime"]
    assert (rel.minimum, rel.maximum, rel.step, rel.unit) == (1, 3000, 10, "ms")


def test_existing_unique_id_keys_unchanged():
    specs = _by_key(coord.build_specs([
        {"tag": "MainLvl", "kind": "level", "channels": 2},
        {"tag": "NvidiaNormPk", "kind": "meter", "channels": 2},
        {"tag": "Mixer1", "kind": "matrixmixer", "inputs": 1, "outputs": 1},
    ]))
    assert {"MainLvl_level_1", "MainLvl_mute_2", "NvidiaNormPk_level_1",
            "Mixer1_crosspointLevel_1_1"} <= set(specs)
    assert specs["NvidiaNormPk_level_2"].token == "NvidiaNormPk|level|2"


def test_presets():
    presets = coord.build_presets(GOOD)
    assert presets == [{"id": 1001, "name": "Karaoke default"}, {"name": "Party"}]
    assert coord.preset_command(presets[0]) == "DEVICE recallPreset 1001"
    assert coord.preset_command(presets[1]) == 'DEVICE recallPresetByName "Party"'
    assert all(s.block is not None for s in coord.build_specs(GOOD))


# -- build_io: one packed subscription per meter block -----------------------
def test_build_io_packs_meters():
    specs = coord.build_specs(GOOD)
    reads, subs, fanout = coord.build_io(specs)
    meter_subs = [s for s in subs if s.block == "NvidiaNormRms"]
    assert len(meter_subs) == 1
    assert (meter_subs[0].attr, meter_subs[0].idx, meter_subs[0].rate_ms) == ("levels", (), 1000)
    assert fanout == {
        "NvidiaNormRms|levels|": ("NvidiaNormRms|level|1", "NvidiaNormRms|level|2")
    }
    meter_reads = [r for r in reads if r.cmd.startswith("NvidiaNormRms")]
    assert [(r.cmd, r.packed) for r in meter_reads] == [("NvidiaNormRms get levels", True)]
    # dynamics are polled, never subscribed; computed specs are neither
    assert not any(s.block in ("VocalGate", "WiiMDuck") for s in subs)
    polled = {r.cmd for r in reads if r.polled}
    assert polled == {
        "VocalGate get bypass", "WiiMDuck get bypass",
        "WiiMDuck get threshold", "WiiMDuck get duckingLevel",
    }
    assert not any(s.token == "NvidiaRawSig|present|" for s in subs)
    assert not any("NvidiaRawSig|present|" in r.tokens for r in reads)
    sig = [s for s in subs if s.block == "NvidiaRawSig"]
    assert [(s.attr, s.idx, s.rate_ms) for s in sig] == [
        ("present", (1,), 1000), ("present", (2,), 1000),
    ]


# -- coordinator data flow (fan-out, aggregates) -------------------------------
class _Hass:
    def async_create_background_task(self, coro, name=None):
        return asyncio.ensure_future(coro)


def _coordinator(design):
    return coord.TesiraForte(_Hass(), "127.0.0.1", 23, coord.validate_design(design))


def test_levels_fan_out_to_channel_tokens():
    c = _coordinator([GOOD[1]])
    c._on_publish("NvidiaNormRms|levels|", "[-12.500000 -30.000000]")
    assert c.data["NvidiaNormRms|level|1"] == "-12.500000"
    assert c.data["NvidiaNormRms|level|2"] == "-30.000000"
    assert "NvidiaNormRms|levels|" not in c.data


def test_any_channel_present_aggregate():
    c = _coordinator([GOOD[2]])
    agg = "NvidiaRawSig|present|"
    c._on_publish("NvidiaRawSig|present|1", "false")
    assert c.data[agg] == "false"
    c._on_publish("NvidiaRawSig|present|2", "true")
    assert c.data[agg] == "true"
    c._on_publish("NvidiaRawSig|present|2", "false")
    assert c.data[agg] == "false"


@pytest.mark.asyncio
async def test_connect_reads_then_subscribes():
    c = _coordinator(GOOD)
    replies = {
        "DEVICE get serialNumber": "SN1", "DEVICE get version": "5.7.2.6",
        "NvidiaNormRms get levels": "[-1.0 -2.0]",
        "NvidiaRawSig get present 1": "true", "NvidiaRawSig get present 2": "false",
        "VocalGate get bypass": "true", "WiiMDuck get bypass": "false",
        "WiiMDuck get threshold": "-50.000000", "WiiMDuck get duckingLevel": "-100.000000",
        "ShieldLvl get level 1": "-10.0", "ShieldLvl get level 2": "-10.0",
        "ShieldLvl get mute 1": "false", "ShieldLvl get mute 2": "false",
    }
    sent = []

    async def get_value(cmd, timeout=5.0):
        sent.append(cmd)
        return replies[cmd]

    async def subscribe(block, attr, idx, token, rate_ms=None):
        sent.append(f"sub {token} {rate_ms}")

    c.ttp.get_value = get_value
    c.ttp.subscribe = subscribe
    await c._on_ttp_connect()
    for t in (c._fault_task, c._reconcile_task):
        t.cancel()
    assert c.data["NvidiaNormRms|level|2"] == "-2.0"
    assert c.data["NvidiaRawSig|present|"] == "true"
    assert c.data["VocalGate|bypass|"] == "true"
    subs = [s for s in sent if s.startswith("sub ")]
    assert "sub NvidiaNormRms|levels| 1000" in subs
    assert not any("bypass" in s or "threshold" in s for s in subs)
    last_get = max(i for i, s in enumerate(sent) if not s.startswith("sub "))
    assert sent.index(subs[0]) > last_get


# -- discovery -----------------------------------------------------------------
# Probe matrix from FW 5.7.2.6 (2026-11-25): attributes each block answers.
PROBES = {
    "ShieldLvl": {"numChannels": "2", "level 1": "-10", "mute 1": "false"},
    "Mixer1": {"numInputs": "6", "numOutputs": "2", "crosspointLevel 1 1": "0"},
    "AecInput1": {"numChannels": "4", "gain 1": "0", "phantomPower 1": "false"},
    "NvidiaNormPk": {"numChannels": "2", "level 1": "-100"},
    "NvidiaRawSig": {"numChannels": "2", "level 1": "-100", "threshold 1": "-50",
                     "present 1": "false"},
    "VocalGate": {"numChannels": "2", "threshold": "-50", "releaseTime": "3000",
                  "bypass": "true"},
    "WiiMDuck": {"numChannels": "2", "threshold": "-50", "releaseTime": "3000",
                 "bypass": "true", "duckingLevel": "-100"},
    "Eq1": {"numChannels": "2", "bypass": "false", "gain 1": "0"},
    "Crossover1": {"numInputs": "1", "numOutputs": "2"},
}


class _FakeClient:
    def __init__(self):
        self.timeouts = set()

    async def get_value(self, cmd, timeout=5.0):
        self.timeouts.add(timeout)
        if cmd == "SESSION get aliases":
            return "[" + " ".join(f'"{t}"' for t in ["DEVICE", *PROBES]) + "]"
        tag, _get, attr = cmd.split(" ", 2)
        try:
            return PROBES[tag][attr]
        except KeyError:
            raise ttp.TTPError(f"-ERR address not found: {cmd}") from None


@pytest.mark.asyncio
async def test_classify_probe_matrix():
    client = _FakeClient()
    design = await disc.discover_design(client)
    assert design == [
        {"tag": "ShieldLvl", "kind": "level", "channels": 2},
        {"tag": "Mixer1", "kind": "matrixmixer", "inputs": 6, "outputs": 2},
        {"tag": "AecInput1", "kind": "aecinput", "channels": 4},
        {"tag": "NvidiaNormPk", "kind": "meter", "channels": 2},
        {"tag": "NvidiaRawSig", "kind": "signalpresent", "channels": 2},
        {"tag": "VocalGate", "kind": "dynamics"},
        {"tag": "WiiMDuck", "kind": "dynamics"},
    ]
    assert client.timeouts == {disc.PROBE_TIMEOUT}
    coord.validate_design(design)


def test_merge_design_keeps_hand_edits():
    discovered = [
        {"tag": "NvidiaNormRms", "kind": "meter", "channels": 2},
        {"tag": "WiiMDuck", "kind": "dynamics"},
        {"tag": "New", "kind": "level", "channels": 1},
    ]
    existing = [
        {"tag": "NvidiaNormRms", "kind": "meter", "channels": 1, "rate_ms": 1000},
        {"tag": "WiiMDuck", "kind": "dynamics", "controls": ["threshold"]},
        {"tag": "Gone", "kind": "level", "channels": 2},
        {"kind": "presets", "presets": [{"id": 1}]},
    ]
    assert disc.merge_design(discovered, existing) == [
        {"tag": "NvidiaNormRms", "kind": "meter", "channels": 2, "rate_ms": 1000},
        {"tag": "WiiMDuck", "kind": "dynamics", "controls": ["threshold"]},
        {"tag": "New", "kind": "level", "channels": 1},
        {"kind": "presets", "presets": [{"id": 1}]},
    ]


# -- translations ---------------------------------------------------------------
def test_flow_descriptions_have_only_identifier_placeholders():
    """HA rejects a description whose {...} isn't an identifier (hassfest and
    MALFORMED_ARGUMENT at runtime), so JSON examples can't go in strings.json."""
    import json
    import re
    from pathlib import Path

    def strings(node):
        if isinstance(node, dict):
            for v in node.values():
                yield from strings(v)
        elif isinstance(node, str):
            yield node

    root = Path(__file__).parents[1] / "custom_components" / "tesira_forte"
    for name in ("strings.json", "translations/en.json"):
        data = json.loads((root / name).read_text(encoding="utf-8"))
        for text in strings(data):
            for ph in re.findall(r"\{([^}]*)\}", text):
                assert re.fullmatch(r"[A-Za-z_]\w*", ph), (name, ph)
