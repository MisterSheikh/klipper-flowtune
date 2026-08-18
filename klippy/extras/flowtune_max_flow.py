# FlowTune
#
# Copyright (C) 2026 Ahmed Sheikh <ahmed.ali.sheikh1998@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
# SPDX-License-Identifier: GPL-3.0-only

"""Causal, bounded release/rebuild detector for maximum-flow replay.

The detector deliberately recognizes one narrow waveform: a large loss of
accumulated extrusion load followed by a bounded rebuild.  It is independent
of a particular load-cell backend and does not select a flow or control
motion.  Samples are consumed in print-time order and no future samples are
used to emit a candidate.
"""

from collections import deque
import copy
import math


DETECTOR_CONFIG_VERSION = "flowtune.max_flow_detector.v1"

# These are replay-tuning constants, not universal material or printer
# defaults.  The thresholds are intentionally broad relative to the clean
# staircase response (under roughly 0.25 kg over 50 ms in the two captures)
# and the observed releases (roughly 2--3 kg).
DEFAULT_REPLAY_TUNING = {
    "version": DETECTOR_CONFIG_VERSION,
    "short_change_window_s": 0.050,
    "minimum_prepeak_load_g": 1400.0,
    "minimum_drop_g": 1200.0,
    "minimum_drop_fraction": 0.45,
    "minimum_rebuild_g": 700.0,
    "minimum_rebuild_fraction": 0.45,
    "maximum_rebuild_window_s": 0.75,
    "cooldown_s": 0.10,
    "polarity_inference_min_change_g": 100.0,
    "polarity_inference_min_samples": 32,
    "safe_change_multiplier": 8.0,
    "max_history_samples": 2048,
    "max_reference_samples": 512,
    "max_safe_change_samples": 512,
    "max_event_count": 128,
}

ACTIVE_PHASES = frozenset(("active", "flow_segment"))
IGNORED_PHASES = frozenset(("inactive", "recovery", "purge", "stop"))


def _finite(value, name):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("%s must be finite" % name)
    if not math.isfinite(result):
        raise ValueError("%s must be finite" % name)
    return result


def _positive(value, name):
    result = _finite(value, name)
    if result <= 0.0:
        raise ValueError("%s must be positive" % name)
    return result


def _fraction(value, name):
    result = _finite(value, name)
    if not 0.0 < result <= 1.0:
        raise ValueError("%s must be in (0, 1]" % name)
    return result


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return float((ordered[middle - 1] + ordered[middle]) / 2.0)


class MaxFlowReleaseRebuildDetector(object):
    """Consume timestamped force samples and emit release/rebuild events.

    ``force_polarity`` is ``+1`` when increasing force means increasing load
    and ``-1`` when decreasing force means increasing load.  If omitted, the
    direction is inferred causally from the first active loading change.
    ``reference_force`` is the unloaded force in backend units.  If omitted,
    the median of bounded ``inactive`` samples before activation is used.

    ``process_sample`` returns only events emitted by that sample.  The
    detector retains only bounded recent history and a bounded event queue;
    callers needing a stream can use ``drain_events``.
    """

    def __init__(self, tuning=None, force_polarity=None,
                 reference_force=None):
        config = copy.deepcopy(DEFAULT_REPLAY_TUNING)
        if tuning:
            config.update(copy.deepcopy(tuning))
        if config.get("version") != DETECTOR_CONFIG_VERSION:
            raise ValueError("unsupported maximum-flow detector config")
        config["short_change_window_s"] = _positive(
            config["short_change_window_s"], "short_change_window_s")
        config["minimum_prepeak_load_g"] = _positive(
            config["minimum_prepeak_load_g"], "minimum_prepeak_load_g")
        config["minimum_drop_g"] = _positive(
            config["minimum_drop_g"], "minimum_drop_g")
        config["minimum_drop_fraction"] = _fraction(
            config["minimum_drop_fraction"], "minimum_drop_fraction")
        config["minimum_rebuild_g"] = _positive(
            config["minimum_rebuild_g"], "minimum_rebuild_g")
        config["minimum_rebuild_fraction"] = _fraction(
            config["minimum_rebuild_fraction"],
            "minimum_rebuild_fraction")
        config["maximum_rebuild_window_s"] = _positive(
            config["maximum_rebuild_window_s"],
            "maximum_rebuild_window_s")
        config["cooldown_s"] = _finite(config["cooldown_s"], "cooldown_s")
        if config["cooldown_s"] < 0.0:
            raise ValueError("cooldown_s must be nonnegative")
        config["polarity_inference_min_change_g"] = _positive(
            config["polarity_inference_min_change_g"],
            "polarity_inference_min_change_g")
        config["polarity_inference_min_samples"] = int(
            config["polarity_inference_min_samples"])
        if config["polarity_inference_min_samples"] < 3:
            raise ValueError(
                "polarity_inference_min_samples must be at least 3")
        config["safe_change_multiplier"] = _positive(
            config["safe_change_multiplier"], "safe_change_multiplier")
        for key in ("max_history_samples", "max_reference_samples",
                    "max_safe_change_samples", "max_event_count"):
            value = int(config[key])
            if value < 1:
                raise ValueError("%s must be positive" % key)
            config[key] = value
        self._config = config
        if force_polarity is not None:
            force_polarity = _finite(force_polarity, "force_polarity")
            if force_polarity not in (-1.0, 1.0):
                raise ValueError("force_polarity must be -1 or +1")
        self._polarity = force_polarity
        self._reference_force = (None if reference_force is None else
                                 _finite(reference_force, "reference_force"))
        self._reference_samples = deque(
            maxlen=self._config["max_reference_samples"])
        self._polarity_samples = deque(
            maxlen=self._config["max_reference_samples"])
        self._history = deque(maxlen=self._config["max_history_samples"])
        self._safe_history = deque(
            maxlen=self._config["max_history_samples"])
        self._safe_changes = deque(
            maxlen=self._config["max_safe_change_samples"])
        self._events = deque(maxlen=self._config["max_event_count"])
        self._candidate = None
        self._active_first_force = None
        self._last_print_time = None
        self._cooldown_until = None
        self._has_active_sample = False

    @property
    def config(self):
        return copy.deepcopy(self._config)

    @property
    def reference_force(self):
        return self._reference_force

    @property
    def force_polarity(self):
        return self._polarity

    @property
    def history_size(self):
        return len(self._history)

    @property
    def events(self):
        return list(self._events)

    def drain_events(self):
        events = list(self._events)
        self._events.clear()
        return events

    def _set_reference_from_inactive(self):
        if self._reference_force is None and self._reference_samples:
            self._reference_force = _median(list(self._reference_samples))

    def _normalized_load(self, force):
        if self._reference_force is None or self._polarity is None:
            return None
        # Negative normalized loads are unloaded or reference drift; they do
        # not contribute to the accumulated extrusion load.
        return max(0.0, self._polarity * (force - self._reference_force))

    def _observe_safe_change(self, timestamp, force):
        cutoff = timestamp - self._config["short_change_window_s"]
        while self._safe_history and self._safe_history[0][0] < cutoff:
            self._safe_history.popleft()
        if self._safe_history:
            self._safe_changes.append(abs(force - self._safe_history[0][1]))
        self._safe_history.append((timestamp, force))

    def _safe_change_scale(self):
        # The bounded queue retains the most recent known-safe changes.  Its
        # maximum is deliberately conservative and is only consulted when a
        # release candidate is evaluated, so it adds no per-sample sorting.
        return (None if not self._safe_changes
                else float(max(self._safe_changes)))

    def _infer_polarity(self, force):
        if self._polarity is not None:
            return
        if self._active_first_force is None:
            self._active_first_force = force
        self._polarity_samples.append(force)
        if len(self._polarity_samples) < self._config[
                "polarity_inference_min_samples"]:
            return
        baseline = (self._reference_force
                    if self._reference_force is not None
                    else self._active_first_force)
        # A CS1237 batch may begin with a brief transient in the direction
        # opposite to sustained extrusion loading.  Infer from the bounded
        # median response instead of permanently locking to the first sample
        # that crosses the magnitude threshold.
        delta = _median(list(self._polarity_samples)) - baseline
        if abs(delta) < self._config["polarity_inference_min_change_g"]:
            return
        self._polarity = -1.0 if delta < 0.0 else 1.0

    def _rebuild_history_loads(self):
        if self._reference_force is None or self._polarity is None:
            return
        rebuilt = deque(maxlen=self._config["max_history_samples"])
        for row in self._history:
            timestamp, force, _load, _segment, _flow = row
            rebuilt.append((timestamp, force,
                            self._normalized_load(force),
                            _segment, _flow))
        self._history = rebuilt

    def _event_context(self, timestamp, segment_index, flow):
        return {
            "source_print_time": float(timestamp),
            "source_time": float(timestamp),
            "segment_index": (None if segment_index is None
                               else int(segment_index)),
            "flow_mm3_s": (None if flow is None else float(flow)),
        }

    def _emit_candidate(self, timestamp, force, load, prepeak, drop,
                        fraction, safe_change, noise_threshold,
                        segment_index, flow):
        event = self._event_context(timestamp, segment_index, flow)
        event.update({
            "type": "release_candidate",
            "prepeak_g": float(prepeak[1]),
            "trough_g": float(force),
            "prepeak_load_g": float(prepeak[2]),
            "trough_load_g": float(load),
            "drop_g": float(drop),
            "drop_fraction": float(fraction),
            "safe_short_change_g": safe_change,
            "noise_relative_threshold_g": float(noise_threshold),
            "rebuild_g": None,
            "rebuild_fraction": None,
            "rebuild_duration_s": None,
            "candidate_segment_index": event["segment_index"],
            "candidate_flow_mm3_s": event["flow_mm3_s"],
        })
        self._events.append(event)
        self._candidate = {
            "time": float(timestamp),
            "force": float(force),
            "load": float(load),
            "prepeak": prepeak,
            "drop_g": float(drop),
            "drop_fraction": float(fraction),
            "segment_index": event["segment_index"],
            "flow_mm3_s": event["flow_mm3_s"],
            "trough_time": float(timestamp),
            "trough_force": float(force),
            "trough_load": float(load),
            "safe_short_change_g": safe_change,
            "noise_relative_threshold_g": float(noise_threshold),
        }
        return event

    def _emit_confirmed(self, timestamp, force, load, segment_index, flow):
        candidate = self._candidate
        rebuild_g = load - candidate["trough_load"]
        rebuild_fraction = (rebuild_g / candidate["drop_g"]
                            if candidate["drop_g"] > 0.0 else 0.0)
        event = self._event_context(timestamp, candidate["segment_index"],
                                    candidate["flow_mm3_s"])
        event.update({
            "type": "release_confirmed",
            "prepeak_g": float(candidate["prepeak"][1]),
            "trough_g": float(candidate["trough_force"]),
            "prepeak_load_g": float(candidate["prepeak"][2]),
            "trough_load_g": float(candidate["trough_load"]),
            "drop_g": float(candidate["drop_g"]),
            "drop_fraction": float(candidate["drop_fraction"]),
            "safe_short_change_g": candidate["safe_short_change_g"],
            "noise_relative_threshold_g": float(
                candidate["noise_relative_threshold_g"]),
            "rebuild_g": float(rebuild_g),
            "rebuild_fraction": float(rebuild_fraction),
            "rebuild_duration_s": float(
                timestamp - candidate["trough_time"]),
            "candidate_to_confirmation_s": float(
                timestamp - candidate["time"]),
            "candidate_print_time": float(candidate["time"]),
            "trough_print_time": float(candidate["trough_time"]),
            "confirmation_print_time": float(timestamp),
            "confirmation_segment_index": (None if segment_index is None
                                             else int(segment_index)),
            "confirmation_flow_mm3_s": (None if flow is None
                                         else float(flow)),
            "candidate_segment_index": candidate["segment_index"],
            "candidate_flow_mm3_s": candidate["flow_mm3_s"],
        })
        self._events.append(event)
        self._candidate = None
        self._cooldown_until = timestamp + self._config["cooldown_s"]
        return event

    def process_sample(self, print_time, force_g, phase="active",
                       segment_index=None, flow_mm3_s=None):
        """Process one sample and return events emitted at its timestamp."""
        emitted = []
        timestamp = _finite(print_time, "print_time")
        force = _finite(force_g, "force_g")
        if self._last_print_time is not None and timestamp < self._last_print_time:
            raise ValueError("print_time must be monotonic")
        self._last_print_time = timestamp
        phase = "active" if phase is None else str(phase)
        if phase not in ACTIVE_PHASES:
            if phase in ("inactive", "recovery", "purge", "stop"):
                if (phase == "inactive" and self._reference_force is None
                        and not self._has_active_sample):
                    self._reference_samples.append(force)
                elif phase in ("recovery", "purge"):
                    # Freeze the initial unloaded reference before a purge or
                    # recovery waveform can contaminate it.
                    self._set_reference_from_inactive()
                if phase == "purge":
                    self._observe_safe_change(timestamp, force)
                    self._infer_polarity(force)
                self._history.clear()
                self._candidate = None
                self._cooldown_until = None
                if self._polarity is None and phase != "purge":
                    self._active_first_force = None
                return []
            raise ValueError("unknown maximum-flow detector phase: %s" % phase)

        if self._reference_force is None:
            self._set_reference_from_inactive()
        if self._reference_force is None:
            self._reference_force = force
        if self._active_first_force is None:
            self._active_first_force = force
        self._infer_polarity(force)
        if self._polarity is None:
            self._history.append((timestamp, force, None,
                                  segment_index, flow_mm3_s))
            self._has_active_sample = True
            return []
        if any(row[2] is None for row in self._history):
            self._rebuild_history_loads()
        load = self._normalized_load(force)
        cutoff = timestamp - self._config["short_change_window_s"]
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()
        prepeak = max(self._history, key=lambda row: row[2], default=None)
        self._history.append((timestamp, force, load,
                              segment_index, flow_mm3_s))
        self._has_active_sample = True

        if self._candidate is not None:
            candidate = self._candidate
            if load < candidate["trough_load"]:
                candidate["trough_load"] = load
                candidate["trough_force"] = force
                candidate["trough_time"] = timestamp
                candidate["drop_g"] = (
                    candidate["prepeak"][2] - candidate["trough_load"])
                candidate["drop_fraction"] = (
                    candidate["drop_g"] / candidate["prepeak"][2])
            if timestamp - candidate["time"] > self._config[
                    "maximum_rebuild_window_s"]:
                self._candidate = None
            elif (load - candidate["trough_load"] >=
                  self._config["minimum_rebuild_g"] and
                  (load - candidate["trough_load"]) >=
                  candidate["drop_g"] *
                  self._config["minimum_rebuild_fraction"]):
                emitted.append(self._emit_confirmed(
                    timestamp, force, load, segment_index, flow_mm3_s))
        if self._candidate is None and prepeak is not None:
            drop = prepeak[2] - load
            fraction = drop / prepeak[2] if prepeak[2] > 0.0 else 0.0
            safe_change = self._safe_change_scale()
            noise_threshold = self._config["minimum_drop_g"]
            if safe_change is not None:
                noise_threshold = max(
                    noise_threshold,
                    safe_change * self._config["safe_change_multiplier"])
            cooldown = (self._cooldown_until is not None and
                        timestamp < self._cooldown_until)
            if (not cooldown and prepeak[2] >=
                    self._config["minimum_prepeak_load_g"] and
                    drop >= noise_threshold and
                    fraction >= self._config["minimum_drop_fraction"]):
                emitted.append(self._emit_candidate(
                    timestamp, force, load, prepeak, drop, fraction,
                    safe_change, noise_threshold, segment_index, flow_mm3_s))
        return emitted

    def process_batch(self, samples, phase="active", segment_index=None,
                      flow_mm3_s=None):
        """Process a batch of ``(print_time, force)`` rows."""
        emitted = []
        for row in samples:
            if isinstance(row, dict):
                timestamp = row.get("print_time")
                force = row.get("force_g", row.get("force"))
                row_phase = row.get("phase", phase)
                row_segment = row.get("segment_index", segment_index)
                row_flow = row.get("flow_mm3_s", flow_mm3_s)
            else:
                timestamp, force = row[:2]
                row_phase, row_segment, row_flow = phase, segment_index, flow_mm3_s
            emitted.extend(self.process_sample(
                timestamp, force, row_phase, row_segment, row_flow))
        return emitted

    update = process_sample
    process = process_sample


class MaxFlowDecisionTracker(object):
    """Record a report-only first-failure proposal without actuating it."""

    def __init__(self, backoff_mm3_s=1.0, fine_step_mm3_s=0.1):
        self.backoff_mm3_s = _positive(backoff_mm3_s, "backoff_mm3_s")
        self.fine_step_mm3_s = _positive(
            fine_step_mm3_s, "fine_step_mm3_s")
        self.current_flow_mm3_s = None
        self.previous_flow_mm3_s = None
        self._flows_by_segment = {}
        self.q_failure_mm3_s = None
        self.q_last_good_mm3_s = None
        self.decision = None

    def observe_marker(self, marker):
        if marker.get("name") != "max_flow_segment_start":
            return None
        payload = marker.get("payload") or {}
        flow = payload.get("commanded_flow_mm3_s",
                          payload.get("volumetric_flow_mm3_s"))
        flow = _positive(flow, "commanded_flow_mm3_s")
        segment_index = payload.get("segment_index")
        if segment_index is not None:
            self._flows_by_segment[int(segment_index)] = flow
        self.previous_flow_mm3_s = self.current_flow_mm3_s
        self.current_flow_mm3_s = flow
        return None

    def observe_event(self, event):
        if (event.get("type") != "release_confirmed" or
                self.decision is not None):
            return None
        failure = event.get("candidate_flow_mm3_s", event.get("flow_mm3_s"))
        failure = _positive(failure, "q_failure_mm3_s")
        segment_index = event.get("candidate_segment_index",
                                 event.get("segment_index"))
        previous = None
        if segment_index is not None:
            previous = self._flows_by_segment.get(int(segment_index) - 1)
            if previous is None:
                prior = [(index, value) for index, value
                         in self._flows_by_segment.items()
                         if index < int(segment_index) and value < failure]
                if prior:
                    previous = sorted(prior)[-1][1]
        if previous is None:
            previous = self.previous_flow_mm3_s
        if previous is None or previous >= failure:
            previous = max(0.0, failure - self.backoff_mm3_s)
        self.q_failure_mm3_s = failure
        self.q_last_good_mm3_s = previous
        proposed_backoff = max(0.0, failure - self.backoff_mm3_s)
        self.decision = {
            "type": "would_act",
            "proposal_only": True,
            "q_failure_mm3_s": failure,
            "q_last_good_mm3_s": previous,
            "backoff_mm3_s": self.backoff_mm3_s,
            "proposed_backoff_flow_mm3_s": proposed_backoff,
            "fine_step_mm3_s": self.fine_step_mm3_s,
            "next_action": (
                "would_stop_coarse_then_backoff_and_refine"),
        }
        return dict(self.decision)


class MaxFlowSearchController(object):
    """Bounded pure search policy used by live ``CONTROL=1``.

    The controller contains no Klipper or worker calls.  It accepts segment
    outcomes and emits the next motion request, which keeps the safety and
    search policy deterministic and directly unit-testable.  A request is a
    mapping with ``flow_mm3_s``, ``stage``, ``segment_index`` and
    ``speculative`` fields.  Callers may queue one request ahead; such a
    request is recorded as speculative and can never define a boundary.
    """

    STATES = frozenset((
        "coarse", "coarse_recovery", "fine", "fine_recovery",
        "fine_repeat", "complete", "no_limit", "provisional",
        "ambiguous", "aborted"))

    MAX_TEST_FLOW_MM3_S = 500.0
    COMMAND_HISTORY_SIZE = 128
    DECISION_HISTORY_SIZE = 256

    def __init__(self, start_flow=14.0, end_flow=None, coarse_step=1.0,
                 fine_step=0.1, recommendation_margin=0.5,
                 fine_recovery_backoff=0.3,
                 coarse_recovery_backoff=None, max_segments=None,
                 max_test_flow=None):
        self.start_flow_mm3_s = _positive(start_flow, "start_flow_mm3_s")
        if end_flow is not None and max_test_flow is not None:
            raise ValueError(
                "end_flow_mm3_s and max_test_flow_mm3_s must not both be "
                "supplied")
        if max_test_flow is None:
            max_test_flow = end_flow
        if max_test_flow is None:
            raise ValueError("max_test_flow_mm3_s must be supplied")
        self.max_test_flow_mm3_s = _positive(
            max_test_flow, "max_test_flow_mm3_s")
        self.coarse_step_mm3_s = _positive(coarse_step,
                                            "coarse_step_mm3_s")
        self.fine_step_mm3_s = _positive(fine_step, "fine_step_mm3_s")
        self.recommendation_margin_mm3_s = _positive(
            recommendation_margin, "recommendation_margin_mm3_s")
        self.fine_recovery_backoff_mm3_s = _positive(
            fine_recovery_backoff, "fine_recovery_backoff_mm3_s")
        if coarse_recovery_backoff is None:
            coarse_recovery_backoff = self.coarse_step_mm3_s
        self.coarse_recovery_backoff_mm3_s = _positive(
            coarse_recovery_backoff, "coarse_recovery_backoff_mm3_s")
        if max_segments is not None:
            try:
                max_segments = int(max_segments)
            except (TypeError, ValueError, OverflowError):
                raise ValueError("max_segments must be an integer")
            if max_segments < 1:
                raise ValueError("max_segments must be positive")
        if self.start_flow_mm3_s > self.MAX_TEST_FLOW_MM3_S:
            raise ValueError("start_flow_mm3_s must be at most %.1f" %
                             self.MAX_TEST_FLOW_MM3_S)
        if self.max_test_flow_mm3_s > self.MAX_TEST_FLOW_MM3_S:
            raise ValueError("max_test_flow_mm3_s must be at most %.1f" %
                             self.MAX_TEST_FLOW_MM3_S)
        if self.max_test_flow_mm3_s < self.start_flow_mm3_s:
            raise ValueError(
                "max_test_flow_mm3_s must be at least start_flow_mm3_s")
        if self.fine_step_mm3_s >= self.coarse_step_mm3_s:
            raise ValueError("fine_step_mm3_s must be smaller than "
                             "coarse_step_mm3_s")
        if self.recommendation_margin_mm3_s >= self.start_flow_mm3_s:
            raise ValueError("recommendation_margin_mm3_s must be smaller "
                             "than start_flow_mm3_s")
        if self.fine_recovery_backoff_mm3_s >= self.coarse_step_mm3_s:
            raise ValueError("fine_recovery_backoff_mm3_s must be smaller "
                             "than coarse_step_mm3_s")
        if self.fine_recovery_backoff_mm3_s < self.fine_step_mm3_s:
            raise ValueError("fine_recovery_backoff_mm3_s must be at least "
                             "fine_step_mm3_s")
        if self.coarse_recovery_backoff_mm3_s < self.coarse_step_mm3_s:
            raise ValueError("coarse_recovery_backoff_mm3_s must be at least "
                             "coarse_step_mm3_s")
        self.max_segments = max_segments
        self.state = "coarse"
        self.next_flow_mm3_s = self.start_flow_mm3_s
        self.q_last_good_mm3_s = None
        self.q_failure_mm3_s = None
        self.q_recommended_mm3_s = None
        self.q_backoff_mm3_s = None
        self.coarse_q_last_good_mm3_s = None
        self.coarse_q_failure_mm3_s = None
        self.fine_repeat_original_failure_mm3_s = None
        self.fine_repeat_next_flow_mm3_s = None
        self.fine_repeat_clean_flows_mm3_s = []
        self.segment_index = 0
        # The artifact receives every command/decision as an event.  Klippy
        # retains only a bounded tail for status and final summary reporting.
        # This is history retention, not a search ceiling.
        self.commanded = deque(maxlen=self.COMMAND_HISTORY_SIZE)
        self.commanded_segment_count = 0
        self.decisions = deque(maxlen=self.DECISION_HISTORY_SIZE)
        self.decision_count = 0
        self.speculative_segments = set()
        self.cancelled_segments = set()
        self.observed_segments = set()
        self._observed_segment_history = deque(maxlen=self.COMMAND_HISTORY_SIZE)
        self.result = None

    @property
    def terminal(self):
        return self.state in frozenset((
            "complete", "no_limit", "provisional", "ambiguous",
            "aborted"))

    @property
    def q_failure(self):
        return self.q_failure_mm3_s

    @property
    def q_recommended(self):
        return self.q_recommended_mm3_s

    @property
    def end_flow_mm3_s(self):
        """Deprecated END_FLOW spelling retained for development callers."""
        return self.max_test_flow_mm3_s

    def _record(self, kind, **payload):
        row = {"kind": kind, "state": self.state}
        row.update(payload)
        self.decisions.append(row)
        self.decision_count += 1
        return row

    def _at_or_beyond_end(self, flow):
        return flow >= self.max_test_flow_mm3_s - max(
            1.0e-9, abs(self.max_test_flow_mm3_s) * 1.0e-9)

    def _next_coarse(self, flow):
        value = flow + self.coarse_step_mm3_s
        return round(min(value, self.max_test_flow_mm3_s), 10)

    def _next_fine(self, flow):
        value = flow + self.fine_step_mm3_s
        upper = (self.max_test_flow_mm3_s
                 if self.coarse_q_failure_mm3_s is None
                 else self.coarse_q_failure_mm3_s)
        return round(min(value, upper), 10)

    def _at_or_beyond_fine_upper(self, flow):
        upper = (self.max_test_flow_mm3_s
                 if self.coarse_q_failure_mm3_s is None
                 else self.coarse_q_failure_mm3_s)
        return flow >= upper - max(1.0e-9, abs(upper) * 1.0e-9)

    def _request(self, flow, stage, speculative=False):
        if self.terminal:
            return None
        if (self.max_segments is not None and
                self.commanded_segment_count >= self.max_segments):
            self.state = "provisional"
            self.result = self._record(
                "result", validity="provisional",
                reason="search_segment_budget_exhausted",
                max_segments=self.max_segments)
            return None
        flow = _positive(flow, "flow_mm3_s")
        if flow > self.max_test_flow_mm3_s + 1.0e-9:
            raise ValueError("requested flow exceeds max_test_flow_mm3_s")
        index = self.segment_index
        self.segment_index += 1
        row = {
            "flow_mm3_s": float(flow),
            "commanded_flow_mm3_s": float(flow),
            "stage": str(stage),
            "segment_index": index,
            "speculative": bool(speculative),
            "state": self.state,
        }
        self.commanded.append(dict(row))
        self.commanded_segment_count += 1
        if speculative:
            self.speculative_segments.add(index)
        self._record("command", **row)
        return row

    def _request_or_reuse(self, flow, stage, speculative=False):
        """Reuse one already queued lookahead request when it matches."""
        for row in self.commanded:
            if (row["segment_index"] not in self.observed_segments and
                    row["segment_index"] not in self.cancelled_segments and
                    row["stage"] == stage and
                    abs(row["flow_mm3_s"] - float(flow)) <= 1.0e-9):
                if not speculative:
                    row["speculative"] = False
                    self.speculative_segments.discard(row["segment_index"])
                return dict(row)
        return self._request(flow, stage, speculative)

    def initial_requests(self, lookahead=1):
        """Return the first request and at most one bounded lookahead."""
        if self.state != "coarse" or self.commanded:
            return []
        requests = [self._request(self.next_flow_mm3_s, "coarse")]
        if int(lookahead) > 0 and not self._at_or_beyond_end(
                self.next_flow_mm3_s):
            requests.append(self._request(
                self._next_coarse(self.next_flow_mm3_s), "coarse",
                speculative=True))
        return requests

    def next_request(self, speculative=False):
        """Return one request after the prior outcome, if any."""
        if self.terminal:
            return None
        if self.state == "coarse":
            return self._request_or_reuse(self.next_flow_mm3_s, "coarse",
                                          speculative)
        if self.state == "coarse_recovery":
            return self._request_or_reuse(self.q_backoff_mm3_s,
                                          "coarse_recovery", speculative)
        if self.state == "fine":
            return self._request_or_reuse(self.next_flow_mm3_s, "fine",
                                          speculative)
        if self.state == "fine_recovery":
            return self._request_or_reuse(self.q_backoff_mm3_s,
                                          "fine_recovery", speculative)
        if self.state == "fine_repeat":
            return self._request_or_reuse(self.fine_repeat_next_flow_mm3_s,
                                          "fine_repeat", speculative)
        raise RuntimeError("no request is defined for state %s" % self.state)

    def _clean_boundary(self, flow, segment_index, speculative):
        if speculative or segment_index in self.speculative_segments:
            return False
        return flow <= self.max_test_flow_mm3_s + 1.0e-9

    def observe_segment(self, flow_mm3_s, failure=False, segment_index=None,
                        speculative=False):
        """Consume a completed search segment and choose the next state.

        ``failure`` must mean a confirmed release/rebuild event.  A candidate
        without rebuild is deliberately not enough to advance the search.
        """
        if self.terminal:
            return None
        flow = _positive(flow_mm3_s, "flow_mm3_s")
        if segment_index is None:
            segment_index = self.commanded[-1]["segment_index"] \
                if self.commanded else None
        segment_index = (None if segment_index is None else int(segment_index))
        speculative = bool(speculative or
                           (segment_index in self.speculative_segments
                            if segment_index is not None else False))
        clean_boundary = self._clean_boundary(flow, segment_index,
                                              speculative)
        self._record("segment_observed", flow_mm3_s=flow,
                     segment_index=segment_index, failure=bool(failure),
                     speculative=speculative,
                     clean_boundary=clean_boundary)
        if segment_index is not None and segment_index in self.observed_segments:
            return None
        if segment_index is not None:
            self.observed_segments.add(segment_index)
            if len(self._observed_segment_history) == (
                    self._observed_segment_history.maxlen):
                # Keep duplicate protection bounded as the lazy search grows.
                self.observed_segments.discard(
                    self._observed_segment_history[0])
            self._observed_segment_history.append(segment_index)
        # A queued-ahead segment is evidence only.  It cannot advance the
        # staircase or become either side of the selected boundary.
        if speculative:
            self._record("speculative_excluded", flow_mm3_s=flow,
                         segment_index=segment_index, failure=bool(failure))
            if segment_index is not None:
                self.speculative_segments.discard(segment_index)
            if failure:
                # Retest a speculative flow as a normal segment before
                # allowing it to define the boundary.
                self.next_flow_mm3_s = flow
                self._record("speculative_failure_retest",
                             flow_mm3_s=flow)
            # A clean speculative segment is also excluded.  Retest its flow
            # without lookahead so the 0.1 mm3/s fine grid is preserved.
            elif self.state in ("coarse", "fine"):
                self.next_flow_mm3_s = flow
            return None

        if self.state == "coarse":
            if failure:
                if self.q_last_good_mm3_s is None:
                    self.state = "ambiguous"
                    self.result = self._record(
                        "result", validity="ambiguous",
                        reason="failure_at_start_flow")
                    return None
                self.q_failure_mm3_s = flow
                self.q_backoff_mm3_s = round(max(
                    self.start_flow_mm3_s,
                    flow - self.coarse_recovery_backoff_mm3_s), 10)
                self.coarse_q_last_good_mm3_s = self.q_last_good_mm3_s
                self.coarse_q_failure_mm3_s = flow
                self.state = "coarse_recovery"
                self.next_flow_mm3_s = self.q_backoff_mm3_s
                return self._record("coarse_failure", q_failure_mm3_s=flow,
                                    q_backoff_mm3_s=self.q_backoff_mm3_s)
            if clean_boundary:
                self.q_last_good_mm3_s = flow
            if self._at_or_beyond_end(flow):
                self.state = "no_limit"
                self.result = self._record(
                    "result", validity="no_limit_within_range",
                    q_failure_mm3_s=None,
                    q_recommended_mm3_s=None,
                    reason="max_test_flow_reached_without_failure",
                    max_test_flow_mm3_s=self.max_test_flow_mm3_s)
                return None
            self.next_flow_mm3_s = self._next_coarse(flow)
            return self._record("coarse_clean", q_last_good_mm3_s=
                                self.q_last_good_mm3_s,
                                next_flow_mm3_s=self.next_flow_mm3_s)

        if self.state == "coarse_recovery":
            raise RuntimeError("coarse recovery requires observe_recovery")
        if self.state == "fine":
            if failure:
                self.q_failure_mm3_s = flow
                if self.q_last_good_mm3_s is None:
                    self.state = "ambiguous"
                    self.result = self._record(
                        "result", validity="ambiguous",
                        reason="fine_failure_without_clean_flow")
                    return None
                self.q_backoff_mm3_s = round(max(
                    self.start_flow_mm3_s,
                    flow - self.fine_recovery_backoff_mm3_s), 10)
                self.state = "fine_recovery"
                self.next_flow_mm3_s = self.q_backoff_mm3_s
                return self._record("fine_failure", q_failure_mm3_s=flow,
                                    q_backoff_mm3_s=self.q_backoff_mm3_s)
            if clean_boundary:
                self.q_last_good_mm3_s = flow
            if self._at_or_beyond_fine_upper(flow):
                self.state = "provisional"
                self.result = self._record(
                    "result", validity="provisional",
                    reason=("coarse_failure_did_not_repeat_during_"
                            "fine_search"),
                    coarse_q_failure_mm3_s=
                    self.coarse_q_failure_mm3_s)
                return None
            self.next_flow_mm3_s = self._next_fine(flow)
            return self._record("fine_clean", q_last_good_mm3_s=
                                self.q_last_good_mm3_s,
                                next_flow_mm3_s=self.next_flow_mm3_s)

        if self.state == "fine_repeat":
            original = self.fine_repeat_original_failure_mm3_s
            tolerance = max(1.0e-9, abs(original) * 1.0e-9)
            if failure:
                repeat_delta = original - flow
                resolution_tolerance = self.fine_step_mm3_s + tolerance
                if repeat_delta > resolution_tolerance:
                    self.state = "provisional"
                    self.result = self._record(
                        "result", validity="provisional",
                        reason="fine_repeat_failed_early",
                        q_failure_mm3_s=self.q_failure_mm3_s,
                        fine_repeat_failure_mm3_s=flow,
                        fine_repeat_original_failure_mm3_s=original,
                        fine_repeat_clean_flows_mm3_s=list(
                            self.fine_repeat_clean_flows_mm3_s))
                    return None
                if flow > original + tolerance:
                    self.state = "provisional"
                    self.result = self._record(
                        "result", validity="provisional",
                        reason="fine_repeat_exceeded_original_failure",
                        q_failure_mm3_s=self.q_failure_mm3_s,
                        fine_repeat_failure_mm3_s=flow,
                        fine_repeat_original_failure_mm3_s=original)
                    return None
                selected_failure = min(original, flow)
                repeated_clean = [
                    value for value in self.fine_repeat_clean_flows_mm3_s
                    if value < selected_failure - tolerance]
                selected_last_good = (
                    max(repeated_clean) if repeated_clean else None)
                self.q_failure_mm3_s = selected_failure
                self.q_last_good_mm3_s = selected_last_good
                self.q_recommended_mm3_s = max(
                    0.0, round(
                        selected_failure -
                        self.recommendation_margin_mm3_s, 10))
                self.state = "complete"
                exact_repeat = abs(flow - original) <= tolerance
                self.result = self._record(
                    "result", validity="valid",
                    reason=("fine_failure_repeated_at_same_flow"
                            if exact_repeat else
                            "fine_failure_repeated_within_resolution"),
                    q_failure_mm3_s=self.q_failure_mm3_s,
                    q_last_good_mm3_s=self.q_last_good_mm3_s,
                    q_recommended_mm3_s=self.q_recommended_mm3_s,
                    recommendation_margin_mm3_s=
                    self.recommendation_margin_mm3_s,
                    fine_repeat_clean_flows_mm3_s=list(
                        self.fine_repeat_clean_flows_mm3_s),
                    fine_repeat_failure_mm3_s=flow,
                    fine_repeat_original_failure_mm3_s=original,
                    repeat_difference_mm3_s=abs(flow - original),
                    repeat_tolerance_mm3_s=self.fine_step_mm3_s)
                return self.result
            if flow >= original - tolerance:
                self.state = "provisional"
                self.result = self._record(
                    "result", validity="provisional",
                    reason="failure_did_not_repeat_at_same_flow",
                    q_failure_mm3_s=self.q_failure_mm3_s,
                    fine_repeat_original_failure_mm3_s=original,
                    fine_repeat_clean_flows_mm3_s=list(
                        self.fine_repeat_clean_flows_mm3_s))
                return None
            self.fine_repeat_clean_flows_mm3_s.append(flow)
            self.fine_repeat_next_flow_mm3_s = round(min(
                original, flow + self.fine_step_mm3_s), 10)
            self.next_flow_mm3_s = self.fine_repeat_next_flow_mm3_s
            return self._record(
                "fine_repeat_clean",
                flow_mm3_s=flow,
                next_flow_mm3_s=self.fine_repeat_next_flow_mm3_s,
                fine_repeat_original_failure_mm3_s=original)
        raise RuntimeError("segment outcome is invalid in state %s" %
                           self.state)

    def observe_recovery(self, load_reengaged=False, rebuild_detected=False,
                         release_signature=False, details=None):
        """Validate a recovery without treating an unloaded trace as clean."""
        if self.state not in ("coarse_recovery", "fine_recovery"):
            raise RuntimeError("recovery is not expected in state %s" %
                               self.state)
        credible = (bool(load_reengaged) and bool(rebuild_detected) and
                    not bool(release_signature))
        payload = {
            "load_reengaged": bool(load_reengaged),
            "rebuild_detected": bool(rebuild_detected),
            "release_signature": bool(release_signature),
            "credible": credible,
        }
        if details:
            payload["details"] = dict(details)
        if not credible:
            self.state = "provisional"
            self.result = self._record(
                "result", validity="provisional",
                reason="recovery_not_credible", recovery=payload)
            return None
        if self.state == "coarse_recovery":
            self.state = "fine"
            self.next_flow_mm3_s = self.q_backoff_mm3_s + self.fine_step_mm3_s
            if self._at_or_beyond_fine_upper(self.q_backoff_mm3_s):
                self.state = "provisional"
                self.result = self._record(
                    "result", validity="provisional",
                    reason="fine_search_has_no_interior_points")
                return None
            return self._record("coarse_recovery_confirmed", recovery=payload,
                                next_flow_mm3_s=self.next_flow_mm3_s)
        self.state = "fine_repeat"
        self.fine_repeat_original_failure_mm3_s = self.q_failure_mm3_s
        self.fine_repeat_clean_flows_mm3_s = []
        self.fine_repeat_next_flow_mm3_s = round(min(
            self.fine_repeat_original_failure_mm3_s,
            self.q_backoff_mm3_s + self.fine_step_mm3_s), 10)
        self.next_flow_mm3_s = self.fine_repeat_next_flow_mm3_s
        return self._record(
            "fine_recovery_confirmed", recovery=payload,
            fine_repeat_flow_mm3_s=self.fine_repeat_next_flow_mm3_s,
            fine_repeat_original_failure_mm3_s=
            self.fine_repeat_original_failure_mm3_s)

    def lookahead_request(self, request):
        """Queue one adjacent search request without widening lookahead."""
        if self.terminal or not request or request.get("stage") not in (
                "coarse", "fine", "fine_repeat"):
            return None
        flow = float(request["flow_mm3_s"])
        if request["stage"] == "coarse":
            next_flow = self._next_coarse(flow)
        elif request["stage"] == "fine":
            next_flow = self._next_fine(flow)
        else:
            next_flow = round(min(
                self.fine_repeat_original_failure_mm3_s,
                flow + self.fine_step_mm3_s), 10)
        if abs(next_flow - flow) <= 1.0e-9:
            return None
        return self._request(next_flow, request["stage"], speculative=True)

    def recovery_lookahead_request(self, request):
        """Return the first post-recovery search point as a motion guard.

        The guard preserves continuous extrusion while recovery evidence is
        finalized.  It remains speculative until ``observe_recovery()``
        accepts the recovery and ``next_request()`` promotes the matching
        request.
        """
        if self.terminal or not request:
            return None
        stage = request.get("stage")
        if stage == "coarse_recovery":
            next_stage = "fine"
        elif stage == "fine_recovery":
            next_stage = "fine_repeat"
        else:
            return None
        next_flow = round(min(
            self.q_failure_mm3_s,
            float(request["flow_mm3_s"]) + self.fine_step_mm3_s), 10)
        return self._request(next_flow, next_stage, speculative=True)

    def promote_lookahead(self, segment_index):
        """Promote a queued lookahead after its predecessor proves clean.

        A lookahead is speculative only while the preceding segment's result
        is unresolved.  Once that predecessor is clean, the already-running
        adjacent segment becomes ordinary evidence instead of being repeated.
        """
        segment_index = int(segment_index)
        if segment_index in self.observed_segments:
            raise RuntimeError("cannot promote an observed segment")
        for row in self.commanded:
            if row["segment_index"] == segment_index:
                if segment_index not in self.speculative_segments:
                    return dict(row)
                row["speculative"] = False
                self.speculative_segments.discard(segment_index)
                self._record("lookahead_promoted",
                             segment_index=segment_index,
                             flow_mm3_s=row["flow_mm3_s"],
                             stage=row["stage"])
                return dict(row)
        raise RuntimeError("unknown lookahead segment %d" % segment_index)

    def cancel_pending(self, segment_indices, reason="motion_interrupted"):
        """Exclude requests whose planned motion did not complete."""
        cancelled = []
        for value in segment_indices:
            index = int(value)
            if index in self.observed_segments:
                continue
            self.cancelled_segments.add(index)
            self.speculative_segments.discard(index)
            cancelled.append(index)
        if cancelled:
            self._record("commands_cancelled", segment_indices=cancelled,
                         reason=str(reason))
        return cancelled

    def abort(self, reason):
        self.state = "aborted"
        self.result = self._record("result", validity="rejected",
                                   reason=str(reason))
        return self.result

    def summary(self):
        result = dict(self.result or {})
        result.update({
            "state": self.state,
            "start_flow_mm3_s": self.start_flow_mm3_s,
            "max_test_flow_mm3_s": self.max_test_flow_mm3_s,
            # Legacy key retained so existing consumers can read a new
            # summary while migrating to MAX_TEST_FLOW.
            "end_flow_mm3_s": self.max_test_flow_mm3_s,
            "coarse_step_mm3_s": self.coarse_step_mm3_s,
            "fine_step_mm3_s": self.fine_step_mm3_s,
            "fine_recovery_backoff_mm3_s":
                self.fine_recovery_backoff_mm3_s,
            "coarse_recovery_backoff_mm3_s":
                self.coarse_recovery_backoff_mm3_s,
            "recommendation_margin_mm3_s":
                self.recommendation_margin_mm3_s,
            "coarse_q_last_good_mm3_s":
                self.coarse_q_last_good_mm3_s,
            "coarse_q_failure_mm3_s": self.coarse_q_failure_mm3_s,
            "q_last_good_mm3_s": self.q_last_good_mm3_s,
            "q_failure_mm3_s": self.q_failure_mm3_s,
            "q_recommended_mm3_s": self.q_recommended_mm3_s,
            "commanded_segment_count": self.commanded_segment_count,
            "commanded_segments": list(self.commanded),
            "speculative_segment_indices": sorted(
                self.speculative_segments),
            "cancelled_segment_indices": sorted(self.cancelled_segments),
            "decision_count": self.decision_count,
            "decisions": list(self.decisions),
            "fine_repeat_original_failure_mm3_s":
                self.fine_repeat_original_failure_mm3_s,
            "fine_repeat_clean_flows_mm3_s": list(
                self.fine_repeat_clean_flows_mm3_s),
        })
        return result


def replay_samples(samples, tuning=None, force_polarity=None,
                   reference_force=None):
    """Replay already-attributed samples and return detector plus events."""
    detector = MaxFlowReleaseRebuildDetector(
        tuning=tuning, force_polarity=force_polarity,
        reference_force=reference_force)
    detector.process_batch(samples)
    return detector, detector.events


__all__ = [
    "ACTIVE_PHASES",
    "DEFAULT_REPLAY_TUNING",
    "DETECTOR_CONFIG_VERSION",
    "IGNORED_PHASES",
    "MaxFlowReleaseRebuildDetector",
    "MaxFlowDecisionTracker",
    "MaxFlowSearchController",
    "replay_samples",
]
