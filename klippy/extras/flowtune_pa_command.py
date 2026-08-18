# FlowTune
#
# Copyright (C) 2026 Ahmed Sheikh <ahmed.ali.sheikh1998@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
# SPDX-License-Identifier: GPL-3.0-only

"""Production FlowPA command orchestration for Klipper."""

from __future__ import division

import logging

from . import flowtune_core
from . import flowtune_pa_worker


DEFAULT_SMOOTH_TIME_S = 0.030
DEFAULT_ACCEL_MM_S2 = 1000.0
DEFAULT_POST_TARGET_DWELL_S = 20.0
DEFAULT_HEAT_TIMEOUT_S = 240.0
DEFAULT_TEMPERATURE_TOLERANCE_C = 1.0


def _parameters(gcmd):
    getter = getattr(gcmd, "get_command_parameters", None)
    return getter() if callable(getter) else {}


def _result_message(result, report_path):
    state = result.get("state", "invalid")
    recommendation = result.get("recommendation", {})
    support = result.get("fall", {}).get("cycle_support", {})
    lines = ["FlowTune FlowPA result: %s" % state]
    if recommendation.get("pressure_advance") is not None:
        lines.append("Recommended PA: %.3f (boundary %.5f, bracket %s)" % (
            recommendation["pressure_advance"],
            recommendation["unrounded_boundary"],
            recommendation.get("bracket")))
    else:
        lines.append("No PA recommendation was produced.")
        range_hint = recommendation.get("range_hint")
        if range_hint:
            range_messages = {
                "test_higher_k_values": "rerun with higher K_VALUES",
                "test_lower_k_values": "rerun with lower K_VALUES",
            }
            lines.append("Range hint: %s." %
                         range_messages.get(range_hint, range_hint))
    lines.append("Cycle support: %s/%s; observed boundary span: %s" % (
        support.get("supported_cycles", 0),
        support.get("total_cycles", 0),
        ("--" if support.get("observed_range") is None else
         "%.5f" % support["observed_range"])))
    lines.append("Report: %s" % report_path)
    return "\n".join(lines)


def run(host, gcmd):
    host._require_ready(gcmd)
    host._require_idle(gcmd)
    if host.active_operation is not None or host.capture.active:
        raise gcmd.error("A FlowTune operation is already active")

    seen = _parameters(gcmd)
    if "TARGET" not in seen:
        raise gcmd.error("FLOWTUNE_PA requires TARGET")

    toolhead = host.printer.lookup_object("toolhead")
    extruder = toolhead.get_extruder()
    if extruder is None or extruder.get_name() != "extruder":
        raise gcmd.error("FLOWTUNE_PA requires the primary extruder")
    heater = extruder.get_heater()
    heaters = host.printer.lookup_object("heaters")
    eventtime = host.reactor.monotonic()
    extruder_status = extruder.get_status(eventtime)
    original_target = float(extruder_status.get("target", 0.0) or 0.0)
    if original_target:
        raise gcmd.error("FLOWTUNE_PA must start with the hotend target off")

    axis_name = gcmd.get("AXIS", "Y").strip().upper()
    if axis_name not in ("X", "Y"):
        raise gcmd.error("AXIS must be X or Y")
    axis_index = {"X": 0, "Y": 1}[axis_name]
    toolhead_status = toolhead.get_status(eventtime)
    if axis_name.lower() not in toolhead_status.get("homed_axes", ""):
        raise gcmd.error("%s must be homed before FLOWTUNE_PA" % axis_name)

    target = gcmd.get_float("TARGET", 210.0, minval=1.0, maxval=400.0)
    tolerance = gcmd.get_float(
        "TOLERANCE", DEFAULT_TEMPERATURE_TOLERANCE_C,
        above=0.0, maxval=10.0)
    dwell = gcmd.get_float(
        "POST_TARGET_DWELL", DEFAULT_POST_TARGET_DWELL_S,
        minval=0.0, maxval=120.0)
    heat_timeout = gcmd.get_float(
        "HEAT_TIMEOUT", DEFAULT_HEAT_TIMEOUT_S,
        minval=dwell, maxval=600.0)
    accel = gcmd.get_float(
        "ACCEL", DEFAULT_ACCEL_MM_S2, above=0.0, maxval=100000.0)
    smooth_time = gcmd.get_float(
        "SMOOTH_TIME", DEFAULT_SMOOTH_TIME_S,
        minval=0.0, maxval=0.2)
    wobble = gcmd.get_float("WOBBLE", 0.05, above=0.0, maxval=20.0)
    slow_flow = gcmd.get_float(
        "SLOW_FLOW", flowtune_core.FLOWPA_REFERENCE_SLOW_FLOW,
        above=0.0, maxval=100.0)
    fast_flow = gcmd.get_float(
        "FAST_FLOW", flowtune_core.FLOWPA_REFERENCE_FAST_FLOW,
        above=0.0, maxval=100.0)
    slow_time = gcmd.get_float(
        "SLOW_TIME", flowtune_core.FLOWPA_REFERENCE_SLOW_TIME,
        above=0.0, maxval=10.0)
    fast_time = gcmd.get_float(
        "FAST_TIME", flowtune_core.FLOWPA_REFERENCE_FAST_TIME,
        above=0.0, maxval=10.0)
    lead_time = gcmd.get_float(
        "LEAD_TIME", flowtune_core.FLOWPA_REFERENCE_LEAD_TIME,
        above=0.0, maxval=30.0)
    conditioning_cycles = gcmd.get_int(
        "CONDITIONING_CYCLES",
        flowtune_core.FLOWPA_REFERENCE_CONDITIONING_CYCLES,
        minval=0, maxval=20)
    cycles = gcmd.get_int(
        "CYCLES", flowtune_core.FLOWPA_REFERENCE_SCORED_CYCLES,
        minval=1, maxval=40)
    purge_filament = gcmd.get_float(
        "PURGE_LENGTH", flowtune_core.FLOWPA_REFERENCE_PURGE_FILAMENT,
        minval=0.0, maxval=100.0)
    purge_flow = gcmd.get_float(
        "PURGE_FLOW", flowtune_core.FLOWPA_REFERENCE_PURGE_FLOW,
        above=0.0, maxval=50.0)
    default_k_values = ",".join(
        "%.3f" % value
        for value in flowtune_core.FLOWPA_REFERENCE_PRESSURE_ADVANCES)
    try:
        k_values = flowtune_core.parse_pressure_advance_values(
            gcmd.get("K_VALUES", default_k_values))
    except ValueError as error:
        raise gcmd.error(str(error))
    if any(right <= left for left, right in zip(k_values, k_values[1:])):
        raise gcmd.error("FLOWTUNE_PA K_VALUES must be strictly ascending")
    label = gcmd.get("LABEL", "flowpa")

    filament_area = float(extruder.filament_area)
    filament_diameter = 2.0 * (
        filament_area / 3.141592653589793) ** 0.5
    try:
        plan = flowtune_core.plan_pa_sweep(
            pressure_advances=k_values,
            filament_diameter=filament_diameter,
            slow_flow=slow_flow, fast_flow=fast_flow,
            slow_time=slow_time, fast_time=fast_time,
            cycles=cycles, initial_warmup_time=lead_time,
            k_lead_time=lead_time, control_cycles=0, wobble=wobble,
            conditioning_cycles=conditioning_cycles,
            purge_filament=purge_filament, purge_flow=purge_flow)
    except ValueError as error:
        raise gcmd.error(str(error))

    max_extrude_ratio = getattr(extruder, "max_extrude_ratio", None)
    if (max_extrude_ratio is not None
            and plan["maximum_extrude_ratio"] > max_extrude_ratio):
        raise gcmd.error(
            "WOBBLE %.4f requires extrusion ratio %.3f, above this "
            "extruder's configured %.3f; increase WOBBLE" % (
                wobble, plan["maximum_extrude_ratio"], max_extrude_ratio))

    base_position = toolhead.get_position()[axis_index]
    offset_position = base_position + wobble
    axis_minimum = toolhead_status.get("axis_minimum")
    axis_maximum = toolhead_status.get("axis_maximum")
    if axis_minimum is not None and offset_position < axis_minimum[axis_index]:
        raise gcmd.error("planned %s movement is below axis minimum" %
                         axis_name)
    if axis_maximum is not None and offset_position > axis_maximum[axis_index]:
        raise gcmd.error("planned %s movement is above axis maximum" %
                         axis_name)

    probe = flowtune_pa_worker.probe_numpy(
        wait_callback=host._reactor_wait)
    if not probe.get("ok"):
        raise gcmd.error("FlowPA NumPy preflight failed: %s" %
                         probe.get("error", "unknown error"))

    start_eventtime = host.reactor.monotonic()
    sensor, initial_status, sample_rate, sensor_range = (
        host._sensor_details(start_eventtime))
    initial_conditions = host._conditions(start_eventtime)
    source = {
        "load_cell_object": host.load_cell_object,
        "interface": host.resolver.compatibility_path,
        "sensor_class": sensor.__class__.__name__,
        "configured_sample_rate_sps": sample_rate,
        "sensor_range_counts": sensor_range,
    }
    parameters = {
        "target_c": target,
        "temperature_tolerance_c": tolerance,
        "post_target_dwell_s": dwell,
        "temperature_stabilization_mode": "fixed_post_target_dwell",
        "heat_timeout_s": heat_timeout,
        "pressure_advance_values": k_values,
        "pressure_advance_smooth_time_s": smooth_time,
        "toolhead_accel_mm_s2": accel,
        "slow_flow_mm3_s": slow_flow,
        "fast_flow_mm3_s": fast_flow,
        "slow_time_s": slow_time,
        "fast_time_s": fast_time,
        "lead_time_s": lead_time,
        "conditioning_cycles": conditioning_cycles,
        "scored_cycles": cycles,
        "purge_filament_mm": purge_filament,
        "purge_flow_mm3_s": purge_flow,
        "axis": axis_name,
        "wobble_mm": wobble,
        "base_position_mm": base_position,
        "numpy_version": probe.get("numpy_version"),
        "waveform": plan,
    }
    writer, metadata = host._new_writer(
        "pa", label, source, initial_status, initial_conditions, parameters)
    nominal_motion_s = (plan["purge"]["duration_s"]
                        if plan.get("purge") is not None else 0.0)
    nominal_motion_s += sum(
        leg["duration_s"] for leg in plan["extrusion_legs"])
    host._begin_operation(
        "pa", start_eventtime, heat_timeout + nominal_motion_s)
    gcmd.respond_info(
        "FlowTune FlowPA armed: K=%s, %.1f->%.1f mm^3/s, %d+%d "
        "cycles, %.2f mm filament planned; NumPy %s." % (
            ",".join("%.3f" % value for value in k_values),
            slow_flow, fast_flow, conditioning_cycles, cycles,
            plan["total_filament_mm"], probe.get("numpy_version")))

    current_pa = float(extruder_status.get("pressure_advance", 0.0) or 0.0)
    original_smooth_time = float(
        extruder_status.get("smooth_time", 0.0) or 0.0)
    original_accel = float(toolhead_status.get("max_accel"))
    captured = None
    thermal_result = None
    sample_capture_started = False
    pa_changed = False
    accel_changed = False
    heater_restored = False
    returned_to_start = False
    output_path = None
    try:
        # The writer records thermal context immediately, while raw load-cell
        # subscription is delayed until the evidence-bearing purge.
        host.capture.prepare(writer=writer)
        host._write_event(sensor, "artifact_start", {
            "experiment_type": "pa",
            "original_heater_target_c": original_target,
        })
        heaters.set_temperature(heater, target)
        host._write_event(sensor, "heater_target_set", {"target_c": target})
        thermal_result = host._wait_for_target_then_dwell(
            sensor, target, tolerance, dwell, heat_timeout)
        if (not thermal_result["target_reached"]
                or thermal_result["dwell_duration_s"] < dwell):
            raise gcmd.error(
                "hotend did not reach %.1f C within +/-%.1f C and "
                "complete the %.1f s dwell" % (target, tolerance, dwell))
        if not extruder.get_status(
                host.reactor.monotonic()).get("can_extrude", False):
            raise gcmd.error(
                "extruder is not ready after reaching target temperature")

        host.gcode.run_script_from_command(
            "SET_VELOCITY_LIMIT ACCEL=%.6f" % accel)
        accel_changed = True
        host.gcode.run_script_from_command(
            "SET_PRESSURE_ADVANCE ADVANCE=%.6f SMOOTH_TIME=%.6f" %
            (k_values[0], smooth_time))
        pa_changed = True
        host._write_event(sensor, "flowpa_settings_applied", {
            "pressure_advance": k_values[0],
            "smooth_time_s": smooth_time,
            "toolhead_accel_mm_s2": accel,
        })

        sample_start = host.reactor.monotonic()
        host.capture.start_sampling(sample_start)
        sample_capture_started = True
        host._write_event(sensor, "load_cell_capture_start", {
            "phase": "purge_and_waveform",
        })
        host._start_motion_telemetry(sensor)
        host._write_event(sensor, "flowpa_sweep_start", {
            "pressure_advance_values": k_values,
            "conditioning_cycles": conditioning_cycles,
            "scored_cycles": cycles,
        })
        if plan.get("purge") is not None:
            host._run_purge(sensor, toolhead, plan["purge"],
                            "flowpa_purge")
        for block in plan["blocks"]:
            block_pa = block["pressure_advance"]
            host.gcode.run_script_from_command(
                "SET_PRESSURE_ADVANCE ADVANCE=%.6f SMOOTH_TIME=%.6f" %
                (block_pa, smooth_time))
            host._schedule_motion_event(toolhead, "pa_candidate_start", {
                "k_index": block["k_index"],
                "pressure_advance": block_pa,
                "smooth_time_s": smooth_time,
                "lead_time_s": block["lead_time_s"],
                "conditioning_cycles": block["conditioning_cycles"],
                "scored_cycles": cycles,
            })
            host._run_planned_legs(
                sensor, toolhead, axis_index, axis_name,
                base_position, block["legs"],
                "pa_k_%02d" % block["k_index"],
                start_axis_offset=block["start_axis_offset_mm"],
                wait=False)
        toolhead.wait_moves()
        failure = host._motion_capture_failure()
        if failure is not None:
            raise RuntimeError(failure)
        host._write_telemetry(sensor)
        host._write_event(sensor, "flowpa_sweep_complete", {
            "pressure_advance_values": k_values,
        })
        host._stop_motion_telemetry()
        host._write_event(sensor, "load_cell_capture_end", {})
        captured = host.capture.stop(host.reactor.monotonic())
        sample_capture_started = False

        cleanup_coord = [None, None, None, None]
        cleanup_coord[axis_index] = base_position
        toolhead.manual_move(cleanup_coord, wobble / fast_time)
        toolhead.wait_moves()
        returned_to_start = True

        host.gcode.run_script_from_command(
            "SET_PRESSURE_ADVANCE ADVANCE=%.6f SMOOTH_TIME=%.6f" %
            (current_pa, original_smooth_time))
        pa_changed = False
        host.gcode.run_script_from_command(
            "SET_VELOCITY_LIMIT ACCEL=%.6f" % original_accel)
        accel_changed = False
        heaters.set_temperature(heater, original_target)
        heater_restored = True
        host._write_event(sensor, "flowpa_settings_restored", {
            "pressure_advance": current_pa,
            "smooth_time_s": original_smooth_time,
            "toolhead_accel_mm_s2": original_accel,
            "heater_target_c": original_target,
        })
        host._write_telemetry(sensor)
        host._write_event(sensor, "artifact_end", {})

        captured["writer_error"] = host.capture.failure()
        end_eventtime = host.reactor.monotonic()
        acquisition_invalid = bool(
            captured["writer_error"] or captured["errors"]
            or captured["overflows"])
        summary = {
            "status": "invalid" if acquisition_invalid else "complete",
            "eventtime": end_eventtime,
            "print_time": host._sensor_print_time(sensor, end_eventtime),
            "sample_count": captured["sample_count"],
            "errors": captured["errors"],
            "overflows": captured["overflows"],
            "writer_error": captured["writer_error"],
            "sensor_status_after": dict(
                host.load_cell.get_status(end_eventtime)),
            "conditions_after": host._conditions(end_eventtime),
            "thermal": thermal_result,
            "planned_filament_mm": plan["total_filament_mm"],
            "returned_to_start": returned_to_start,
            "experiment_type": "pa",
        }
        output_path = host._finish_writer(writer, summary)
        if captured["writer_error"]:
            raise gcmd.error(captured["writer_error"])

        host.state = "analyzing"
        analysis = flowtune_pa_worker.analyze_capture(
            output_path, wait_callback=host._reactor_wait)
        if not analysis.get("ok"):
            raise gcmd.error("FlowPA analysis failed: %s. Raw capture: %s" %
                             (analysis.get("error", "unknown error"),
                              output_path))
        result = analysis["result"]
        report_path = analysis["report_path"]
        host.last_result = {
            "operation": "pa",
            "status": result["state"],
            "run_id": metadata["run"]["id"],
            "artifact": output_path,
            "report": report_path,
            "recommendation": result["recommendation"],
            "cycle_support": result["fall"]["cycle_support"],
        }
        gcmd.respond_info(_result_message(result, report_path))
    except Exception as error:
        host._stop_motion_telemetry()
        if sample_capture_started or host.capture.active:
            try:
                captured = host.capture.stop(host.reactor.monotonic())
            except Exception:
                logging.exception("FlowTune: failed to stop FlowPA capture")
            sample_capture_started = False
        if not host.printer.is_shutdown():
            if pa_changed:
                try:
                    host.gcode.run_script_from_command(
                        "SET_PRESSURE_ADVANCE ADVANCE=%.6f "
                        "SMOOTH_TIME=%.6f" %
                        (current_pa, original_smooth_time))
                except Exception:
                    logging.exception(
                        "FlowTune: failed to restore pressure advance")
            if accel_changed:
                try:
                    host.gcode.run_script_from_command(
                        "SET_VELOCITY_LIMIT ACCEL=%.6f" % original_accel)
                except Exception:
                    logging.exception(
                        "FlowTune: failed to restore acceleration")
            if not heater_restored:
                try:
                    heaters.set_temperature(heater, original_target)
                except Exception:
                    logging.exception(
                        "FlowTune: failed to restore heater target")
        if writer is not None and not writer.finished:
            try:
                now = host.reactor.monotonic()
                host._finish_writer(writer, {
                    "status": "aborted",
                    "eventtime": now,
                    "print_time": host._sensor_print_time(sensor, now),
                    "sample_count": (0 if captured is None else
                                     captured.get("sample_count", 0)),
                    "errors": (0 if captured is None else
                               captured.get("errors", 0)),
                    "overflows": (0 if captured is None else
                                  captured.get("overflows", 0)),
                    "abort_reason": str(error),
                    "returned_to_start": returned_to_start,
                    "experiment_type": "pa",
                })
            except Exception:
                logging.exception(
                    "FlowTune: failed to finalize aborted FlowPA capture")
        host.last_result = {
            "operation": "pa",
            "status": "error",
            "message": str(error),
            "artifact": output_path,
        }
        raise
    finally:
        host._stop_motion_telemetry()
        host._set_idle()


__all__ = ["run"]
