# Biamp Tesira Forte Plus — Home Assistant integration

> Fork of [itskevinb/home-assistant-tesira-forte](https://github.com/itskevinb/home-assistant-tesira-forte)
> (v0.3.0). It keeps the `tesira_forte` domain and unique-ID scheme, so it is a
> drop-in replacement: existing config entries and entity IDs carry over. It adds
> Signal Present meters, dynamics blocks (Noise Gate / Ducker / Compressor),
> packed meter subscriptions with per-block rates, preset buttons, and
> re-discovery over the live session.

A native (config-flow) Home Assistant integration for **Biamp Tesira** DSPs
(built and tested on a **Tesira FORTE**). It holds one persistent
[Tesira Text Protocol](https://support.biamp.com/Tesira/Control/Tesira_Text_Protocol)
(TTP) session to the DSP on TCP port 23 and mirrors the blocks you name into HA
as `number`, `switch`, `sensor`, `binary_sensor` and `button` entities.

No broker, no add-on, no external bridge — it runs in-process and pushes state
via TTP `subscribe`, so edits made in Tesira software or by logic blocks show up
in HA too.

## One session, and only one

The Tesira's Telnet server is the bottleneck, not bandwidth. Opening a session
takes the unit ~2.5 s of option negotiation, concurrent opens time out, and a
handful of clients each opening a session every few seconds can lock TTP out
entirely (the unit reports no fault and recovers once the load stops).

So **everything goes over the integration's single persistent session**:
initial reads, subscriptions, the polling of attributes Tesira can't push,
writes, preset recalls and Re-discover. Don't run other pollers against the
same unit (`command_line`, `shell_command`, Node-RED, …) — expose what you need
through this integration instead.

## Install

**HACS → ⋮ → Custom repositories** → add `https://github.com/hellosamblack/home-assistant-tesira-forte-plus`
as an **Integration** → install → restart Home Assistant. If the upstream
repository is installed, remove it from HACS first (keep the config entry).

Or copy `custom_components/tesira_forte/` into your `config/custom_components/`.

Then **Settings → Devices & Services → Add Integration → Biamp Tesira** and enter
the DSP's control-network IP (port 23).

> TTP on the Tesira is **unauthenticated**. Keep the control port on a trusted VLAN.

## The design is auto-discovered

On setup the integration connects and reads the **running design** off the device
— `SESSION get aliases` lists every instance tag, and each block is then probed
(`get numInputs` / `numChannels` / `crosspointLevel` / `gain` …) to work out its
kind and size. The setup flow shows you the result as an editable JSON list so
you can **trim or tweak it before it's saved** (discovery errs toward including a
block; EQ / filter / generator / crossover blocks that expose nothing drivable
are left out automatically).

Afterwards, edit the list any time in **the integration's ⚙ → Configure**, or
tick **Re-discover from device** there to pull it again. Re-discover runs over the
integration's open session (it needs the entry loaded and connected) and keeps
your hand edits — `rate_ms`, `controls` and the `presets` entry.

A `tesira_forte:` block in `configuration.yaml` (see below) still works and, if
present, seeds an entry that has no design of its own.

The design is a **JSON list of blocks**, each `{"tag": "<instanceTag>", "kind":
"<kind>", …size}`:

| kind | size field(s) | entities created per unit |
|---|---|---|
| `aecinput` | `channels` | `gain` (number, 0–66 dB) + `phantomPower` (switch) |
| `input` | `channels` | same as `aecinput` (Mic/Line Input block) |
| `meter` | `channels` (+ `rate_ms`) | `level` (sensor, dB). One packed `levels` subscription per block, default 500 ms |
| `signalpresent` | `channels` (+ `rate_ms`) | per channel `present` (binary_sensor, `sound`, disabled by default), plus one **"&lt;tag&gt; present"** binary_sensor that is on when any channel is |
| `dynamics` | — (+ `controls`) | **"&lt;tag&gt; active"** switch (on = `bypass false`) + optional numbers: `threshold` (−80…0 dB), `releaseTime` (1…3000 ms), `duckingLevel` (−100…0 dB) |
| `level` | `channels` | `level` (number, −100…+12 dB) + `mute` (switch) |
| `mute` | `channels` | `mute` (switch) |
| `standardmixer` | `inputs`, `outputs` | input & output level + mute, `crosspoint` route (switch) |
| `matrixmixer` | `inputs`, `outputs` | input & output level + mute, per-crosspoint `crosspointLevel` (number) + `crosspointLevelState` (switch) |

And one optional, tag-less entry that turns device presets into buttons:

| kind | fields | entities |
|---|---|---|
| `presets` | `presets`: list of `{"id": <int>, "name": "<text>"}` | one **Recall …** button each: `DEVICE recallPreset <id>`, or `DEVICE recallPresetByName "<name>"` when only a name is given |

Plus, always: a **Fault** binary_sensor (`problem`), a **Fault detail** sensor,
and a **Firmware** sensor.

### Finding an instance tag by hand

Discovery names blocks for you, but if you're adding one manually: in **Tesira
software**, right-click a processing block → **Properties** — the **Instance
Tag** is what goes in `"tag"`. (Or select a block and read the Instance Tag
field in the ribbon.) The tag is what TTP addresses, e.g. `Mixer1 get
inputLevel 1`.

### Example

```json
[
  { "tag": "Mic1",   "kind": "input",        "channels": 2 },
  { "tag": "Program", "kind": "level",       "channels": 2 },
  { "tag": "AudioMeter1", "kind": "meter",   "channels": 2 },
  { "tag": "RmsMeter1", "kind": "meter",     "channels": 2, "rate_ms": 1000 },
  { "tag": "SignalPresent1", "kind": "signalpresent", "channels": 2 },
  { "tag": "VocalGate", "kind": "dynamics", "controls": ["threshold", "releaseTime"] },
  { "tag": "Matrix1", "kind": "matrixmixer", "inputs": 4, "outputs": 4 },
  { "kind": "presets", "presets": [{ "id": 1001, "name": "Default" }] }
]
```

### YAML (optional)

Used as the default for any config entry that has no design set in its options —
handy for a single-DSP install:

```yaml
tesira_forte:
  design:
    - { tag: Mic1, kind: input, channels: 2 }
    - { tag: Matrix1, kind: matrixmixer, inputs: 4, outputs: 4 }
```

## Push vs. poll

Verified on a FORTÉ AVB VI, firmware 5.7.2.6:

| Attribute | How it reaches HA |
|---|---|
| Level / mute / crosspoints / phantom | `subscribe`, pushed on change |
| Meter `levels` | one `subscribe levels` per block; every channel in one array, at `rate_ms` |
| Signal Present `present` | `subscribe`, pushed only on change |
| Dynamics `bypass`, `threshold`, `releaseTime`, `duckingLevel` | **not subscribable** — read on connect, every 30 s, and right after a write |
| AEC / Mic-Line input `gain` | not subscribable — polled the same way |

## Notes

- Keep `releaseTime` ≤ 3000 ms on a Noise Gate: the unit rejected a gate set to
  10000 ms / −70 dBu.
- After a preset recall, the polled attributes are re-read straight away.
- Mic gain on an AEC/Mic-Line input is a discrete 6 dB-step preamp; the number
  entity is clamped to that grid. It's also the one attribute Tesira won't let
  you `subscribe` to, so it's polled (30 s + immediately after a write).
- Levels use the Tesira −100…+12 dB fader range.
- The fault poll (15 s) doubles as the session keepalive; a stalled session
  reconnects with exponential backoff and re-subscribes everything.
- If your firmware rejects `subscribe` on an attribute this integration expects
  to be subscribable, please open an issue with the model + firmware version.

## License

MIT
