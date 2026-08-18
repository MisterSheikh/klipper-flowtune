#!/usr/bin/env bash

set -eu

FLOWTUNE_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
FLOWTUNE_KLIPPER_DIR=${FLOWTUNE_KLIPPER_DIR:-/home/mks/klipper}
FLOWTUNE_KLIPPY_PYTHON=${FLOWTUNE_KLIPPY_PYTHON:-/home/mks/klippy-env/bin/python}
FLOWTUNE_CONFIG_DIR=${FLOWTUNE_CONFIG_DIR:-/home/mks/printer_data/config}
FLOWTUNE_EXTRAS_DIR="$FLOWTUNE_KLIPPER_DIR/klippy/extras"
FLOWTUNE_RESULTS_DIR="$FLOWTUNE_CONFIG_DIR/flowtune/results"
FLOWTUNE_CONFIG_FILE="$FLOWTUNE_CONFIG_DIR/flowtune.cfg"

FLOWTUNE_MODULES="
flowtune.py
flowtune_core.py
flowtune_capture.py
flowtune_e_drip.py
flowtune_max_flow.py
flowtune_max_flow_worker.py
flowtune_pa.py
flowtune_pa_command.py
flowtune_pa_worker.py
"

fail() {
    echo "FlowTune install failed: $*" >&2
    exit 1
}

echo "Installing FlowTune from $FLOWTUNE_ROOT"

[ -d "$FLOWTUNE_EXTRAS_DIR" ] || \
    fail "Klipper extras directory not found: $FLOWTUNE_EXTRAS_DIR"
[ -x "$FLOWTUNE_KLIPPY_PYTHON" ] || \
    fail "Klippy Python not found: $FLOWTUNE_KLIPPY_PYTHON"
[ -f "$FLOWTUNE_ROOT/config/flowtune-q2.cfg" ] || \
    fail "Q2 configuration template is missing"

for FLOWTUNE_MODULE in $FLOWTUNE_MODULES; do
    [ -f "$FLOWTUNE_ROOT/klippy/extras/$FLOWTUNE_MODULE" ] || \
        fail "required module is missing: $FLOWTUNE_MODULE"
done

if ! "$FLOWTUNE_KLIPPY_PYTHON" -c 'import numpy' >/dev/null 2>&1; then
    echo "NumPy is missing from Klippy's Python environment; installing 2.0.2..."
    "$FLOWTUNE_KLIPPY_PYTHON" -m pip install numpy==2.0.2 || \
        fail "NumPy installation failed"
fi

FLOWTUNE_NUMPY_VERSION=$(
    "$FLOWTUNE_KLIPPY_PYTHON" -c 'import numpy; print(numpy.__version__)'
)
echo "Klippy NumPy: $FLOWTUNE_NUMPY_VERSION"

for FLOWTUNE_MODULE in $FLOWTUNE_MODULES; do
    ln -sfn \
        "$FLOWTUNE_ROOT/klippy/extras/$FLOWTUNE_MODULE" \
        "$FLOWTUNE_EXTRAS_DIR/$FLOWTUNE_MODULE"
done
echo "Linked the required FlowTune runtime modules into $FLOWTUNE_EXTRAS_DIR"

mkdir -p "$FLOWTUNE_RESULTS_DIR"
echo "Results directory: $FLOWTUNE_RESULTS_DIR"

if [ -e "$FLOWTUNE_CONFIG_FILE" ]; then
    echo "Preserved existing configuration: $FLOWTUNE_CONFIG_FILE"
else
    cp "$FLOWTUNE_ROOT/config/flowtune-q2.cfg" "$FLOWTUNE_CONFIG_FILE"
    echo "Installed Q2 configuration: $FLOWTUNE_CONFIG_FILE"
fi

echo
echo "FlowTune files are installed. The installer did not edit printer.cfg"
echo "or restart Klipper. Add this line to printer.cfg if it is not present:"
echo
echo "  [include flowtune.cfg]"
echo
echo "Then restart Klipper and run these commands in the console:"
echo
echo "  FLOWTUNE_STATUS"
echo "  FLOWTUNE_SENSOR_CHECK DURATION=2 SAVE=1"
echo
echo "The sensor check must report a nonzero sample count, 0 errors,"
echo "0 overflows, and a saved capture path."
