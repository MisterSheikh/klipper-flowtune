# Command reference

FlowTune registers the five Klipper commands listed below. The tables use the
names shown by FlowTune.

## FLOWTUNE_STATUS

`FLOWTUNE_STATUS` reports the current FlowTune state, the configured load-cell
object, the sample rate, calibration status, sensor errors, and sensor
overflows.

An idle Qidi Q2 reports a line that begins like this:

```text
FlowTune idle; load_cell_object=load_cell_probe; sample_rate=1280.0 SPS
```

## FLOWTUNE_SENSOR_CHECK

`FLOWTUNE_SENSOR_CHECK` records stationary load-cell data. It does not heat,
home, move, or extrude.

```text
FLOWTUNE_SENSOR_CHECK DURATION=2 SAVE=1
```

| Parameter | Default | Description |
| --- | ---: | --- |
| `DURATION` | `[flowtune]` `capture_duration` | Capture length in seconds. The shipped default is `2.0`. |
| `SAVE` | `1` | Set to `1` to save a CSV or `0` to discard it. |
| `LABEL` | none | Optional label stored with the capture. |

## FLOWTUNE_THERMAL_CHECK

`FLOWTUNE_THERMAL_CHECK` records load-cell drift while the hotend heats. It
restores the previous heater target when the check ends. It does not move or
extrude.

```text
FLOWTUNE_THERMAL_CHECK TARGET=210 STABLE_DURATION=30 TIMEOUT=180
```

| Parameter | Default | Description |
| --- | ---: | --- |
| `TARGET` | `210` | Hotend temperature in degrees Celsius. |
| `TOLERANCE` | `1.0` | Allowed temperature difference from `TARGET`, in degrees Celsius. |
| `STABLE_DURATION` | `30` | Time to remain near `TARGET`, in seconds. |
| `TIMEOUT` | `180` | Maximum total time, in seconds. |
| `SAVE` | `1` | Set to `1` to save a CSV or `0` to discard it. |
| `LABEL` | `thermal_check` | Label stored with the capture. |

The command requires the hotend target to be `0` when it starts.

## FLOWTUNE_PA

`FLOWTUNE_PA` runs the FlowPA calibration. `TARGET` is required on every run.

```text
FLOWTUNE_PA TARGET=210
```

| Parameter | Default | Description |
| --- | ---: | --- |
| `TARGET` | required | Hotend temperature in degrees Celsius. |
| `K_VALUES` | `0.034,0.038,0.042,0.046,0.050` | Strictly ascending pressure advance values. |
| `SLOW_FLOW` | `4` | Low volumetric flow in mm³/s. |
| `FAST_FLOW` | `12` | High volumetric flow in mm³/s. |
| `CONDITIONING_CYCLES` | `3` | Preparation cycles for each pressure advance value. |
| `CYCLES` | `3` | Measured cycles for each pressure advance value. |
| `AXIS` | `Y` | Carrier axis. Valid values are `X` and `Y`. |
| `TOLERANCE` | `1.0` | Allowed temperature difference when the hotend first reaches `TARGET`, in degrees Celsius. |
| `POST_TARGET_DWELL` | `20` | Wait after the hotend first reaches the target range, in seconds. |
| `PURGE_LENGTH` | `30` | Input filament used for the purge, in millimetres. |
| `PURGE_FLOW` | `12` | Purge flow in mm³/s. |
| `HEAT_TIMEOUT` | `240` | Maximum heat and wait time, in seconds. |
| `SLOW_TIME` | `1.0` | Low-flow part of each cycle, in seconds. |
| `FAST_TIME` | `0.35` | High-flow part of each cycle, in seconds. |
| `LEAD_TIME` | `2.0` | Low-flow preparation time before measurements, in seconds. |
| `SMOOTH_TIME` | `0.03` | Klipper pressure advance smooth time, in seconds. |
| `ACCEL` | `1000` | Toolhead acceleration during the test, in mm/s². |
| `WOBBLE` | `0.05` | Alternating carrier-axis movement, in millimetres. |
| `LABEL` | `flowpa` | Label stored with the result. |

FlowPA can report these result states:

| State | Meaning |
| --- | --- |
| `valid` | One boundary has enough cycle support. FlowPA reports a recommendation. |
| `no_boundary_within_range` | The tested `K_VALUES` do not contain a boundary. The console tells you whether to test lower or higher values when the data supports that direction. |
| `provisional` | A possible boundary lacks enough cycle support. |
| `ambiguous` | The data contains more than one possible boundary. |
| `invalid` | The capture or sensor data failed validation. |

Only `valid` produces a recommendation.

## FLOWTUNE_MAX_FLOW

`FLOWTUNE_MAX_FLOW` runs the automatic FlowMax search. `TARGET` is required on
every run.

```text
FLOWTUNE_MAX_FLOW TARGET=210
```

The table lists the built-in values. A command parameter changes its value for
that run.

| Parameter | Built-in default | Description |
| --- | ---: | --- |
| `TARGET` | required | Hotend temperature in degrees Celsius. |
| `START_FLOW` | `10` | First tested flow in mm³/s. |
| `MAX_TEST_FLOW` | `50` | Highest flow that FlowMax can test, in mm³/s. |
| `COARSE_STEP` | `1.0` | Flow increment during the first search, in mm³/s. |
| `FINE_STEP` | `0.1` | Flow increment during the refined search, in mm³/s. It must be smaller than `COARSE_STEP`. |
| `STEP_LENGTH` | `15` | Input filament used at each search flow, in millimetres. |
| `STABILIZE_TIME` | `20` | Wait after the hotend first reaches the target range, in seconds. |
| `TOLERANCE` | `1.0` | Allowed temperature difference when the hotend first reaches `TARGET`, in degrees Celsius. |
| `HEAT_TIMEOUT` | `240` | Maximum heat and wait time, in seconds. |
| `PURGE_LENGTH` | `30` | Input filament used for the purge, in millimetres. |
| `PURGE_FLOW` | `12` | Purge flow in mm³/s. |
| `COARSE_BACKOFF` | `COARSE_STEP` | Amount to reduce flow after the first detected boundary, in mm³/s. |
| `FINE_BACKOFF` | `0.3` | Backoff before the repeated refined search, in mm³/s. |
| `RECOMMENDATION_MARGIN` | `0.5` | Amount subtracted from the repeated boundary, in mm³/s. |
| `LABEL` | `max_flow` | Label stored with the capture. |

FlowMax can report these result states:

| State | Meaning |
| --- | --- |
| `valid` | The repeated boundary and capture passed validation. FlowMax reports a recommendation. |
| `no_limit_within_range` | FlowMax found no boundary through `MAX_TEST_FLOW`. The endpoint is not a measured limit. |
| `provisional` | A possible boundary did not repeat closely enough. |
| `ambiguous` | The search did not establish one lower boundary. |
| `invalid` | The capture, sensor data, worker, or motion record failed validation. |
| `rejected` | FlowMax rejected the run after an interruption or failed condition. |

Only `valid` produces a recommendation.

## Output files

FlowTune writes finalized files to `[flowtune]` `output_dir`.

| Command | Files |
| --- | --- |
| `FLOWTUNE_SENSOR_CHECK` | `flowtune-sensor-check-<timestamp>-<runid>.csv` when `SAVE=1` |
| `FLOWTUNE_THERMAL_CHECK` | `flowtune-thermal-check-<timestamp>-<runid>.csv` when `SAVE=1` |
| `FLOWTUNE_PA` | `flowtune-pa-<timestamp>-<runid>.csv` and `flowtune-pa-<timestamp>-<runid>.flowpa.json` |
| `FLOWTUNE_MAX_FLOW` | `flowtune-max-flow-capture-<timestamp>-<runid>.csv` |

A `.partial` suffix marks an interrupted recording. Do not treat that file as
a completed capture.
