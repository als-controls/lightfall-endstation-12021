"""Live PI-MTE tests. Run with ``pytest -m hw`` (LightField must be closed).

For a fuller characterization (constraints, stop latency, soak) run
``python scripts/evaluate_picam.py``.
"""
from __future__ import annotations

import os
import threading
import time

import h5py
import numpy as np
import pytest

from lightfall_endstation_12021.devices import PicamCamera, PIMTEDetector

pytestmark = pytest.mark.hw
SERIAL = os.environ.get("PIMTE_SERIAL", "02062413")


@pytest.fixture(scope="module")
def cam():
    c = PicamCamera(SERIAL)
    yield c
    c.set_roi()
    c.set("Exposure Time", 100.0)
    c.close()


def test_identity_and_constraints(cam):
    assert cam.serial == SERIAL
    assert cam.get("Sensor Active Width") == 2048 and cam.get("Sensor Active Height") == 2048
    assert 1.0 in cam.collection("ADC Speed")
    assert "Always Closed" in cam.enum_choices("Shutter Timing Mode")
    assert cam.range("Exposure Time")["minimum"] == 0.0


def test_full_frame(cam):
    cam.set_roi()
    f = cam.acquire(1)
    assert f.shape == (1, 2048, 2048) and f.dtype == np.uint16 and f.max() > 0


def test_binned_streamed_frames(cam):
    cam.set_roi(0, 0, 2048, 2048, 4, 4)
    got = []
    assert cam.run(3, lambda img, i: got.append((i, img.shape))) == 3
    assert got == [(0, (512, 512)), (1, (512, 512)), (2, (512, 512))]
    cam.set_roi()


def test_continuous_and_stop_latency(cam):
    cam.set_roi(0, 0, 2048, 2048, 8, 8)
    t = {}

    def stopper():
        time.sleep(3)
        t["req"] = time.perf_counter()
        cam.request_stop()

    threading.Thread(target=stopper, daemon=True).start()
    n = cam.run(None, lambda img, i: None)
    assert n >= 2 and time.perf_counter() - t["req"] < 1.0
    cam.set_roi()


def test_detector_run_and_live_view(cam, tmp_path):
    # PICam allows one handle per camera, so share the module's open driver.
    det = PIMTEDetector(name="mte", serial=SERIAL, write_dir=tmp_path, poll_period=0)
    det._driver = cam
    try:
        det.set_roi(0, 0, 2048, 2048, 4, 4)
        det.cam.acquire_time.set(0.01).wait(5)
        det.stage()
        path = det._writer.path
        for _ in range(2):
            det.trigger().wait(60)
        names = [n for n, _ in det.collect_asset_docs()]
        det.unstage()
        assert names == ["stream_resource", "stream_datum", "stream_datum"]
        with h5py.File(path, "r") as f:
            assert f["/entry/data/data"].shape == (2, 512, 512)

        det.cam.image_mode.put("Continuous")
        det.cam.acquire.put(1)
        t_end = time.monotonic() + 20
        while det.cam.array_counter.get() < 2 and time.monotonic() < t_end:
            time.sleep(0.1)
        det.cam.acquire.put(0)
        while det.cam.acquire.get() and time.monotonic() < t_end:
            time.sleep(0.05)
        assert det.image.array_data.get().shape == (512, 512)
        assert det.cam.acquire.get() == 0
    finally:
        det.set_roi()
        det.cam.acquire_time.set(0.1).wait(5)
        det._driver = None        # the module fixture closes the camera


def test_release_and_reconnect(cam):
    # Hand the module's camera over to a detector, release it, and reconnect.
    cam.close()
    det = PIMTEDetector(name="mte", serial=SERIAL, poll_period=0)
    det.connect_camera()
    assert det.camera_connected
    det.release_camera()
    probe = PicamCamera(SERIAL)          # free for another owner now
    probe.close()
    det.connect_camera()
    assert det.cam.temperature.get() > -100
    det.close()
    cam.__init__(SERIAL)                 # reopen for the module fixture teardown
