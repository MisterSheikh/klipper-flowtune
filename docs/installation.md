# Install or update FlowTune

Install FlowTune on the device that runs Klipper. The installer links the
FlowTune modules into the active Klipper checkout.

## Check the requirements

FlowTune requires:

- a working Klipper installation.
- an existing load-cell configuration that FlowTune can read.
- the Python executable used by Klippy.
- an absolute `[flowtune]` `output_dir` that exists and is writable before
  Klipper loads FlowTune. The installer creates the documented default
  directory.

The installer checks for NumPy in Klippy's Python environment. If NumPy is
missing, the installer installs the tested `2.0.2` release.

The tested Qidi Q2 setup uses the
[mainline Klipper port](https://github.com/MisterSheikh/Qidi_Q2_Mainline_Klipper)
and its existing `[load_cell_probe]`. Install and test that port before you add
FlowTune. These instructions do not support stock Qidi firmware.

## Install on a Qidi Q2

The Q2 procedure uses these paths:

```text
/home/mks/klipper
/home/mks/klippy-env
/home/mks/printer_data
```

### Connect to the Q2

In the command below, replace `<printer-ip>` with the Q2 IP address. Connect
from a computer on the same network:

```bash
ssh mks@<printer-ip>
```

Run the remaining Q2 commands in that SSH session.

### Download and install FlowTune

```bash
cd /home/mks
git clone https://github.com/MisterSheikh/klipper-flowtune.git flowtune
cd /home/mks/flowtune
./install.sh
```

The installer performs these actions:

- links the required runtime modules into `/home/mks/klipper/klippy/extras`.
- creates `/home/mks/printer_data/config/flowtune/results`.
- installs `config/flowtune-q2.cfg` as `flowtune.cfg` if that file does not
  exist.
- leaves an existing `flowtune.cfg` unchanged.

### Include the configuration

Add this line to `/home/mks/printer_data/config/printer.cfg` if it is not
already present:

```ini
[include flowtune.cfg]
```

Do not add another `[load_cell_probe]` section. FlowTune reads the load cell
through the section already used by the Q2.

### Restart and test FlowTune

Restart Klipper from Mainsail or Fluidd. Then run:

```text
FLOWTUNE_STATUS
```

Check the response for all of these values:

- `FlowTune idle`
- `load_cell_object=load_cell_probe`
- a sample rate near `1280.0 SPS`
- no load-cell compatibility error

Run the required sensor check. It reads the stationary sensor and does not
heat, home, or move the printer:

```text
FLOWTUNE_SENSOR_CHECK DURATION=2 SAVE=1
```

The check passes when it reports all of these results:

- `FlowTune sensor capture complete`
- a nonzero sample count
- `0 errors`
- `0 overflows`
- a `Capture:` path

If either command fails, stop here and use the
[troubleshooting guide](troubleshooting.md).

## Update a Qidi Q2 installation

Connect to the Q2 over SSH. Run:

```bash
cd /home/mks/flowtune
git pull --ff-only
./install.sh
```

The installer refreshes the module links and keeps the existing
`/home/mks/printer_data/config/flowtune.cfg`.

Restart Klipper. Run `FLOWTUNE_STATUS` and confirm that the response begins
with `FlowTune idle`.

## Install on another Klipper printer

Other printers have not been tested. The generic procedure requires you to
identify the Klipper paths and select an existing load-cell configuration.

### Set the installation paths

Find these paths on the printer:

- the Klipper checkout that contains `klippy/extras`.
- the Python executable used by Klippy.
- the configuration directory that contains `printer.cfg`.

Export those paths in the shell where you will run the installer:

```bash
export FLOWTUNE_KLIPPER_DIR=/actual/path/to/klipper
export FLOWTUNE_KLIPPY_PYTHON=/actual/path/to/klippy-env/bin/python
export FLOWTUNE_CONFIG_DIR=/actual/path/to/printer_data/config
```

### Download FlowTune

Choose a permanent directory for the checkout. The installed module links
point to this directory, so do not delete it after installation.

```bash
cd /actual/path/for/source-checkouts
git clone https://github.com/MisterSheikh/klipper-flowtune.git
cd klipper-flowtune
```

### Create the generic configuration

If `flowtune.cfg` does not exist, copy the generic template:

```bash
cp config/flowtune.cfg "$FLOWTUNE_CONFIG_DIR/flowtune.cfg"
```

If the file already exists, keep it and add or update its `[flowtune]`
section.

Edit `$FLOWTUNE_CONFIG_DIR/flowtune.cfg`:

1. Set `load_cell_object` to the name of the existing load-cell configuration
   section. Omit the brackets.
2. Set `output_dir` to an absolute directory that Klipper can write to.

The output directory is not Q2-specific. A directory under
`printer_data/config` is useful because Mainsail and Fluidd can display its
files.

The installer creates `$FLOWTUNE_CONFIG_DIR/flowtune/results`. If you set a
different `output_dir`, create it before you restart Klipper.

### Run the installer

From the FlowTune checkout, run:

```bash
./install.sh
```

The exported variables replace the installer's Q2 path defaults. The installer
keeps the `flowtune.cfg` that you created.

### Include and test the configuration

Add this line to `printer.cfg` if it is not already present:

```ini
[include flowtune.cfg]
```

Restart Klipper. Run:

```text
FLOWTUNE_STATUS
FLOWTUNE_SENSOR_CHECK DURATION=2 SAVE=1
```

The sensor check reads the stationary sensor. It does not heat, home, or move
the printer.

The status response must begin with `FlowTune idle` and show the selected
`load_cell_object`. The sensor check must report a nonzero sample count,
`0 errors`, `0 overflows`, and a `Capture:` path.

## Update another Klipper installation

Connect to the printer. Export the same three paths used for installation.
Run:

```bash
cd /actual/path/to/klipper-flowtune
export FLOWTUNE_KLIPPER_DIR=/actual/path/to/klipper
export FLOWTUNE_KLIPPY_PYTHON=/actual/path/to/klippy-env/bin/python
export FLOWTUNE_CONFIG_DIR=/actual/path/to/printer_data/config
git pull --ff-only
./install.sh
```

Restart Klipper and run `FLOWTUNE_STATUS`.

## Continue after installation

After both checks pass, choose a calibration guide:

- [Calibrate pressure advance with FlowPA](flowpa.md)
- [Calibrate maximum volumetric flow with FlowMax](flowmax.md)

Use the [command reference](command-reference.md) to look up parameters and
saved file names.
