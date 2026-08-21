# Calibrate pressure advance with FlowPA

FlowPA recommends one pressure advance value for the tested filament, nozzle,
hotend, temperature, and flow conditions. It measures the load-cell response
during a free-air extrusion test.

Acceleration can affect the result. How long the filament spends heating and
melting in the hotend can also affect the result. The default 30 mm purge
reduces variation from filament that was already hot before the test.

The result is a starting point. FlowPA cannot judge the surface quality of a
print, so confirm the value with a printed pressure advance test. FlowPA never
applies or saves the recommendation as a printer setting.

## Prepare the printer

1. Load the filament that you want to calibrate.
2. Confirm that no print is active.
3. Set the hotend target to `0` to turn off the heater. The nozzle does not
   need to be cold.
4. Home the printer.
5. Position the nozzle near the center of the bed.
6. Lower the bed to a Z position of 160 mm or greater. This leaves enough
   space below the nozzle for extrusion.
7. Turn off the part-cooling fan if its airflow can disturb the nozzle or
   extruded filament.
8. Stay near the printer until the test ends.

FlowPA does not move Z or choose a safe toolhead position.

## Run the test

Replace `210` with the test temperature for your filament:

```text
FLOWTUNE_PA TARGET=210
```

The short command is the recommended starting point. Review these parameters
when you change the test:

- `TARGET` sets the temperature for the filament and is required.
- `PA_START` sets the first pressure advance value.
- `PA_END` sets the last pressure advance value and includes it in the test.
- `PA_STEP` sets the increment between pressure advance values.
- `CYCLES` sets the number of measured cycles for each value.
- `AXIS` selects the carrier axis. The default is Y.

The default test performs these actions:

1. Heats the hotend until it first enters the target range of plus or minus
   1 degree Celsius.
2. Waits 20 seconds.
3. Purges 30 mm of input filament at 12 mm³/s.
4. Tests pressure advance values `0.034`, `0.038`, `0.042`, `0.046`, and
   `0.050`.
5. Runs three preparation cycles and three measured cycles for each value.
6. Restores the previous pressure advance, acceleration, and heater target.

The default test uses about 150 mm of input filament.

## Check the result

A valid result looks similar to this:

```text
FlowTune FlowPA result: valid
Recommended PA: 0.041 (boundary 0.04145, bracket [0.038, 0.042])
Cycle support: 3/3; observed boundary span: 0.00084
Report: /path/to/flowtune-pa-<timestamp>-<runid>.flowpa.json
```

Use the rounded value after `Recommended PA`. The `boundary` is the calculated
transition point. The `bracket` contains the two adjacent values around that
point.

`Cycle support` shows how many measured cycles agreed. `observed boundary
span` shows the range of their boundary estimates. A smaller span means the
cycles agreed more closely.

Only a `valid` result includes a recommendation. For another result:

- If FlowPA reports `no_boundary_within_range`, follow the console hint and
  test a lower or higher PA range.
- If FlowPA reports `provisional`, check the filament and temperature. Increase
  `CYCLES` only after you rule out a preparation problem.
- If FlowPA reports `ambiguous`, check the preparation and Klipper log before
  you repeat the test.
- If FlowPA reports `invalid`, see
  [FlowPA reports invalid](troubleshooting.md#flowpa-reports-invalid).
- If the command stops with an error, start with that error. Correct the
  interruption before running the test again.

FlowPA does not guess between conflicting boundaries or extrapolate beyond the
tested range.

## Use the recommendation

Enter the recommended value in the pressure advance field of the profile for
the filament you tested.

## Validate the recommendation

Run a printed pressure advance calibration in your slicer to confirm the
recommendation. Use the printed result to choose the final value for the
filament profile.

## Adjusting test parameters

Use the [FLOWTUNE_PA command reference](command-reference.md#flowtune_pa) to
change the pressure advance range, cycle count, flow, timing, or carrier axis.

For example, this command tests a wider range with five measured cycles:

```text
FLOWTUNE_PA TARGET=210 PA_START=0.030 PA_END=0.055 PA_STEP=0.005 CYCLES=5
```

## Output files

FlowPA writes two files to `[flowtune]` `output_dir`:

- `flowtune-pa-<timestamp>-<runid>.csv` contains the raw capture.
- `flowtune-pa-<timestamp>-<runid>.flowpa.json` contains the calculated result.

The Klipper console prints the JSON report path when analysis completes.
