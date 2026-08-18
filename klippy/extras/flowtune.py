# FlowTune
#
# Copyright (C) 2026 Ahmed Sheikh <ahmed.ali.sheikh1998@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
# SPDX-License-Identifier: GPL-3.0-only

"""Load-cell-guided extrusion calibration and diagnostics for Klipper.

FlowTune verifies the configured load cell, records calibration captures, and
runs the production FlowPA and FlowMax procedures.
"""

from __future__ import division

import datetime
import logging
import os
import time
import uuid

from . import flowtune_core
from . import flowtune_capture
from . import flowtune_e_drip
from . import flowtune_max_flow
from . import flowtune_max_flow_worker
from . import flowtune_pa_command


FLOWTUNE_VERSION = "0.1.0"

SHADOW_MARKER_NAMES = frozenset((
    "max_flow_purge_start",
    "max_flow_purge_end",
    "max_flow_segment_start",
    "max_flow_segment_end",
    "max_flow_recovery_start",
    "max_flow_recovery_end",
    "max_flow_rechallenge_start",
    "max_flow_rechallenge_end",
    "max_flow_final_confirmation_start",
    "max_flow_final_confirmation_end",
    "max_flow_staircase_end",
))

DRIP_GUARD_LEAD_S = 0.10
MAX_FLOW_SHADOW_MESSAGES_PER_PASS = 32

MAX_FLOW_PRODUCTION_DEFAULTS = {
    "start_flow": 10.0,
    "max_test_flow": 50.0,
    "coarse_step": 1.0,
    "fine_step": 0.1,
    "step_length": 15.0,
    "stabilize_time": 20.0,
    "purge_length": 30.0,
    "purge_flow": 12.0,
    "fine_backoff": 0.3,
    "recommendation_margin": 0.5,
    "temperature_tolerance": 1.0,
    "heat_timeout": 240.0,
}


class _MaxFlowControlledAbort(RuntimeError):
    """Expected CONTROL=1 abort that must remain a G-Code error."""


class LoadCellResolver(object):
    """Resolve Klipper LoadCell objects without depending on a sensor chip."""

    REQUIRED_METHODS = ("add_client", "get_sensor", "get_status")

    def __init__(self, printer, object_name):
        self.printer = printer
        self.object_name = object_name
        self.compatibility_path = None

    def _is_load_cell(self, candidate):
        return candidate is not None and all(
            callable(getattr(candidate, method, None))
            for method in self.REQUIRED_METHODS)

    def resolve(self):
        configured = self.printer.lookup_object(self.object_name, None)
        if configured is None:
            raise self.printer.config_error(
                "FlowTune load_cell_object '%s' was not found"
                % (self.object_name,))
        if self._is_load_cell(configured):
            self.compatibility_path = "direct"
            return configured

        accessor = getattr(configured, "get_load_cell", None)
        if callable(accessor):
            load_cell = accessor()
            if self._is_load_cell(load_cell):
                self.compatibility_path = "get_load_cell"
                return load_cell

        # Mainline's current [load_cell_probe] owns a LoadCell but does not
        # expose a public accessor.  Keep this isolated so it can disappear as
        # soon as Klipper provides get_load_cell().
        load_cell = getattr(configured, "_load_cell", None)
        if self._is_load_cell(load_cell):
            self.compatibility_path = "load_cell_probe_private_fallback"
            logging.warning(
                "FlowTune: using [load_cell_probe] compatibility fallback; "
                "a public get_load_cell() accessor is preferred")
            return load_cell

        raise self.printer.config_error(
            "FlowTune object '%s' does not expose Klipper's LoadCell client "
            "interface" % (self.object_name,))


class StreamCapture(object):
    def __init__(self, load_cell):
        self.load_cell = load_cell
        self.writer = None
        self.sample_count = 0
        self.active = False
        self.writer_error = None
        self.start_errors = 0
        self.start_overflows = 0
        self.last_errors = 0
        self.last_overflows = 0
        self.shadow_worker = None
        self.shadow_dropped_batches = 0
        self.shadow_dropped_markers = 0
        self.shadow_error_count = 0
        self.shadow_last_error = None
        self.shadow_disabled = False

    def _status_counts(self, eventtime):
        status = self.load_cell.get_status(eventtime)
        return (int(status.get("errors", 0) or 0),
                int(status.get("overflows", 0) or 0))

    def prepare(self, writer=None, shadow_worker=None):
        """Attach output consumers without subscribing to load-cell data."""
        if self.active:
            raise RuntimeError("capture already active")
        self.writer = writer
        self.sample_count = 0
        self.writer_error = None
        self.shadow_worker = shadow_worker
        self.shadow_dropped_batches = 0
        self.shadow_dropped_markers = 0
        self.shadow_error_count = 0
        self.shadow_last_error = None
        self.shadow_disabled = False

    def start_sampling(self, eventtime):
        """Begin the bounded raw-sample portion of a prepared capture."""
        if self.active:
            raise RuntimeError("capture already active")
        if self.writer_error is not None:
            raise RuntimeError(self.writer_error)
        self.start_errors, self.start_overflows = self._status_counts(eventtime)
        self.last_errors = self.start_errors
        self.last_overflows = self.start_overflows
        self.active = True
        try:
            self.load_cell.add_client(self._on_batch)
        except Exception:
            self.active = False
            raise

    def start(self, eventtime, writer=None, shadow_worker=None):
        self.prepare(writer=writer, shadow_worker=shadow_worker)
        self.start_sampling(eventtime)

    def _on_batch(self, message):
        if not self.active:
            return False
        data = message.get("data") or []
        if self.writer is not None and data:
            if not self.writer.write_samples(data):
                self.writer_error = self.writer.failure()
                self.active = False
                return False
        self.sample_count += len(data)
        if self.shadow_worker is not None and data:
            if self.shadow_disabled:
                self.shadow_dropped_batches += 1
            else:
                try:
                    accepted = self.shadow_worker.submit_samples(
                        data, sent_monotonic=time.monotonic())
                except Exception as error:
                    accepted = False
                    self.shadow_error_count += 1
                    self.shadow_last_error = str(error)
                if not accepted:
                    self.shadow_dropped_batches += 1
                    self.shadow_disabled = True
                    if self.shadow_last_error is None:
                        reasons = getattr(self.shadow_worker,
                                          "parent_failure_reasons", ())
                        self.shadow_last_error = (str(reasons[-1]) if reasons
                                                  else
                                                  "shadow queue rejected "
                                                  "batch")
        self.last_errors = int(message.get("errors", self.last_errors) or 0)
        self.last_overflows = int(
            message.get("overflows", self.last_overflows) or 0)
        return True

    def write_record(self, record_type, eventtime=None, print_time=None,
                     name="", payload=None):
        if self.writer is None:
            return True
        if not self.writer.write_record(
                record_type, eventtime=eventtime, print_time=print_time,
                name=name, payload=payload):
            self.writer_error = self.writer.failure()
            self.active = False
            return False
        return True

    def shadow_marker(self, name, print_time, payload=None):
        """Best-effort, nonblocking marker fan-out beside the raw writer."""
        if (self.shadow_worker is None or
                name not in SHADOW_MARKER_NAMES):
            return True
        if self.shadow_disabled:
            self.shadow_dropped_markers += 1
            return False
        try:
            accepted = self.shadow_worker.submit_marker({
                "name": name,
                "print_time": print_time,
                "payload": payload or {},
            })
        except Exception as error:
            accepted = False
            self.shadow_error_count += 1
            self.shadow_last_error = str(error)
        if not accepted:
            self.shadow_dropped_markers += 1
            self.shadow_disabled = True
            if self.shadow_last_error is None:
                reasons = getattr(self.shadow_worker,
                                  "parent_failure_reasons", ())
                self.shadow_last_error = (str(reasons[-1]) if reasons else
                                          "shadow queue rejected marker")
        return accepted

    def failure(self):
        if self.writer_error is not None:
            return self.writer_error
        if self.writer is not None:
            return self.writer.failure()
        return None

    def shadow_failure_reasons(self):
        reasons = []
        if self.shadow_dropped_batches:
            reasons.append("shadow dropped %d batch(es)" %
                           self.shadow_dropped_batches)
        if self.shadow_dropped_markers:
            reasons.append("shadow dropped %d marker(s)" %
                           self.shadow_dropped_markers)
        if self.shadow_error_count:
            reasons.append("shadow callback raised %d error(s)" %
                           self.shadow_error_count)
        if self.shadow_last_error:
            reasons.append("shadow fan-out failed: %s" %
                           self.shadow_last_error)
        return reasons

    def stop(self, eventtime):
        self.active = False
        status_errors, status_overflows = self._status_counts(eventtime)
        self.last_errors = max(self.last_errors, status_errors)
        self.last_overflows = max(self.last_overflows, status_overflows)
        return {
            "sample_count": self.sample_count,
            "errors": max(0, self.last_errors - self.start_errors),
            "overflows": max(0,
                             self.last_overflows - self.start_overflows),
            "writer_error": self.failure(),
            "shadow_dropped_batches": self.shadow_dropped_batches,
            "shadow_dropped_markers": self.shadow_dropped_markers,
            "shadow_error_count": self.shadow_error_count,
            "shadow_last_error": self.shadow_last_error,
        }


class FlowTune(object):
    cmd_FLOWTUNE_STATUS_help = "Report FlowTune load-cell integration status"
    cmd_FLOWTUNE_SENSOR_CHECK_help = (
        "Verify the configured load cell with a stationary capture")
    cmd_FLOWTUNE_THERMAL_CHECK_help = (
        "Diagnostic: capture load-cell drift through a bounded hotend "
        "heating cycle")
    cmd_FLOWTUNE_PA_help = (
        "Measure and recommend scalar pressure advance from one bounded sweep")
    cmd_FLOWTUNE_MAX_FLOW_help = (
        "Estimate maximum volumetric flow using the configured load cell")

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.load_cell_object = config.get("load_cell_object",
                                           "load_cell_probe")
        self.output_dir = os.path.abspath(os.path.expanduser(
            config.get("output_dir")))
        self.default_capture_duration = config.getfloat(
            "capture_duration", 2.0, minval=0.5, maxval=30.0)
        self.minimum_rate_ratio = config.getfloat(
            "minimum_sample_rate_ratio", 0.90,
            above=0.0, maxval=1.0)
        self.maximum_gap_intervals = config.getfloat(
            "maximum_gap_intervals", 1.5, above=1.0, maxval=20.0)
        self.writer_queue_batches = config.getint(
            "writer_queue_batches", 16, minval=4, maxval=128)
        self.load_cell = None
        self.resolver = LoadCellResolver(self.printer,
                                         self.load_cell_object)
        self.capture = None
        self.state = "initializing"
        self.active_operation = None
        self.operation_started = None
        self.operation_duration = None
        self.last_result = None
        self._motion_telemetry_timer = None
        self._motion_telemetry_error = None
        self._shadow_worker = None
        self._shadow_reporter = None
        self._shadow_last_status = None
        self._shadow_last_report_eventtime = None
        self._shadow_worker_error_count = 0
        self._shadow_decision_tracker = None
        self._shadow_semantic_reporting = False
        self._shadow_search_controller = None
        self._shadow_search_events = []
        self._shadow_search_recoveries = []
        self._shadow_search_outcomes = []
        self._active_e_drip = None
        self._max_flow_drip_timings = []
        self._shadow_poll_message_limit = None

        self.gcode.register_command(
            "FLOWTUNE_STATUS", self.cmd_FLOWTUNE_STATUS,
            desc=self.cmd_FLOWTUNE_STATUS_help)
        self.gcode.register_command(
            "FLOWTUNE_SENSOR_CHECK", self.cmd_FLOWTUNE_SENSOR_CHECK,
            desc=self.cmd_FLOWTUNE_SENSOR_CHECK_help)
        self.gcode.register_command(
            "FLOWTUNE_THERMAL_CHECK", self.cmd_FLOWTUNE_THERMAL_CHECK,
            desc=self.cmd_FLOWTUNE_THERMAL_CHECK_help)
        self.gcode.register_command(
            "FLOWTUNE_PA", self.cmd_FLOWTUNE_PA,
            desc=self.cmd_FLOWTUNE_PA_help)
        self.gcode.register_command(
            "FLOWTUNE_MAX_FLOW", self.cmd_FLOWTUNE_MAX_FLOW,
            desc=self.cmd_FLOWTUNE_MAX_FLOW_help)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _handle_ready(self):
        self.load_cell = self.resolver.resolve()
        self.capture = StreamCapture(self.load_cell)
        self.state = "idle"

    def get_status(self, eventtime):
        progress = None
        if (self.active_operation is not None
                and self.operation_started is not None
                and self.operation_duration):
            elapsed = max(0.0, eventtime - self.operation_started)
            progress = min(1.0, elapsed / self.operation_duration)
        return {
            "state": self.state,
            "active_operation": self.active_operation,
            "progress": progress,
            "load_cell_object": self.load_cell_object,
            "load_cell_interface": self.resolver.compatibility_path,
            "shadow_status": (None if getattr(
                self, "_shadow_last_status", None) is None else
                dict(self._shadow_last_status)),
            "last_result": (None if self.last_result is None
                            else dict(self.last_result)),
        }

    def _begin_operation(self, operation, eventtime, duration=None):
        self.state = "collecting"
        self.active_operation = operation
        self.operation_started = eventtime
        self.operation_duration = duration

    def _set_idle(self):
        self.state = "idle"
        self.active_operation = None
        self.operation_started = None
        self.operation_duration = None

    def _require_ready(self, gcmd):
        if self.load_cell is None or self.capture is None:
            raise gcmd.error("FlowTune is not ready")
        if self.printer.is_shutdown():
            raise gcmd.error("FlowTune cannot run while Klipper is shutdown")

    def _require_idle(self, gcmd):
        print_stats = self.printer.lookup_object("print_stats", None)
        if print_stats is None:
            return
        state = print_stats.get_status(self.reactor.monotonic()).get("state")
        if state in ("printing", "paused"):
            raise gcmd.error(
                "FlowTune cannot run a capture operation during a print")

    def _sensor_details(self, eventtime):
        sensor = self.load_cell.get_sensor()
        status = dict(self.load_cell.get_status(eventtime))
        sample_rate = status.get("sample_rate")
        if sample_rate is None:
            sample_rate = sensor.get_samples_per_second()
        sensor_range = None
        get_range = getattr(sensor, "get_range", None)
        if callable(get_range):
            sensor_range = list(get_range())
        return sensor, status, float(sample_rate), sensor_range

    def cmd_FLOWTUNE_STATUS(self, gcmd):
        self._require_ready(gcmd)
        _sensor, status, sample_rate, _sensor_range = self._sensor_details(
            self.reactor.monotonic())
        gcmd.respond_info(
            "FlowTune %s; load_cell_object=%s; "
            "sample_rate=%.1f SPS; calibrated=%s; errors=%s; overflows=%s"
            % (self.state, self.load_cell_object, sample_rate,
               status.get("is_calibrated", "unknown"),
               status.get("errors", "unknown"),
               status.get("overflows", "unknown")))

    def _wait_for_capture(self, duration):
        deadline = self.reactor.monotonic() + duration
        while True:
            now = self.reactor.monotonic()
            self._poll_shadow(now)
            failure = self.capture.failure()
            if failure is not None:
                raise RuntimeError(failure)
            if now >= deadline:
                return now
            if self.printer.is_shutdown():
                return now
            self.reactor.pause(min(deadline, now + 0.10))

    def _reactor_wait(self, delay):
        now = self.reactor.monotonic()
        self.reactor.pause(now + max(0.0, float(delay)))

    def _record_shadow_decision(self, event):
        tracker = getattr(self, "_shadow_decision_tracker", None)
        if tracker is None:
            return None
        decision = tracker.observe_event(event)
        if decision is None:
            return None
        if not self.capture.write_record(
                "event", eventtime=self.reactor.monotonic(),
                print_time=event.get("source_print_time"),
                name="max_flow_shadow_would_act", payload=decision):
            raise RuntimeError(self.capture.failure())
        reporter = getattr(self, "_shadow_reporter", None)
        if reporter is not None:
            reporter(
                "FlowTune shadow would-act: q_failure=%s q_last_good=%s "
                "backoff_target=%s fine_step=%s proposal_only=1" % (
                    decision["q_failure_mm3_s"],
                    decision["q_last_good_mm3_s"],
                    decision["proposed_backoff_flow_mm3_s"],
                    decision["fine_step_mm3_s"]))
        return decision

    def _poll_shadow(self, eventtime, force=False):
        worker = getattr(self, "_shadow_worker", None)
        reporter = getattr(self, "_shadow_reporter", None)
        if worker is None:
            return
        message_limit = getattr(self, "_shadow_poll_message_limit", None)
        messages = (worker.drain() if message_limit is None else
                    worker.drain(max_messages=message_limit))
        for message in messages:
            message_type = message.get("type")
            if message_type == "status":
                self._shadow_last_status = message
                previous = self._shadow_last_report_eventtime
                should_report = (force or previous is None or
                                 eventtime - previous >= 1.0)
                semantic = bool(getattr(self, "_shadow_semantic_reporting",
                                        False))
                if should_report and reporter is not None and not semantic:
                    capture = self.capture
                    status = self._shadow_last_status
                    sensor_errors = (0 if capture is None else max(
                        0, capture.last_errors - capture.start_errors))
                    overflows = (0 if capture is None else max(
                        0, capture.last_overflows -
                        capture.start_overflows))
                    reporter(
                        "FlowTune shadow: samples=%s detector=%s "
                        "latency=%ss backlog=%s age=%ss drops=%s/%s "
                        "sensor_errors=%s overflows=%s worker_errors=%s "
                        "candidates=%s confirmed=%s" % (
                            status.get("sample_count", 0),
                            status.get("detector_state", "--"),
                            status.get("processing_latency_s", "--"),
                            status.get("backlog", "--"),
                            status.get("data_age_s", "--"),
                            (0 if capture is None else
                             capture.shadow_dropped_batches),
                            (0 if capture is None else
                             capture.shadow_dropped_markers),
                            sensor_errors,
                            overflows,
                            self._shadow_worker_error_count,
                            status.get("candidate_count", 0),
                            status.get("confirmed_count", 0)))
                    self._shadow_last_report_eventtime = eventtime
            elif message_type in ("candidate", "confirmed"):
                event = message.get("event", {})
                search_controller = getattr(self, "_shadow_search_controller",
                                            None)
                if (message_type == "confirmed" and
                        search_controller is not None):
                    self._shadow_search_events.append(event)
                if reporter is not None and not getattr(
                        self, "_shadow_semantic_reporting", False):
                    reporter(
                        "FlowTune shadow %s: flow=%s segment=%s print_time=%s"
                        % (message_type,
                           event.get("flow_mm3_s"),
                           event.get("segment_index"),
                           event.get("source_print_time")))
                if message_type == "confirmed":
                    self._record_shadow_decision(event)
            elif message_type == "recovery":
                if hasattr(self, "_shadow_search_recoveries"):
                    self._shadow_search_recoveries.append(dict(message))
            elif message_type == "segment_outcome":
                if hasattr(self, "_shadow_search_outcomes"):
                    self._shadow_search_outcomes.append(dict(message))
            elif message_type == "error":
                self._shadow_worker_error_count += 1
                if reporter is not None:
                    reporter("FlowTune shadow error: %s" %
                             message.get("error", "worker error"))

    def _finish_shadow(self, eventtime=None):
        worker = self._shadow_worker
        if worker is None:
            return None
        if eventtime is None:
            eventtime = self.reactor.monotonic()
        try:
            self._poll_shadow(eventtime, force=True)
            failures = ([] if self.capture is None else
                        self.capture.shadow_failure_reasons())
            summary = worker.finish(
                extra_failure_reasons=failures,
                wait_callback=self._reactor_wait)
            self._poll_shadow(self.reactor.monotonic(), force=True)
            tracker = self._shadow_decision_tracker
            if tracker is not None and tracker.decision is None:
                for event in summary.get("events", ()):
                    if event.get("type") == "release_confirmed":
                        self._record_shadow_decision(event)
                        break
            summary["decision"] = (None if tracker is None
                                    else tracker.decision)
            return summary
        finally:
            if worker.process.is_alive():
                worker.terminate()
            self._shadow_worker = None
            self._shadow_decision_tracker = None

    def _provenance(self):
        start_args = self.printer.get_start_args()
        return {
            "flowtune_version": FLOWTUNE_VERSION,
            "klipper_start_args": {
                "software_version": start_args.get("software_version"),
            },
        }

    def _conditions(self, eventtime):
        conditions = {}
        extruder = self.printer.lookup_object("extruder", None)
        if extruder is not None:
            status = extruder.get_status(eventtime)
            conditions["extruder"] = {
                key: status[key]
                for key in ("temperature", "target", "power", "can_extrude")
                if key in status
            }
        return conditions

    def _capture_identity(self, experiment_type):
        created_utc = (datetime.datetime.utcnow().replace(microsecond=0)
                       .isoformat() + "Z")
        run_id = str(uuid.uuid4())
        stamp = created_utc.replace(":", "").replace("-", "")
        stamp = stamp.replace("T", "-").replace("Z", "")
        filename = "flowtune-%s-%s-%s.csv" % (
            experiment_type.replace("_", "-"), stamp, run_id[:8])
        return run_id, created_utc, os.path.join(self.output_dir, filename)

    def _new_writer(self, experiment_type, label, source, sensor_status,
                    conditions, parameters):
        run_id, created_utc, output_path = self._capture_identity(
            experiment_type)
        metadata = {
            "schema": {"id": "flowtune.capture.csv", "version": 1},
            "run": {
                "id": run_id,
                "created_utc": created_utc,
                "experiment_type": experiment_type,
                "label": label,
            },
            "source": source,
            "sensor_status_before": sensor_status,
            "conditions_before": conditions,
            "parameters": parameters,
            "validation": {
                "minimum_sample_rate_ratio": self.minimum_rate_ratio,
                "maximum_gap_intervals": self.maximum_gap_intervals,
            },
            "provenance": self._provenance(),
        }
        writer = flowtune_capture.CaptureWriter(
            output_path, metadata, queue_batches=self.writer_queue_batches)
        writer.start()
        return writer, metadata

    def _finish_writer(self, writer, summary):
        if writer is None:
            return None
        self.state = "writing"

        def reactor_wait(delay):
            now = self.reactor.monotonic()
            self.reactor.pause(now + delay)

        return writer.finish(summary, wait_callback=reactor_wait)

    def _sensor_print_time(self, sensor, eventtime):
        get_mcu = getattr(sensor, "get_mcu", None)
        if not callable(get_mcu):
            return None
        mcu = get_mcu()
        estimate = getattr(mcu, "estimated_print_time", None)
        if not callable(estimate):
            return None
        return estimate(eventtime)

    def _thermal_timeline_row(self, sensor, eventtime):
        extruder = self._conditions(eventtime).get("extruder", {})
        return [
            eventtime,
            self._sensor_print_time(sensor, eventtime),
            extruder.get("temperature"),
            extruder.get("target"),
            extruder.get("power"),
        ]

    def _wait_for_thermal_stability(self, sensor, target, tolerance,
                                    stable_duration, timeout):
        start = self.reactor.monotonic()
        deadline = start + timeout
        stable_since = None
        stable_start_print_time = None
        while True:
            now = self.reactor.monotonic()
            self._poll_shadow(now)
            row = self._thermal_timeline_row(sensor, now)
            if not self.capture.write_record(
                    "telemetry", eventtime=row[0], print_time=row[1],
                    name="extruder", payload={
                        "temperature_c": row[2],
                        "target_c": row[3],
                        "power": row[4],
                    }):
                raise RuntimeError(self.capture.failure())
            temperature = row[2]
            if temperature is not None:
                previous = stable_since
                stable_since = flowtune_core.update_temperature_stability(
                    stable_since, now, temperature, target, tolerance)
                if stable_since is not None and previous is None:
                    stable_start_print_time = row[1]
                elif stable_since is None:
                    stable_start_print_time = None
            if stable_since is not None:
                self.state = "stabilizing"
                if now - stable_since >= stable_duration:
                    return {
                        "stable_reached": True,
                        "stable_start_eventtime": stable_since,
                        "stable_start_print_time": stable_start_print_time,
                        "end_eventtime": now,
                        "elapsed_s": now - start,
                    }
            else:
                self.state = "heating"
            if self.printer.is_shutdown() or now >= deadline:
                return {
                    "stable_reached": False,
                    "stable_start_eventtime": None,
                    "stable_start_print_time": None,
                    "end_eventtime": now,
                    "elapsed_s": now - start,
                }
            self.reactor.pause(min(deadline, now + 0.50))

    def _wait_for_target_then_dwell(self, sensor, target, tolerance,
                                    dwell_duration, timeout):
        """Reach target once, then wait a fixed non-resetting dwell."""
        start = self.reactor.monotonic()
        deadline = start + timeout
        target_reached_at = None
        target_reached_print_time = None
        last_stability_report = -1
        while True:
            now = self.reactor.monotonic()
            self._poll_shadow(now)
            row = self._thermal_timeline_row(sensor, now)
            if not self.capture.write_record(
                    "telemetry", eventtime=row[0], print_time=row[1],
                    name="extruder", payload={
                        "temperature_c": row[2],
                        "target_c": row[3],
                        "power": row[4],
                    }):
                raise RuntimeError(self.capture.failure())
            temperature = row[2]
            if (target_reached_at is None and temperature is not None
                    and abs(float(temperature) - target) <= tolerance):
                target_reached_at = now
                target_reached_print_time = row[1]
            if target_reached_at is not None:
                self.state = "dwelling"
                if (getattr(self, "_shadow_semantic_reporting", False) and
                        getattr(self, "_shadow_reporter", None) is not None):
                    elapsed = min(dwell_duration,
                                  max(0.0, now - target_reached_at))
                    report_step = int(elapsed // 5.0)
                    if report_step != last_stability_report:
                        last_stability_report = report_step
                        self._shadow_reporter(
                            "FlowTune max-flow: stabilization %.0f/%.0f s."
                            % (elapsed, dwell_duration))
                if now - target_reached_at >= dwell_duration:
                    return {
                        "target_reached": True,
                        "target_reached_eventtime": target_reached_at,
                        "target_reached_print_time": (
                            target_reached_print_time),
                        "dwell_duration_s": dwell_duration,
                        "end_eventtime": now,
                        "elapsed_s": now - start,
                        "mode": "fixed_post_target_dwell",
                    }
            else:
                self.state = "heating"
            if self.printer.is_shutdown() or now >= deadline:
                return {
                    "target_reached": target_reached_at is not None,
                    "target_reached_eventtime": target_reached_at,
                    "target_reached_print_time": target_reached_print_time,
                    "dwell_duration_s": (
                        0.0 if target_reached_at is None else
                        max(0.0, now - target_reached_at)),
                    "end_eventtime": now,
                    "elapsed_s": now - start,
                    "mode": "fixed_post_target_dwell",
                }
            self.reactor.pause(min(deadline, now + 0.50))

    def _write_event(self, sensor, name, payload=None, print_time=None):
        eventtime = self.reactor.monotonic()
        if print_time is None:
            print_time = self._sensor_print_time(sensor, eventtime)
        if not self.capture.write_record(
                "event", eventtime=eventtime, print_time=print_time,
                name=name, payload=payload or {}):
            raise RuntimeError(self.capture.failure())
        tracker = getattr(self, "_shadow_decision_tracker", None)
        if tracker is not None and name in SHADOW_MARKER_NAMES:
            tracker.observe_marker({"name": name,
                                    "print_time": print_time,
                                    "payload": payload or {}})
        shadow_marker = getattr(self.capture, "shadow_marker", None)
        if callable(shadow_marker):
            shadow_marker(name, print_time, payload or {})

    def _write_telemetry(self, sensor):
        row = self._thermal_timeline_row(sensor, self.reactor.monotonic())
        if not self.capture.write_record(
                "telemetry", eventtime=row[0], print_time=row[1],
                name="extruder", payload={
                    "temperature_c": row[2],
                    "target_c": row[3],
                    "power": row[4],
                }):
            raise RuntimeError(self.capture.failure())

    def _start_motion_telemetry(self, sensor, period=0.50):
        """Record heater state during queued motion without adding waits."""
        self._motion_telemetry_error = None
        register_timer = getattr(self.reactor, "register_timer", None)
        if not callable(register_timer):
            # This is useful for minimal test reactors.  Real Klipper reactors
            # always provide register_timer; motion remains valid without the
            # optional periodic context in a deliberately small fake.
            return

        def callback(eventtime):
            if not self.capture.active:
                return getattr(self.reactor, "NEVER", 9999999999999999.)
            try:
                self._poll_shadow(eventtime)
                self._write_telemetry(sensor)
            except Exception as error:
                self._motion_telemetry_error = str(error)
                return getattr(self.reactor, "NEVER", 9999999999999999.)
            return eventtime + period

        self._motion_telemetry_timer = register_timer(
            callback, self.reactor.monotonic() + period)

    def _stop_motion_telemetry(self):
        timer = self._motion_telemetry_timer
        self._motion_telemetry_timer = None
        if timer is None:
            return
        unregister_timer = getattr(self.reactor, "unregister_timer", None)
        if callable(unregister_timer):
            unregister_timer(timer)

    def _motion_capture_failure(self):
        return (getattr(self, "_motion_telemetry_error", None)
                or self.capture.failure())

    def _wait_recorded_phase(self, sensor, name, duration):
        self._write_event(sensor, name + "_start", {"duration_s": duration})
        deadline = self.reactor.monotonic() + duration
        while True:
            now = self.reactor.monotonic()
            failure = self.capture.failure()
            if failure is not None:
                raise RuntimeError(failure)
            self._write_telemetry(sensor)
            if self.printer.is_shutdown():
                raise RuntimeError("Klipper shutdown during %s" % name)
            if now >= deadline:
                break
            self.reactor.pause(min(deadline, now + 0.50))
        self._write_event(sensor, name + "_end", {"duration_s": duration})

    def _schedule_motion_event(self, toolhead, name, payload):
        def record(print_time):
            eventtime = self.reactor.monotonic()
            if not self.capture.write_record(
                    "event", eventtime=eventtime, print_time=print_time,
                    name=name, payload=payload):
                raise RuntimeError(self.capture.failure())
            tracker = getattr(self, "_shadow_decision_tracker", None)
            if tracker is not None and name in SHADOW_MARKER_NAMES:
                tracker.observe_marker({"name": name,
                                        "print_time": print_time,
                                        "payload": payload})
            shadow_marker = getattr(self.capture, "shadow_marker", None)
            if callable(shadow_marker):
                shadow_marker(name, print_time, payload)
        toolhead.register_lookahead_callback(record)

    def _run_planned_legs(self, sensor, toolhead, axis_index, axis_name,
                          base_position, legs, sequence_name,
                          start_axis_offset=0.0, wait=True):
        e_position = toolhead.get_position()[3]
        previous_offset = start_axis_offset
        for index, leg in enumerate(legs):
            target = base_position + leg["axis_offset_mm"]
            e_position += leg["filament_mm"]
            payload = dict(leg)
            payload.update({
                "sequence": sequence_name,
                "leg_index": index,
                "axis": axis_name,
                "axis_target_mm": target,
                "extruder_target_mm": e_position,
            })
            self._schedule_motion_event(
                toolhead, "motion_leg_start", payload)
            coord = [None, None, None, None]
            coord[axis_index] = target
            if leg["filament_mm"]:
                coord[3] = e_position
            axis_distance = abs(leg["axis_offset_mm"] - previous_offset)
            if not axis_distance:
                raise RuntimeError(
                    "%s leg %d has no carrier-axis movement"
                    % (sequence_name, index))
            carrier_speed = axis_distance / leg["duration_s"]
            toolhead.manual_move(coord, carrier_speed)
            previous_offset = leg["axis_offset_mm"]
        self._schedule_motion_event(
            toolhead, sequence_name + "_end", {
                "leg_count": len(legs),
                "axis": axis_name,
            })
        if wait:
            toolhead.wait_moves()
            failure = self.capture.failure()
            if failure is not None:
                raise RuntimeError(failure)
            self._write_telemetry(sensor)

    def _run_purge(self, sensor, toolhead, purge,
                   sequence_name="pa_purge"):
        """Queue one recorded pure-E purge and wait for its completion."""
        e_position = toolhead.get_position()[3]
        e_position += purge["filament_mm"]
        payload = dict(purge)
        payload.update({
            "sequence": sequence_name,
            "phase": "purge",
            "cycle": 0,
            "extruder_target_mm": e_position,
        })
        self._schedule_motion_event(toolhead, "motion_leg_start", payload)
        coord = [None, None, None, e_position]
        toolhead.manual_move(coord, purge["feed_mm_s"])
        self._schedule_motion_event(toolhead, sequence_name + "_end", {
            "filament_mm": purge["filament_mm"],
            "volumetric_flow_mm3_s": purge["volumetric_flow_mm3_s"],
            "duration_s": purge["duration_s"],
        })
        toolhead.wait_moves()
        failure = self.capture.failure()
        if failure is not None:
            raise RuntimeError(failure)
        self._write_telemetry(sensor)

    def _run_max_flow_purge(self, sensor, toolhead, purge):
        """Queue and complete the marked pure-E conditioning purge."""
        e_position = toolhead.get_position()[3]
        target_e = e_position + purge["filament_mm"]
        payload = dict(purge)
        payload.update({
            "sequence": "max_flow_purge",
            "phase": "purge",
            "segment_index": None,
            "starting_e_mm": e_position,
            "start_e_mm": e_position,
            "target_e_mm": target_e,
        })
        self._schedule_motion_event(
            toolhead, "max_flow_purge_start", payload)
        toolhead.manual_move([None, None, None, target_e],
                             purge["feed_mm_s"])
        self._schedule_motion_event(toolhead, "max_flow_purge_end", {
            "sequence": "max_flow_purge",
            "filament_mm": purge["filament_mm"],
            "volumetric_flow_mm3_s": purge["volumetric_flow_mm3_s"],
            "duration_s": purge["duration_s"],
            "starting_e_mm": e_position,
            "start_e_mm": e_position,
            "target_e_mm": target_e,
        })
        toolhead.wait_moves()
        failure = self._motion_capture_failure()
        if failure is not None:
            raise RuntimeError(failure)
        self._write_telemetry(sensor)

    def _run_max_flow_staircase(self, sensor, toolhead, plan,
                                wait=True):
        """Queue marked consecutive pure-E segments and optionally wait once."""
        e_position = toolhead.get_position()[3]
        for segment in plan["segments"]:
            start_e = e_position
            e_position += segment["filament_mm"]
            payload = dict(segment)
            payload.update({
                "sequence": "max_flow_staircase",
                "phase": "flow_segment",
                "starting_e_mm": start_e,
                "start_e_mm": start_e,
                "target_e_mm": e_position,
            })
            self._schedule_motion_event(
                toolhead, "max_flow_segment_start", payload)
            toolhead.manual_move([None, None, None, e_position],
                                 segment["feed_mm_s"])
            # A failed writer must stop scheduling additional segments.  The
            # normal lookahead queue still controls motion timing; this check
            # only protects the bounded diagnostic from a known acquisition
            # failure.
            failure = self._motion_capture_failure()
            if failure is not None:
                raise RuntimeError(failure)
        self._schedule_motion_event(toolhead, "max_flow_staircase_end", {
            "sequence": "max_flow_staircase",
            "segment_count": plan["segment_count"],
            "flow_values_mm3_s": plan["flow_values_mm3_s"],
            "max_test_flow_mm3_s": plan.get(
                "max_test_flow_mm3_s", plan.get("end_flow_mm3_s")),
        })
        if wait:
            toolhead.wait_moves()
            failure = self._motion_capture_failure()
            if failure is not None:
                raise RuntimeError(failure)
            self._write_telemetry(sensor)

    def _drip_marker_prefix(self, stage):
        return ("max_flow_recovery" if "recovery" in str(stage)
                else "max_flow_segment")

    def _write_drip_segment_marker(self, sensor, entry, suffix,
                                   print_time, interrupted=False):
        request = entry["request"]
        segment = entry["segment"]
        stage = request["stage"]
        payload = dict(segment)
        payload.update({
            "sequence": "max_flow_controlled_search",
            "stage": stage,
            "phase": ("recovery" if "recovery" in stage else
                      "flow_segment"),
            "flow_mm3_s": request["flow_mm3_s"],
            "segment_index": request["segment_index"],
            "speculative": bool(request.get("speculative", False)),
            "starting_e_mm": entry["start_e"],
            "start_e_mm": entry["start_e"],
            "target_e_mm": entry["end_e"],
            "planned_start_print_time": entry["start_time"],
            "planned_end_print_time": entry["end_time"],
            "interrupted": bool(interrupted),
        })
        self._write_event(
            sensor, self._drip_marker_prefix(stage) + "_" + suffix,
            payload, print_time=print_time)

    def _emit_committed_drip_markers(self, sensor, drip):
        """Publish markers only after their boundary is irrevocably queued."""
        reporter = getattr(self, "_shadow_reporter", None)
        semantic = bool(getattr(self, "_shadow_semantic_reporting", False))
        for entry in drip.entries:
            if (not entry["start_marker_emitted"] and
                    entry["start_time"] <= drip.flush_time + 1.0e-12):
                self._write_drip_segment_marker(
                    sensor, entry, "start", entry["start_time"])
                entry["start_marker_emitted"] = True
                if reporter and semantic:
                    request = entry["request"]
                    if "recovery" in request["stage"]:
                        reporter(
                            "FlowTune max-flow: backing down to %.1f "
                            "mm^3/s for recovery."
                            % request["flow_mm3_s"])
                    elif request["stage"] == "fine_repeat":
                        reporter(
                            "FlowTune max-flow: repeating fine approach "
                            "at %.1f mm^3/s."
                            % request["flow_mm3_s"])
                    else:
                        reporter(
                            "FlowTune max-flow: testing %.1f mm^3/s."
                            % request["flow_mm3_s"])
            if (not entry["end_marker_emitted"] and
                    entry["end_time"] <= drip.flush_time + 1.0e-12):
                self._write_drip_segment_marker(
                    sensor, entry, "end", entry["end_time"])
                entry["end_marker_emitted"] = True

    def _active_drip_entry(self, drip, print_time):
        for entry in drip.entries:
            if (entry["start_time"] - 1.0e-12 <= print_time <=
                    entry["end_time"] + 1.0e-12):
                return entry
        return drip.entries[-1] if drip.entries else None

    def _max_flow_motion_shutdown(self, drip=None):
        printer = getattr(self, "printer", None)
        is_shutdown = getattr(printer, "is_shutdown", None)
        if callable(is_shutdown) and is_shutdown():
            return True
        motion_queuing = getattr(drip, "motion_queuing", None)
        return (motion_queuing is not None and
                getattr(motion_queuing, "can_pause", True) is False)

    def _record_max_flow_drip_timing(self, drip):
        if getattr(drip, "_flowtune_timing_recorded", False):
            return
        timing_summary = getattr(drip, "timing_summary", None)
        if not callable(timing_summary):
            return
        timings = getattr(self, "_max_flow_drip_timings", None)
        if timings is None:
            timings = []
            self._max_flow_drip_timings = timings
        timings.append(timing_summary())
        drip._flowtune_timing_recorded = True

    def _max_flow_drip_timing_summary(self):
        sessions = list(getattr(self, "_max_flow_drip_timings", ()))
        if not sessions:
            return None
        minimum_leads = [
            row.get("minimum_committed_lead_s") for row in sessions
            if row.get("minimum_committed_lead_s") is not None]
        return {
            "session_count": len(sessions),
            "low_water_s": sessions[0].get("low_water_s"),
            "high_water_s": sessions[0].get("high_water_s"),
            "step_chunk_s": sessions[0].get("step_chunk_s"),
            "minimum_committed_lead_s": (
                min(minimum_leads) if minimum_leads else None),
            "maximum_refill_gap_s": max(
                row.get("maximum_refill_gap_s", 0.0)
                for row in sessions),
            "refill_count": sum(
                row.get("refill_count", 0) for row in sessions),
            "late_refill_count": sum(
                row.get("late_refill_count", 0) for row in sessions),
            "sessions": sessions,
        }

    def _run_max_flow_drip_session(self, sensor, toolhead, extruder, gcmd,
                                   filament_diameter, segment_length,
                                   controller):
        """Run one continuous search path until interruption or completion."""
        try:
            acceleration = float(extruder.max_e_accel)
        except (AttributeError, TypeError, ValueError, OverflowError):
            raise RuntimeError(
                "E-drip requires a finite configured extruder acceleration")
        if acceleration <= 0.0:
            raise RuntimeError(
                "E-drip requires a positive configured extruder acceleration")
        request = controller.next_request()
        if request is None:
            return {"interrupted": False, "reason": None}

        drip = flowtune_e_drip.EDripQueue(toolhead, extruder)
        drip.begin()
        self._active_e_drip = drip
        completion = self.reactor.completion()
        in_flight = {}
        confirmed_event = None
        failure_reason = None
        final_outcome_deadline = None
        reporter = getattr(self, "_shadow_reporter", None)
        semantic = bool(getattr(self, "_shadow_semantic_reporting", False))

        def queue_request(request, final=False):
            segment = flowtune_core.plan_max_flow_segment(
                filament_diameter, request["flow_mm3_s"], segment_length,
                segment_index=request["segment_index"],
                stage=request["stage"],
                speculative=request.get("speculative", False))
            self._validate_max_flow_segment_limits(segment, extruder, gcmd)
            entry = drip.append(
                segment, acceleration, stop_at_end=bool(final))
            entry["request"] = request
            entry["segment"] = segment
            in_flight[int(request["segment_index"])] = entry
            self._write_event(sensor, "max_flow_search_state", {
                "state": controller.state,
                "stage": request["stage"],
                "flow_mm3_s": request["flow_mm3_s"],
                "segment_index": request["segment_index"],
                "speculative": bool(request.get("speculative", False)),
                "executor": "e_drip",
            })
            return entry

        def append_lookahead(request):
            if "recovery" in request["stage"]:
                lookahead = controller.recovery_lookahead_request(request)
            else:
                lookahead = controller.lookahead_request(request)
            if lookahead is None:
                return None
            final = (lookahead["flow_mm3_s"] >=
                     controller.max_test_flow_mm3_s - 1.0e-9)
            return queue_request(lookahead, final=final)

        queue_request(
            request,
            final=(request["flow_mm3_s"] >=
                   controller.max_test_flow_mm3_s - 1.0e-9))
        append_lookahead(request)
        drip.enter_drip_mode()
        self._emit_committed_drip_markers(sensor, drip)

        while not controller.terminal:
            if self._max_flow_motion_shutdown(drip):
                failure_reason = "Klipper shutdown during E-drip motion"
                completion.complete(failure_reason)
                break
            # Protect the MCU step queue before doing any IPC, detector,
            # controller, marker, or capture work.  Klipper's native drip loop
            # can use a smaller horizon because it performs no such work
            # between waking and generating its next step chunk.
            advanced = drip.advance_once(completion)
            if self._max_flow_motion_shutdown(drip):
                failure_reason = "Klipper shutdown during E-drip motion"
                completion.complete(failure_reason)
                break
            self._emit_committed_drip_markers(sensor, drip)

            now = self.reactor.monotonic()
            self._poll_shadow(now, force=True)
            capture_failure = self._motion_capture_failure()
            shadow_failures = self.capture.shadow_failure_reasons()
            if capture_failure is not None or shadow_failures:
                failure_reason = (capture_failure or
                                  "; ".join(shadow_failures))
                completion.complete(failure_reason)
                break

            events = self._shadow_search_events
            self._shadow_search_events = []
            candidates = [event for event in events
                          if event.get("type") == "release_confirmed"
                          and event.get("candidate_segment_index") is not None
                          and int(event["candidate_segment_index"])
                          in in_flight]
            if candidates:
                confirmed_event = min(
                    candidates,
                    key=lambda row: float(row.get(
                        "candidate_print_time",
                        row.get("source_print_time", 0.0))))
                completion.complete(confirmed_event)
                break

            recoveries = self._shadow_search_recoveries
            self._shadow_search_recoveries = []
            for recovery in recoveries:
                recovery_entries = [entry for entry in in_flight.values()
                                    if "recovery" in
                                    entry["request"]["stage"]]
                if not recovery_entries:
                    continue
                recovery_entry = recovery_entries[0]
                recovery_request = recovery_entry["request"]
                controller.observe_recovery(
                    load_reengaged=recovery.get("load_reengaged", False),
                    rebuild_detected=recovery.get("rebuild_detected", False),
                    release_signature=recovery.get(
                        "release_signature", False),
                    details=recovery)
                self._write_event(sensor, "max_flow_search_decision",
                                  controller.decisions[-1])
                in_flight.pop(
                    int(recovery_request["segment_index"]), None)
                if controller.terminal:
                    completion.complete(controller.result)
                    break
                promoted = controller.next_request()
                promoted_entry = in_flight.get(
                    int(promoted["segment_index"]))
                if promoted_entry is None:
                    failure_reason = (
                        "post-recovery E-drip guard was not queued")
                    completion.complete(failure_reason)
                    break
                promoted_entry["request"].update(promoted)
                promoted_entry["segment"]["speculative"] = False
                append_lookahead(promoted_entry["request"])

            if completion.test() or controller.terminal:
                break

            outcomes = self._shadow_search_outcomes
            self._shadow_search_outcomes = []
            for outcome in outcomes:
                index = int(outcome["segment_index"])
                entry = in_flight.get(index)
                if entry is None or "recovery" in entry["request"]["stage"]:
                    continue
                request = entry["request"]
                if outcome.get("failure", False):
                    confirmed_event = {
                        "type": "release_confirmed",
                        "candidate_segment_index": index,
                        "candidate_flow_mm3_s": request["flow_mm3_s"],
                        "source_print_time": entry["end_time"],
                    }
                    completion.complete(confirmed_event)
                    break
                controller.observe_segment(
                    request["flow_mm3_s"], failure=False,
                    segment_index=index,
                    speculative=request.get("speculative", False))
                self._write_event(sensor, "max_flow_search_decision",
                                  controller.decisions[-1])
                in_flight.pop(index, None)
                if controller.terminal:
                    completion.complete(controller.result)
                    break
                adjacent = min(
                    (row for row in in_flight.values()
                     if row["request"].get("speculative", False)),
                    key=lambda row: row["request"]["segment_index"],
                    default=None)
                if adjacent is None:
                    failure_reason = "E-drip search guard was not queued"
                    completion.complete(failure_reason)
                    break
                promoted = controller.promote_lookahead(
                    adjacent["request"]["segment_index"])
                adjacent["request"].update(promoted)
                adjacent["segment"]["speculative"] = False
                append_lookahead(adjacent["request"])

            if completion.test() or controller.terminal:
                break

            if not advanced and not completion.test():
                est_print_time = drip.motion_queuing.mcu.estimated_print_time(
                    self.reactor.monotonic())
                final_cap_queued = any(
                    entry["request"]["flow_mm3_s"] >=
                    controller.max_test_flow_mm3_s - 1.0e-9
                    for entry in in_flight.values())
                at_planned_end = (
                    drip.flush_time >= drip.planned_end_time - 1.0e-12)
                if final_cap_queued and at_planned_end:
                    # The cap has no following guard to extend.  Motion may
                    # finish just before the worker returns its final clean
                    # segment outcome, so wait for that verdict instead of
                    # treating the natural end of the path as starvation.
                    now = self.reactor.monotonic()
                    if final_outcome_deadline is None:
                        final_outcome_deadline = now + 3.0
                    if now >= final_outcome_deadline:
                        failure_reason = (
                            "timed out waiting for final max-flow worker "
                            "segment outcome")
                        completion.complete(failure_reason)
                        break
                    completion.wait(now + 0.01)
                elif est_print_time >= (
                        drip.planned_end_time - DRIP_GUARD_LEAD_S):
                    failure_reason = (
                        "max-flow analysis did not extend the E-drip path "
                        "before its committed guard ended")
                    completion.complete(failure_reason)
                    break
                else:
                    completion.wait(self.reactor.monotonic() + 0.01)

        interrupted = bool(
            confirmed_event is not None or failure_reason is not None or
            (completion.test() and
             drip.flush_time < drip.planned_end_time - 1.0e-9))
        motion_shutdown = self._max_flow_motion_shutdown(drip)
        try:
            stop = drip.finish(interrupted=interrupted)
        finally:
            self._record_max_flow_drip_timing(drip)
            if getattr(self, "_active_e_drip", None) is drip:
                self._active_e_drip = None
        if not motion_shutdown:
            self._emit_committed_drip_markers(sensor, drip)

        if interrupted:
            active = self._active_drip_entry(
                drip, stop["stop_print_time"])
            if (not motion_shutdown and active is not None and
                    not active["end_marker_emitted"]):
                self._write_drip_segment_marker(
                    sensor, active, "end", stop["stop_print_time"],
                    interrupted=True)
                active["end_marker_emitted"] = True
            self._write_event(sensor, "max_flow_drip_interruption", {
                "reason": (failure_reason or
                           ("confirmed_release_rebuild"
                            if confirmed_event is not None else
                            "controller_terminal_before_planned_end")),
                "candidate_segment_index": (
                    None if confirmed_event is None else
                    confirmed_event.get("candidate_segment_index")),
                "candidate_flow_mm3_s": (
                    None if confirmed_event is None else
                    confirmed_event.get("candidate_flow_mm3_s")),
                "stop_print_time": stop["stop_print_time"],
                "planned_end_print_time": stop[
                    "planned_end_print_time"],
                "actual_e_mm": stop["actual_e_mm"],
                "position_known": not motion_shutdown,
                "motion_shutdown": motion_shutdown,
                "executor": "e_drip",
            }, print_time=stop["stop_print_time"])
            # The marker must reach the worker before samples cross the stop
            # boundary.  Only then wait for the already committed MCU motion
            # to drain before starting recovery.
            if not motion_shutdown:
                toolhead.wait_moves()

        if failure_reason is not None:
            controller.abort(failure_reason)
            self._write_event(sensor, "max_flow_search_decision",
                              controller.decisions[-1])
            raise _MaxFlowControlledAbort(failure_reason)

        if confirmed_event is not None:
            index = int(confirmed_event["candidate_segment_index"])
            entry = in_flight.get(index)
            if entry is None:
                reason = "confirmed E-drip event had no active segment"
                controller.abort(reason)
                raise _MaxFlowControlledAbort(reason)
            request = entry["request"]
            if request.get("speculative", False):
                # A confirmation may arrive just after the preceding clean
                # boundary while its worker outcome is still in transit.
                # Resolve that predecessor before allowing the guard flow to
                # define a boundary.
                deadline = self.reactor.monotonic() + 1.0
                while (request.get("speculative", False) and
                       self.reactor.monotonic() < deadline):
                    self._poll_shadow(self.reactor.monotonic(), force=True)
                    outcomes = self._shadow_search_outcomes
                    self._shadow_search_outcomes = []
                    for outcome in outcomes:
                        prior_index = int(outcome["segment_index"])
                        prior = in_flight.get(prior_index)
                        if prior is None or prior_index >= index:
                            continue
                        prior_request = prior["request"]
                        if outcome.get("failure", False):
                            confirmed_event = dict(confirmed_event)
                            confirmed_event["candidate_segment_index"] = (
                                prior_index)
                            confirmed_event["candidate_flow_mm3_s"] = (
                                prior_request["flow_mm3_s"])
                            index = prior_index
                            entry = prior
                            request = prior_request
                            break
                        controller.observe_segment(
                            prior_request["flow_mm3_s"], failure=False,
                            segment_index=prior_index,
                            speculative=prior_request.get(
                                "speculative", False))
                        in_flight.pop(prior_index, None)
                        if index in controller.speculative_segments:
                            promoted = controller.promote_lookahead(index)
                            request.update(promoted)
                    if request.get("speculative", False):
                        self._reactor_wait(0.02)
                if request.get("speculative", False):
                    reason = (
                        "confirmed E-drip guard lacked a resolved clean "
                        "predecessor")
                    controller.abort(reason)
                    raise _MaxFlowControlledAbort(reason)
            controller.cancel_pending(
                [pending_index for pending_index in in_flight
                 if pending_index != index],
                reason="e_drip_interrupted_after_confirmation")
            controller.observe_segment(
                request["flow_mm3_s"], failure=True,
                segment_index=index, speculative=False)
            self._write_event(sensor, "max_flow_search_decision",
                              controller.decisions[-1])
            if reporter and semantic:
                reporter(
                    "FlowTune max-flow limit incident: confirmed release "
                    "and rebuild at %.1f mm^3/s. Backing down immediately."
                    % request["flow_mm3_s"])
        return {
            "interrupted": bool(confirmed_event is not None),
            "reason": None,
            "stop": stop,
        }

    def _run_max_flow_controlled_search_drip(
            self, sensor, toolhead, filament_diameter, segment_length,
            controller, extruder, gcmd):
        """Run CONTROL=1 with continuous, interruptible pure-E generation."""
        self._shadow_search_events = []
        self._shadow_search_recoveries = []
        self._shadow_search_outcomes = []
        previous_message_limit = getattr(
            self, "_shadow_poll_message_limit", None)
        self._shadow_poll_message_limit = MAX_FLOW_SHADOW_MESSAGES_PER_PASS
        try:
            while not controller.terminal:
                self._run_max_flow_drip_session(
                    sensor, toolhead, extruder, gcmd, filament_diameter,
                    segment_length, controller)
        except Exception as error:
            if not controller.terminal:
                controller.abort(
                    "controlled E-drip execution failed: %s" % error)
                try:
                    self._write_event(
                        sensor, "max_flow_search_decision",
                        controller.decisions[-1])
                except Exception:
                    logging.exception(
                        "FlowTune: failed to record E-drip abort decision")
            # A validation, marker, worker, or reactor exception must not
            # leave Klipper's background motion flusher disabled.  The queue
            # cleanup is intentionally local to the narrow E-drip adapter.
            drip = getattr(self, "_active_e_drip", None)
            if drip is not None and drip.active:
                try:
                    try:
                        drip.finish(interrupted=True)
                    finally:
                        self._record_max_flow_drip_timing(drip)
                    if not self._max_flow_motion_shutdown(drip):
                        toolhead.wait_moves()
                except Exception:
                    logging.exception(
                        "FlowTune: failed to clean up interrupted E-drip")
            self._active_e_drip = None
            self._shadow_poll_message_limit = previous_message_limit
            raise
        try:
            toolhead.wait_moves()
            self._poll_shadow(self.reactor.monotonic(), force=True)
            self._write_event(sensor, "max_flow_staircase_end", {
                "sequence": "max_flow_controlled_search",
                "executor": "e_drip",
                "state": controller.state,
                "segment_count": controller.commanded_segment_count,
            })
            return controller.summary()
        finally:
            self._shadow_poll_message_limit = previous_message_limit

    def _validate_max_flow_limits(self, plan, extruder, gcmd):
        """Check pure-E limits when the active Klipper extruder exposes them."""
        max_distance = getattr(extruder, "max_e_dist", None)
        if max_distance is not None:
            try:
                max_distance = float(max_distance)
            except (TypeError, ValueError, OverflowError):
                max_distance = None
        if (max_distance is not None
                and plan["maximum_distance_mm"] > max_distance):
            raise gcmd.error(
                "maximum pure-E distance %.3f mm exceeds configured %.3f mm"
                % (plan["maximum_distance_mm"], max_distance))

        max_feed = getattr(extruder, "max_e_velocity", None)
        if max_feed is not None:
            try:
                max_feed = float(max_feed)
            except (TypeError, ValueError, OverflowError):
                max_feed = None
        if max_feed is not None and plan["maximum_feed_mm_s"] > max_feed:
            raise gcmd.error(
                "requested pure-E feed %.3f mm/s exceeds configured %.3f "
                "mm/s" % (plan["maximum_feed_mm_s"], max_feed))

    def _validate_max_flow_segment_limits(self, segment, extruder, gcmd):
        """Validate only the next controlled segment being requested.

        Automatic searches intentionally do not prevalidate the unreachable
        endpoint feed or cumulative material range.  Klipper's ordinary
        per-move limits still apply when a concrete flow is requested.
        """
        max_distance = getattr(extruder, "max_e_dist", None)
        if max_distance is not None:
            try:
                max_distance = float(max_distance)
            except (TypeError, ValueError, OverflowError):
                max_distance = None
        if (max_distance is not None and
                segment["filament_mm"] > max_distance):
            raise gcmd.error(
                "maximum pure-E segment distance %.3f mm exceeds "
                "configured %.3f mm" %
                (segment["filament_mm"], max_distance))

        max_feed = getattr(extruder, "max_e_velocity", None)
        if max_feed is not None:
            try:
                max_feed = float(max_feed)
            except (TypeError, ValueError, OverflowError):
                max_feed = None
        if max_feed is not None and segment["feed_mm_s"] > max_feed:
            raise gcmd.error(
                "requested pure-E feed %.3f mm/s exceeds configured %.3f "
                "mm/s" % (segment["feed_mm_s"], max_feed))

    def _disable_extruder_stepper(self, extruder):
        """Release only the active extruder motor after a max-flow run."""
        extruder_name = extruder.get_name()
        if not extruder_name:
            raise RuntimeError("active extruder has no stepper name")
        self.gcode.run_script_from_command(
            "SET_STEPPER_ENABLE STEPPER=%s ENABLE=0" % extruder_name)

    def cmd_FLOWTUNE_MAX_FLOW(self, gcmd):
        """Run the production-facing automatic maximum-flow search."""
        return self._run_max_flow_command(gcmd, production=True)

    def _max_flow_float(self, gcmd, parameters_seen, name, legacy_name,
                        default, **constraints):
        """Read one canonical max-flow parameter with a legacy alias."""
        return self._aliased_float(
            gcmd, parameters_seen, name, legacy_name, default,
            **constraints)

    def _aliased_float(self, gcmd, parameters_seen, name, legacy_name,
                       default, **constraints):
        """Read one canonical float parameter with an optional old name."""
        has_name = name in parameters_seen
        has_legacy = (legacy_name is not None and
                      legacy_name in parameters_seen)
        if has_name and has_legacy:
            raise gcmd.error("%s and %s cannot both be supplied"
                             % (name, legacy_name))
        selected = name if has_name or not has_legacy else legacy_name
        return gcmd.get_float(selected, default, **constraints)

    def _compose_max_flow_result_validity(self, search_summary, captured,
                                          shadow_summary, motion_scheduling):
        """Compose search-policy validity with acquisition and worker health.

        The search controller's ``validity`` only describes the fine-boundary
        protocol.  A controller-valid boundary must not be presented when
        acquisition, writer, live-worker, marker, sensor-counter, or
        scheduling evidence is inadequate.
        """
        controller_validity = (search_summary or {}).get("validity")
        captured = captured or {}
        failures = []
        errors = int(captured.get("errors", 0) or 0)
        overflows = int(captured.get("overflows", 0) or 0)
        if errors:
            failures.append(
                "load-cell reported %d error(s) during acquisition"
                % errors)
        if overflows:
            failures.append(
                "load-cell reported %d overflow(s) during acquisition"
                % overflows)
        writer_error = captured.get("writer_error")
        if writer_error:
            failures.append("capture writer failed: %s" % writer_error)
        if shadow_summary is not None:
            if not shadow_summary.get("valid", False):
                reasons = shadow_summary.get("failure_reasons") or ()
                failures.append(
                    "live detector worker invalid%s" % (
                        ": %s" % "; ".join(str(reason)
                                           for reason in reasons)
                        if reasons else ""))
        if motion_scheduling:
            late = int(motion_scheduling.get("late_refill_count", 0) or 0)
            if late:
                failures.append(
                    "E-drip scheduling had %d late refill(s)" % late)
        # Evidence health takes precedence over every controller outcome.
        # In particular, a sensor or worker failure makes a claimed
        # ``no_limit_within_range`` endpoint just as untrustworthy as a
        # repeated boundary recommendation.
        if failures:
            return "invalid", failures
        return controller_validity, failures

    def _production_max_flow_result_message(self, search_summary,
                                            result_validity,
                                            validity_failures,
                                            max_test_flow, output_path):
        """Format the concise operator-facing terminal result."""
        q_failure = search_summary.get("q_failure_mm3_s")
        q_recommended = search_summary.get("q_recommended_mm3_s")
        if result_validity == "valid" and q_recommended is not None:
            return (
                "FlowTune maximum-flow test complete: estimated failure "
                "boundary %.1f mm^3/s; recommended maximum %.1f mm^3/s.\n"
                "Raw capture: %s"
                % (q_failure, q_recommended, output_path))
        if result_validity == "no_limit_within_range":
            return (
                "FlowTune maximum-flow test complete: no limit was detected "
                "through %.1f mm^3/s, so no recommendation was produced.\n"
                "Raw capture: %s" % (max_test_flow, output_path))
        lines = [
            "FlowTune maximum-flow test complete but no valid "
            "recommendation was produced (result: %s)."
            % (result_validity or "unknown")]
        if validity_failures:
            lines.append("Evidence failures: %s"
                         % "; ".join(str(reason)
                                     for reason in validity_failures))
        lines.append("Raw capture: %s" % output_path)
        return "\n".join(lines)

    def _run_max_flow_command(self, gcmd, production=False):
        """Run either the production search or development capture surface."""
        self._require_ready(gcmd)
        self._require_idle(gcmd)
        if self.active_operation is not None or self.capture.active:
            raise gcmd.error("A FlowTune operation is already active")
        parameters_seen = getattr(gcmd, "get_command_parameters", None)
        parameters_seen = (parameters_seen() if callable(parameters_seen)
                           else {})
        control_supplied = "CONTROL" in parameters_seen
        if production and control_supplied:
            raise gcmd.error(
                "FLOWTUNE_MAX_FLOW is always automatic; do not supply "
                "CONTROL")
        control = (1 if production else
                   gcmd.get_int("CONTROL", 0, minval=0, maxval=1))
        controlled = control == 1
        report_only = (not production and control_supplied and control == 0)
        if production and "TARGET" not in parameters_seen:
            raise gcmd.error(
                "FLOWTUNE_MAX_FLOW requires TARGET for the filament test")
        if controlled and not hasattr(self, "printer"):
            # Keep a clear unit-test/configuration error before attempting any
            # hardware lookup; this is not the production CONTROL=1 gate.
            raise gcmd.error("CONTROL=1 requires an initialized printer")

        toolhead = self.printer.lookup_object("toolhead")
        extruder = toolhead.get_extruder()
        if extruder is None:
            raise gcmd.error(
                "%s requires an active extruder" % (
                    "FLOWTUNE_MAX_FLOW" if production else
                    "FLOWTUNE_MAX_FLOW_CAPTURE"))
        heater = extruder.get_heater()
        heaters = self.printer.lookup_object("heaters")
        eventtime = self.reactor.monotonic()
        extruder_status = extruder.get_status(eventtime)
        original_target = float(extruder_status.get("target", 0.) or 0.)
        if original_target:
            raise gcmd.error(
                "%s must start with the hotend target off" % (
                    "FLOWTUNE_MAX_FLOW" if production else
                    "FLOWTUNE_MAX_FLOW_CAPTURE"))

        target = gcmd.get_float("TARGET", 210.0, minval=1.0, maxval=400.0)
        tolerance = gcmd.get_float(
            "TOLERANCE",
            (MAX_FLOW_PRODUCTION_DEFAULTS["temperature_tolerance"]
             if controlled else 1.0),
            above=0.0, maxval=10.0)
        hot_stable = self._max_flow_float(
            gcmd, parameters_seen, "STABILIZE_TIME", "HOT_STABLE",
            (MAX_FLOW_PRODUCTION_DEFAULTS["stabilize_time"]
             if controlled else 20.0),
            minval=1.0, maxval=120.0)
        heat_timeout = gcmd.get_float(
            "HEAT_TIMEOUT",
            (MAX_FLOW_PRODUCTION_DEFAULTS["heat_timeout"]
             if controlled else 240.0),
            minval=hot_stable, maxval=600.0)
        start_flow = gcmd.get_float(
            "START_FLOW", (MAX_FLOW_PRODUCTION_DEFAULTS["start_flow"]
                           if controlled else 8.0),
            above=0.0, maxval=500.0)
        has_max_test_flow = "MAX_TEST_FLOW" in parameters_seen
        has_end_flow = "END_FLOW" in parameters_seen
        if has_max_test_flow and has_end_flow:
            raise gcmd.error(
                "MAX_TEST_FLOW and END_FLOW cannot both be supplied")
        endpoint_name = ("MAX_TEST_FLOW" if has_max_test_flow
                         else "END_FLOW" if has_end_flow else None)
        if controlled:
            max_test_flow = (
                MAX_FLOW_PRODUCTION_DEFAULTS["max_test_flow"]
                if endpoint_name is None else
                gcmd.get_float(
                    endpoint_name,
                    MAX_FLOW_PRODUCTION_DEFAULTS["max_test_flow"],
                    above=0.0,
                    maxval=500.0))
        else:
            if endpoint_name is None:
                raise gcmd.error(
                    "CONTROL=0 requires MAX_TEST_FLOW or END_FLOW")
            max_test_flow = gcmd.get_float(
                endpoint_name, None, above=0.0, maxval=500.0)
        end_flow = max_test_flow
        flow_step = self._max_flow_float(
            gcmd, parameters_seen, "COARSE_STEP", "FLOW_STEP",
            (MAX_FLOW_PRODUCTION_DEFAULTS["coarse_step"]
             if controlled else 2.0), above=0.0, maxval=100.0)
        step_length = self._max_flow_float(
            gcmd, parameters_seen, "STEP_LENGTH", "SEGMENT_LENGTH",
            (MAX_FLOW_PRODUCTION_DEFAULTS["step_length"]
             if controlled else 20.0), above=0.0, maxval=50.0)
        purge_length = gcmd.get_float(
            "PURGE_LENGTH", (MAX_FLOW_PRODUCTION_DEFAULTS["purge_length"]
                             if controlled else 30.0),
            minval=0.0, maxval=100.0)
        purge_flow = gcmd.get_float(
            "PURGE_FLOW", (MAX_FLOW_PRODUCTION_DEFAULTS["purge_flow"]
                           if controlled else 12.0),
            above=0.0, maxval=100.0)
        coarse_backoff = self._max_flow_float(
            gcmd, parameters_seen, "COARSE_BACKOFF", "BACKOFF",
            flow_step if controlled else 1.0,
            above=0.0, maxval=100.0)
        fine_step = gcmd.get_float(
            "FINE_STEP", (MAX_FLOW_PRODUCTION_DEFAULTS["fine_step"]
                          if controlled else 0.1),
            above=0.0, maxval=100.0)
        fine_backoff = gcmd.get_float(
            "FINE_BACKOFF", (MAX_FLOW_PRODUCTION_DEFAULTS["fine_backoff"]
                             if controlled else 0.3),
            above=0.0, maxval=100.0)
        recommendation_margin = gcmd.get_float(
            "RECOMMENDATION_MARGIN", None, above=0.0, maxval=100.0)
        if recommendation_margin is None:
            recommendation_margin = gcmd.get_float(
                "MARGIN",
                (MAX_FLOW_PRODUCTION_DEFAULTS["recommendation_margin"]
                 if controlled else 0.5),
                above=0.0, maxval=100.0)
        if controlled and fine_step >= flow_step:
            raise gcmd.error(
                "FINE_STEP must be smaller than the coarse flow step")
        if controlled and recommendation_margin >= start_flow:
            raise gcmd.error(
                "RECOMMENDATION_MARGIN must be smaller than START_FLOW")
        if controlled and fine_backoff >= flow_step:
            raise gcmd.error(
                "FINE_BACKOFF must be smaller than the coarse flow step")
        if controlled and coarse_backoff < flow_step:
            raise gcmd.error(
                "COARSE_BACKOFF must be at least the coarse flow step")
        if controlled and fine_backoff < fine_step:
            raise gcmd.error(
                "FINE_BACKOFF must be at least FINE_STEP")
        effective_backoff = coarse_backoff
        label = gcmd.get(
            "LABEL", "max_flow" if production else "max_flow_capture")
        filament_diameter = float(
            extruder.filament_area * 4.0 /
            3.141592653589793) ** 0.5
        try:
            if controlled:
                # CONTROL=1 is lazy by construction: only static setup and
                # the optional purge are planned here.  Each search flow is
                # converted into one bounded segment at request time.
                plan = flowtune_core.plan_controlled_max_flow_setup(
                    filament_diameter=filament_diameter,
                    start_flow=start_flow,
                    max_test_flow=max_test_flow,
                    coarse_step=flow_step, fine_step=fine_step,
                    segment_length=step_length,
                    purge_length=purge_length, purge_flow=purge_flow)
                controlled_budget = (
                    flowtune_core.plan_controlled_max_flow_budget(
                        filament_diameter=filament_diameter,
                        start_flow=start_flow,
                        max_test_flow=max_test_flow,
                        coarse_step=flow_step, fine_step=fine_step,
                        segment_length=step_length,
                        purge_length=purge_length,
                        purge_flow=purge_flow))
            else:
                plan = flowtune_core.plan_max_flow_capture(
                    filament_diameter=filament_diameter,
                    start_flow=start_flow, end_flow=max_test_flow,
                    flow_step=flow_step, segment_length=step_length,
                    purge_length=purge_length, purge_flow=purge_flow)
                controlled_budget = None
        except ValueError as error:
            raise gcmd.error(str(error))
        self._validate_max_flow_limits(plan, extruder, gcmd)

        current_pa = float(extruder_status.get("pressure_advance", 0.) or 0.)
        smooth_time = float(extruder_status.get("smooth_time", 0.) or 0.)
        start_eventtime = self.reactor.monotonic()
        sensor, initial_status, sample_rate, sensor_range = (
            self._sensor_details(start_eventtime))
        initial_conditions = self._conditions(start_eventtime)
        source = {
            "load_cell_object": self.load_cell_object,
            "interface": self.resolver.compatibility_path,
            "sensor_class": sensor.__class__.__name__,
            "configured_sample_rate_sps": sample_rate,
            "sensor_range_counts": sensor_range,
        }
        parameters = {
            "target_c": target,
            "temperature_tolerance_c": tolerance,
            "hot_stable_duration_s": hot_stable,
            "temperature_stabilization_mode": "fixed_post_target_dwell",
            "heat_timeout_s": heat_timeout,
            "pressure_advance": current_pa,
            "pressure_advance_smooth_time": smooth_time,
            "original_heater_target_c": original_target,
            "control_mode": ("automatic" if controlled else
                             "report_only" if report_only else "legacy"),
            "report_only": report_only,
            "automatic_control": controlled,
            "decision_inputs": {
                "backoff_mm3_s": effective_backoff,
                "coarse_recovery_backoff_mm3_s": coarse_backoff,
                "recommendation_margin_mm3_s": recommendation_margin,
                "fine_step_mm3_s": fine_step,
                "fine_recovery_backoff_mm3_s": fine_backoff,
                "proposal_only": not controlled,
            },
            "search_policy": ({
                "start_flow_mm3_s": start_flow,
                "max_test_flow_mm3_s": max_test_flow,
                "end_flow_mm3_s": max_test_flow,
                "coarse_step_mm3_s": flow_step,
                "fine_step_mm3_s": fine_step,
                "fine_recovery_backoff_mm3_s": fine_backoff,
                "recommendation_margin_mm3_s": recommendation_margin,
                "lookahead_segments": 1,
                "lazy_generation": True,
                "step_length_mm": step_length,
                # Retained in the artifact schema for existing analyzers.
                "segment_length_mm": step_length,
                "material_time_budget": controlled_budget,
            } if controlled else None),
            "staircase": plan,
        }
        writer, metadata = self._new_writer(
            "max_flow_capture", label, source, initial_status,
            initial_conditions, parameters)
        motion_duration = (plan["nominal_duration_s"] if not controlled else
                           None)
        fixed_duration = (None if controlled else
                          heat_timeout + motion_duration + 2.0)
        operation_name = "max_flow" if production else "max_flow_capture"
        self._begin_operation(operation_name, start_eventtime, fixed_duration)
        if production:
            gcmd.respond_info(
                "FlowTune maximum-flow test armed: %.1f->%.1f mm^3/s, "
                "%.1f mm filament per step."
                % (start_flow, max_test_flow, step_length))
        else:
            gcmd.respond_info(
                "FlowTune maximum-flow diagnostic armed: %.1f->%.1f "
                "mm^3/s; %s generation."
                % (start_flow, max_test_flow,
                   ("lazy controlled" if controlled else "fixed")))

        captured = None
        thermal_result = None
        shadow_worker = None
        shadow_summary = None
        if report_only or controlled:
            shadow_worker = flowtune_max_flow_worker.MaxFlowShadowWorker(
                queue_size=(max(16, plan["segment_count"] + 16)
                            if report_only else
                            flowtune_max_flow_worker.DEFAULT_INPUT_QUEUE_SIZE))
            self._shadow_worker = shadow_worker
            self._shadow_reporter = gcmd.respond_info
            self._shadow_semantic_reporting = controlled
            self._shadow_last_status = None
            self._shadow_last_report_eventtime = None
            self._shadow_worker_error_count = 0
            self._shadow_decision_tracker = (
                flowtune_max_flow.MaxFlowDecisionTracker(
                    backoff_mm3_s=coarse_backoff,
                    fine_step_mm3_s=fine_step)
                if report_only else None)
            self._shadow_search_controller = (
                flowtune_max_flow.MaxFlowSearchController(
                    start_flow=start_flow, end_flow=end_flow,
                    coarse_step=flow_step, fine_step=fine_step,
                    recommendation_margin=recommendation_margin,
                    fine_recovery_backoff=fine_backoff,
                    coarse_recovery_backoff=coarse_backoff)
                if controlled else None)
            if report_only:
                gcmd.respond_info(
                    "MAX-FLOW REPORT-ONLY; NO AUTOMATIC MOTION CHANGES. "
                    "CONTROL=0; proposed BACKOFF=%.3f, FINE_STEP=%.3f."
                    % (coarse_backoff, fine_step))
            else:
                gcmd.respond_info(
                    "FlowTune max-flow automatic search: heating to %.1f C."
                    % target)
        pa_changed = False
        heater_restored = False
        extruder_stepper_disabled = False
        capture_started = False
        self._max_flow_drip_timings = []
        try:
            if shadow_worker is not None and shadow_worker.start(
                    wait_callback=self._reactor_wait) is None:
                raise gcmd.error("FlowTune max-flow shadow worker did not start")
            # Start capture before heating so the raw artifact includes the
            # complete thermal approach and exact target/dwell context.
            self.capture.start(start_eventtime, writer=writer,
                               shadow_worker=shadow_worker)
            capture_started = True
            self._write_event(sensor, "capture_start", {
                "experiment_type": "max_flow_capture",
                "original_heater_target_c": original_target,
            })
            heaters.set_temperature(heater, target)
            self._write_event(sensor, "heater_target_set", {
                "target_c": target,
            })
            if controlled:
                gcmd.respond_info(
                    "FlowTune max-flow: heating to %.1f C (target %.1f C)."
                    % (target, target))
            thermal_result = self._wait_for_target_then_dwell(
                sensor, target, tolerance, hot_stable, heat_timeout)
            if (not thermal_result["target_reached"]
                    or thermal_result["dwell_duration_s"] < hot_stable):
                raise gcmd.error(
                    "hotend did not reach %.1f C within +/-%.1f C and "
                    "complete the %.1f s dwell"
                    % (target, tolerance, hot_stable))
            if not extruder.get_status(
                    self.reactor.monotonic()).get("can_extrude", False):
                raise gcmd.error(
                    "extruder is not ready after reaching target temperature")
            self._write_event(sensor, "hot_stable_complete", {
                "target_c": target,
                "mode": "fixed_post_target_dwell",
                "dwell_duration_s": hot_stable,
            })
            if controlled:
                gcmd.respond_info(
                    "FlowTune max-flow: stabilization complete at %.1f C "
                    "for %.1f s. Beginning flow test."
                    % (target, hot_stable))

            pa_changed = True
            self.gcode.run_script_from_command(
                "SET_PRESSURE_ADVANCE ADVANCE=0.000000 "
                "SMOOTH_TIME=%.17g" % smooth_time)
            self._write_event(sensor, "pa_settings_applied", {
                "pressure_advance": 0.0,
                "smooth_time_s": smooth_time,
                "restored_pressure_advance": current_pa,
            })
            self._start_motion_telemetry(sensor)
            self._write_event(sensor, "max_flow_staircase_start", {
                "flow_values_mm3_s": plan["flow_values_mm3_s"],
                "segment_count": plan["segment_count"],
                "segment_length_mm": plan["segment_length_mm"],
            })
            motion_start_e = float(toolhead.get_position()[3])
            if plan["purge"]["filament_mm"] > 0.0:
                self._run_max_flow_purge(sensor, toolhead, plan["purge"])
            else:
                self._write_event(sensor, "max_flow_purge_skipped", {
                    "filament_mm": 0.0,
                    "volumetric_flow_mm3_s": plan["purge_flow_mm3_s"],
                })
            if controlled:
                self._run_max_flow_controlled_search_drip(
                    sensor, toolhead, filament_diameter,
                    step_length, self._shadow_search_controller,
                    extruder=extruder, gcmd=gcmd)
            else:
                self._run_max_flow_staircase(sensor, toolhead, plan, wait=True)
            self._stop_motion_telemetry()
            actual_segment_count = (
                plan["segment_count"] if not controlled else
                self._shadow_search_controller.commanded_segment_count)
            actual_filament = max(
                0.0, float(toolhead.get_position()[3]) - motion_start_e)
            self._write_event(sensor, "max_flow_capture_motion_complete", {
                "segment_count": actual_segment_count,
                "commanded_filament_mm": actual_filament,
            })

            self.gcode.run_script_from_command(
                "SET_PRESSURE_ADVANCE ADVANCE=%.17g SMOOTH_TIME=%.17g"
                % (current_pa, smooth_time))
            pa_changed = False
            self._write_event(sensor, "pa_settings_restored", {
                "pressure_advance": current_pa,
                "smooth_time_s": smooth_time,
            })
            heaters.set_temperature(heater, original_target)
            heater_restored = True
            self._write_event(sensor, "heater_target_restored", {
                "target_c": original_target,
            })
            self._disable_extruder_stepper(extruder)
            extruder_stepper_disabled = True
            self._write_event(sensor, "extruder_stepper_disabled", {
                "extruder": extruder.get_name(),
            })
            self._write_telemetry(sensor)
            self._write_event(sensor, "capture_end", {})
            captured = self.capture.stop(self.reactor.monotonic())
            capture_started = False
            if shadow_worker is not None:
                shadow_summary = self._finish_shadow(
                    self.reactor.monotonic())
                captured["shadow"] = shadow_summary
            search_summary = (None if not controlled else
                              self._shadow_search_controller.summary())
            if self.printer.is_shutdown():
                raise gcmd.error(
                    "Klipper shutdown during FlowTune maximum-flow capture")
            captured["writer_error"] = self.capture.failure()
            end_eventtime = self.reactor.monotonic()
            motion_scheduling = (
                self._max_flow_drip_timing_summary()
                if controlled else None)
            result_validity, validity_failures = (
                self._compose_max_flow_result_validity(
                    search_summary, captured, shadow_summary,
                    motion_scheduling))
            summary = {
                "status": ("invalid" if captured["writer_error"]
                           else "complete"),
                "eventtime": end_eventtime,
                "print_time": self._sensor_print_time(sensor, end_eventtime),
                "sample_count": captured["sample_count"],
                "errors": captured["errors"],
                "overflows": captured["overflows"],
                "writer_error": captured["writer_error"],
                "shadow": shadow_summary,
                "shadow_valid": (None if shadow_summary is None else
                                  shadow_summary.get("valid", False)),
                "shadow_dropped_batches": captured.get(
                    "shadow_dropped_batches", 0),
                "shadow_dropped_markers": captured.get(
                    "shadow_dropped_markers", 0),
                "shadow_worker_error_count": self._shadow_worker_error_count,
                "motion_scheduling": motion_scheduling,
                "decision": (None if shadow_summary is None else
                              shadow_summary.get("decision")),
                "search": search_summary,
                "result_validity": result_validity,
                "validity_failures": validity_failures,
                "sensor_status_after": dict(
                    self.load_cell.get_status(end_eventtime)),
                "conditions_after": self._conditions(end_eventtime),
                "thermal": thermal_result,
                "commanded_filament_mm": actual_filament,
                "maximum_planned_filament_mm": (
                    plan["total_filament_mm"] if not controlled else None),
                "planned_filament_mm": (
                    plan["total_filament_mm"] if not controlled else None),
                "experiment_type": "max_flow_capture",
            }
            output_path = self._finish_writer(writer, summary)
            if captured["writer_error"]:
                raise gcmd.error(captured["writer_error"])
            self.last_result = {
                "operation": operation_name,
                "experiment_type": "max_flow_capture",
                "status": "captured",
                "run_id": metadata["run"]["id"],
                "artifact": output_path,
                "shadow_valid": (None if shadow_summary is None else
                                  shadow_summary.get("valid", False)),
                "decision": (None if shadow_summary is None else
                              shadow_summary.get("decision")),
                "motion_scheduling": motion_scheduling,
                "search": search_summary,
                "result_validity": result_validity,
                "validity_failures": validity_failures,
            }
            if controlled and production:
                gcmd.respond_info(self._production_max_flow_result_message(
                    search_summary, result_validity, validity_failures,
                    max_test_flow, output_path))
            elif controlled:
                gcmd.respond_info(
                    "FlowTune max-flow result: validity=%s Q_failure=%s "
                    "Q_recommended=%s. Artifact: %s" % (
                        result_validity or "unknown",
                        search_summary.get("q_failure_mm3_s", "--"),
                        search_summary.get("q_recommended_mm3_s", "--"),
                        output_path))
            else:
                gcmd.respond_info(
                    "FlowTune maximum-flow diagnostic complete: %d samples, "
                    "%.2f mm filament planned. Analyze the raw artifact "
                    "offline.\nCapture: %s" % (captured["sample_count"],
                                                 plan["total_filament_mm"],
                                                 output_path))
        except Exception as error:
            self._stop_motion_telemetry()
            if capture_started or self.capture.active:
                try:
                    captured = self.capture.stop(self.reactor.monotonic())
                except Exception:
                    logging.exception(
                        "FlowTune: failed to stop maximum-flow capture")
                finally:
                    capture_started = False
            if shadow_worker is not None and self._shadow_worker is not None:
                try:
                    shadow_summary = self._finish_shadow(
                        self.reactor.monotonic())
                    if captured is not None:
                        captured["shadow"] = shadow_summary
                except Exception:
                    logging.exception(
                        "FlowTune: failed to finalize max-flow shadow worker")
            aborted_search_summary = (
                None if not controlled else
                self._shadow_search_controller.summary())
            aborted_result_validity, _aborted_failures = (
                self._compose_max_flow_result_validity(
                    aborted_search_summary, captured, shadow_summary,
                    self._max_flow_drip_timing_summary()))
            if not self.printer.is_shutdown():
                if pa_changed:
                    try:
                        self.gcode.run_script_from_command(
                            "SET_PRESSURE_ADVANCE ADVANCE=%.17g "
                            "SMOOTH_TIME=%.17g" % (current_pa, smooth_time))
                    except Exception:
                        logging.exception(
                            "FlowTune: failed to restore pressure advance")
                if not heater_restored:
                    try:
                        heaters.set_temperature(heater, original_target)
                    except Exception:
                        logging.exception(
                            "FlowTune: failed to restore heater target")
            if writer is not None and not writer.finished:
                try:
                    now = self.reactor.monotonic()
                    self._finish_writer(writer, {
                        "status": "aborted",
                        "eventtime": now,
                        "print_time": self._sensor_print_time(sensor, now),
                        "sample_count": (0 if captured is None else
                                         captured["sample_count"]),
                        "errors": (0 if captured is None else
                                   captured["errors"]),
                        "overflows": (0 if captured is None else
                                       captured["overflows"]),
                        "shadow": shadow_summary,
                        "shadow_dropped_batches": (0 if captured is None
                                                    else captured.get(
                                                        "shadow_dropped_batches",
                                                        0)),
                        "shadow_dropped_markers": (0 if captured is None
                                                    else captured.get(
                                                        "shadow_dropped_markers",
                                                        0)),
                        "motion_scheduling": (
                            self._max_flow_drip_timing_summary()
                            if controlled else None),
                        "decision": (None if shadow_summary is None else
                                      shadow_summary.get("decision")),
                        "search": aborted_search_summary,
                        "result_validity": aborted_result_validity,
                        "abort_reason": str(error),
                        "experiment_type": "max_flow_capture",
                    })
                except Exception:
                    logging.exception(
                        "FlowTune: failed to finalize aborted maximum-flow "
                        "capture")
            self.last_result = {
                "operation": operation_name,
                "experiment_type": "max_flow_capture",
                "status": "error",
                "message": str(error),
                "decision": (None if shadow_summary is None else
                              shadow_summary.get("decision")),
                "motion_scheduling": (
                    self._max_flow_drip_timing_summary()
                    if controlled else None),
                "search": aborted_search_summary,
                "result_validity": aborted_result_validity,
            }
            if isinstance(error, _MaxFlowControlledAbort):
                raise gcmd.error(str(error))
            raise
        finally:
            self._stop_motion_telemetry()
            if (not extruder_stepper_disabled and
                    not self.printer.is_shutdown()):
                try:
                    self._disable_extruder_stepper(extruder)
                except Exception:
                    logging.exception(
                        "FlowTune: failed to disable extruder stepper")
            self._shadow_worker = None
            self._shadow_reporter = None
            self._shadow_semantic_reporting = False
            self._shadow_decision_tracker = None
            self._shadow_search_controller = None
            self._shadow_search_events = []
            self._shadow_search_recoveries = []
            self._shadow_search_outcomes = []
            self._active_e_drip = None
            self._set_idle()

    def cmd_FLOWTUNE_SENSOR_CHECK(self, gcmd):
        self._require_ready(gcmd)
        if self.active_operation is not None or self.capture.active:
            raise gcmd.error("A FlowTune operation is already active")
        duration = gcmd.get_float(
            "DURATION", self.default_capture_duration,
            minval=0.5, maxval=30.0)
        save = gcmd.get_int("SAVE", 1, minval=0, maxval=1)
        label = gcmd.get("LABEL", None)
        start_eventtime = self.reactor.monotonic()
        sensor, initial_status, sample_rate, sensor_range = (
            self._sensor_details(start_eventtime))
        initial_conditions = self._conditions(start_eventtime)
        source = {
            "load_cell_object": self.load_cell_object,
            "interface": self.resolver.compatibility_path,
            "sensor_class": sensor.__class__.__name__,
            "configured_sample_rate_sps": sample_rate,
            "sensor_range_counts": sensor_range,
        }
        writer = None
        metadata = None
        if save:
            writer, metadata = self._new_writer(
                "sensor_check", label, source, initial_status,
                initial_conditions, {"duration_s": duration})
        gcmd.respond_info(
            "FlowTune collecting %.2fs of stationary load-cell data..."
            % (duration,))
        self._begin_operation("sensor_check", start_eventtime, duration)
        captured = None
        try:
            self.capture.start(start_eventtime, writer=writer)
            self.capture.write_record(
                "event", eventtime=start_eventtime,
                print_time=self._sensor_print_time(sensor, start_eventtime),
                name="capture_start", payload={})
            try:
                end_eventtime = self._wait_for_capture(duration)
            finally:
                captured = self.capture.stop(self.reactor.monotonic())
            if self.printer.is_shutdown():
                raise gcmd.error(
                    "Klipper shutdown during FlowTune sensor check")

            final_status = dict(self.load_cell.get_status(end_eventtime))
            final_conditions = self._conditions(end_eventtime)
            self.capture.write_record(
                "event", eventtime=end_eventtime,
                print_time=self._sensor_print_time(sensor, end_eventtime),
                name="capture_end", payload={})
            captured["writer_error"] = self.capture.failure()
            summary = {
                "status": ("invalid" if captured["writer_error"]
                           else "complete"),
                "eventtime": end_eventtime,
                "print_time": self._sensor_print_time(
                    sensor, end_eventtime),
                "sample_count": captured["sample_count"],
                "errors": captured["errors"],
                "overflows": captured["overflows"],
                "writer_error": captured["writer_error"],
                "sensor_status_after": final_status,
                "conditions_after": final_conditions,
            }
            output_path = self._finish_writer(writer, summary)
            if captured["writer_error"]:
                raise gcmd.error(captured["writer_error"])

            self.last_result = {
                "operation": "sensor_check",
                "status": "captured",
                "run_id": (None if metadata is None
                           else metadata["run"]["id"]),
                "artifact": output_path,
            }
            message = (
                "FlowTune sensor capture complete: %d samples, %d errors, "
                "%d overflows."
                % (captured["sample_count"], captured["errors"],
                   captured["overflows"]))
            if output_path is not None:
                message += "\nCapture: %s" % (output_path,)
            gcmd.respond_info(message)
        except Exception as error:
            if self.capture.active:
                captured = self.capture.stop(self.reactor.monotonic())
            if writer is not None and not writer.finished:
                try:
                    now = self.reactor.monotonic()
                    self._finish_writer(writer, {
                        "status": "aborted",
                        "eventtime": now,
                        "print_time": self._sensor_print_time(sensor, now),
                        "sample_count": (0 if captured is None else
                                         captured["sample_count"]),
                        "errors": (0 if captured is None else
                                   captured["errors"]),
                        "overflows": (0 if captured is None else
                                      captured["overflows"]),
                        "abort_reason": str(error),
                    })
                except Exception:
                    logging.exception(
                        "FlowTune: failed to finalize aborted capture")
            self.last_result = {
                "operation": "sensor_check",
                "status": "error",
                "message": str(error),
            }
            raise
        finally:
            self._set_idle()

    def cmd_FLOWTUNE_THERMAL_CHECK(self, gcmd):
        self._require_ready(gcmd)
        self._require_idle(gcmd)
        if self.active_operation is not None or self.capture.active:
            raise gcmd.error("A FlowTune operation is already active")
        target = gcmd.get_float("TARGET", 210.0, minval=1.0, maxval=400.0)
        tolerance = gcmd.get_float(
            "TOLERANCE", 1.0, above=0.0, maxval=10.0)
        stable_duration = gcmd.get_float(
            "STABLE_DURATION", 30.0, minval=5.0, maxval=120.0)
        timeout = gcmd.get_float(
            "TIMEOUT", 180.0, minval=stable_duration, maxval=300.0)
        save = gcmd.get_int("SAVE", 1, minval=0, maxval=1)
        label = gcmd.get("LABEL", "thermal_check")
        extruder = self.printer.lookup_object("extruder", None)
        if extruder is None:
            raise gcmd.error("FlowTune thermal check requires [extruder]")
        heater = extruder.get_heater()
        heaters = self.printer.lookup_object("heaters")

        start_eventtime = self.reactor.monotonic()
        sensor, initial_status, sample_rate, sensor_range = (
            self._sensor_details(start_eventtime))
        initial_conditions = self._conditions(start_eventtime)
        original_target = initial_conditions["extruder"].get("target", 0.0)
        if original_target:
            raise gcmd.error(
                "FlowTune thermal check requires the hotend target to be off")

        source = {
            "load_cell_object": self.load_cell_object,
            "interface": self.resolver.compatibility_path,
            "sensor_class": sensor.__class__.__name__,
            "configured_sample_rate_sps": sample_rate,
            "sensor_range_counts": sensor_range,
        }
        parameters = {
            "target_c": target,
            "tolerance_c": tolerance,
            "stable_duration_s": stable_duration,
            "timeout_s": timeout,
        }
        writer = None
        metadata = None
        if save:
            writer, metadata = self._new_writer(
                "thermal_check", label, source, initial_status,
                initial_conditions, parameters)

        gcmd.respond_info(
            "FlowTune capturing thermal drift to %.1f C; waiting for %.1f s "
            "within +/-%.1f C (%.1f s timeout)..."
            % (target, stable_duration, tolerance, timeout))
        self._begin_operation("thermal_check", start_eventtime, timeout)
        thermal_result = None
        final_conditions = None
        captured = None
        heater_restored = False
        try:
            self.capture.start(start_eventtime, writer=writer)
            self.capture.write_record(
                "event", eventtime=start_eventtime,
                print_time=self._sensor_print_time(sensor, start_eventtime),
                name="capture_start", payload={})
            try:
                heaters.set_temperature(heater, target)
                heater_set_eventtime = self.reactor.monotonic()
                heater_set_print_time = self._sensor_print_time(
                    sensor, heater_set_eventtime)
                self.capture.write_record(
                    "event", eventtime=heater_set_eventtime,
                    print_time=heater_set_print_time,
                    name="heater_target_set", payload={"target_c": target})
                thermal_result = self._wait_for_thermal_stability(
                    sensor, target, tolerance, stable_duration, timeout)
                end_eventtime = thermal_result["end_eventtime"]
                final_conditions = self._conditions(end_eventtime)
            finally:
                captured = self.capture.stop(self.reactor.monotonic())
                if not self.printer.is_shutdown():
                    heaters.set_temperature(heater, original_target)
                    heater_restored = True
            restore_eventtime = self.reactor.monotonic()
            self.capture.write_record(
                "event", eventtime=restore_eventtime,
                print_time=self._sensor_print_time(sensor, restore_eventtime),
                name="heater_target_restored",
                payload={"target_c": original_target})
            captured["writer_error"] = self.capture.failure()
            if self.printer.is_shutdown():
                raise gcmd.error(
                    "Klipper shutdown during FlowTune thermal check")

            final_status = dict(self.load_cell.get_status(end_eventtime))
            thermal_conditions = {
                "stable_reached": thermal_result["stable_reached"],
                "heater_set_eventtime": heater_set_eventtime,
                "heater_set_print_time": heater_set_print_time,
                "stable_start_eventtime": (
                    thermal_result["stable_start_eventtime"]),
                "stable_start_print_time": (
                    thermal_result["stable_start_print_time"]),
                "elapsed_s": thermal_result["elapsed_s"],
                "restored_target_c": original_target,
            }
            summary = {
                "status": ("invalid" if captured["writer_error"]
                           else "complete"),
                "eventtime": end_eventtime,
                "print_time": self._sensor_print_time(
                    sensor, end_eventtime),
                "sample_count": captured["sample_count"],
                "errors": captured["errors"],
                "overflows": captured["overflows"],
                "writer_error": captured["writer_error"],
                "sensor_status_after": final_status,
                "conditions_after": final_conditions,
                "thermal_check": thermal_conditions,
            }
            output_path = self._finish_writer(writer, summary)
            if captured["writer_error"]:
                raise gcmd.error(captured["writer_error"])

            self.last_result = {
                "operation": "thermal_check",
                "status": "captured",
                "run_id": (None if metadata is None
                           else metadata["run"]["id"]),
                "artifact": output_path,
            }
            message = (
                "FlowTune thermal capture complete: %.1f C target, stable=%s, "
                "%.1f s elapsed, %d samples"
                % (target, thermal_result["stable_reached"],
                   thermal_result["elapsed_s"],
                   captured["sample_count"]))
            if output_path is not None:
                message += "\nCapture: %s" % (output_path,)
            gcmd.respond_info(message)
        except Exception as error:
            if self.capture.active:
                captured = self.capture.stop(self.reactor.monotonic())
            if not heater_restored and not self.printer.is_shutdown():
                heaters.set_temperature(heater, original_target)
            if writer is not None and not writer.finished:
                try:
                    now = self.reactor.monotonic()
                    self._finish_writer(writer, {
                        "status": "aborted",
                        "eventtime": now,
                        "print_time": self._sensor_print_time(sensor, now),
                        "sample_count": (0 if captured is None else
                                         captured["sample_count"]),
                        "errors": (0 if captured is None else
                                   captured["errors"]),
                        "overflows": (0 if captured is None else
                                      captured["overflows"]),
                        "abort_reason": str(error),
                    })
                except Exception:
                    logging.exception(
                        "FlowTune: failed to finalize aborted thermal capture")
            self.last_result = {
                "operation": "thermal_check",
                "status": "error",
                "message": str(error),
            }
            raise
        finally:
            self._set_idle()

    def cmd_FLOWTUNE_PA(self, gcmd):
        """Run the production fixed-list FlowPA calibration."""
        return flowtune_pa_command.run(self, gcmd)

def load_config(config):
    return FlowTune(config)
