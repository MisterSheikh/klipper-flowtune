# Qidi Q2 compatibility and configuration

The Qidi Q2 is the only printer tested for this release. The tested setup uses
the
[mainline Klipper port](https://github.com/MisterSheikh/Qidi_Q2_Mainline_Klipper)
and reads the stock nozzle load cell through `[load_cell_probe]`.

FlowTune does not add CS1237 support to stock Qidi firmware. Install and test
the mainline Klipper port before you install FlowTune.

## Q2 configuration

The installer copies
[`config/flowtune-q2.cfg`](../config/flowtune-q2.cfg) to the Q2 configuration
directory on a new installation. Its active settings are:

```ini
[flowtune]
load_cell_object: load_cell_probe
output_dir: /home/mks/printer_data/config/flowtune/results
capture_duration: 2.0
minimum_sample_rate_ratio: 0.90
maximum_gap_intervals: 1.5
writer_queue_batches: 16
```

The existing `[load_cell_probe]` section configures and owns the CS1237.
FlowTune reads samples from that Klipper object. It does not configure the
CS1237 or its pins. It does not change probing behavior.

Do not add another `[load_cell_probe]` section or a separate CS1237
configuration for FlowTune.

Follow [Install on a Qidi Q2](installation.md#install-on-a-qidi-q2) to install
and test the extension.

## Q2 precautions

- Do not run input-shaper or accelerometer capture while FlowTune is active.
  The Q2 load cell and accelerometer share toolhead MCU resources.
- FlowPA moves the selected X or Y axis by `0.05 mm` by default. Follow the
  [FlowPA preparation steps](flowpa.md#prepare-the-printer) before running the
  test.
- FlowMax depends on private Klipper motion interfaces tested with the
  documented mainline port. It can reject an incompatible Klipper revision.
- FlowMax does not support flexible filaments. Do not use FlowMax with TPU or
  another flexible filament.

## Tested software

FlowTune was tested with the Q2 mainline setup based on upstream Klipper commit
`9c1ae230eaebd5ec4df76d5a87537e2f35defab0`. Klippy used Python `3.9.2` and
NumPy `2.0.2`. The tested load cell reported about `1280 SPS`.

A stock upstream Klipper checkout does not include the Q2 load-cell support
required by this setup. Other revisions may work, but this release does not
claim test coverage for them.
