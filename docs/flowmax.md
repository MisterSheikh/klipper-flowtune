# Calibrate maximum volumetric flow with FlowMax

FlowMax recommends a conservative maximum volumetric flow for the tested
filament, nozzle, hotend, extruder, and temperature. It raises the extrusion
flow while the load cell records the force response.

FlowMax looks for a repeatable force release, then subtracts a safety margin
from that boundary. It cannot measure actual filament output or inspect a
printed surface. Confirm its recommendation with a printed maximum-flow test.

FlowMax never changes a slicer limit.

## Know the tested limits

FlowMax was tested on the documented Qidi Q2 setup. Results for the tested PLA
and ABS agreed with printed tests. Other printers and load-cell configurations
have not been tested.

FlowMax does not support flexible filaments. Do not use FlowMax with TPU or
another flexible filament.

## Prepare the printer

1. Load the filament that you want to test.
2. Confirm that no print is active.
3. Set the hotend target to `0` to turn off the heater. The nozzle does not
   need to be cold.
4. Home the printer.
5. Position the nozzle near the center of the bed.
6. Lower the bed to a Z position of 160 mm or greater. This leaves enough
   space below the nozzle for extrusion.
7. Make sure that extruded filament cannot collect around the nozzle or
   printer.
8. Turn off the part-cooling fan if its airflow can disturb the nozzle or
   extruded filament.
9. Stay near the printer until the test ends.

FlowMax does not move X, Y, or Z after the command starts.

The default search can consume hundreds of millimetres of input filament if it
reaches the 50 mm³/s endpoint.

## Run the test

Replace `210` with the test temperature for your filament:

```text
FLOWTUNE_MAX_FLOW TARGET=210
```

The short command is the recommended starting point. Review these parameters
when you change the test:

- `TARGET` sets the temperature for the filament and is required.
- `START_FLOW` sets the first flow in the search.
- `MAX_TEST_FLOW` sets the highest flow that FlowMax can test.
- `RECOMMENDATION_MARGIN` sets the distance below the repeated boundary.

The default test performs these actions:

1. Heats the hotend until it first enters the target range of plus or minus
   1 degree Celsius.
2. Waits 20 seconds.
3. Sets pressure advance to `0` for the test.
4. Purges 30 mm of input filament at 12 mm³/s.
5. Raises the flow from 10 mm³/s toward a maximum of 50 mm³/s.
6. Reduces the flow and repeats the search near the first detected boundary.
7. Subtracts 0.5 mm³/s from the repeated boundary.
8. Restores the previous pressure advance and heater target.

FlowMax sends extrusion in short stages. After it confirms the expected
failure signal, it stops adding stages to the current search.

## Check the result

A valid result looks similar to this:

```text
FlowTune maximum-flow test complete: estimated failure boundary 24.3 mm^3/s;
recommended maximum 23.8 mm^3/s.
Raw capture: /path/to/flowtune-max-flow-capture-<timestamp>-<runid>.csv
```

Use the `recommended maximum`, not the failure boundary.

Only a `valid` result includes a recommendation. For another result:

- If FlowMax reports `no_limit_within_range`, do not use `MAX_TEST_FLOW` as a
  measured limit. Raise the endpoint only when the printer and filament path
  can support another test.
- If FlowMax reports `provisional` or `ambiguous`, check the filament path,
  preparation, console, and Klipper log before you repeat the test.
- If FlowMax reports `invalid` or `rejected`, see
  [FlowMax produces no valid
  recommendation](troubleshooting.md#flowmax-produces-no-valid-recommendation).
  If the command aborts with an error, start with that error.

## Use the recommendation

Set the `recommended maximum` as the maximum volumetric speed in the slicer
profile for the filament you tested. A recommendation of `23.8 mm³/s` means a
profile limit of `23.8 mm³/s`.

## Validate the recommendation

Run a printed maximum-flow calibration in your slicer to confirm the
recommendation. Use the printed result to choose the final maximum volumetric
speed for the filament profile. The free-air result does not prove that every
model can print cleanly at the same flow.

## Adjusting test parameters

Use the
[FLOWTUNE_MAX_FLOW command reference](command-reference.md#flowtune_max_flow)
to change the search range, increments, filament use, timing, or margin.

For example, this command uses a lower endpoint and a larger margin:

```text
FLOWTUNE_MAX_FLOW TARGET=215 START_FLOW=12 MAX_TEST_FLOW=35 \
  RECOMMENDATION_MARGIN=0.7
```

Raise `MAX_TEST_FLOW` only when the hotend, extruder, filament path, clearance,
and configured extrusion limits support it. Reaching the endpoint does not
produce a recommendation.

## Output files

FlowMax writes the raw capture to `[flowtune]` `output_dir`:

```text
flowtune-max-flow-capture-<timestamp>-<runid>.csv
```

The Klipper console prints the path when the test ends.
