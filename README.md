# FlowTune

FlowTune adds two load-cell-assisted calibration tools to Klipper:

- FlowPA recommends one pressure advance value.
- FlowMax recommends a conservative maximum volumetric flow.

FlowTune records and reports its results. It does not change a slicer profile
or the printer configuration. Check each recommendation with a printed test
before you use it in a production profile.

## Supported setup

FlowTune was tested on a Qidi Q2 with the documented mainline Klipper port and
the stock nozzle load cell. FlowPA completed an end-to-end calibration on that
setup. FlowMax results for PLA and ABS agreed with printed tests.

Other printers, load-cell configurations, hotends, and Klipper revisions have
not been tested.

FlowMax does not support flexible filaments. Do not use FlowMax with TPU or
another flexible filament.

## Install FlowTune on a Qidi Q2

The Q2 instructions require the documented mainline Klipper installation.
Stock Qidi firmware is not supported.

SSH into the printer, then run:

```bash
cd /home/mks
git clone https://github.com/MisterSheikh/klipper-flowtune.git flowtune
cd /home/mks/flowtune
./install.sh
```

Add this line to `printer.cfg`:

```ini
[include flowtune.cfg]
```

Restart Klipper. Run these commands in the Klipper console:

```text
FLOWTUNE_STATUS
FLOWTUNE_SENSOR_CHECK DURATION=2 SAVE=1
```

The status response must begin with `FlowTune idle`. The sensor check must
report a nonzero sample count, `0 errors`, `0 overflows`, and a `Capture:`
path.

Read the [installation guide](docs/installation.md) before you install FlowTune
on another printer.

## Run a calibration

Both calibration commands heat the hotend and extrude filament. Start them
only when no print is active and the hotend target is `0`.

Read the preparation steps before you run either command:

- [Calibrate pressure advance with FlowPA](docs/flowpa.md)
- [Calibrate maximum volumetric flow with FlowMax](docs/flowmax.md)

## What a recommendation tells you

A `valid` result means that the capture passed FlowTune's data-quality checks
and the measured boundary repeated. It does not confirm deposited-flow
accuracy or print quality. It also does not show that every model can print
reliably at the recommended value.

Run a printed calibration before you add the value to a production profile.

## Documentation

- [Install or update FlowTune](docs/installation.md)
- [Qidi Q2 compatibility and configuration](docs/q2-integration.md)
- [Check the load cell](docs/sensor-foundation.md)
- [Command reference](docs/command-reference.md)
- [Troubleshooting](docs/troubleshooting.md)

## License

Copyright (C) 2026 Ahmed Sheikh <ahmed.ali.sheikh1998@gmail.com>

FlowTune is licensed under the GNU General Public License, version 3 only.
The SPDX identifier is `GPL-3.0-only`. See [LICENSE](LICENSE).
