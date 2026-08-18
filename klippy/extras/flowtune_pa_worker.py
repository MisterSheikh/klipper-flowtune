# FlowTune
#
# Copyright (C) 2026 Ahmed Sheikh <ahmed.ali.sheikh1998@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
# SPDX-License-Identifier: GPL-3.0-only

"""Low-priority process wrapper for finalized FlowPA analysis."""

from __future__ import division

import json
import multiprocessing
import os
import queue
import resource
import time


DEFAULT_TIMEOUT_S = 60.0


def _limit_numeric_threads():
    # These variables must be set before importing NumPy in the child.
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS",
                 "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"


def _nice_child():
    try:
        os.nice(20)
    except (AttributeError, OSError):
        pass


def _import_analyzer():
    try:
        from . import flowtune_pa
    except ImportError:
        import flowtune_pa
    return flowtune_pa


def _probe_main(output_queue):
    try:
        _limit_numeric_threads()
        _nice_child()
        import numpy
        output_queue.put({
            "ok": True,
            "numpy_version": numpy.__version__,
        })
    except BaseException as error:
        output_queue.put({
            "ok": False,
            "error": "%s: %s" % (error.__class__.__name__, error),
        })


def _analysis_main(output_queue, capture_path, report_path):
    started = time.monotonic()
    try:
        _limit_numeric_threads()
        _nice_child()
        analyzer = _import_analyzer()
        result = analyzer.analyze_capture(capture_path)
        usage = resource.getrusage(resource.RUSAGE_SELF)
        result["analyzer_process"] = {
            "elapsed_s": time.monotonic() - started,
            "peak_rss_kib": int(usage.ru_maxrss),
            "numpy_version": __import__("numpy").__version__,
            "numeric_threads": 1,
            "nice": 20,
        }
        temp_path = report_path + ".partial"
        with open(temp_path, "w") as output:
            json.dump(result, output, indent=2, sort_keys=True,
                      allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, report_path)
        output_queue.put({
            "ok": True,
            "result": result,
            "report_path": report_path,
        })
    except BaseException as error:
        output_queue.put({
            "ok": False,
            "error": "%s: %s" % (error.__class__.__name__, error),
            "report_path": report_path,
        })


def _wait_process(process, output_queue, timeout_s, wait_callback):
    deadline = time.monotonic() + float(timeout_s)
    message = None
    while time.monotonic() < deadline:
        try:
            message = output_queue.get_nowait()
            break
        except queue.Empty:
            pass
        if not process.is_alive():
            break
        if wait_callback is None:
            time.sleep(0.05)
        else:
            wait_callback(0.05)
    if message is None:
        try:
            message = output_queue.get_nowait()
        except queue.Empty:
            message = None
    if process.is_alive() and message is not None:
        grace_deadline = time.monotonic() + 1.0
        while process.is_alive() and time.monotonic() < grace_deadline:
            if wait_callback is None:
                time.sleep(0.01)
            else:
                wait_callback(0.01)
    if process.is_alive():
        process.terminate()
        process.join(timeout=1.0 if wait_callback is None else 0.0)
    else:
        process.join(timeout=0.0)
    if message is None:
        if time.monotonic() >= deadline:
            return {"ok": False, "error": "FlowPA analyzer timed out"}
        return {"ok": False,
                "error": "FlowPA analyzer exited without a result"}
    if process.exitcode not in (None, 0) and message.get("ok"):
        return {"ok": False,
                "error": "FlowPA analyzer exited with status %s" %
                         process.exitcode}
    return message


def probe_numpy(timeout_s=10.0, wait_callback=None):
    output_queue = multiprocessing.Queue(maxsize=1)
    process = multiprocessing.Process(
        target=_probe_main, args=(output_queue,))
    process.daemon = True
    process.start()
    return _wait_process(
        process, output_queue, timeout_s, wait_callback)


def analyze_capture(capture_path, report_path=None,
                    timeout_s=DEFAULT_TIMEOUT_S, wait_callback=None):
    capture_path = os.path.abspath(capture_path)
    if report_path is None:
        stem, _extension = os.path.splitext(capture_path)
        report_path = stem + ".flowpa.json"
    report_path = os.path.abspath(report_path)
    output_queue = multiprocessing.Queue(maxsize=1)
    process = multiprocessing.Process(
        target=_analysis_main,
        args=(output_queue, capture_path, report_path))
    process.daemon = True
    process.start()
    return _wait_process(
        process, output_queue, timeout_s, wait_callback)


__all__ = ["DEFAULT_TIMEOUT_S", "analyze_capture", "probe_numpy"]
