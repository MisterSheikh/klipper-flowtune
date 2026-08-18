# FlowTune
#
# Copyright (C) 2026 Ahmed Sheikh <ahmed.ali.sheikh1998@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
# SPDX-License-Identifier: GPL-3.0-only

"""Narrow interruptible pure-E motion support for FlowTune.

Klipper's public ``toolhead.drip_move()`` deliberately removes all extra-axis
motion, including E.  Maximum-flow calibration needs the same progressively
generated motion semantics while keeping the toolhead stationary.  This module
contains the small amount of private-interface adaptation needed for that path
and keeps its motion-profile arithmetic independently testable.
"""

from __future__ import division

import math


DRIP_SEGMENT_TIME = 0.050
# Klipper's native drip loop keeps approximately 100ms queued because it does
# no detector or controller work before generating the next step chunk.
# FlowTune performs bounded host-side analysis between refills, so it uses a
# low/high-water window instead of allowing that work to consume the native
# drip margin.
DRIP_LOW_WATER_TIME = 0.150
DRIP_HIGH_WATER_TIME = 0.250
SDS_CHECK_TIME = 0.001


def _finite_nonnegative(value, name):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("%s must be a finite nonnegative number" % name)
    if not math.isfinite(value) or value < 0.0:
        raise ValueError("%s must be a finite nonnegative number" % name)
    return value


def _finite_positive(value, name):
    value = _finite_nonnegative(value, name)
    if value <= 0.0:
        raise ValueError("%s must be a finite positive number" % name)
    return value


def plan_forward_segment(distance, start_velocity, cruise_velocity,
                         acceleration, stop_at_end=False):
    """Return a forward-only trapezoid without a zero-speed junction.

    Ascending search segments accelerate at their beginning and retain their
    attained velocity at the boundary.  A caller may request a final decel to
    zero only when it already knows the path ends at this segment.
    """
    distance = _finite_positive(distance, "distance")
    start_velocity = _finite_nonnegative(start_velocity, "start_velocity")
    cruise_velocity = _finite_positive(cruise_velocity, "cruise_velocity")
    acceleration = _finite_positive(acceleration, "acceleration")
    if cruise_velocity + 1.0e-12 < start_velocity:
        raise ValueError("forward drip segments may not reduce velocity")

    target_velocity = cruise_velocity
    accel_distance = max(
        0.0, (target_velocity * target_velocity -
              start_velocity * start_velocity) / (2.0 * acceleration))
    decel_distance = (target_velocity * target_velocity /
                      (2.0 * acceleration) if stop_at_end else 0.0)

    if accel_distance + decel_distance > distance + 1.0e-12:
        raise ValueError(
            "segment is too short to reach the requested flow%s at the "
            "configured extruder acceleration" %
            (" and stop" if stop_at_end else ""))

    cruise_distance = max(0.0, distance - accel_distance - decel_distance)
    accel_time = ((target_velocity - start_velocity) / acceleration
                  if target_velocity > start_velocity else 0.0)
    cruise_time = cruise_distance / target_velocity
    decel_time = target_velocity / acceleration if stop_at_end else 0.0
    duration = accel_time + cruise_time + decel_time
    return {
        "distance": distance,
        "accel_t": accel_time,
        "cruise_t": cruise_time,
        "decel_t": decel_time,
        "duration_s": duration,
        "start_v": start_velocity,
        "cruise_v": target_velocity,
        "end_v": 0.0 if stop_at_end else target_velocity,
        "accel": acceleration,
        "stop_at_end": bool(stop_at_end),
    }


def profile_distance_at(profile, elapsed):
    """Return distance travelled through ``profile`` at ``elapsed``."""
    elapsed = max(0.0, min(float(elapsed), profile["duration_s"]))
    accel_t = profile["accel_t"]
    cruise_t = profile["cruise_t"]
    decel_t = profile["decel_t"]
    start_v = profile["start_v"]
    cruise_v = profile["cruise_v"]
    accel = profile["accel"]
    if elapsed <= accel_t:
        return start_v * elapsed + 0.5 * accel * elapsed * elapsed
    distance = (start_v * accel_t +
                0.5 * accel * accel_t * accel_t)
    elapsed -= accel_t
    if elapsed <= cruise_t:
        return distance + cruise_v * elapsed
    distance += cruise_v * cruise_t
    elapsed = min(elapsed - cruise_t, decel_t)
    return distance + cruise_v * elapsed - 0.5 * accel * elapsed * elapsed


class EDripQueue(object):
    """Adapter around the Q2/mainline extruder trapq and motion flusher."""

    def __init__(self, toolhead, extruder):
        self.toolhead = toolhead
        self.extruder = extruder
        self.motion_queuing = getattr(toolhead, "motion_queuing", None)
        required_toolhead = (
            "_flush_lookahead", "_calc_print_time", "_advance_move_time",
            "dwell")
        required_queue = (
            "_await_flush_time", "_advance_flush_time",
            "get_kin_flush_delay", "note_mcu_movequeue_activity",
            "wipe_trapq")
        missing = [name for name in required_toolhead
                   if not callable(getattr(toolhead, name, None))]
        missing += ["motion_queuing.%s" % name for name in required_queue
                    if not callable(getattr(self.motion_queuing, name, None))]
        if not callable(getattr(extruder, "trapq_append", None)):
            missing.append("extruder.trapq_append")
        if getattr(extruder, "trapq", None) is None:
            missing.append("extruder.trapq")
        if missing:
            raise RuntimeError(
                "E-drip is unavailable; missing Klipper interfaces: %s" %
                ", ".join(missing))
        self.start_time = None
        self.planned_end_time = None
        self.flush_time = None
        self.current_e = None
        self.end_velocity = 0.0
        self.entries = []
        self.active = False
        self.minimum_committed_lead_s = None
        self.maximum_refill_gap_s = 0.0
        self.refill_count = 0
        self.late_refill_count = 0
        self._last_refill_eventtime = None

    def begin(self):
        if self.active:
            raise RuntimeError("E-drip queue is already active")
        self.toolhead.dwell(
            self.motion_queuing.get_kin_flush_delay())
        self.toolhead._calc_print_time()
        self.start_time = float(self.toolhead.print_time)
        self.planned_end_time = self.start_time
        self.flush_time = self.start_time
        self.current_e = float(self.toolhead.get_position()[3])
        self.end_velocity = 0.0
        self.entries = []
        self.active = True
        self.minimum_committed_lead_s = None
        self.maximum_refill_gap_s = 0.0
        self.refill_count = 0
        self.late_refill_count = 0
        self._last_refill_eventtime = None
        return self.start_time

    def append(self, segment, acceleration, stop_at_end=False):
        if not self.active:
            raise RuntimeError("E-drip queue has not begun")
        feed = _finite_positive(segment["feed_mm_s"], "feed_mm_s")
        profile = plan_forward_segment(
            segment["filament_mm"], self.end_velocity, feed,
            acceleration, stop_at_end=stop_at_end)
        start_time = self.planned_end_time
        end_time = start_time + profile["duration_s"]
        start_e = self.current_e
        end_e = start_e + profile["distance"]
        self.extruder.trapq_append(
            self.extruder.trapq, start_time,
            profile["accel_t"], profile["cruise_t"], profile["decel_t"],
            start_e, 0.0, 0.0, 1.0, 0.0, 0.0,
            profile["start_v"], profile["cruise_v"], profile["accel"])
        entry = {
            "segment": dict(segment),
            "profile": profile,
            "start_time": start_time,
            "end_time": end_time,
            "start_e": start_e,
            "end_e": end_e,
            "start_marker_emitted": False,
            "end_marker_emitted": False,
        }
        self.entries.append(entry)
        self.planned_end_time = end_time
        self.current_e = end_e
        self.end_velocity = profile["end_v"]
        self.toolhead.commanded_pos[3] = end_e
        self.extruder.last_position = end_e
        self.toolhead._advance_move_time(end_time)
        return entry

    def position_at(self, print_time):
        if not self.entries:
            return self.current_e
        print_time = float(print_time)
        for entry in self.entries:
            if print_time <= entry["end_time"]:
                elapsed = max(0.0, print_time - entry["start_time"])
                return (entry["start_e"] +
                        profile_distance_at(entry["profile"], elapsed))
        return self.entries[-1]["end_e"]

    def enter_drip_mode(self):
        mq = self.motion_queuing
        mq.drip_start_times.append(self.start_time)
        mq._await_flush_time(self.start_time)
        mq.reactor.update_timer(mq.flush_timer, mq.reactor.NEVER)
        mq.do_kick_flush_timer = False
        mq._advance_flush_time(
            self.start_time - SDS_CHECK_TIME, self.start_time)
        self._last_refill_eventtime = mq.reactor.monotonic()

    def _record_lead(self, lead):
        lead = float(lead)
        if (self.minimum_committed_lead_s is None or
                lead < self.minimum_committed_lead_s):
            self.minimum_committed_lead_s = lead
        if lead < 0.0:
            self.late_refill_count += 1

    def _record_refill(self, eventtime):
        eventtime = float(eventtime)
        if self._last_refill_eventtime is not None:
            self.maximum_refill_gap_s = max(
                self.maximum_refill_gap_s,
                eventtime - self._last_refill_eventtime)
        self._last_refill_eventtime = eventtime
        self.refill_count += 1

    def advance_once(self, completion):
        if completion.test():
            return False
        mq = self.motion_queuing
        if not mq.can_pause:
            return False
        curtime = mq.reactor.monotonic()
        est_print_time = mq.mcu.estimated_print_time(curtime)
        lead = self.flush_time - est_print_time
        self._record_lead(lead)
        wait_time = lead - DRIP_LOW_WATER_TIME
        if wait_time > 0.0:
            completion.wait(curtime + wait_time)
            if completion.test() or not mq.can_pause:
                return False
            curtime = mq.reactor.monotonic()
            est_print_time = mq.mcu.estimated_print_time(curtime)
            lead = self.flush_time - est_print_time
            self._record_lead(lead)
        if self.flush_time >= self.planned_end_time:
            return False
        target = min(
            self.planned_end_time,
            est_print_time + DRIP_HIGH_WATER_TIME)
        advanced = False
        while self.flush_time < target - 1.0e-12:
            self.flush_time = min(
                self.flush_time + DRIP_SEGMENT_TIME, target)
            mq.note_mcu_movequeue_activity(self.flush_time)
            mq._advance_flush_time(
                self.flush_time - SDS_CHECK_TIME, self.flush_time)
            self._record_refill(curtime)
            advanced = True
        return advanced

    def timing_summary(self):
        return {
            "low_water_s": DRIP_LOW_WATER_TIME,
            "high_water_s": DRIP_HIGH_WATER_TIME,
            "step_chunk_s": DRIP_SEGMENT_TIME,
            "minimum_committed_lead_s": self.minimum_committed_lead_s,
            "maximum_refill_gap_s": self.maximum_refill_gap_s,
            "refill_count": self.refill_count,
            "late_refill_count": self.late_refill_count,
        }

    def finish(self, interrupted=False):
        if not self.active:
            return None
        mq = self.motion_queuing
        committed_through = min(self.flush_time, self.planned_end_time)
        stop_time = min(
            committed_through + mq.get_kin_flush_delay(),
            self.planned_end_time)
        try:
            mq.reactor.update_timer(mq.flush_timer, mq.reactor.NOW)
            mq._advance_flush_time(stop_time)
        finally:
            if self.start_time in mq.drip_start_times:
                mq.drip_start_times.remove(self.start_time)
            try:
                mq.wipe_trapq(self.extruder.trapq)
            finally:
                self.active = False
        # The final kinematic flush may commit a small amount beyond the
        # regular drip horizon.  Treat that as the actual stop boundary for
        # both position reconciliation and event-marker publication.
        self.flush_time = stop_time
        if interrupted:
            actual_e = self.position_at(stop_time)
            self.toolhead.commanded_pos[3] = actual_e
            self.extruder.last_position = actual_e
            self.current_e = actual_e
            self.toolhead.print_time = stop_time
        return {
            "interrupted": bool(interrupted),
            "stop_print_time": stop_time,
            "committed_through_print_time": committed_through,
            "planned_end_print_time": self.planned_end_time,
            "actual_e_mm": self.current_e,
        }


__all__ = [
    "DRIP_HIGH_WATER_TIME",
    "DRIP_LOW_WATER_TIME",
    "DRIP_SEGMENT_TIME",
    "EDripQueue",
    "plan_forward_segment",
    "profile_distance_at",
]
