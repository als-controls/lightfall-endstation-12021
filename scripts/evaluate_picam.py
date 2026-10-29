"""Evaluate the ctypes PICam driver against a live camera (LightField must be closed).

    python scripts/evaluate_picam.py [--serial 02062413] [--soak N]

Prints a pass/fail line per check plus measurements. Leaves the camera at full
frame, 100 ms exposure, shutter Normal.
"""
from __future__ import annotations

import argparse
import threading
import time

import numpy as np

from lightfall_endstation_12021.devices.picam import PicamCamera, PicamError, _PicamLib

results: list[tuple[str, bool, str]] = []


def check(name):
    def deco(fn):
        def run(*a, **k):
            t = time.perf_counter()
            try:
                info = fn(*a, **k) or ""
                ok = True
            except Exception as e:  # noqa: BLE001
                info, ok = f"{type(e).__name__}: {e}", False
            results.append((name, ok, f"{info} [{time.perf_counter() - t:.1f}s]"))
            print(f"{'PASS' if ok else 'FAIL'}  {name}: {results[-1][2]}", flush=True)
        return run
    return deco


@check("open/close x5 (no leak, lib refcount returns to 0)")
def open_close(serial):
    for _ in range(5):
        PicamCamera(serial).close()
    assert _PicamLib._refs == 0, _PicamLib._refs
    return "ok"


@check("constraints")
def constraints(cam):
    out = {
        "exposure_ms": cam.range("Exposure Time"),
        "adc_speed_MHz": cam.collection("ADC Speed"),
        "shutter": list(cam.enum_choices("Shutter Timing Mode")),
        "trigger": list(cam.enum_choices("Trigger Response")),
        "readout_control": list(cam.enum_choices("Readout Control Mode")),
        "vshift": cam.collection("Vertical Shift Rate"),
    }
    for k in ("ADC Analog Gain", "ADC Quality"):
        if k in cam.params:
            out[k] = list(cam.enum_choices(k))
    return out


@check("online-readable parameters")
def readable(cam):
    return sorted(k for k, v in cam._readable.items() if v)


@check("snap 3 frames @4x4 binning (streamed)")
def snap(cam):
    cam.set_roi(0, 0, 2048, 2048, 4, 4)
    cam.set("Exposure Time", 10.0)
    times = []
    t0 = time.perf_counter()
    n = cam.run(3, lambda img, i: times.append((time.perf_counter() - t0, img.shape, int(img.mean()))))
    assert n == 3 and all(s == (512, 512) for _, s, _ in times)
    return [f"{t:.2f}s mean={m}" for t, _, m in times]


@check("acquire() wrapper returns (n,H,W)")
def wrapper(cam):
    f = cam.acquire(2)
    assert f.shape == (2, 512, 512) and f.dtype == np.uint16
    return f"{f.shape} {f.dtype}"


@check("continuous for ~6 s @8x8, then stop")
def continuous(cam):
    cam.set_roi(0, 0, 2048, 2048, 8, 8)
    cam.set("Exposure Time", 1.0)
    count = [0]
    t_stop = {}

    def stopper():
        time.sleep(6)
        t_stop["req"] = time.perf_counter()
        cam.request_stop()

    threading.Thread(target=stopper, daemon=True).start()
    t0 = time.perf_counter()
    n = cam.run(None, lambda img, i: count.__setitem__(0, count[0] + 1))
    t_end = time.perf_counter()
    assert n == count[0] and n > 1
    return f"{n} frames in {t_end - t0:.1f}s ({n / (t_end - t0):.2f} fps), stop latency {t_end - t_stop['req']:.2f}s"


@check("stop mid full-frame readout")
def stop_mid(cam):
    cam.set_roi()
    cam.set("Exposure Time", 100.0)
    t = {}

    def stopper():
        time.sleep(2.0)
        t["req"] = time.perf_counter()
        cam.request_stop()

    threading.Thread(target=stopper, daemon=True).start()
    n = cam.run(5, lambda img, i: None)
    lat = time.perf_counter() - t["req"]
    assert n < 5
    return f"delivered {n} of 5, stop latency {lat:.2f}s"


@check("timeout raises and camera stays usable")
def timeout(cam):
    cam.set_roi()
    try:
        cam.run(1, lambda img, i: None, frame_timeout=0.5)
    except PicamError as e:
        msg = str(e)
    else:
        raise AssertionError("no timeout raised")
    cam.set_roi(0, 0, 2048, 2048, 8, 8)
    f = cam.acquire(1)
    return f"raised '{msg}', then got {f.shape}"


@check("re-entry guard")
def reentry(cam):
    cam.set_roi()
    th = threading.Thread(target=lambda: cam.run(1, lambda img, i: None), daemon=True)
    th.start()
    time.sleep(0.5)
    try:
        cam.run(1, lambda img, i: None)
    except PicamError as e:
        res = str(e)
    else:
        res = None
    cam.stop()
    th.join(30)
    assert res and "in progress" in res
    return res


@check("online temperature read during acquisition")
def temp_during(cam):
    cam.set_roi()
    vals, errs = [], []

    def reader():
        time.sleep(1.0)
        for _ in range(5):
            try:
                vals.append(cam.get("Sensor Temperature Reading", online=True))
            except Exception as e:  # noqa: BLE001
                errs.append(str(e))
            time.sleep(0.5)

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    cam.run(1, lambda img, i: None)
    th.join()
    return f"values={vals} errors={errs[:2]}"


@check("soak")
def soak(cam, n):
    cam.set_roi(0, 0, 2048, 2048, 8, 8)
    cam.set("Exposure Time", 1.0)
    means = []
    t0 = time.perf_counter()
    for _ in range(n // 10):
        cam.run(10, lambda img, i: means.append(float(img.mean())))
    dt = time.perf_counter() - t0
    assert len(means) == (n // 10) * 10
    return f"{len(means)} frames in {dt:.0f}s, mean counts {np.mean(means):.0f}±{np.std(means):.0f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="02062413")
    ap.add_argument("--soak", type=int, default=200)
    a = ap.parse_args()

    open_close(a.serial)
    cam = PicamCamera(a.serial)
    try:
        cam.set("Shutter Timing Mode", "Normal")
        for fn in (constraints, readable, snap, wrapper, continuous, stop_mid, timeout,
                   reentry, temp_during):
            fn(cam)
        if a.soak:
            soak(cam, a.soak)
    finally:
        cam.stop()
        cam.set_roi()
        cam.set("Exposure Time", 100.0)
        cam.close()
    print(f"\n{sum(ok for _, ok, _ in results)}/{len(results)} checks passed")


if __name__ == "__main__":
    main()
