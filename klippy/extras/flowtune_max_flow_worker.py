# FlowTune
#
# Copyright (C) 2026 Ahmed Sheikh <ahmed.ali.sheikh1998@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
# SPDX-License-Identifier: GPL-3.0-only

"""Bounded-process shadow replay for the maximum-flow detector.

This module deliberately has no capture-file or Klipper dependencies.  The
parent owns the CSV reader and sends small sample batches plus sparse phase
markers over a bounded multiprocessing queue.  The child owns one
``MaxFlowReleaseRebuildDetector`` and emits only sparse lifecycle, status, and
detector-event messages.

The worker is a replay/shadow tool, not a motion or experiment controller.
"""

from __future__ import division

import math
import multiprocessing
import os
import queue
import time
from collections import deque

try:
    from .flowtune_max_flow import MaxFlowReleaseRebuildDetector
except ImportError:
    # The offline replay tool imports this module directly from extras/.
    from flowtune_max_flow import MaxFlowReleaseRebuildDetector


DEFAULT_INPUT_QUEUE_SIZE = 16
DEFAULT_STATUS_INTERVAL_S = 1.0
DEFAULT_MAX_BACKLOG_AGE_S = 0.5
DEFAULT_NICE = 0
DEFAULT_OUTPUT_QUEUE_SIZE = 128
DEFAULT_MAX_PENDING_MARKERS = 128

_PHASE_MARKERS = {
    "max_flow_purge_start": ("purge", None, None),
    "max_flow_purge_end": ("inactive", None, None),
    "max_flow_segment_start": ("active", "segment_index",
                                "commanded_flow_mm3_s"),
    "max_flow_segment_end": ("inactive", None, None),
    "max_flow_recovery_start": ("recovery", "segment_index",
                                 "commanded_flow_mm3_s"),
    "max_flow_recovery_end": ("inactive", None, None),
    "max_flow_rechallenge_start": ("active", "segment_index",
                                    "commanded_flow_mm3_s"),
    "max_flow_rechallenge_end": ("inactive", None, None),
    "max_flow_final_confirmation_start": ("active", "segment_index",
                                           "commanded_flow_mm3_s"),
    "max_flow_final_confirmation_end": ("inactive", None, None),
    "max_flow_staircase_end": ("inactive", None, None),
}

_ACTIVE_START_MARKERS = frozenset((
    "max_flow_segment_start",
    "max_flow_rechallenge_start",
    "max_flow_final_confirmation_start",
))

_ACTIVE_END_MARKERS = frozenset((
    "max_flow_segment_end",
    "max_flow_rechallenge_end",
    "max_flow_final_confirmation_end",
))


def _finite(value, name):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("%s must be finite" % name)
    if not math.isfinite(result):
        raise ValueError("%s must be finite" % name)
    return result


def _marker_time(marker):
    if not isinstance(marker, dict):
        raise ValueError("marker must be a mapping")
    return _finite(marker.get("print_time"), "marker print_time")


def _sample_values(sample):
    if isinstance(sample, dict):
        timestamp = sample.get("print_time")
        force = sample.get("force_g", sample.get("force"))
        return (_finite(timestamp, "sample print_time"),
                _finite(force, "sample force_g"))
    try:
        timestamp, force = sample[:2]
    except (TypeError, IndexError):
        raise ValueError("sample must contain print_time and force_g")
    return (_finite(timestamp, "sample print_time"),
            _finite(force, "sample force_g"))


def _safe_qsize(message_queue):
    try:
        return int(message_queue.qsize())
    except (AttributeError, NotImplementedError, OSError):
        return 0


class _WorkerState(object):
    """Child-only state machine; all fields remain bounded."""

    def __init__(self, detector, output_queue, input_queue,
                 status_interval_s, max_backlog_age_s, clock,
                 max_pending_markers=DEFAULT_MAX_PENDING_MARKERS):
        self.detector = detector
        self.output_queue = output_queue
        self.input_queue = input_queue
        self.status_interval_s = status_interval_s
        self.max_backlog_age_s = max_backlog_age_s
        self.max_pending_markers = int(max_pending_markers)
        if self.max_pending_markers < 1:
            raise ValueError("max_pending_markers must be positive")
        self.clock = clock
        self.phase = "inactive"
        self.segment_index = None
        self.flow_mm3_s = None
        self.pending_markers = []
        self.marker_sequence = 0
        self.last_sample_time = None
        self.last_marker_time = None
        self.latest_data_age_s = None
        self.maximum_data_age_s = 0.0
        self.backlog_warning_count = 0
        self.latest_latency_s = None
        self.sample_count = 0
        self.batch_count = 0
        self.marker_count = 0
        self.candidate_count = 0
        self.confirmed_count = 0
        # ``drain_events`` clears the detector's transient event list.  Keep
        # the same bounded history for the final summary and failure path.
        self.event_history = deque(
            maxlen=int(detector.config.get("max_event_count", 256)))
        self.failure_reasons = []
        self.last_status_clock = None
        self.started_clock = self.clock()
        self.finished = False
        self.recovery_baseline_load = None
        self.recovery_peak_load = None
        self.recovery_sample_count = 0
        self.recovery_release_events = 0
        self.active_segment = None
        self.pending_segment_outcomes = []
        self.confirmed_segment_indices = set()
        self.candidate_segment_indices = set()

    @property
    def valid(self):
        return not self.failure_reasons

    def fail(self, reason):
        reason = str(reason)
        if reason not in self.failure_reasons:
            self.failure_reasons.append(reason)
        self._send({"type": "error", "error": reason,
                    "failure_reasons": list(self.failure_reasons)})

    def _send(self, message):
        # The parent drains this bounded queue while feeding the worker.  A
        # blocking put preserves sparse event/error/final messages without
        # silently dropping them or allowing unbounded child memory.
        self.output_queue.put(message)

    def _detector_state(self):
        if getattr(self.detector, "_candidate", None) is not None:
            return "candidate_pending"
        if self.detector.force_polarity is None:
            return "initializing"
        return "tracking"

    def _status(self, force=False):
        now = self.clock()
        if (not force and self.last_status_clock is not None and
                now - self.last_status_clock < self.status_interval_s):
            return
        self.last_status_clock = now
        self._send({
            "type": "status",
            "phase": self.phase,
            "flow_mm3_s": self.flow_mm3_s,
            "segment_index": self.segment_index,
            "detector_state": self._detector_state(),
            "sample_count": self.sample_count,
            "batch_count": self.batch_count,
            "marker_count": self.marker_count,
            "candidate_count": self.candidate_count,
            "confirmed_count": self.confirmed_count,
            "processing_latency_s": self.latest_latency_s,
            "backlog": _safe_qsize(self.input_queue),
            "data_age_s": self.latest_data_age_s,
            "maximum_data_age_s": self.maximum_data_age_s,
            "backlog_warning_count": self.backlog_warning_count,
            "last_print_time": self.last_sample_time,
            "valid": self.valid,
        })

    def _apply_marker(self, marker):
        marker_time = _marker_time(marker)
        name = str(marker.get("name", ""))
        if name not in _PHASE_MARKERS:
            # Capture lifecycle/telemetry markers do not alter detector phase,
            # but retaining their order is unnecessary for the pure detector.
            self.last_marker_time = marker_time
            self.marker_count += 1
            return
        phase, index_key, flow_key = _PHASE_MARKERS[name]
        payload = marker.get("payload") or {}
        if name in _ACTIVE_END_MARKERS:
            self._finish_active_segment()
        if name == "max_flow_recovery_start":
            history = getattr(self.detector, "_history", ())
            previous = list(history)[-1] if history else None
            self.recovery_baseline_load = (
                None if previous is None else previous[2])
            self.recovery_peak_load = 0.0
            self.recovery_sample_count = 0
            self.recovery_release_events = 0
        if phase == "active":
            if not isinstance(payload, dict):
                raise ValueError("segment marker payload must be a mapping")
            index = payload.get(index_key)
            if index is None:
                index = payload.get("index")
            flow = payload.get(flow_key)
            if flow is None:
                flow = payload.get("volumetric_flow_mm3_s")
            try:
                index = int(index)
                flow = _finite(flow, "segment commanded flow")
            except (TypeError, ValueError, OverflowError):
                raise ValueError("segment marker has malformed payload")
            self.segment_index = index
            self.flow_mm3_s = flow
            if name in _ACTIVE_START_MARKERS:
                self.active_segment = {
                    "segment_index": index,
                    "flow_mm3_s": flow,
                    "stage": str(payload.get("stage", "search")),
                    "speculative": bool(payload.get("speculative", False)),
                    "sample_count": 0,
                    "peak_load_g": 0.0,
                }
        else:
            self.segment_index = None
            self.flow_mm3_s = None
        self.phase = phase
        self.last_marker_time = marker_time
        self.marker_count += 1
        if name == "max_flow_recovery_end":
            baseline = self.recovery_baseline_load
            peak = self.recovery_peak_load
            threshold = max(
                float(self.detector.config.get("minimum_rebuild_g", 0.0)),
                (0.45 * float(baseline)
                 if baseline is not None else 0.0))
            engaged = (self.recovery_sample_count > 0 and peak is not None
                       and peak >= threshold)
            self._send({
                "type": "recovery",
                "load_reengaged": bool(engaged),
                "rebuild_detected": bool(engaged),
                "release_signature": bool(self.recovery_release_events),
                "baseline_load_g": baseline,
                "peak_load_g": peak,
                "sample_count": self.recovery_sample_count,
            })

    def _finish_active_segment(self):
        segment = self.active_segment
        self.active_segment = None
        if segment is None:
            return
        index = segment["segment_index"]
        candidate = getattr(self.detector, "_candidate", None)
        candidate_index = (None if candidate is None else
                           candidate.get("segment_index"))
        if (index not in self.confirmed_segment_indices and
                candidate_index == index):
            self.pending_segment_outcomes.append(segment)
            return
        self._emit_segment_outcome(
            segment, index in self.confirmed_segment_indices)

    def _emit_segment_outcome(self, segment, failure):
        threshold = float(self.detector.config.get(
            "minimum_prepeak_load_g", 0.0))
        peak = float(segment.get("peak_load_g", 0.0) or 0.0)
        self._send({
            "type": "segment_outcome",
            "segment_index": segment["segment_index"],
            "flow_mm3_s": segment["flow_mm3_s"],
            "stage": segment["stage"],
            "speculative": segment["speculative"],
            "failure": bool(failure),
            "release_signature": bool(
                segment["segment_index"] in self.candidate_segment_indices),
            "load_reengaged": bool(
                segment.get("sample_count", 0) > 0 and peak >= threshold),
            "peak_load_g": peak,
            "load_threshold_g": threshold,
            "sample_count": segment.get("sample_count", 0),
        })

    def _resolve_pending_segment_outcomes(self):
        if not self.pending_segment_outcomes:
            return
        candidate = getattr(self.detector, "_candidate", None)
        candidate_index = (None if candidate is None else
                           candidate.get("segment_index"))
        unresolved = []
        for segment in self.pending_segment_outcomes:
            index = segment["segment_index"]
            if (index not in self.confirmed_segment_indices and
                    candidate_index == index):
                unresolved.append(segment)
                continue
            self._emit_segment_outcome(
                segment, index in self.confirmed_segment_indices)
        self.pending_segment_outcomes = unresolved

    def receive_marker(self, marker):
        marker_time = _marker_time(marker)
        # A marker that arrives after a sample at or beyond its print time can
        # no longer be applied before that sample.  Treat this as invalid
        # rather than silently changing the detector's phase retroactively.
        if (self.last_sample_time is not None and
                marker_time <= self.last_sample_time):
            raise ValueError("late marker at print_time %.9f" % marker_time)
        if (self.last_marker_time is not None and
                marker_time < self.last_marker_time):
            raise ValueError("non-monotonic marker print_time %.9f" %
                             marker_time)
        if (self.pending_markers and
                marker_time < self.pending_markers[-1][0]):
            raise ValueError("out-of-order pending marker print_time %.9f" %
                             marker_time)
        if len(self.pending_markers) >= self.max_pending_markers:
            raise ValueError("pending marker timeline is full")
        self.marker_sequence += 1
        self.pending_markers.append(
            (marker_time, self.marker_sequence, marker))

    def _apply_markers_through(self, timestamp):
        while (self.pending_markers and
               self.pending_markers[0][0] <= timestamp):
            marker_time, _sequence, marker = self.pending_markers.pop(0)
            if (self.last_sample_time is not None and
                    marker_time <= self.last_sample_time):
                raise ValueError("late marker at print_time %.9f" %
                                 marker_time)
            self._apply_marker(marker)

    def receive_samples(self, samples, sent_monotonic=None):
        if not isinstance(samples, (list, tuple)):
            raise ValueError("sample batch must be a list")
        if not samples:
            return
        now = self.clock()
        if sent_monotonic is not None:
            sent_monotonic = _finite(sent_monotonic, "batch sent time")
            age = max(0.0, now - sent_monotonic)
            self.latest_data_age_s = age
            self.maximum_data_age_s = max(self.maximum_data_age_s, age)
            if age > self.max_backlog_age_s:
                # Batch age is diagnostic only.  The experiment controller
                # owns the actual decision deadline, which is tied to the end
                # of its one queued-ahead guard segment.  Killing the worker
                # at an unrelated wall-clock threshold can discard a result
                # that is still early enough to preserve continuous motion.
                self.backlog_warning_count += 1
        self.batch_count += 1
        batch_started = self.clock()
        for sample in samples:
            timestamp, force = _sample_values(sample)
            if (self.last_sample_time is not None and
                    timestamp < self.last_sample_time):
                raise ValueError("non-monotonic sample print_time %.9f" %
                                 timestamp)
            self._apply_markers_through(timestamp)
            self.detector.process_sample(
                timestamp, force, self.phase, self.segment_index,
                self.flow_mm3_s)
            if self.phase == "active" and self.active_segment is not None:
                self.active_segment["sample_count"] += 1
                load = getattr(
                    self.detector, "_normalized_load", lambda x: None)(force)
                if load is not None:
                    self.active_segment["peak_load_g"] = max(
                        float(self.active_segment["peak_load_g"]),
                        float(load))
            if self.phase == "recovery":
                self.recovery_sample_count += 1
                load = getattr(self.detector, "_normalized_load", lambda x: None)(
                    force)
                if load is not None:
                    previous_peak = self.recovery_peak_load
                    if (previous_peak is not None and
                            previous_peak >= float(self.detector.config.get(
                                "minimum_prepeak_load_g", 0.0)) and
                            previous_peak - load >= float(
                                self.detector.config.get(
                                    "minimum_drop_g", 0.0)) and
                            (previous_peak - load) / previous_peak >=
                            float(self.detector.config.get(
                                "minimum_drop_fraction", 1.0))):
                        self.recovery_release_events += 1
                    self.recovery_peak_load = max(
                        float(self.recovery_peak_load or 0.0), float(load))
            self.last_sample_time = timestamp
            self.sample_count += 1
            for event in self.detector.drain_events():
                self.event_history.append(event)
                event_type = event.get("type")
                if event_type == "release_candidate":
                    self.candidate_count += 1
                    event_index = event.get(
                        "candidate_segment_index", event.get("segment_index"))
                    if event_index is not None:
                        self.candidate_segment_indices.add(int(event_index))
                    self._send({"type": "candidate", "event": event})
                elif event_type == "release_confirmed":
                    self.confirmed_count += 1
                    event_index = event.get(
                        "candidate_segment_index", event.get("segment_index"))
                    if event_index is not None:
                        self.confirmed_segment_indices.add(int(event_index))
                    self._send({"type": "confirmed", "event": event})
            self._resolve_pending_segment_outcomes()
        self.latest_latency_s = self.clock() - batch_started
        self._status()

    def final_summary(self, extra_failure_reasons=None):
        reasons = list(self.failure_reasons)
        for reason in extra_failure_reasons or ():
            reason = str(reason)
            if reason not in reasons:
                reasons.append(reason)
        events = list(self.event_history)
        confirmed = [event for event in events
                     if event.get("type") == "release_confirmed"]
        first_flow = None
        for event in confirmed:
            flow = event.get("candidate_flow_mm3_s")
            if flow is None:
                flow = event.get("flow_mm3_s")
            if flow is not None:
                first_flow = float(flow)
                break
        return {
            "type": "final",
            "valid": not reasons,
            "replay_valid": not reasons,
            "failure_reasons": reasons,
            "sample_count": self.sample_count,
            "batch_count": self.batch_count,
            "marker_count": self.marker_count,
            "candidate_count": self.candidate_count,
            "confirmed_count": self.confirmed_count,
            "first_confirmed_failure_flow_mm3_s": first_flow,
            "events": events,
            "detector_config": self.detector.config,
            "phase": self.phase,
            "flow_mm3_s": self.flow_mm3_s,
            "segment_index": self.segment_index,
            "last_print_time": self.last_sample_time,
            "processing_latency_s": self.latest_latency_s,
            "data_age_s": self.latest_data_age_s,
            "maximum_data_age_s": self.maximum_data_age_s,
            "backlog_warning_count": self.backlog_warning_count,
        }

    def finish(self, extra_failure_reasons=None):
        if self.finished:
            return
        # A terminal marker may legitimately be later than the final sample
        # (for example, the recorded staircase end).  Apply such markers so
        # the final phase/timeline state is complete; markers at or before a
        # sample were already rejected as late on receipt.
        if self.pending_markers:
            self._apply_markers_through(float("inf"))
        self._status(force=True)
        summary = self.final_summary(extra_failure_reasons)
        self._send(summary)
        self.finished = True


def _worker_main(input_queue, output_queue, tuning, status_interval_s,
                 max_backlog_age_s, nice_value, max_pending_markers):
    """Process target.  Do not add file or Klipper access here."""
    try:
        try:
            os.nice(int(nice_value))
        except (AttributeError, OSError, TypeError, ValueError):
            pass
        detector = MaxFlowReleaseRebuildDetector(tuning=tuning)
        state = _WorkerState(
            detector, output_queue, input_queue,
            float(status_interval_s), float(max_backlog_age_s),
            time.monotonic, max_pending_markers=max_pending_markers)
        output_queue.put({
            "type": "ready",
            "pid": os.getpid(),
            "queue_size": getattr(input_queue, "_maxsize", None),
            "detector_config": detector.config,
        })
        while True:
            message = input_queue.get()
            if not isinstance(message, (tuple, list)) or not message:
                raise ValueError("malformed worker message")
            message_type = message[0]
            if message_type == "samples":
                sent_time = message[2] if len(message) > 2 else None
                state.receive_samples(message[1], sent_time)
            elif message_type == "marker":
                state.receive_marker(message[1])
            elif message_type == "finish":
                extras = message[1] if len(message) > 1 else None
                state.finish(extras)
                break
            elif message_type == "abort":
                state.fail(message[1] if len(message) > 1 else "aborted")
                state.finish()
                break
            else:
                raise ValueError("unknown worker message %r" % message_type)
    except BaseException as error:
        detail = "%s: %s" % (error.__class__.__name__, error)
        try:
            output_queue.put({"type": "error", "error": detail,
                              "failure_reasons": [detail]})
            output_queue.put({
                "type": "final", "valid": False,
                "replay_valid": False,
                "failure_reasons": [detail],
                "sample_count": getattr(locals().get("state"),
                                         "sample_count", 0),
                "batch_count": getattr(locals().get("state"),
                                        "batch_count", 0),
                "marker_count": getattr(locals().get("state"),
                                         "marker_count", 0),
                "candidate_count": getattr(locals().get("state"),
                                            "candidate_count", 0),
                "confirmed_count": getattr(locals().get("state"),
                                            "confirmed_count", 0),
                "first_confirmed_failure_flow_mm3_s": None,
                "events": (list(locals()["state"].event_history)
                           if "state" in locals() else []),
                "detector_config": (locals()["state"].detector.config
                                     if "state" in locals() else None),
            })
        except BaseException:
            # There is no safe parent-side recovery if even the error pipe is
            # unavailable; the wrapper will report the abnormal exit.
            pass


class MaxFlowShadowWorker(object):
    """Parent-side bounded process wrapper.

    ``submit_samples`` and ``submit_marker`` return ``False`` on a full input
    queue and record an explicit invalid result.  They never silently drop a
    message.  Use ``finish`` to drain the worker and obtain a JSON-compatible
    final summary.
    """

    def __init__(self, tuning=None, queue_size=DEFAULT_INPUT_QUEUE_SIZE,
                 status_interval_s=DEFAULT_STATUS_INTERVAL_S,
                 max_backlog_age_s=DEFAULT_MAX_BACKLOG_AGE_S,
                 nice_value=DEFAULT_NICE,
                 output_queue_size=DEFAULT_OUTPUT_QUEUE_SIZE,
                 max_pending_markers=DEFAULT_MAX_PENDING_MARKERS):
        queue_size = int(queue_size)
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        output_queue_size = int(output_queue_size)
        if output_queue_size < 1:
            raise ValueError("output_queue_size must be positive")
        max_pending_markers = int(max_pending_markers)
        if max_pending_markers < 1:
            raise ValueError("max_pending_markers must be positive")
        self.input_queue = multiprocessing.Queue(maxsize=queue_size)
        self.output_queue = multiprocessing.Queue(maxsize=output_queue_size)
        self.process = multiprocessing.Process(
            target=_worker_main,
            args=(self.input_queue, self.output_queue, tuning,
                  float(status_interval_s), float(max_backlog_age_s),
                  int(nice_value), max_pending_markers))
        self.process.daemon = True
        self.started = False
        self.closed = False
        self.parent_failure_reasons = []
        # Keep only a bounded diagnostic tail.  Consumers should process the
        # return value of ``drain``; this deque is for tests/debug inspection.
        self.messages = deque(maxlen=output_queue_size)
        self.final_summary = None

    def start(self, timeout_s=5.0, wait_callback=None):
        if self.started:
            raise RuntimeError("worker already started")
        self.process.start()
        self.started = True
        deadline = time.monotonic() + float(timeout_s)
        while time.monotonic() < deadline:
            if wait_callback is None:
                message = self.poll(timeout_s=min(0.1, max(
                    0.0, deadline - time.monotonic())))
            else:
                message = self.poll(timeout_s=0.0)
            if message and message.get("type") == "ready":
                return message
            if not self.process.is_alive() and self.final_summary is None:
                break
            if wait_callback is not None:
                remaining = max(0.0, deadline - time.monotonic())
                if remaining:
                    wait_callback(min(0.05, remaining))
        self._record_parent_failure("worker did not become ready")
        return None

    def _record_parent_failure(self, reason):
        reason = str(reason)
        if reason not in self.parent_failure_reasons:
            self.parent_failure_reasons.append(reason)

    def _put(self, message):
        if not self.started or self.closed:
            self._record_parent_failure("worker is not active")
            return False
        if not self.process.is_alive():
            self._record_parent_failure("worker exited unexpectedly")
            return False
        try:
            self.input_queue.put_nowait(message)
        except queue.Full:
            self._record_parent_failure("worker input queue is full")
            return False
        return True

    def submit_samples(self, samples, sent_monotonic=None):
        if sent_monotonic is None:
            sent_monotonic = time.monotonic()
        return self._put(("samples", list(samples), sent_monotonic))

    def submit_marker(self, marker):
        return self._put(("marker", dict(marker)))

    def _put_wait(self, message, timeout_s=1.0):
        """Apply bounded backpressure without treating a transient full queue
        as data loss.  Persistent fullness remains an explicit failure.
        """
        deadline = time.monotonic() + float(timeout_s)
        while True:
            if not self.started or self.closed:
                self._record_parent_failure("worker is not active")
                return False
            if not self.process.is_alive():
                self._record_parent_failure("worker exited unexpectedly")
                return False
            try:
                self.input_queue.put_nowait(message)
                return True
            except queue.Full:
                if time.monotonic() >= deadline:
                    self._record_parent_failure("worker input queue is full")
                    return False
                time.sleep(0.001)

    def submit_samples_wait(self, samples, sent_monotonic=None,
                            timeout_s=1.0):
        if sent_monotonic is None:
            sent_monotonic = time.monotonic()
        return self._put_wait(
            ("samples", list(samples), sent_monotonic), timeout_s)

    def submit_marker_wait(self, marker, timeout_s=1.0):
        return self._put_wait(("marker", dict(marker)), timeout_s)

    # Short aliases make the protocol convenient for small integration tests.
    send_samples = submit_samples
    send_marker = submit_marker

    def poll(self, timeout_s=0.0):
        try:
            message = self.output_queue.get(timeout=float(timeout_s))
        except queue.Empty:
            return None
        self.messages.append(message)
        if message.get("type") == "final":
            self.final_summary = message
        return message

    def drain(self, max_messages=None):
        drained = []
        if max_messages is not None:
            max_messages = max(1, int(max_messages))
        while max_messages is None or len(drained) < max_messages:
            message = self.poll()
            if message is None:
                return drained
            drained.append(message)
        return drained

    def _queue_control(self, message, deadline, wait_callback):
        while True:
            if not self.process.is_alive():
                return False
            try:
                self.input_queue.put_nowait(message)
                return True
            except queue.Full:
                if time.monotonic() >= deadline:
                    return False
                if wait_callback is None:
                    try:
                        self.input_queue.put(message, timeout=min(
                            0.1, max(0.0, deadline - time.monotonic())))
                        return True
                    except queue.Full:
                        continue
                wait_callback(min(0.05, max(
                    0.0, deadline - time.monotonic())))

    def finish(self, extra_failure_reasons=None, timeout_s=30.0,
               wait_callback=None):
        if self.closed:
            return self.final_summary
        self.closed = True
        reasons = list(self.parent_failure_reasons)
        for reason in extra_failure_reasons or ():
            reason = str(reason)
            if reason not in reasons:
                reasons.append(reason)
        deadline = time.monotonic() + float(timeout_s)
        if self.started and self.process.is_alive():
            control = (("finish", reasons) if not reasons else
                       ("abort", "; ".join(reasons)))
            try:
                if not self._queue_control(control, deadline,
                                           wait_callback):
                    if not reasons:
                        reasons.append(
                            "worker input queue is full while finalizing")
            except (OSError, ValueError):
                if not reasons:
                    reasons.append("worker control queue failed")
        while self.final_summary is None and time.monotonic() < deadline:
            message = self.poll(timeout_s=(0.0 if wait_callback is not None
                                           else min(
                                               0.1, max(
                                                   0.0, deadline -
                                                   time.monotonic()))))
            if message is None and not self.process.is_alive():
                break
            if message is None and wait_callback is not None:
                remaining = max(0.0, deadline - time.monotonic())
                if remaining:
                    wait_callback(min(0.05, remaining))
        if self.process.is_alive() and wait_callback is None:
            self.process.join(timeout=max(0.0, deadline - time.monotonic()))
        if self.process.is_alive():
            self.process.terminate()
            if wait_callback is None:
                self.process.join(timeout=1.0)
            else:
                self.process.join(timeout=0.0)
        self.drain()
        if self.final_summary is None:
            self.final_summary = {
                "type": "final",
                "valid": False,
                "replay_valid": False,
                "failure_reasons": reasons or [
                    "worker exited without final summary"],
                "sample_count": 0,
                "batch_count": 0,
                "marker_count": 0,
                "candidate_count": 0,
                "confirmed_count": 0,
                "first_confirmed_failure_flow_mm3_s": None,
                "events": [],
                "detector_config": None,
            }
        elif reasons:
            merged = list(self.final_summary.get("failure_reasons", []))
            for reason in reasons:
                if reason not in merged:
                    merged.append(reason)
            self.final_summary["failure_reasons"] = merged
            self.final_summary["valid"] = False
            self.final_summary["replay_valid"] = False
        return self.final_summary

    def terminate(self):
        """Stop an unfinished child without waiting on Klippy's reactor."""
        self.closed = True
        if self.started and self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=0.0)

    close = finish


__all__ = ["MaxFlowShadowWorker", "DEFAULT_INPUT_QUEUE_SIZE",
           "DEFAULT_STATUS_INTERVAL_S", "DEFAULT_MAX_BACKLOG_AGE_S",
           "DEFAULT_OUTPUT_QUEUE_SIZE", "DEFAULT_MAX_PENDING_MARKERS"]
