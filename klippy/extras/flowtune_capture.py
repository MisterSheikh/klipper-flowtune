# FlowTune
#
# Copyright (C) 2026 Ahmed Sheikh <ahmed.ali.sheikh1998@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
# SPDX-License-Identifier: GPL-3.0-only

"""Streaming raw-capture transport shared by Klippy and offline tools."""

from __future__ import division

import csv
import json
import multiprocessing
import os
import queue
import time


FORMAT_MAGIC = "# flowtune.capture.csv,1"
METADATA_PREFIX = "# metadata "
COLUMNS = [
    "record_type",
    "eventtime",
    "print_time",
    "force_g",
    "counts",
    "tare_counts",
    "name",
    "payload",
]


class CaptureWriterError(RuntimeError):
    pass


def _json_text(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _writer_main(message_queue, status_queue, temp_path, final_path,
                 metadata):
    try:
        try:
            os.nice(20)
        except OSError:
            pass
        with open(temp_path, "w", newline="") as output:
            output.write(FORMAT_MAGIC + "\n")
            output.write(METADATA_PREFIX + _json_text(metadata) + "\n")
            writer = csv.writer(output, lineterminator="\n")
            writer.writerow(COLUMNS)
            while True:
                message = message_queue.get()
                message_type = message[0]
                if message_type == "samples":
                    for sample in message[1]:
                        row = list(sample[:4])
                        if len(row) < 4:
                            row.extend([None] * (4 - len(row)))
                        writer.writerow([
                            "sample", "", row[0], row[1], row[2], row[3],
                            "", "",
                        ])
                elif message_type == "record":
                    _kind, record_type, eventtime, print_time, name, payload = (
                        message)
                    writer.writerow([
                        record_type, eventtime, print_time, "", "", "",
                        name, _json_text(payload),
                    ])
                elif message_type == "finish":
                    writer.writerow([
                        "summary", message[1].get("eventtime"),
                        message[1].get("print_time"), "", "", "", "",
                        _json_text(message[1]),
                    ])
                    output.flush()
                    os.fsync(output.fileno())
                    break
                else:
                    raise CaptureWriterError(
                        "unknown writer message %r" % (message_type,))
        os.rename(temp_path, final_path)
        status_queue.put(("complete", final_path))
    except BaseException as error:
        status_queue.put(("error", "%s: %s"
                          % (error.__class__.__name__, error)))


class CaptureWriter(object):
    """Append capture batches in a child process through a bounded queue."""

    def __init__(self, final_path, metadata, queue_batches=16):
        self.final_path = final_path
        self.temp_path = final_path + ".partial"
        self.metadata = metadata
        self.message_queue = multiprocessing.Queue(maxsize=queue_batches)
        self.status_queue = multiprocessing.Queue(maxsize=1)
        self.process = multiprocessing.Process(
            target=_writer_main,
            args=(self.message_queue, self.status_queue, self.temp_path,
                  self.final_path, self.metadata))
        self.process.daemon = True
        self.started = False
        self.finished = False
        self.producer_error = None

    def start(self):
        if self.started:
            raise CaptureWriterError("capture writer already started")
        output_dir = os.path.dirname(self.final_path)
        if output_dir and not os.path.isdir(output_dir):
            os.makedirs(output_dir)
        self.process.start()
        self.started = True

    def _put_nowait(self, message):
        if not self.started or self.finished:
            self.producer_error = "capture writer is not active"
            return False
        if not self.process.is_alive():
            self.producer_error = self._child_error(
                "capture writer exited unexpectedly")
            return False
        try:
            self.message_queue.put_nowait(message)
        except queue.Full:
            self.producer_error = "capture writer queue is full"
            return False
        return True

    def write_samples(self, samples):
        return self._put_nowait(("samples", samples))

    def write_record(self, record_type, eventtime=None, print_time=None,
                     name="", payload=None):
        if payload is None:
            payload = {}
        return self._put_nowait((
            "record", record_type, eventtime, print_time, name, payload))

    def _child_error(self, fallback):
        try:
            status, detail = self.status_queue.get_nowait()
        except queue.Empty:
            return fallback
        if status == "error":
            return detail
        return fallback

    def failure(self):
        if self.producer_error is not None:
            return self.producer_error
        if self.started and not self.finished and not self.process.is_alive():
            return self._child_error("capture writer exited unexpectedly")
        return None

    def finish(self, summary, wait_callback=None, timeout=30.0):
        if not self.started:
            raise CaptureWriterError("capture writer was not started")
        if self.finished:
            raise CaptureWriterError("capture writer already finished")
        if wait_callback is None:
            wait_callback = time.sleep
        deadline = time.monotonic() + timeout
        finish_message = ("finish", summary)
        while True:
            if not self.process.is_alive():
                raise CaptureWriterError(self._child_error(
                    "capture writer exited before finalization"))
            try:
                self.message_queue.put_nowait(finish_message)
                break
            except queue.Full:
                if time.monotonic() >= deadline:
                    raise CaptureWriterError(
                        "timed out queueing capture summary")
                wait_callback(0.05)
        while self.process.is_alive():
            if time.monotonic() >= deadline:
                raise CaptureWriterError(
                    "timed out finalizing capture file")
            wait_callback(0.05)
        self.process.join()
        self.finished = True
        try:
            status, detail = self.status_queue.get_nowait()
        except queue.Empty:
            status, detail = ("error", "capture writer returned no status")
        if status != "complete" or self.process.exitcode:
            raise CaptureWriterError(detail)
        return detail


def _optional_float(value):
    return None if value in (None, "") else float(value)


def _optional_int(value):
    return None if value in (None, "") else int(value)


def read_capture(path):
    """Load one streaming capture for offline validation and analysis."""
    with open(path, "r", newline="") as capture_file:
        magic = capture_file.readline().rstrip("\r\n")
        if magic != FORMAT_MAGIC:
            raise ValueError("unsupported FlowTune capture format")
        metadata_line = capture_file.readline().rstrip("\r\n")
        if not metadata_line.startswith(METADATA_PREFIX):
            raise ValueError("FlowTune capture metadata is missing")
        metadata = json.loads(metadata_line[len(METADATA_PREFIX):])
        reader = csv.DictReader(capture_file)
        if reader.fieldnames != COLUMNS:
            raise ValueError("unexpected FlowTune capture columns")
        samples = []
        events = []
        telemetry = []
        summaries = []
        for row in reader:
            record_type = row["record_type"]
            if record_type == "sample":
                samples.append([
                    float(row["print_time"]),
                    _optional_float(row["force_g"]),
                    _optional_int(row["counts"]),
                    _optional_int(row["tare_counts"]),
                ])
                continue
            payload = json.loads(row["payload"] or "{}")
            record = {
                "record_type": record_type,
                "eventtime": _optional_float(row["eventtime"]),
                "print_time": _optional_float(row["print_time"]),
                "name": row["name"],
                "payload": payload,
            }
            if record_type == "event":
                events.append(record)
            elif record_type == "telemetry":
                telemetry.append(record)
            elif record_type == "summary":
                summaries.append(record)
            else:
                raise ValueError(
                    "unknown FlowTune record type %r" % (record_type,))
    if len(summaries) != 1:
        raise ValueError(
            "capture must contain exactly one summary record")
    return {
        "metadata": metadata,
        "samples": samples,
        "events": events,
        "telemetry": telemetry,
        "summary": summaries[0]["payload"],
    }
