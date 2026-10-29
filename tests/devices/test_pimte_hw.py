"""Live PI-MTE tests. Run with ``pytest -m hw`` (LightField must be closed)."""
from __future__ import annotations

import os

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
    c.close()


def test_identity(cam):
    assert cam.serial == SERIAL
    assert cam.get("Sensor Active Width") == 2048 and cam.get("Sensor Active Height") == 2048


def test_full_frame(cam):
    cam.set_roi()
    f = cam.acquire(1)
    assert f.shape == (1, 2048, 2048) and f.dtype == np.uint16 and f.max() > 0


def test_binned_frames(cam):
    cam.set_roi(0, 0, 2048, 2048, 4, 4)
    f = cam.acquire(2)
    assert f.shape == (2, 512, 512)
    cam.set_roi()


def test_detector_run(cam, tmp_path):
    # PICam allows one handle per camera, so share the module's open driver.
    det = PIMTEDetector(name="mte", serial=SERIAL, write_dir=tmp_path)
    det._driver = cam
    try:
        det.set_roi(0, 0, 2048, 2048, 4, 4)
        det.acquire_time.set(0.01).wait(5)
        det.stage()
        path = det._writer.path
        for _ in range(2):
            det.trigger().wait(60)
        names = [n for n, _ in det.collect_asset_docs()]
        det.unstage()
        assert names == ["stream_resource", "stream_datum", "stream_datum"]
        with h5py.File(path, "r") as f:
            assert f["/entry/data/data"].shape == (2, 512, 512)
    finally:
        det.set_roi()
        det.acquire_time.set(0.1).wait(5)
        det._driver = None        # the module fixture closes the camera
