# FlowTune
#
# Copyright (C) 2026 Ahmed Sheikh <ahmed.ali.sheikh1998@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
# SPDX-License-Identifier: GPL-3.0-only

"""Memory-bounded NumPy analysis for a finalized FlowPA capture.

The analyzer deliberately reads the CSV twice.  The first pass discovers the
exact scored transition windows; the second validates the complete sample
timeline while retaining only those short windows.  This keeps the raw CSV
authoritative without materializing the complete capture on small printer
hosts.
"""

from __future__ import division

import csv
import json
import math
import os

import numpy as np

try:
    from . import flowtune_capture
except ImportError:
    import flowtune_capture


ANALYSIS_ID = "flowtune.flowpa"
ANALYSIS_VERSION = 1
GRID_STEP_S = 0.001
BASELINE_START_S = -0.200
BASELINE_END_S = -0.040
FALL_WINDOW_END_S = 0.150
DECISION_END_S = 0.050
SMOOTHING_SAMPLES = 5


def _finite(value, name):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("%s must be finite" % name)
    if not math.isfinite(result):
        raise ValueError("%s must be finite" % name)
    return result


def _read_header(capture_file):
    magic = capture_file.readline().rstrip("\r\n")
    if magic != flowtune_capture.FORMAT_MAGIC:
        raise ValueError("unsupported FlowTune capture format")
    metadata_line = capture_file.readline().rstrip("\r\n")
    prefix = flowtune_capture.METADATA_PREFIX
    if not metadata_line.startswith(prefix):
        raise ValueError("FlowTune capture metadata is missing")
    metadata = json.loads(metadata_line[len(prefix):])
    reader = csv.reader(capture_file)
    try:
        columns = next(reader)
    except StopIteration:
        raise ValueError("FlowTune capture columns are missing")
    if columns != flowtune_capture.COLUMNS:
        raise ValueError("unexpected FlowTune capture columns")
    return metadata, reader


def _first_pass(path):
    with open(path, "r", newline="") as capture_file:
        metadata, reader = _read_header(capture_file)
        paired = {}
        telemetry = []
        summaries = []
        for row in reader:
            if len(row) != len(flowtune_capture.COLUMNS):
                raise ValueError("malformed FlowTune capture row")
            record_type = row[0]
            if record_type == "event" and row[6] == "motion_leg_start":
                payload = json.loads(row[7] or "{}")
                sequence = str(payload.get("sequence", ""))
                phase = payload.get("phase")
                if (not sequence.startswith("pa_k_")
                        or phase not in ("fast", "slow")
                        or not payload.get("scored", False)):
                    continue
                key = (int(payload["k_index"]), int(payload["cycle"]))
                item = paired.setdefault(key, {
                    "k_index": key[0],
                    "cycle": key[1],
                    "pressure_advance": _finite(
                        payload["pressure_advance"], "pressure_advance"),
                })
                if phase in item:
                    raise ValueError("duplicate scored %s marker %r" %
                                     (phase, key))
                item[phase] = _finite(row[2], "%s print_time" % phase)
                if phase == "fast":
                    item["fast_duration_s"] = _finite(
                        payload.get("duration_s"), "fast duration")
            elif record_type == "telemetry":
                payload = json.loads(row[7] or "{}")
                telemetry.append({
                    "print_time": (None if row[2] == "" else
                                   _finite(row[2], "telemetry print_time")),
                    "temperature_c": payload.get("temperature_c"),
                    "target_c": payload.get("target_c"),
                    "power": payload.get("power"),
                })
            elif record_type == "summary":
                summaries.append(json.loads(row[7] or "{}"))
    if len(summaries) != 1:
        raise ValueError("capture must contain exactly one summary record")
    if not paired:
        raise ValueError("capture has no scored FlowPA transitions")
    transitions = []
    for key in sorted(paired):
        item = paired[key]
        if "fast" not in item or "slow" not in item:
            raise ValueError("scored cycle %r does not contain both edges" %
                             (key,))
        if item["slow"] <= item["fast"]:
            raise ValueError("scored cycle %r has reversed edge markers" %
                             (key,))
        transitions.append(item)
    k_order = []
    for item in transitions:
        value = item["pressure_advance"]
        if value not in k_order:
            k_order.append(value)
    if any(right <= left for left, right in zip(k_order, k_order[1:])):
        raise ValueError("production FlowPA K values must be ascending")
    return metadata, summaries[0], telemetry, transitions


def _sample_windows(transitions):
    windows = []
    for index, item in enumerate(transitions):
        windows.append({
            "transition_index": index,
            "start": item["fast"] + BASELINE_START_S,
            "end": item["slow"] + FALL_WINDOW_END_S,
            "times": [],
            "force": [],
        })
    windows.sort(key=lambda row: row["start"])
    for left, right in zip(windows, windows[1:]):
        if right["start"] <= left["end"]:
            raise ValueError("scored FlowPA analysis windows overlap")
    return windows


def _second_pass(path, metadata, transitions):
    windows = _sample_windows(transitions)
    current = 0
    sample_count = 0
    first_time = last_time = None
    previous_time = None
    maximum_gap = 0.0
    non_monotonic = 0
    malformed = 0
    saturation_count = 0
    source = metadata.get("source", {})
    sensor_range = source.get("sensor_range_counts")
    with open(path, "r", newline="") as capture_file:
        _unused_metadata, reader = _read_header(capture_file)
        for row in reader:
            if not row or row[0] != "sample":
                continue
            try:
                timestamp = _finite(row[2], "sample print_time")
                counts = int(row[4])
            except (ValueError, TypeError, OverflowError):
                malformed += 1
                continue
            sample_count += 1
            if first_time is None:
                first_time = timestamp
            if previous_time is not None:
                delta = timestamp - previous_time
                if delta <= 0.0:
                    non_monotonic += 1
                else:
                    maximum_gap = max(maximum_gap, delta)
            previous_time = timestamp
            last_time = timestamp
            if (isinstance(sensor_range, (list, tuple))
                    and len(sensor_range) == 2
                    and (counts <= int(sensor_range[0])
                         or counts >= int(sensor_range[1]))):
                saturation_count += 1
            while current < len(windows) and timestamp > windows[current]["end"]:
                current += 1
            if current >= len(windows):
                continue
            window = windows[current]
            if timestamp < window["start"]:
                continue
            if row[3] == "":
                raise ValueError("FlowPA analysis requires calibrated force")
            window["times"].append(timestamp)
            window["force"].append(_finite(row[3], "sample force_g"))
    expected_rate = _finite(source.get("configured_sample_rate_sps"),
                            "configured sample rate")
    duration = (None if first_time is None or last_time is None
                else last_time - first_time)
    measured_rate = (None if not duration or sample_count < 2 else
                     (sample_count - 1) / duration)
    validation = metadata.get("validation", {})
    minimum_ratio = float(validation.get("minimum_sample_rate_ratio", 0.90))
    maximum_gap_intervals = float(
        validation.get("maximum_gap_intervals", 1.5))
    failures = []
    if malformed:
        failures.append("malformed_samples")
    if sample_count < 2:
        failures.append("insufficient_samples")
    if non_monotonic:
        failures.append("non_monotonic_timestamps")
    if measured_rate is not None and measured_rate < expected_rate * minimum_ratio:
        failures.append("sample_rate_below_threshold")
    if maximum_gap > maximum_gap_intervals / expected_rate:
        failures.append("timestamp_gaps")
    if saturation_count:
        failures.append("saturated_samples")
    tolerance = 2.5 / expected_rate
    for window in windows:
        if len(window["times"]) < 8:
            failures.append("incomplete_analysis_window")
            continue
        if (window["times"][0] > window["start"] + tolerance
                or window["times"][-1] < window["end"] - tolerance):
            failures.append("incomplete_analysis_window")
    return windows, {
        "sample_count": sample_count,
        "retained_sample_count": sum(len(row["times"]) for row in windows),
        "start_print_time": first_time,
        "end_print_time": last_time,
        "duration_s": duration,
        "expected_sample_rate_sps": expected_rate,
        "measured_sample_rate_sps": measured_rate,
        "maximum_timestamp_gap_s": maximum_gap,
        "non_monotonic_timestamp_count": non_monotonic,
        "malformed_sample_count": malformed,
        "saturated_sample_count": saturation_count,
        "failure_reasons": list(dict.fromkeys(failures)),
    }


def _smooth(trace):
    kernel = np.ones(SMOOTHING_SAMPLES, dtype=float) / SMOOTHING_SAMPLES
    return np.convolve(trace, kernel, mode="same")


def _median_window(grid, trace, start, end):
    selected = (grid >= start) & (grid < end)
    if not np.any(selected):
        raise ValueError("analysis window contains no samples")
    return float(np.median(trace[selected]))


def _trace_metric(grid, trace, start=0.0, end=DECISION_END_S):
    selected = (grid >= start) & (grid <= end)
    if not np.any(selected):
        raise ValueError("decision window contains no samples")
    values = trace[selected]
    index = int(np.argmax(values))
    times = grid[selected]
    return {
        "peak_0_50ms_g": float(values[index]),
        "peak_time_ms": float(times[index] * 1000.0),
        "median_0_25ms_g": _median_window(grid, trace, 0.0, 0.025),
        "median_25_50ms_g": _median_window(grid, trace, 0.025, 0.050),
    }


def _extract_traces(transitions, windows):
    by_index = {row["transition_index"]: row for row in windows}
    extracted = []
    plateau_deltas = []
    for index, item in enumerate(transitions):
        window = by_index[index]
        times = np.asarray(window["times"], dtype=np.float64)
        force = np.asarray(window["force"], dtype=np.float64)
        baseline_selected = (
            (times >= item["fast"] + BASELINE_START_S)
            & (times < item["fast"] + BASELINE_END_S))
        if not np.any(baseline_selected):
            raise ValueError("FlowPA baseline window contains no samples")
        baseline = float(np.median(force[baseline_selected]))
        fast_duration = item["slow"] - item["fast"]
        rise_grid = np.arange(BASELINE_START_S,
                              fast_duration + GRID_STEP_S * 0.5,
                              GRID_STEP_S, dtype=np.float64)
        # Keep pre-transition samples in the fall trace so the five-sample
        # smoother has real context at t=0 instead of zero-padding the
        # decision boundary.
        fall_grid = np.arange(BASELINE_START_S,
                              FALL_WINDOW_END_S + GRID_STEP_S * 0.5,
                              GRID_STEP_S, dtype=np.float64)
        rise = np.interp(item["fast"] + rise_grid, times, force) - baseline
        fall = np.interp(item["slow"] + fall_grid, times, force) - baseline
        plateau_end = fast_duration - 0.040
        plateau_start = max(0.080, plateau_end - 0.100)
        if plateau_end <= plateau_start:
            raise ValueError("fast leg is too short for a plateau reference")
        plateau = _median_window(rise_grid, rise,
                                 plateau_start, plateau_end)
        plateau_deltas.append(plateau)
        extracted.append({
            "transition": item,
            "rise_grid": rise_grid,
            "fall_grid": fall_grid,
            "rise": rise,
            "fall": fall,
            "plateau_start_s": plateau_start,
            "plateau_end_s": plateau_end,
        })
    polarity = float(np.sign(np.median(
        np.asarray(plateau_deltas, dtype=np.float64))))
    if polarity == 0.0:
        raise ValueError("could not infer load-cell pressure polarity")
    for row in extracted:
        row["rise"] = polarity * row["rise"]
        row["fall"] = -polarity * row["fall"]
    return polarity, extracted


def _group_metrics(extracted):
    k_values = sorted(set(
        row["transition"]["pressure_advance"] for row in extracted))
    rows = []
    for k_value in k_values:
        selected = [row for row in extracted
                    if row["transition"]["pressure_advance"] == k_value]
        fall_raw = np.asarray([row["fall"] for row in selected])
        rise_raw = np.asarray([row["rise"] for row in selected])
        fall_ensemble = _smooth(np.mean(fall_raw, axis=0))
        rise_ensemble = _smooth(np.mean(rise_raw, axis=0))
        plateau_start = selected[0]["plateau_start_s"]
        plateau_end = selected[0]["plateau_end_s"]
        rise_plateau = _median_window(selected[0]["rise_grid"],
                                      rise_ensemble,
                                      plateau_start, plateau_end)
        rise_relative = rise_ensemble - rise_plateau
        fall_metrics = _trace_metric(selected[0]["fall_grid"], fall_ensemble)
        rise_metrics = _trace_metric(selected[0]["rise_grid"], rise_relative)
        fall_cycles = []
        rise_cycles = []
        for source, fall, rise in zip(selected, fall_raw, rise_raw):
            fall_metric = _trace_metric(
                source["fall_grid"], _smooth(fall))
            rise_smoothed = _smooth(rise)
            plateau = _median_window(source["rise_grid"], rise_smoothed,
                                     plateau_start, plateau_end)
            rise_metric = _trace_metric(
                source["rise_grid"], rise_smoothed - plateau)
            cycle = int(source["transition"]["cycle"])
            fall_metric["cycle"] = cycle
            rise_metric["cycle"] = cycle
            fall_cycles.append(fall_metric)
            rise_cycles.append(rise_metric)
        fall_values = [row["peak_0_50ms_g"] for row in fall_cycles]
        rise_values = [row["peak_0_50ms_g"] for row in rise_cycles]
        rows.append({
            "pressure_advance": float(k_value),
            "cycles": len(selected),
            "fall": {
                "peak_0_50ms_g": fall_metrics["peak_0_50ms_g"],
                "peak_time_ms": fall_metrics["peak_time_ms"],
                "median_0_25ms_g": fall_metrics["median_0_25ms_g"],
                "median_25_50ms_g": fall_metrics["median_25_50ms_g"],
                "cycle_metrics": fall_cycles,
                "cycle_minimum_g": float(min(fall_values)),
                "cycle_maximum_g": float(max(fall_values)),
                "cycle_stddev_g": float(np.std(fall_values)),
            },
            "rise": {
                "peak_0_50ms_above_plateau_g":
                    rise_metrics["peak_0_50ms_g"],
                "peak_time_ms": rise_metrics["peak_time_ms"],
                "cycle_metrics": rise_cycles,
                "cycle_minimum_g": float(min(rise_values)),
                "cycle_maximum_g": float(max(rise_values)),
                "cycle_stddev_g": float(np.std(rise_values)),
            },
        })
    return rows


def _edge_value(row, edge):
    if edge == "fall":
        return float(row["fall"]["peak_0_50ms_g"])
    return float(row["rise"]["peak_0_50ms_above_plateau_g"])


def _crossings(rows, edge, cycle=None):
    values = []
    for row in rows:
        if cycle is None:
            value = _edge_value(row, edge)
        else:
            key = ("peak_0_50ms_g" if edge == "fall"
                   else "peak_0_50ms_g")
            metrics = row[edge]["cycle_metrics"]
            match = next((item for item in metrics
                          if item["cycle"] == cycle), None)
            if match is None:
                return []
            value = float(match[key])
        values.append(value)
    found = []
    for left, right, y0, y1 in zip(rows, rows[1:], values, values[1:]):
        if y0 == 0.0:
            found.append({
                "bracket": [left["pressure_advance"]],
                "estimate": left["pressure_advance"],
            })
        elif y0 * y1 < 0.0:
            k0 = float(left["pressure_advance"])
            k1 = float(right["pressure_advance"])
            found.append({
                "bracket": [k0, k1],
                "estimate": float(k0 - y0 * (k1 - k0) / (y1 - y0)),
            })
    if values and values[-1] == 0.0:
        found.append({
            "bracket": [rows[-1]["pressure_advance"]],
            "estimate": rows[-1]["pressure_advance"],
        })
    return found


def _edge_summary(rows, edge):
    ensemble = _crossings(rows, edge)
    values = [_edge_value(row, edge) for row in rows]
    cycles = sorted(set.intersection(*[
        set(item["cycle"] for item in row[edge]["cycle_metrics"])
        for row in rows
    ]))
    by_cycle = []
    for cycle in cycles:
        found = _crossings(rows, edge, cycle=cycle)
        by_cycle.append({
            "cycle": cycle,
            "zero_crossing_count": len(found),
            "bracket": (found[0]["bracket"] if len(found) == 1 else None),
            "estimate": (found[0]["estimate"] if len(found) == 1 else None),
        })
    supported = [row for row in by_cycle if row["estimate"] is not None]
    estimates = [row["estimate"] for row in supported]
    required = len(cycles) // 2 + 1
    return {
        "metric_values_g": values,
        "nondecreasing": all(
            right >= left for left, right in zip(values, values[1:])),
        "zero_crossing_count": len(ensemble),
        "zero_crossing_bracket": (
            ensemble[0]["bracket"] if len(ensemble) == 1 else None),
        "zero_crossing_estimate": (
            ensemble[0]["estimate"] if len(ensemble) == 1 else None),
        "cycle_support": {
            "supported_cycles": len(supported),
            "total_cycles": len(cycles),
            "required_cycles": required,
            "majority_supports_crossing": len(supported) >= required,
            "minimum_estimate": (None if not estimates else min(estimates)),
            "maximum_estimate": (None if not estimates else max(estimates)),
            "observed_range": (None if not estimates else
                               max(estimates) - min(estimates)),
            "by_cycle": by_cycle,
        },
    }


def _thermal_summary(telemetry, start, end):
    selected = [row for row in telemetry
                if row["print_time"] is not None
                and start is not None and end is not None
                and start <= row["print_time"] <= end]
    temperatures = [float(row["temperature_c"]) for row in selected
                    if row["temperature_c"] is not None]
    powers = [float(row["power"]) for row in selected
              if row["power"] is not None]
    return {
        "telemetry_count": len(selected),
        "minimum_temperature_c": (
            None if not temperatures else min(temperatures)),
        "maximum_temperature_c": (
            None if not temperatures else max(temperatures)),
        "mean_temperature_c": (
            None if not temperatures else float(np.mean(temperatures))),
        "minimum_heater_power": None if not powers else min(powers),
        "maximum_heater_power": None if not powers else max(powers),
        "mean_heater_power": (
            None if not powers else float(np.mean(powers))),
    }


def _empty_edge_summary():
    return {
        "metric_values_g": [],
        "nondecreasing": False,
        "zero_crossing_count": 0,
        "zero_crossing_bracket": None,
        "zero_crossing_estimate": None,
        "cycle_support": {
            "supported_cycles": 0,
            "total_cycles": 0,
            "required_cycles": 0,
            "majority_supports_crossing": False,
            "minimum_estimate": None,
            "maximum_estimate": None,
            "observed_range": None,
            "by_cycle": [],
        },
    }


def analyze_capture(path):
    """Analyze one finalized production PA capture."""
    metadata, summary, telemetry, transitions = _first_pass(path)
    windows, acquisition = _second_pass(path, metadata, transitions)
    failure_reasons = list(acquisition["failure_reasons"])
    if summary.get("status") != "complete":
        failure_reasons.append("capture_not_complete")
    if int(summary.get("errors", 0) or 0):
        failure_reasons.append("sensor_errors")
    if int(summary.get("overflows", 0) or 0):
        failure_reasons.append("sensor_overflows")
    if summary.get("writer_error"):
        failure_reasons.append("writer_error")
    failure_reasons = list(dict.fromkeys(failure_reasons))

    # A truncated window cannot produce a trustworthy interpolated trace.
    # Preserve the acquisition diagnosis in a normal report instead of
    # turning an expected invalid hardware capture into an analyzer crash.
    if ("insufficient_samples" in failure_reasons
            or "incomplete_analysis_window" in failure_reasons):
        empty_fall = _empty_edge_summary()
        return {
            "analysis": ANALYSIS_ID,
            "version": ANALYSIS_VERSION,
            "capture": os.path.abspath(path),
            "capture_id": metadata.get("run", {}).get("id"),
            "state": "invalid",
            "failure_reasons": failure_reasons,
            "recommendation": {
                "pressure_advance": None,
                "unrounded_boundary": None,
                "bracket": None,
                "range_hint": None,
            },
            "condition": metadata.get("parameters", {}),
            "acquisition": acquisition,
            "thermal_during_motion": _thermal_summary(
                telemetry, acquisition["start_print_time"],
                acquisition["end_print_time"]),
            "pressure_response_sign_in_force_data": None,
            "scored_transition_count": len(transitions),
            "candidate_count": 0,
            "by_k": [],
            "fall": empty_fall,
            "rise_diagnostic": _empty_edge_summary(),
            "metric_definition": {
                "local_baseline": (
                    "median force 200 to 40 ms before fast start"),
                "smoothing_samples": SMOOTHING_SAMPLES,
                "fall_selector": (
                    "maximum normalized response 0 to 50 ms after "
                    "fast-to-slow"),
                "rise_diagnostic": (
                    "maximum above late fast plateau 0 to 50 ms after "
                    "slow-to-fast"),
            },
        }

    polarity, extracted = _extract_traces(transitions, windows)
    rows = _group_metrics(extracted)
    fall = _edge_summary(rows, "fall")
    rise = _edge_summary(rows, "rise")
    if failure_reasons:
        state = "invalid"
    elif fall["zero_crossing_count"] == 0:
        state = "no_boundary_within_range"
    elif fall["zero_crossing_count"] > 1:
        state = "ambiguous"
    elif not fall["cycle_support"]["majority_supports_crossing"]:
        state = "provisional"
    else:
        state = "valid"
    boundary = fall["zero_crossing_estimate"]
    range_hint = None
    if state == "no_boundary_within_range":
        values = fall["metric_values_g"]
        if values and all(value < 0.0 for value in values):
            range_hint = "test_higher_k_values"
        elif values and all(value > 0.0 for value in values):
            range_hint = "test_lower_k_values"
    recommendation = (None if state != "valid" or boundary is None else
                      float(round(boundary, 3)))
    result = {
        "analysis": ANALYSIS_ID,
        "version": ANALYSIS_VERSION,
        "capture": os.path.abspath(path),
        "capture_id": metadata.get("run", {}).get("id"),
        "state": state,
        "failure_reasons": failure_reasons,
        "recommendation": {
            "pressure_advance": recommendation,
            "unrounded_boundary": boundary,
            "bracket": fall["zero_crossing_bracket"],
            "range_hint": range_hint,
        },
        "condition": metadata.get("parameters", {}),
        "acquisition": acquisition,
        "thermal_during_motion": _thermal_summary(
            telemetry, acquisition["start_print_time"],
            acquisition["end_print_time"]),
        "pressure_response_sign_in_force_data": (
            "positive" if polarity > 0.0 else "negative"),
        "scored_transition_count": len(transitions),
        "candidate_count": len(rows),
        "by_k": rows,
        "fall": fall,
        "rise_diagnostic": rise,
        "metric_definition": {
            "local_baseline": "median force 200 to 40 ms before fast start",
            "smoothing_samples": SMOOTHING_SAMPLES,
            "fall_selector": "maximum normalized response 0 to 50 ms after fast-to-slow",
            "rise_diagnostic": "maximum above late fast plateau 0 to 50 ms after slow-to-fast",
        },
    }
    return result


def render_text(result):
    recommendation = result.get("recommendation", {})
    support = result.get("fall", {}).get("cycle_support", {})
    lines = ["FlowPA result: %s" % result.get("state", "invalid")]
    if recommendation.get("pressure_advance") is not None:
        lines.append("Recommended pressure advance: %.3f" %
                     recommendation["pressure_advance"])
        lines.append("Measured boundary: %.5f in bracket %s" % (
            recommendation["unrounded_boundary"],
            recommendation.get("bracket")))
    else:
        lines.append("No pressure-advance recommendation is available.")
        if recommendation.get("range_hint"):
            lines.append("Range hint: %s" % recommendation["range_hint"])
    lines.append("Cycle crossing support: %s/%s (required %s)" % (
        support.get("supported_cycles", 0), support.get("total_cycles", 0),
        support.get("required_cycles", 0)))
    if support.get("observed_range") is not None:
        lines.append("Observed cycle-boundary span: %.5f" %
                     support["observed_range"])
    lines.append("Rise edge is reported as a diagnostic and is not averaged.")
    return "\n".join(lines)


__all__ = ["ANALYSIS_ID", "ANALYSIS_VERSION", "analyze_capture",
           "render_text"]
