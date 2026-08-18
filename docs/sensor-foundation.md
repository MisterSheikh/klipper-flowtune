# Check the load cell

Run a sensor check after installation. This check confirms that FlowTune can
read and save data from the configured load cell.

FlowPA and FlowMax validate the data from each calibration. You do not need to
run a separate sensor check before every calibration.

## Check FlowTune status

Run:

```text
FLOWTUNE_STATUS
```

Confirm that the response contains:

- `FlowTune idle`
- the expected `load_cell_object`
- the expected sample rate
- no load-cell compatibility error

On the tested Qidi Q2, `load_cell_object` is `load_cell_probe` and the sample
rate is about `1280 SPS`.

If FlowTune cannot read the selected load cell, use the
[troubleshooting guide](troubleshooting.md#flowtune-cannot-find-the-load-cell).

## Run the installation sensor check

Keep the printer idle, then run:

```text
FLOWTUNE_SENSOR_CHECK DURATION=2 SAVE=1
```

The command records two seconds of stationary load-cell data. It does not
heat, home, move, or extrude.

The saved CSV contains the load-cell samples and the final sensor error and
overflow counts.

The check passes when FlowTune reports:

- `FlowTune sensor capture complete`
- a nonzero sample count
- `0 errors`
- `0 overflows`
- a `Capture:` path

Treat any missing condition as a failed installation check. Fix the problem
before you run FlowPA or FlowMax. See
[The installation sensor check fails](troubleshooting.md#the-installation-sensor-check-fails).

Run this check again when you investigate:

- intermittent sensor errors.
- sample-timing interference from another task.
- an `invalid` FlowPA or FlowMax result.

## Check thermal drift

Use the thermal check only when the load-cell force reading drifts as the
hotend heats.

Set the hotend target to `0`. Run:

```text
FLOWTUNE_THERMAL_CHECK TARGET=210 STABLE_DURATION=30 TIMEOUT=180
```

Replace `210` with the temperature you want to test. FlowTune records the load
cell before and during heating. It waits near the target for 30 seconds, then
restores the previous heater target. The command does not move or extrude.

This diagnostic does not copy the timing used by FlowPA or FlowMax. Those
commands use a fixed 20-second wait after the hotend reaches its target range.

## Find the saved data

Sensor and thermal checks write CSV files to `[flowtune]` `output_dir`. A
`.partial` file belongs to an interrupted recording. Do not use it as a
completed capture.

See the [command reference](command-reference.md) for optional parameters and
file names.
