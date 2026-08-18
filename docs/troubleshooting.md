# Troubleshoot FlowTune

Read the Klipper console message first. Then find the first related error in
`klippy.log`. On the documented Qidi Q2, the log is:

```text
/home/mks/printer_data/logs/klippy.log
```

FlowTune does not report a recommendation when it cannot trust the captured
data or confirm that the test completed correctly.

## Klipper does not start or the commands are missing

1. Open the existing FlowTune checkout. On the Q2, use
   `/home/mks/flowtune`.
2. Run `./install.sh` again. On another printer, export the custom paths first
   as described in [Update another Klipper
   installation](installation.md#update-another-klipper-installation).
3. Confirm that `printer.cfg` includes `[include flowtune.cfg]` or contains a
   valid `[flowtune]` section.
4. Confirm that `[flowtune]` `output_dir` is absolute and writable by Klipper.
5. Restart Klipper.
6. Read the first FlowTune error in `klippy.log`.

## The installation sensor check fails

Run the required check again:

```text
FLOWTUNE_SENSOR_CHECK DURATION=2 SAVE=1
```

A passing result has a nonzero sample count, `0 errors`, `0 overflows`, and a
`Capture:` path.

If the command fails or misses one of those results, check each item:

- `load_cell_object` names the load-cell section used by the printer.
- No input-shaper, accelerometer, or other high-rate sensor task is active.
- The load cell reports no errors or overflows.
- `output_dir` exists and Klipper can write to it.

Fix the problem and repeat the sensor check before you run a calibration.

## FlowTune cannot find the load cell

Check `load_cell_object` in the `[flowtune]` section. On the documented Q2, use:

```ini
load_cell_object: load_cell_probe
```

Keep the printer's existing load-cell configuration. Confirm that the load
cell works without FlowTune before you continue.

## FlowPA reports a NumPy preflight failure

On the documented Q2, check NumPy with Klippy's Python executable:

```bash
/home/mks/klippy-env/bin/python -c "import numpy; print(numpy.__version__)"
```

If NumPy is missing, install the tested release:

```bash
/home/mks/klippy-env/bin/python -m pip install numpy==2.0.2
```

On another printer, use that installation's Klippy Python executable. Restart
Klipper after installation.

## FlowPA says that X or Y must be homed

Home the printer, then follow the
[FlowPA preparation steps](flowpa.md#prepare-the-printer). FlowPA uses Y unless
you set `AXIS=X`.

## A calibration says that the hotend target must be off

Stop the current print or heating task. Set the hotend target to `0`, then run
the calibration again. The nozzle does not need to cool first.

## FlowTune reports that another operation is active

Wait for the active FlowTune command to finish. If Klipper restarted during a
run, restart Klipper again and run `FLOWTUNE_STATUS`. Confirm that the response
begins with `FlowTune idle`.

## The hotend does not reach the target in time

Check the requested temperature, heater, and thermistor. FlowPA and FlowMax use
a default `HEAT_TIMEOUT` of 240 seconds. That time includes the wait after the
hotend first reaches the target range.

Increase `HEAT_TIMEOUT` only when the printer needs more than 240 seconds for a
normal heat cycle.

## FlowPA reports invalid

An `invalid` result means that the sensor data or completed recording failed
validation. Check:

- the first related error in `klippy.log`.
- the sensor error and overflow counts.
- other high-rate sensor tasks that ran during the test.
- whether the output directory contains a completed CSV instead of only a
  `.partial` file.

Fix the reported problem before you repeat the test.

## FlowPA produces no recommendation

If the result is `no_boundary_within_range`, follow the console hint and test
lower or higher `K_VALUES`.

For a `provisional` result, check the filament preparation and temperature.
Increase `CYCLES` only after you rule out a preparation problem.

For an `ambiguous` result, review the console and Klipper log. Check the test
preparation before you run it again.

FlowPA does not extrapolate beyond the tested range or choose between
conflicting boundaries.

## FlowMax reports an E-drip compatibility error

The error below means that the installed Klipper revision lacks a private
motion interface required by FlowMax:

```text
E-drip is unavailable; missing Klipper interfaces: ...
```

Do not run FlowMax on that revision. FlowPA may still work. Compare the
installation with [Qidi Q2 compatibility and
configuration](q2-integration.md).

If E-drip requires a finite or positive extruder acceleration, fix the
printer's extruder-acceleration configuration before you run FlowMax again.

## FlowMax produces no valid recommendation

For `no_limit_within_range`, FlowMax found no boundary before
`MAX_TEST_FLOW`. Do not use that endpoint as a measured limit. Raise it only
when the hardware and test setup can support a higher flow.

For `provisional` or `ambiguous`, check the filament path, preparation,
console, and Klipper log before you repeat the test.

For `invalid`, inspect the saved capture and the reasons listed after
`Evidence failures` in the console or `klippy.log`.

For `rejected`, fix the interruption before you repeat the test. If the
command aborts with an error, start with that error.

## Capture and log locations

FlowTune saves captures in `[flowtune]` `output_dir`. The Q2 configuration uses:

```text
Captures: /home/mks/printer_data/config/flowtune/results
Klipper log: /home/mks/printer_data/logs/klippy.log
```

On another printer, read `output_dir` from its `[flowtune]` section. A path
under the printer configuration directory is usually visible in Mainsail or
Fluidd.

FlowPA saves its JSON report beside the raw CSV. A `.partial` file is an
interrupted recording, not a completed capture.
