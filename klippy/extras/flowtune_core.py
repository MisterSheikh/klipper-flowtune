# FlowTune
#
# Copyright (C) 2026 Ahmed Sheikh <ahmed.ali.sheikh1998@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
# SPDX-License-Identifier: GPL-3.0-only

"""Pure-Python capture validation and serialization helpers for FlowTune.

This module deliberately has no Klipper imports so recorded traces can be
replayed and tested away from a printer.
"""

from __future__ import division

import copy
import math


SCHEMA_ID = "flowtune.capture"
SCHEMA_VERSION = 1
MAX_TEST_FLOW_LIMIT_MM3_S = 500.0

# Q2 PLA+ reference candidate used by the current balanced FlowPA confirmation.
# These remain command-overridable; they define a recorded reference condition,
# not universal filament constants.
FLOWPA_REFERENCE_PRESSURE_ADVANCES = (0.034, 0.038, 0.042, 0.046, 0.050)
FLOWPA_REFERENCE_SLOW_FLOW = 4.0
FLOWPA_REFERENCE_FAST_FLOW = 12.0
FLOWPA_REFERENCE_SLOW_TIME = 1.0
FLOWPA_REFERENCE_FAST_TIME = 0.35
FLOWPA_REFERENCE_LEAD_TIME = 2.0
FLOWPA_REFERENCE_CONDITIONING_CYCLES = 3
FLOWPA_REFERENCE_SCORED_CYCLES = 3
FLOWPA_REFERENCE_PURGE_FILAMENT = 30.0
FLOWPA_REFERENCE_PURGE_FLOW = 12.0


def plan_pa_waveform(filament_diameter=1.75, slow_flow=2.0,
                     fast_flow=14.0, slow_time=1.0, fast_time=0.25,
                     cycles=3, warmup_time=4.0, control_cycles=4,
                     wobble=0.05, conditioning_cycles=3):
    """Build the bounded movement-control and fixed-K extrusion legs.

    Flow values are volumetric (mm^3/s); returned filament distances are
    linear input-filament millimetres.  Axis offsets alternate between the
    starting position and ``start + wobble``.  The control sequence returns
    to its start, while the extrusion sequence finishes at the offset after
    its final slow leg so the last fast-to-slow response has a full plateau.
    """
    values = {
        "filament_diameter": filament_diameter,
        "slow_flow": slow_flow,
        "fast_flow": fast_flow,
        "slow_time": slow_time,
        "fast_time": fast_time,
        "warmup_time": warmup_time,
        "wobble": wobble,
    }
    for name, value in values.items():
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("%s must be a finite positive number" % name)
        if not math.isfinite(value) or value <= 0.:
            raise ValueError("%s must be a finite positive number" % name)
        values[name] = value
    try:
        cycles = int(cycles)
        control_cycles = int(control_cycles)
        conditioning_cycles = int(conditioning_cycles)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("cycle counts must be integers")
    if cycles < 1:
        raise ValueError("cycles must be at least one")
    if control_cycles < 0:
        raise ValueError("control_cycles must be at least zero")
    if not 0 <= conditioning_cycles <= 20:
        raise ValueError("conditioning_cycles must be between zero and twenty")
    if values["fast_flow"] <= values["slow_flow"]:
        raise ValueError("fast_flow must be greater than slow_flow")

    filament_area = math.pi * (values["filament_diameter"] * 0.5) ** 2
    slow_feed = values["slow_flow"] / filament_area
    fast_feed = values["fast_flow"] / filament_area
    slow_filament = slow_feed * values["slow_time"]
    fast_filament = fast_feed * values["fast_time"]
    warmup_filament = slow_feed * values["warmup_time"]

    control_legs = []
    for cycle in range(1, control_cycles + 1):
        control_legs.extend([
            {
                "kind": "control",
                "phase": "slow",
                "cycle": cycle,
                "duration_s": values["slow_time"],
                "axis_offset_mm": values["wobble"],
                "filament_mm": 0.0,
                "volumetric_flow_mm3_s": 0.0,
                "transition_before": None,
            },
            {
                "kind": "control",
                "phase": "fast",
                "cycle": cycle,
                "duration_s": values["fast_time"],
                "axis_offset_mm": 0.0,
                "filament_mm": 0.0,
                "volumetric_flow_mm3_s": 0.0,
                "transition_before": None,
            },
        ])

    extrusion_legs = [{
        "kind": "extrusion",
        "phase": "warmup_slow",
        "cycle": 0,
        "duration_s": values["warmup_time"],
        "axis_offset_mm": values["wobble"],
        "filament_mm": warmup_filament,
        "volumetric_flow_mm3_s": values["slow_flow"],
        "transition_before": None,
        "scored": False,
    }]
    current_offset = values["wobble"]
    for cycle in range(1, conditioning_cycles + 1):
        current_offset = (values["wobble"]
                          if current_offset == 0.0 else 0.0)
        extrusion_legs.append({
            "kind": "extrusion",
            "phase": "conditioning_fast",
            "cycle": cycle,
            "duration_s": values["fast_time"],
            "axis_offset_mm": current_offset,
            "filament_mm": fast_filament,
            "volumetric_flow_mm3_s": values["fast_flow"],
            "transition_before": "slow_to_fast",
            "scored": False,
        })
        current_offset = (values["wobble"]
                          if current_offset == 0.0 else 0.0)
        extrusion_legs.append({
            "kind": "extrusion",
            "phase": "conditioning_slow",
            "cycle": cycle,
            "duration_s": values["slow_time"],
            "axis_offset_mm": current_offset,
            "filament_mm": slow_filament,
            "volumetric_flow_mm3_s": values["slow_flow"],
            "transition_before": "fast_to_slow",
            "scored": False,
        })
    for cycle in range(1, cycles + 1):
        current_offset = (values["wobble"]
                          if current_offset == 0.0 else 0.0)
        extrusion_legs.extend([
            {
                "kind": "extrusion",
                "phase": "fast",
                "cycle": cycle,
                "duration_s": values["fast_time"],
                "axis_offset_mm": current_offset,
                "filament_mm": fast_filament,
                "volumetric_flow_mm3_s": values["fast_flow"],
                "transition_before": "slow_to_fast",
                "scored": True,
            },
            {
                "kind": "extrusion",
                "phase": "slow",
                "cycle": cycle,
                "duration_s": values["slow_time"],
                "axis_offset_mm": (values["wobble"]
                                   if current_offset == 0.0 else 0.0),
                "filament_mm": slow_filament,
                "volumetric_flow_mm3_s": values["slow_flow"],
                "transition_before": "fast_to_slow",
                "scored": True,
            },
        ])
        current_offset = extrusion_legs[-1]["axis_offset_mm"]

    total_filament = sum(
        leg["filament_mm"] for leg in extrusion_legs)
    maximum_leg_filament = max(
        leg["filament_mm"] for leg in extrusion_legs)
    return {
        "filament_diameter_mm": values["filament_diameter"],
        "filament_area_mm2": filament_area,
        "slow_flow_mm3_s": values["slow_flow"],
        "fast_flow_mm3_s": values["fast_flow"],
        "slow_feed_mm_s": slow_feed,
        "fast_feed_mm_s": fast_feed,
        "slow_time_s": values["slow_time"],
        "fast_time_s": values["fast_time"],
        "warmup_time_s": values["warmup_time"],
        "cycles": cycles,
        "conditioning_cycles": conditioning_cycles,
        "control_cycles": control_cycles,
        "wobble_mm": values["wobble"],
        "control_legs": control_legs,
        "extrusion_legs": extrusion_legs,
        "total_filament_mm": total_filament,
        "maximum_leg_filament_mm": maximum_leg_filament,
        "maximum_extrude_ratio": maximum_leg_filament / values["wobble"],
    }


def parse_pressure_advance_values(value):
    """Parse and validate a comma-separated PA sweep."""
    if isinstance(value, str):
        candidates = [item.strip() for item in value.split(",")]
        if any(not item for item in candidates):
            raise ValueError("K_VALUES must be comma-separated numbers")
    else:
        try:
            candidates = list(value)
        except TypeError:
            raise ValueError("K_VALUES must be comma-separated numbers")
    if len(candidates) < 2:
        raise ValueError("K_VALUES must contain at least two values")
    if len(candidates) > 12:
        raise ValueError("K_VALUES may contain at most twelve values")
    values = []
    for candidate in candidates:
        try:
            candidate = float(candidate)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("K_VALUES must be comma-separated numbers")
        if not math.isfinite(candidate) or not 0. <= candidate <= 1.:
            raise ValueError("each K value must be between 0 and 1")
        if candidate in values:
            raise ValueError("K_VALUES must not contain duplicates")
        values.append(candidate)
    return values


def parse_flow_values(value, name):
    """Parse exactly three unique positive volumetric-flow values."""
    if isinstance(value, str):
        candidates = [item.strip() for item in value.split(",")]
        if any(not item for item in candidates):
            raise ValueError("%s must be three comma-separated numbers" % name)
    else:
        try:
            candidates = list(value)
        except TypeError:
            raise ValueError(
                "%s must be three comma-separated numbers" % name)
    if len(candidates) != 3:
        raise ValueError("%s must contain exactly three values" % name)
    values = []
    for candidate in candidates:
        try:
            candidate = float(candidate)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(
                "%s must be three comma-separated numbers" % name)
        if not math.isfinite(candidate) or candidate <= 0.:
            raise ValueError("each %s value must be positive" % name)
        if candidate in values:
            raise ValueError("%s must not contain duplicates" % name)
        values.append(candidate)
    return sorted(values)


def _matrix_flow_pair_order(low_flows, high_flows):
    """Return a time-balanced traversal of a three-by-three flow grid."""
    low0, low1, low2 = low_flows
    high0, high1, high2 = high_flows
    return [
        (low1, high1), (low0, high2), (low2, high0),
        (low0, high0), (low2, high1), (low1, high2),
        (low2, high2), (low1, high0), (low0, high1),
    ]


def _plan_purge(filament_area, filament_mm, volumetric_flow):
    try:
        filament_mm = float(filament_mm)
        volumetric_flow = float(volumetric_flow)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("purge parameters must be finite numbers")
    if not math.isfinite(filament_mm) or filament_mm < 0.0:
        raise ValueError("purge_filament must be a finite nonnegative number")
    if not math.isfinite(volumetric_flow) or volumetric_flow <= 0.0:
        raise ValueError("purge_flow must be a finite positive number")
    if filament_mm == 0.0:
        return None
    duration = filament_mm * filament_area / volumetric_flow
    return {
        "filament_mm": filament_mm,
        "volumetric_flow_mm3_s": volumetric_flow,
        "duration_s": duration,
        "feed_mm_s": filament_mm / duration,
        "scored": False,
    }


def _finite_positive(value, name):
    """Return a finite positive float or raise a planner error."""
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("%s must be a finite positive number" % name)
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError("%s must be a finite positive number" % name)
    return value


def _finite_nonnegative(value, name):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("%s must be a finite nonnegative number" % name)
    if not math.isfinite(value) or value < 0.0:
        raise ValueError("%s must be a finite nonnegative number" % name)
    return value


def _inclusive_flow_values(start_flow, end_flow, flow_step,
                           max_values=None):
    """Build a deterministic inclusive staircase, rejecting partial steps."""
    start_flow = _finite_positive(start_flow, "start_flow")
    end_flow = _finite_positive(end_flow, "end_flow")
    flow_step = _finite_positive(flow_step, "flow_step")
    if end_flow < start_flow:
        raise ValueError("end_flow must be at least start_flow")

    ratio = (end_flow - start_flow) / flow_step
    if not math.isfinite(ratio):
        raise ValueError(
            "requested flow staircase exceeds the maximum segment count")
    nearest = int(round(ratio))
    tolerance = max(1.0e-9, abs(ratio) * 1.0e-9)
    if abs(ratio - nearest) > tolerance:
        raise ValueError(
            "end_flow must be reached by start_flow plus flow_step steps")
    if max_values is not None and nearest + 1 > max_values:
        raise ValueError(
            "requested flow staircase exceeds the maximum segment count")
    values = [start_flow + index * flow_step
              for index in range(nearest + 1)]
    # Preserve the caller's requested endpoint when it is representable within
    # the tolerance; this avoids a surprising printed value such as 14.0000001
    # while retaining deterministic float construction for all interior steps.
    if values and abs(values[-1] - end_flow) <= max(
            1.0e-9, abs(end_flow) * 1.0e-9):
        values[-1] = end_flow
    return values


def plan_max_flow_capture(filament_diameter=1.75, start_flow=8.0,
                          end_flow=None, flow_step=2.0,
                          segment_length=20.0, purge_length=30.0,
                          purge_flow=12.0, max_segments=64,
                          max_test_flow=None):
    """Plan the raw pure-E maximum-flow diagnostic staircase.

    The plan contains only commanded motion and exact marker payload data.  It
    deliberately has no force thresholds, endpoint classification, or result
    recommendation.  ``end_flow`` is required so a caller cannot
    accidentally run an open-ended diagnostic.
    """
    if end_flow is not None and max_test_flow is not None:
        raise ValueError(
            "end_flow and max_test_flow must not both be supplied")
    if max_test_flow is not None:
        end_flow = max_test_flow
    if end_flow is None:
        raise ValueError("end_flow must be explicitly supplied")
    filament_diameter = _finite_positive(
        filament_diameter, "filament_diameter")
    start_flow = _finite_positive(start_flow, "start_flow")
    end_flow = _finite_positive(end_flow, "end_flow")
    flow_step = _finite_positive(flow_step, "flow_step")
    segment_length = _finite_positive(segment_length, "segment_length")
    purge_length = _finite_nonnegative(purge_length, "purge_length")
    purge_flow = _finite_positive(purge_flow, "purge_flow")
    try:
        max_segments_float = float(max_segments)
        max_segments = int(max_segments_float)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("max_segments must be an integer")
    if (not math.isfinite(max_segments_float)
            or max_segments_float != max_segments):
        raise ValueError("max_segments must be an integer")
    if max_segments < 1:
        raise ValueError("max_segments must be at least one")

    flow_values = _inclusive_flow_values(
        start_flow, end_flow, flow_step, max_values=max_segments)
    filament_area = math.pi * (filament_diameter * 0.5) ** 2

    purge = _plan_purge(filament_area, purge_length, purge_flow)
    # _plan_purge returns None for a zero-length optional purge.  Preserve a
    # stable payload shape so motion code can skip it without special cases.
    if purge is None:
        purge = {
            "filament_mm": 0.0,
            "volumetric_flow_mm3_s": purge_flow,
            "duration_s": 0.0,
            "feed_mm_s": 0.0,
            "scored": False,
        }

    segments = []
    current_e = 0.0
    for index, volumetric_flow in enumerate(flow_values):
        feed = volumetric_flow / filament_area
        duration = segment_length / feed
        start_e = current_e
        current_e += segment_length
        segments.append({
            "kind": "max_flow_segment",
            "phase": "flow_segment",
            "index": index,
            "segment_index": index,
            "volumetric_flow_mm3_s": volumetric_flow,
            "commanded_flow_mm3_s": volumetric_flow,
            "filament_mm": segment_length,
            "feed_mm_s": feed,
            "duration_s": duration,
            "nominal_duration_s": duration,
            "starting_e_mm": start_e,
            "start_e_mm": start_e,
            "target_e_mm": current_e,
            "scored": False,
        })

    total_filament = purge_length + segment_length * len(segments)
    nominal_duration = purge["duration_s"] + sum(
        segment["duration_s"] for segment in segments)
    maximum_feed = max([purge["feed_mm_s"]] + [
        segment["feed_mm_s"] for segment in segments])
    return {
        "experiment_type": "max_flow_capture",
        "filament_diameter_mm": filament_diameter,
        "filament_area_mm2": filament_area,
        "start_flow_mm3_s": start_flow,
        # ``max_test_flow_mm3_s`` is the canonical name for new artifacts.
        # Keep the old field while readers migrate captures produced by the
        # development END_FLOW command.
        "max_test_flow_mm3_s": end_flow,
        "end_flow_mm3_s": end_flow,
        "flow_step_mm3_s": flow_step,
        "flow_values_mm3_s": flow_values,
        "flows_mm3_s": list(flow_values),
        "segment_length_mm": segment_length,
        "segments": segments,
        "segment_count": len(segments),
        "purge": purge,
        "purge_length_mm": purge_length,
        "purge_flow_mm3_s": purge_flow,
        "total_filament_mm": total_filament,
        "maximum_segment_distance_mm": segment_length,
        "maximum_distance_mm": max(segment_length, purge_length),
        "maximum_feed_mm_s": maximum_feed,
        "max_feed_mm_s": maximum_feed,
        "nominal_duration_s": nominal_duration,
        "intentional_dwell_between_segments_s": 0.0,
    }


def plan_controlled_max_flow_setup(
        filament_diameter=1.75, start_flow=14.0, max_test_flow=50.0,
        coarse_step=1.0, fine_step=0.1, segment_length=20.0,
        purge_length=30.0, purge_flow=12.0):
    """Validate controlled-search inputs without materializing a staircase.

    ``CONTROL=1`` requests are generated by :class:`MaxFlowSearchController`
    one at a time.  This setup payload contains only static configuration and
    the optional purge; it deliberately has no endpoint feed, segment list, or
    full-range material/time budget.  The fixed staircase planner remains the
    ``CONTROL=0`` diagnostic path.
    """
    filament_diameter = _finite_positive(
        filament_diameter, "filament_diameter")
    start_flow = _finite_positive(start_flow, "start_flow")
    max_test_flow = _finite_positive(max_test_flow, "max_test_flow")
    coarse_step = _finite_positive(coarse_step, "coarse_step")
    fine_step = _finite_positive(fine_step, "fine_step")
    segment_length = _finite_positive(segment_length, "segment_length")
    purge_length = _finite_nonnegative(purge_length, "purge_length")
    purge_flow = _finite_positive(purge_flow, "purge_flow")
    if max_test_flow > MAX_TEST_FLOW_LIMIT_MM3_S:
        raise ValueError(
            "max_test_flow must be at most %.1f mm3/s" %
            MAX_TEST_FLOW_LIMIT_MM3_S)
    if start_flow > MAX_TEST_FLOW_LIMIT_MM3_S:
        raise ValueError(
            "start_flow must be at most %.1f mm3/s" %
            MAX_TEST_FLOW_LIMIT_MM3_S)
    if max_test_flow < start_flow:
        raise ValueError("max_test_flow must be at least start_flow")
    if fine_step >= coarse_step:
        raise ValueError("fine_step must be smaller than coarse_step")
    filament_area = math.pi * (filament_diameter * 0.5) ** 2
    purge = _plan_purge(filament_area, purge_length, purge_flow)
    if purge is None:
        purge = {
            "filament_mm": 0.0,
            "volumetric_flow_mm3_s": purge_flow,
            "duration_s": 0.0,
            "feed_mm_s": 0.0,
            "scored": False,
        }
    return {
        "experiment_type": "max_flow_capture",
        "filament_diameter_mm": filament_diameter,
        "filament_area_mm2": filament_area,
        "start_flow_mm3_s": start_flow,
        "max_test_flow_mm3_s": max_test_flow,
        # Legacy readers can still identify the endpoint in a mixed artifact.
        "end_flow_mm3_s": max_test_flow,
        "flow_step_mm3_s": coarse_step,
        "coarse_step_mm3_s": coarse_step,
        "fine_step_mm3_s": fine_step,
        "flow_values_mm3_s": [],
        "flows_mm3_s": [],
        "segments": [],
        "segment_count": 0,
        "lazy": True,
        "segment_length_mm": segment_length,
        "purge": purge,
        "purge_length_mm": purge_length,
        "purge_flow_mm3_s": purge_flow,
        "total_filament_mm": purge_length,
        "maximum_segment_distance_mm": segment_length,
        "maximum_distance_mm": max(segment_length, purge_length),
        "maximum_feed_mm_s": purge["feed_mm_s"],
        "max_feed_mm_s": purge["feed_mm_s"],
        "nominal_duration_s": purge["duration_s"],
        "intentional_dwell_between_segments_s": 0.0,
    }


def plan_max_flow_segment(filament_diameter, flow, segment_length,
                          segment_index=0, stage="search",
                          speculative=False):
    """Build one bounded pure-E segment for adaptive max-flow control.

    Unlike :func:`plan_max_flow_capture`, this helper does not construct a
    staircase in advance.  The controller can therefore stop after a
    confirmed boundary while retaining the same typed marker payload shape.
    """
    filament_diameter = _finite_positive(
        filament_diameter, "filament_diameter")
    flow = _finite_positive(flow, "flow")
    segment_length = _finite_positive(segment_length, "segment_length")
    try:
        segment_index = int(segment_index)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("segment_index must be an integer")
    if segment_index < 0:
        raise ValueError("segment_index must be nonnegative")
    filament_area = math.pi * (filament_diameter * 0.5) ** 2
    feed = flow / filament_area
    duration = segment_length / feed
    return {
        "kind": "max_flow_segment",
        "phase": "flow_segment",
        "stage": str(stage),
        "index": segment_index,
        "segment_index": segment_index,
        "volumetric_flow_mm3_s": flow,
        "commanded_flow_mm3_s": flow,
        "flow_mm3_s": flow,
        "filament_mm": segment_length,
        "feed_mm_s": feed,
        "duration_s": duration,
        "nominal_duration_s": duration,
        "starting_e_mm": 0.0,
        "start_e_mm": 0.0,
        "target_e_mm": segment_length,
        "scored": False,
        "speculative": bool(speculative),
    }


def plan_controlled_max_flow_budget(filament_diameter, start_flow,
                                    end_flow=None, coarse_step=1.0,
                                    fine_step=0.1, segment_length=20.0,
                                    purge_length=0.0, purge_flow=12.0,
                                    max_test_flow=None):
    """Return a lazy controlled-search descriptor.

    The former implementation calculated a full-range material/time budget by
    eagerly building the coarse staircase.  That made a deliberately remote
    endpoint fail before the detector could stop the run.  Keep this helper
    for callers that used its name, but return only validated static setup;
    motion and material accounting are now recorded as requests are made.
    """
    if end_flow is not None and max_test_flow is not None:
        raise ValueError(
            "end_flow and max_test_flow must not both be supplied")
    if max_test_flow is None:
        max_test_flow = end_flow
    if max_test_flow is None:
        raise ValueError("max_test_flow must be supplied")
    setup = plan_controlled_max_flow_setup(
        filament_diameter=filament_diameter,
        start_flow=start_flow,
        max_test_flow=max_test_flow,
        coarse_step=coarse_step,
        fine_step=fine_step,
        segment_length=segment_length,
        purge_length=purge_length,
        purge_flow=purge_flow)
    return {
        "lazy": True,
        "max_test_flow_mm3_s": setup["max_test_flow_mm3_s"],
        "end_flow_mm3_s": setup["max_test_flow_mm3_s"],
        "start_flow_mm3_s": setup["start_flow_mm3_s"],
        "coarse_step_mm3_s": setup["coarse_step_mm3_s"],
        "fine_step_mm3_s": setup["fine_step_mm3_s"],
        "segment_length_mm": setup["segment_length_mm"],
        "purge_length_mm": setup["purge_length_mm"],
        "maximum_segment_count": None,
        "coarse_segment_count": None,
        "fine_segment_count": None,
        "decision_gated_segment_count": None,
        "maximum_filament_mm": None,
        "maximum_nominal_duration_s": None,
    }


def plan_pa_matrix(pressure_advances=(0.034, 0.038, 0.042, 0.046, 0.050),
                   low_flows=(2.0, 4.0, 6.0),
                   high_flows=(10.0, 12.0, 14.0),
                   filament_diameter=1.75, slow_time=1.0, fast_time=0.35,
                   lead_time=2.0, cycles=3, conditioning_cycles=3,
                   control_cycles=4, wobble=0.05,
                   purge_filament=20.0, purge_flow=5.0):
    """Build the fixed development PA flow-response matrix.

    Flow-pair blocks use a deterministic balanced order.  K runs upward in
    one block and downward in the next, leaving the K unchanged across every
    flow-block boundary.  Each condition retains an identical flowing lead,
    conditioning sequence, and scored sequence.
    """
    pressure_advances = sorted(parse_pressure_advance_values(
        pressure_advances))
    low_flows = parse_flow_values(low_flows, "LOW_FLOWS")
    high_flows = parse_flow_values(high_flows, "HIGH_FLOWS")
    if min(high_flows) <= max(low_flows):
        raise ValueError(
            "every HIGH_FLOWS value must exceed every LOW_FLOWS value")
    numeric = {
        "filament_diameter": filament_diameter,
        "slow_time": slow_time,
        "fast_time": fast_time,
        "lead_time": lead_time,
        "wobble": wobble,
        "purge_filament": purge_filament,
        "purge_flow": purge_flow,
    }
    for name, value in numeric.items():
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("%s must be a finite positive number" % name)
        if not math.isfinite(value) or value <= 0.:
            raise ValueError("%s must be a finite positive number" % name)
        numeric[name] = value
    try:
        cycles = int(cycles)
        conditioning_cycles = int(conditioning_cycles)
        control_cycles = int(control_cycles)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("cycle counts must be integers")
    if cycles < 1:
        raise ValueError("cycles must be at least one")
    if not 0 <= conditioning_cycles <= 20:
        raise ValueError("conditioning_cycles must be between zero and twenty")
    if control_cycles < 1:
        raise ValueError("control_cycles must be at least one")

    # Reuse the established movement-only waveform shape.  The chosen flows
    # do not affect these no-extrusion control legs.
    control = plan_pa_waveform(
        filament_diameter=numeric["filament_diameter"],
        slow_flow=low_flows[0], fast_flow=high_flows[-1],
        slow_time=numeric["slow_time"], fast_time=numeric["fast_time"],
        cycles=cycles, warmup_time=numeric["lead_time"],
        control_cycles=control_cycles, wobble=numeric["wobble"],
        conditioning_cycles=0)
    filament_area = control["filament_area_mm2"]
    flow_pairs = _matrix_flow_pair_order(low_flows, high_flows)
    conditions = []
    all_legs = []
    current_offset = 0.0
    condition_index = 0
    for flow_pair_index, flow_pair in enumerate(flow_pairs):
        low_flow, high_flow = flow_pair
        k_direction = "ascending" if flow_pair_index % 2 == 0 \
            else "descending"
        k_values = (pressure_advances if k_direction == "ascending"
                    else list(reversed(pressure_advances)))
        low_feed = low_flow / filament_area
        high_feed = high_flow / filament_area
        for k_within_pair, pressure_advance in enumerate(k_values):
            start_offset = current_offset
            shared = {
                "condition_index": condition_index,
                "flow_pair_index": flow_pair_index,
                "k_within_flow_pair": k_within_pair,
                "k_direction": k_direction,
                "pressure_advance": pressure_advance,
                "low_flow_mm3_s": low_flow,
                "high_flow_mm3_s": high_flow,
                "flow_step_mm3_s": high_flow - low_flow,
                "mean_flow_mm3_s": 0.5 * (high_flow + low_flow),
            }
            current_offset = (numeric["wobble"]
                              if current_offset == 0.0 else 0.0)
            legs = [dict(shared, **{
                "kind": "extrusion",
                "phase": "condition_lead_slow",
                "cycle": 0,
                "duration_s": numeric["lead_time"],
                "axis_offset_mm": current_offset,
                "filament_mm": low_feed * numeric["lead_time"],
                "volumetric_flow_mm3_s": low_flow,
                "transition_before": None,
                "scored": False,
            })]
            for cycle_kind, cycle_count in (
                    ("conditioning", conditioning_cycles),
                    ("scored", cycles)):
                for cycle in range(1, cycle_count + 1):
                    scored = cycle_kind == "scored"
                    current_offset = (numeric["wobble"]
                                      if current_offset == 0.0 else 0.0)
                    legs.append(dict(shared, **{
                        "kind": "extrusion",
                        "phase": "fast" if scored else "conditioning_fast",
                        "cycle": cycle,
                        "duration_s": numeric["fast_time"],
                        "axis_offset_mm": current_offset,
                        "filament_mm": high_feed * numeric["fast_time"],
                        "volumetric_flow_mm3_s": high_flow,
                        "transition_before": "slow_to_fast",
                        "scored": scored,
                    }))
                    current_offset = (numeric["wobble"]
                                      if current_offset == 0.0 else 0.0)
                    legs.append(dict(shared, **{
                        "kind": "extrusion",
                        "phase": "slow" if scored else "conditioning_slow",
                        "cycle": cycle,
                        "duration_s": numeric["slow_time"],
                        "axis_offset_mm": current_offset,
                        "filament_mm": low_feed * numeric["slow_time"],
                        "volumetric_flow_mm3_s": low_flow,
                        "transition_before": "fast_to_slow",
                        "scored": scored,
                    }))
            condition = dict(shared)
            condition.update({
                "lead_time_s": numeric["lead_time"],
                "conditioning_cycles": conditioning_cycles,
                "scored_cycles": cycles,
                "start_axis_offset_mm": start_offset,
                "end_axis_offset_mm": current_offset,
                "legs": legs,
                "total_filament_mm": sum(
                    leg["filament_mm"] for leg in legs),
            })
            conditions.append(condition)
            all_legs.extend(legs)
            condition_index += 1

    maximum_leg_filament = max(
        leg["filament_mm"] for leg in all_legs)
    purge_duration = (
        numeric["purge_filament"] * filament_area / numeric["purge_flow"])
    waveform_filament = sum(
        condition["total_filament_mm"] for condition in conditions)
    return {
        "filament_diameter_mm": numeric["filament_diameter"],
        "filament_area_mm2": filament_area,
        "low_flow_values_mm3_s": low_flows,
        "high_flow_values_mm3_s": high_flows,
        "pressure_advance_values": pressure_advances,
        "slow_time_s": numeric["slow_time"],
        "fast_time_s": numeric["fast_time"],
        "lead_time_s": numeric["lead_time"],
        "conditioning_cycles_per_condition": conditioning_cycles,
        "scored_cycles_per_condition": cycles,
        "control_cycles": control_cycles,
        "wobble_mm": numeric["wobble"],
        "ordering": "balanced_serpentine_v1",
        "flow_pairs": [
            {"flow_pair_index": index,
             "low_flow_mm3_s": pair[0],
             "high_flow_mm3_s": pair[1]}
            for index, pair in enumerate(flow_pairs)],
        "conditions": conditions,
        "condition_count": len(conditions),
        "control_legs": control["control_legs"],
        "extrusion_legs": all_legs,
        "purge": {
            "filament_mm": numeric["purge_filament"],
            "volumetric_flow_mm3_s": numeric["purge_flow"],
            "duration_s": purge_duration,
            "feed_mm_s": numeric["purge_filament"] / purge_duration,
            "scored": False,
        },
        "waveform_filament_mm": waveform_filament,
        "total_filament_mm": waveform_filament + numeric["purge_filament"],
        "maximum_leg_filament_mm": maximum_leg_filament,
        "maximum_extrude_ratio": (
            maximum_leg_filament / numeric["wobble"]),
    }


def plan_pa_sweep(pressure_advances=(0.0, 0.032, 0.064),
                  filament_diameter=1.75, slow_flow=2.0,
                  fast_flow=14.0, slow_time=1.0, fast_time=0.25,
                  cycles=3, initial_warmup_time=4.0, k_lead_time=2.0,
                  control_cycles=4, wobble=0.05,
                  conditioning_cycles=3, purge_filament=0.0,
                  purge_flow=5.0):
    """Build a continuous multi-K waveform with no idle gaps between Ks.

    The first K receives the longer initial slow-flow warmup.  Later K blocks
    begin with one ordinary slow-flow lead.  Carrier-axis targets continue to
    alternate across block boundaries, so every extrusion leg remains a
    combined XY/E move and pressure is not deliberately relaxed between Ks.
    """
    pressure_advances = parse_pressure_advance_values(pressure_advances)
    base = plan_pa_waveform(
        filament_diameter=filament_diameter,
        slow_flow=slow_flow, fast_flow=fast_flow,
        slow_time=slow_time, fast_time=fast_time,
        cycles=cycles, warmup_time=initial_warmup_time,
        control_cycles=control_cycles, wobble=wobble,
        conditioning_cycles=0)
    try:
        conditioning_cycles = int(conditioning_cycles)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("conditioning_cycles must be an integer")
    if not 0 <= conditioning_cycles <= 20:
        raise ValueError("conditioning_cycles must be between zero and twenty")
    try:
        k_lead_time = float(k_lead_time)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("k_lead_time must be a finite positive number")
    if not math.isfinite(k_lead_time) or k_lead_time <= 0.:
        raise ValueError("k_lead_time must be a finite positive number")

    slow_feed = base["slow_feed_mm_s"]
    fast_feed = base["fast_feed_mm_s"]
    current_offset = 0.0
    blocks = []
    all_legs = []
    for k_index, pressure_advance in enumerate(pressure_advances):
        start_offset = current_offset
        lead_time = (base["warmup_time_s"] if k_index == 0
                     else k_lead_time)
        current_offset = (base["wobble_mm"]
                          if current_offset == 0.0 else 0.0)
        legs = [{
            "kind": "extrusion",
            "phase": ("initial_warmup_slow" if k_index == 0
                      else "k_lead_slow"),
            "cycle": 0,
            "duration_s": lead_time,
            "axis_offset_mm": current_offset,
            "filament_mm": slow_feed * lead_time,
            "volumetric_flow_mm3_s": base["slow_flow_mm3_s"],
            "transition_before": None,
            "k_index": k_index,
            "pressure_advance": pressure_advance,
            "scored": False,
        }]
        for conditioning_cycle in range(1, conditioning_cycles + 1):
            current_offset = (base["wobble_mm"]
                              if current_offset == 0.0 else 0.0)
            legs.append({
                "kind": "extrusion",
                "phase": "conditioning_fast",
                "cycle": conditioning_cycle,
                "duration_s": base["fast_time_s"],
                "axis_offset_mm": current_offset,
                "filament_mm": fast_feed * base["fast_time_s"],
                "volumetric_flow_mm3_s": base["fast_flow_mm3_s"],
                "transition_before": "slow_to_fast",
                "k_index": k_index,
                "pressure_advance": pressure_advance,
                "scored": False,
            })
            current_offset = (base["wobble_mm"]
                              if current_offset == 0.0 else 0.0)
            legs.append({
                "kind": "extrusion",
                "phase": "conditioning_slow",
                "cycle": conditioning_cycle,
                "duration_s": base["slow_time_s"],
                "axis_offset_mm": current_offset,
                "filament_mm": slow_feed * base["slow_time_s"],
                "volumetric_flow_mm3_s": base["slow_flow_mm3_s"],
                "transition_before": "fast_to_slow",
                "k_index": k_index,
                "pressure_advance": pressure_advance,
                "scored": False,
            })
        for cycle in range(1, base["cycles"] + 1):
            current_offset = (base["wobble_mm"]
                              if current_offset == 0.0 else 0.0)
            legs.append({
                "kind": "extrusion",
                "phase": "fast",
                "cycle": cycle,
                "duration_s": base["fast_time_s"],
                "axis_offset_mm": current_offset,
                "filament_mm": fast_feed * base["fast_time_s"],
                "volumetric_flow_mm3_s": base["fast_flow_mm3_s"],
                "transition_before": "slow_to_fast",
                "k_index": k_index,
                "pressure_advance": pressure_advance,
                "scored": True,
            })
            current_offset = (base["wobble_mm"]
                              if current_offset == 0.0 else 0.0)
            legs.append({
                "kind": "extrusion",
                "phase": "slow",
                "cycle": cycle,
                "duration_s": base["slow_time_s"],
                "axis_offset_mm": current_offset,
                "filament_mm": slow_feed * base["slow_time_s"],
                "volumetric_flow_mm3_s": base["slow_flow_mm3_s"],
                "transition_before": "fast_to_slow",
                "k_index": k_index,
                "pressure_advance": pressure_advance,
                "scored": True,
            })
        block = {
            "k_index": k_index,
            "pressure_advance": pressure_advance,
            "lead_time_s": lead_time,
            "conditioning_cycles": conditioning_cycles,
            "start_axis_offset_mm": start_offset,
            "end_axis_offset_mm": current_offset,
            "legs": legs,
            "total_filament_mm": sum(
                leg["filament_mm"] for leg in legs),
        }
        blocks.append(block)
        all_legs.extend(legs)

    maximum_leg_filament = max(
        leg["filament_mm"] for leg in all_legs)
    purge = _plan_purge(
        base["filament_area_mm2"], purge_filament, purge_flow)
    waveform_filament = sum(leg["filament_mm"] for leg in all_legs)
    return {
        "filament_diameter_mm": base["filament_diameter_mm"],
        "filament_area_mm2": base["filament_area_mm2"],
        "slow_flow_mm3_s": base["slow_flow_mm3_s"],
        "fast_flow_mm3_s": base["fast_flow_mm3_s"],
        "slow_feed_mm_s": slow_feed,
        "fast_feed_mm_s": fast_feed,
        "slow_time_s": base["slow_time_s"],
        "fast_time_s": base["fast_time_s"],
        "initial_warmup_time_s": base["warmup_time_s"],
        "k_lead_time_s": k_lead_time,
        "cycles_per_k": base["cycles"],
        "conditioning_cycles_per_k": conditioning_cycles,
        "control_cycles": base["control_cycles"],
        "wobble_mm": base["wobble_mm"],
        "pressure_advance_values": pressure_advances,
        "control_legs": base["control_legs"],
        "blocks": blocks,
        "extrusion_legs": all_legs,
        "purge": purge,
        "waveform_filament_mm": waveform_filament,
        "total_filament_mm": (
            waveform_filament
            + (0.0 if purge is None else purge["filament_mm"])),
        "maximum_leg_filament_mm": maximum_leg_filament,
        "maximum_extrude_ratio": (
            maximum_leg_filament / base["wobble_mm"]),
    }


def _mean(values):
    return sum(values) / len(values)


def _population_stddev(values, mean):
    if not values:
        return None
    return math.sqrt(sum((value - mean) ** 2 for value in values)
                     / len(values))


def update_temperature_stability(stable_since, eventtime, temperature,
                                 target, tolerance):
    """Track the start of a continuous in-tolerance temperature window."""
    if abs(float(temperature) - float(target)) > float(tolerance):
        return None
    return eventtime if stable_since is None else stable_since


def _linear_drift_per_second(samples, values):
    if len(samples) < 2:
        return None
    timestamps = [row[0] for row in samples]
    mean_time = _mean(timestamps)
    mean_value = _mean(values)
    time_variance = sum(
        (timestamp - mean_time) ** 2 for timestamp in timestamps)
    if not time_variance:
        return None
    return sum(
        (timestamp - mean_time) * (value - mean_value)
        for timestamp, value in zip(timestamps, values)) / time_variance


def _signal_stats(samples):
    calibrated = all(len(row) > 1 and row[1] is not None for row in samples)
    column = 1 if calibrated else 2
    units = "g" if calibrated else "counts"
    values = [float(row[column]) for row in samples]
    if not values:
        return {
            "source": "force" if calibrated else "counts",
            "units": units,
            "mean": None,
            "standard_deviation": None,
            "minimum": None,
            "maximum": None,
            "peak_to_peak": None,
            "linear_drift_per_s": None,
            "detrended_standard_deviation": None,
            "detrended_peak_to_peak": None,
        }
    mean = _mean(values)
    minimum = min(values)
    maximum = max(values)
    drift = _linear_drift_per_second(samples, values)
    detrended_stddev = None
    detrended_peak_to_peak = None
    if drift is not None:
        mean_time = _mean([row[0] for row in samples])
        residuals = [
            value - (mean + drift * (row[0] - mean_time))
            for row, value in zip(samples, values)
        ]
        residual_mean = _mean(residuals)
        detrended_stddev = _population_stddev(residuals, residual_mean)
        detrended_peak_to_peak = max(residuals) - min(residuals)
    return {
        "source": "force" if calibrated else "counts",
        "units": units,
        "mean": mean,
        "standard_deviation": _population_stddev(values, mean),
        "minimum": minimum,
        "maximum": maximum,
        "peak_to_peak": maximum - minimum,
        "linear_drift_per_s": drift,
        "detrended_standard_deviation": detrended_stddev,
        "detrended_peak_to_peak": detrended_peak_to_peak,
    }


def analyze_stream(samples, expected_sample_rate, errors=0, overflows=0,
                   sensor_range=None, minimum_rate_ratio=0.90,
                   maximum_gap_intervals=1.5):
    """Assess whether a load-cell capture is suitable for later experiments.

    Rows follow Klipper's LoadCell client format:
    ``[print_time, force_g, counts, tare_counts]``.  Force may be ``None``
    when the sensor has not been calibrated; timestamp and count validation
    still work in that case.
    """
    reasons = []
    if expected_sample_rate is None or expected_sample_rate <= 0:
        raise ValueError("expected_sample_rate must be positive")
    if not 0. < minimum_rate_ratio <= 1.:
        raise ValueError("minimum_rate_ratio must be in (0, 1]")
    if maximum_gap_intervals <= 1.:
        raise ValueError("maximum_gap_intervals must be greater than one")

    valid_rows = []
    malformed_rows = 0
    for row in samples:
        if len(row) < 3:
            malformed_rows += 1
            continue
        try:
            timestamp = float(row[0])
            counts = int(row[2])
        except (TypeError, ValueError, OverflowError):
            malformed_rows += 1
            continue
        if not math.isfinite(timestamp):
            malformed_rows += 1
            continue
        force = row[1]
        if force is not None:
            try:
                force = float(force)
            except (TypeError, ValueError, OverflowError):
                malformed_rows += 1
                continue
            if not math.isfinite(force):
                malformed_rows += 1
                continue
        tare_counts = row[3] if len(row) > 3 else None
        valid_rows.append([timestamp, force, counts, tare_counts])

    if malformed_rows:
        reasons.append("malformed_samples")
    if not valid_rows:
        reasons.append("no_samples")

    timestamps = [row[0] for row in valid_rows]
    deltas = [later - earlier
              for earlier, later in zip(timestamps, timestamps[1:])]
    non_monotonic = sum(1 for delta in deltas if delta <= 0.)
    if non_monotonic:
        reasons.append("non_monotonic_timestamps")

    positive_deltas = [delta for delta in deltas if delta > 0.]
    expected_interval = 1. / float(expected_sample_rate)
    gap_limit = maximum_gap_intervals * expected_interval
    gap_count = sum(1 for delta in positive_deltas if delta > gap_limit)
    if gap_count:
        reasons.append("timestamp_gaps")

    duration = None
    measured_rate = None
    if len(timestamps) >= 2 and timestamps[-1] > timestamps[0]:
        duration = timestamps[-1] - timestamps[0]
        measured_rate = (len(timestamps) - 1) / duration
        if measured_rate < expected_sample_rate * minimum_rate_ratio:
            reasons.append("sample_rate_below_threshold")
    elif valid_rows:
        reasons.append("insufficient_timestamp_span")

    saturation_count = 0
    if sensor_range is not None:
        sensor_min, sensor_max = sensor_range
        saturation_count = sum(
            1 for row in valid_rows
            if row[2] <= sensor_min or row[2] >= sensor_max)
        if saturation_count:
            reasons.append("saturated_samples")

    if errors:
        reasons.append("sensor_errors")
    if overflows:
        reasons.append("sensor_overflows")

    # Preserve order while de-duplicating reason codes.
    reasons = list(dict.fromkeys(reasons))
    return {
        "passed": not reasons,
        "failure_reasons": reasons,
        "sample_count": len(valid_rows),
        "malformed_sample_count": malformed_rows,
        "start_print_time": timestamps[0] if timestamps else None,
        "end_print_time": timestamps[-1] if timestamps else None,
        "duration_s": duration,
        "expected_sample_rate_sps": float(expected_sample_rate),
        "measured_sample_rate_sps": measured_rate,
        "minimum_rate_ratio": minimum_rate_ratio,
        "timestamp_non_monotonic_count": non_monotonic,
        "timestamp_gap_count": gap_count,
        "maximum_timestamp_gap_s": (max(positive_deltas)
                                     if positive_deltas else None),
        "maximum_allowed_gap_s": gap_limit,
        "sensor_error_count": int(errors or 0),
        "sensor_overflow_count": int(overflows or 0),
        "saturated_sample_count": saturation_count,
        "signal": _signal_stats(valid_rows),
    }


def normalize_samples(samples):
    """Return rows in the stable, JSON-compatible schema column order."""
    normalized = []
    for row in samples:
        if len(row) < 3:
            continue
        try:
            timestamp = float(row[0])
            force = None if row[1] is None else float(row[1])
            counts = int(row[2])
            tare = None if len(row) < 4 or row[3] is None else int(row[3])
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(timestamp):
            continue
        if force is not None and not math.isfinite(force):
            continue
        normalized.append([timestamp, force, counts, tare])
    return normalized


def build_capture_record(run_id, created_utc, source, sensor_status,
                         qualification, samples, label=None,
                         provenance=None, conditions=None,
                         experiment_type="sensor_qualification"):
    """Build a versioned raw sensor-qualification capture."""
    return {
        "schema": {
            "id": SCHEMA_ID,
            "version": SCHEMA_VERSION,
        },
        "run": {
            "id": run_id,
            "created_utc": created_utc,
            "experiment_type": experiment_type,
            "label": label,
            "status": "passed" if qualification.get("passed") else "failed",
        },
        "source": source,
        "sensor_status": sensor_status,
        "conditions": conditions or {},
        "qualification": qualification,
        "provenance": provenance or {},
        "samples": {
            "columns": ["print_time", "force_g", "counts", "tare_counts"],
            "data": normalize_samples(samples),
        },
    }


def reanalyze_capture(record):
    """Replay capture qualification without modifying the raw record."""
    replay = copy.deepcopy(record)
    quality = replay.get("qualification", {})
    source = replay.get("source", {})
    samples = replay.get("samples", {}).get("data", [])
    expected_rate = source.get("configured_sample_rate_sps")
    if expected_rate is None:
        expected_rate = quality.get("expected_sample_rate_sps")
    minimum_rate_ratio = quality.get("minimum_rate_ratio", 0.90)
    maximum_allowed_gap = quality.get("maximum_allowed_gap_s")
    maximum_gap_intervals = 1.5
    if maximum_allowed_gap is not None and expected_rate:
        maximum_gap_intervals = maximum_allowed_gap * expected_rate
    replayed_quality = analyze_stream(
        samples, expected_rate,
        errors=quality.get("sensor_error_count", 0),
        overflows=quality.get("sensor_overflow_count", 0),
        sensor_range=source.get("sensor_range_counts"),
        minimum_rate_ratio=minimum_rate_ratio,
        maximum_gap_intervals=maximum_gap_intervals)

    thermal = replay.get("conditions", {}).get("thermal_check")
    if thermal is not None:
        stable_start = thermal.get("stable_start_print_time")
        stable_samples = ([] if stable_start is None else [
            row for row in samples if row and row[0] >= stable_start])
        stable_tail = analyze_stream(
            stable_samples, expected_rate,
            sensor_range=source.get("sensor_range_counts"),
            minimum_rate_ratio=minimum_rate_ratio,
            maximum_gap_intervals=maximum_gap_intervals)
        thermal["stable_tail"] = stable_tail
        if not thermal.get("stable_reached"):
            replayed_quality["passed"] = False
            replayed_quality["failure_reasons"].append(
                "temperature_not_stable")
        elif not stable_tail["passed"]:
            replayed_quality["passed"] = False
            replayed_quality["failure_reasons"].append(
                "stable_tail_invalid")
    replay["qualification"] = replayed_quality
    replay["run"]["status"] = (
        "passed" if replayed_quality["passed"] else "failed")
    return replay


def render_sensor_report(record):
    """Render the compact review summary used by the offline CLI."""
    schema = record.get("schema", {})
    if (schema.get("id") != SCHEMA_ID
            or schema.get("version") != SCHEMA_VERSION):
        raise ValueError("unsupported FlowTune capture schema")
    run = record.get("run", {})
    quality = record.get("qualification", {})
    signal = quality.get("signal", {})
    measured_rate = quality.get("measured_sample_rate_sps")
    measured_text = ("unknown" if measured_rate is None
                     else "%.1f SPS" % (measured_rate,))
    reasons = quality.get("failure_reasons") or []
    run_text = "Run: %s (%s)" % (
        run.get("id", "unknown"), run.get("created_utc", "unknown"))
    if run.get("label"):
        run_text += "; label=%s" % (run["label"],)
    lines = [
        "FlowTune sensor qualification: %s"
        % ("PASSED" if quality.get("passed") else "FAILED"),
        run_text,
        "Source: %s via %s" % (
            record.get("source", {}).get("sensor_class", "unknown"),
            record.get("source", {}).get("load_cell_object", "unknown")),
        "Samples: %s; measured rate: %s; gaps: %s; non-monotonic: %s; "
        "malformed: %s"
        % (quality.get("sample_count", 0), measured_text,
           quality.get("timestamp_gap_count", 0),
           quality.get("timestamp_non_monotonic_count", 0),
           quality.get("malformed_sample_count", 0)),
        "Sensor errors: %s; overflows: %s; saturated samples: %s"
        % (quality.get("sensor_error_count", 0),
           quality.get("sensor_overflow_count", 0),
           quality.get("saturated_sample_count", 0)),
        "Signal (%s): mean=%s, standard deviation=%s, peak-to-peak=%s"
        % (signal.get("units", "unknown"), signal.get("mean"),
           signal.get("standard_deviation"), signal.get("peak_to_peak")),
        "Linear drift: %s %s/s"
        % (signal.get("linear_drift_per_s"), signal.get("units", "unknown")),
        "Detrended signal: standard deviation=%s; peak-to-peak=%s %s"
        % (signal.get("detrended_standard_deviation"),
           signal.get("detrended_peak_to_peak"),
           signal.get("units", "unknown")),
    ]
    conditions = record.get("conditions", {})
    before_extruder = conditions.get("before", {}).get("extruder", {})
    after_extruder = conditions.get("after", {}).get("extruder", {})
    if before_extruder or after_extruder:
        lines.append(
            "Extruder temperature: %s -> %s C; target: %s -> %s C"
            % (before_extruder.get("temperature"),
               after_extruder.get("temperature"),
               before_extruder.get("target"),
               after_extruder.get("target")))
    thermal = conditions.get("thermal_check")
    if thermal:
        lines.append(
            "Thermal check: target=%s C; stable=%s; stable duration=%s s; "
            "elapsed=%s s"
            % (thermal.get("target_c"), thermal.get("stable_reached"),
               thermal.get("stable_duration_s"), thermal.get("elapsed_s")))
        tail = thermal.get("stable_tail", {})
        tail_signal = tail.get("signal", {})
        if tail:
            lines.append(
                "Stable tail: %s samples; drift=%s %s/s; "
                "detrended standard deviation=%s"
                % (tail.get("sample_count"),
                   tail_signal.get("linear_drift_per_s"),
                   tail_signal.get("units", "unknown"),
                   tail_signal.get("detrended_standard_deviation")))
    if reasons:
        lines.append("Failure reasons: %s" % (", ".join(reasons),))
    return "\n".join(lines)
