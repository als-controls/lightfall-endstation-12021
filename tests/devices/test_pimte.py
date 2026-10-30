"""PIMTEDetector behaviour, using a fake PICam driver (no hardware)."""
from __future__ import annotations

import json
import threading
import time
from importlib import resources
from importlib.metadata import entry_points

import h5py
import numpy as np
import pytest
from event_model import DocumentNames, StreamRange, compose_run, schema_validators
from ophyd.utils import DisconnectedError

from lightfall_endstation_12021.devices import PIMTEDetector
from lightfall_endstation_12021.devices import pimte as pimte_mod


class FakePicam:
    model, serial, sensor = "PI-MTE: 2048B", "FAKE0001", "fake sensor"
    dtype = np.dtype("<u2")
    instances = 0

    def __init__(self, serial=None, shape=(8, 6)):
        FakePicam.instances += 1
        self.roi = {"x": 0, "width": shape[1], "x_binning": 1, "y": 0, "height": shape[0],
                    "y_binning": 1}
        self.params = {"Exposure Time": 100.0, "Sensor Temperature Reading": 20.0,
                       "Sensor Temperature Set Point": 25.0, "Sensor Temperature Status": "Unlocked",
                       "ADC Speed": 1.0, "Shutter Timing Mode": "Normal",
                       "Trigger Response": "No Response", "Readout Time Calculation": 4478.0,
                       "Sensor Active Width": shape[1], "Sensor Active Height": shape[0]}
        self.choices = {"Shutter Timing Mode": {"Normal": 1, "Always Closed": 2, "Always Open": 3},
                        "Trigger Response": {"No Response": 1, "Readout Per Trigger": 2}}
        self.acquiring = False
        self.is_open = True
        self.online_reads = 0
        self.n_acquired = 0
        self.gate: threading.Event | None = None
        self._stop = threading.Event()

    def get(self, name, online=False):
        self.online_reads += online
        return self.params[name]

    def set(self, name, value, commit=True):
        if name in self.choices and value not in self.choices[name]:
            raise ValueError(value)
        self.params[name] = value

    def enum_choices(self, name):
        return self.choices[name]

    def get_roi(self):
        return dict(self.roi)

    def set_roi(self, x=0, y=0, width=None, height=None, x_binning=1, y_binning=1):
        self.roi = {"x": x, "width": width or self.params["Sensor Active Width"] - x,
                    "x_binning": x_binning, "y": y,
                    "height": height or self.params["Sensor Active Height"] - y,
                    "y_binning": y_binning}

    def image_shape(self):
        return self.roi["height"] // self.roi["y_binning"], self.roi["width"] // self.roi["x_binning"]

    def run(self, n, on_frame, frame_timeout=-1.0):
        self.acquiring = True
        self._stop.clear()
        h, w = self.image_shape()
        i = 0
        try:
            while (n is None or i < n) and not self._stop.is_set():
                if self.gate is not None:
                    self.gate.wait(5)
                img = (np.arange(h * w, dtype="<u2").reshape(h, w) + self.n_acquired)
                self.n_acquired += 1
                on_frame(img, i)
                i += 1
                if n is None:
                    time.sleep(0.01)
            return i
        finally:
            self.acquiring = False

    def request_stop(self):
        self._stop.set()

    def stop(self, timeout=30):
        self._stop.set()
        return True

    def close(self):
        self.is_open = False


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(pimte_mod, "PicamCamera", FakePicam)
    FakePicam.instances = 0


@pytest.fixture
def det(tmp_path, fake):
    d = PIMTEDetector(name="mte", write_dir=tmp_path / "data", poll_period=0)
    yield d
    if d._writer is not None:
        d.unstage()


def _wait(pred, timeout=5):
    t = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > t:
            raise TimeoutError
        time.sleep(0.01)


def test_instantiation_does_not_open_camera(det):
    assert FakePicam.instances == 0 and not det.camera_connected


def test_image_is_external_stream_and_not_in_read(det):
    desc = det.describe()["mte_image"]
    assert desc["external"] == "STREAM:"
    assert desc["shape"] == [8, 6] and desc["dtype_numpy"] == "<u2"
    reading = det.read()
    assert "mte_image" not in reading
    assert "mte_cam_temperature" in reading
    assert not any("array_data" in k for k in reading)


def test_configuration_includes_roi_and_settings(det):
    cfg = det.read_configuration()
    for key in ("mte_cam_acquire_time", "mte_cam_num_images", "mte_cam_shutter_mode",
                "mte_cam_bin_x", "mte_cam_size_x", "mte_cam_max_size_y", "mte_cam_adc_speed"):
        assert key in cfg, key


def test_acquire_time_is_seconds(det):
    assert det.cam.acquire_time.get() == pytest.approx(0.1)
    det.cam.acquire_time.set(0.5).wait(1)
    assert det.driver.params["Exposure Time"] == pytest.approx(500.0)


def test_enum_signal_index_and_labels(det):
    sig = det.cam.shutter_mode
    assert sig.enum_strs == ("Normal", "Always Closed", "Always Open")
    assert sig.get() == 0
    sig.put(1)
    assert det.driver.params["Shutter Timing Mode"] == "Always Closed" and sig.get() == 1
    sig.put("Always Open")
    assert sig.get() == 2
    assert det.describe_configuration()["mte_cam_shutter_mode"]["enum_strs"][2] == "Always Open"


def test_shutter_control_for_dark_frames(det):
    det.cam.shutter_control.put(0)
    assert det.driver.params["Shutter Timing Mode"] == "Always Closed"
    assert det.cam.shutter_control.get() == 0
    det.cam.shutter_control.put(1)
    assert det.driver.params["Shutter Timing Mode"] == "Normal"


def test_cooling_guard(det):
    with pytest.raises(PermissionError):
        det.cam.temperature_setpoint.put(-40.0)
    assert det.driver.params["Sensor Temperature Set Point"] == 25.0
    det.cam.temperature_setpoint.put(22.0)            # above the safe threshold is fine
    det.allow_cooling = True
    det.cam.temperature_setpoint.put(-40.0)
    assert det.driver.params["Sensor Temperature Set Point"] == -40.0


def test_roi_signals(det):
    det.cam.bin_x.put(2)
    assert det.driver.roi["x_binning"] == 2 and det.cam.bin_x.get() == 2
    det.set_roi(0, 0, 6, 8, 2, 2)
    assert det.frame_shape() == (4, 3)
    assert det.cam.size_x.get() == 6 and det.cam.bin_y.get() == 2


def test_stage_trigger_writes_hdf5_and_valid_stream_docs(det):
    run = compose_run()
    det.stage()
    desc = run.compose_descriptor(name="primary", data_keys=det.describe())
    path = det._writer.path
    docs = []
    for seq in (1, 2, 3):
        det.trigger().wait(5)
        for name, doc in det.collect_asset_docs():
            if name == "stream_resource":
                doc["run_start"] = run.start_doc["uid"]
            else:
                doc["descriptor"] = desc.descriptor_doc["uid"]
                doc["seq_nums"] = StreamRange(start=seq, stop=seq + 1)
            schema_validators[DocumentNames[name]].validate(doc)
            docs.append((name, doc))
    det.unstage()

    assert [n for n, _ in docs] == ["stream_resource"] + ["stream_datum"] * 3
    sres = docs[0][1]
    assert sres["mimetype"] == "application/x-hdf5" and sres["data_key"] == "mte_image"
    assert sres["parameters"] == {"dataset": "/entry/data/data", "chunk_shape": [1, 8, 6], "swmr": True}
    assert sres["uri"] == path.as_uri()
    assert [d["indices"] for _, d in docs[1:]] == [{"start": i, "stop": i + 1} for i in range(3)]

    with h5py.File(path, "r") as f:
        ds = f["/entry/data/data"]
        assert ds.shape == (3, 8, 6) and ds.dtype == np.dtype("<u2") and ds.chunks == (1, 8, 6)
        assert ds.attrs["serial"] == "FAKE0001"
        np.testing.assert_array_equal(ds[2], det.last_image)
    assert det.cam.acquire.get() == 0 and det.cam.detector_state.get() == 0


def test_consolidator_accepts_documents(det):
    consolidators = pytest.importorskip("bluesky_tiled_plugins.writing.consolidators")
    run = compose_run()
    det.stage()
    desc = run.compose_descriptor(name="primary", data_keys=det.describe())
    det.trigger().wait(5)
    det.trigger().wait(5)
    docs = list(det.collect_asset_docs())
    det.unstage()
    sres = dict(docs[0][1], run_start=run.start_doc["uid"])
    cons = consolidators.consolidator_factory(sres, desc.descriptor_doc)
    for i, (_, d) in enumerate(docs[1:]):
        cons.consume_stream_datum(dict(d, descriptor=desc.descriptor_doc["uid"],
                                       seq_nums={"start": i + 1, "stop": i + 2}))
    assert type(cons).__name__ == "HDF5Consolidator"
    assert cons.shape == (2, 8, 6)


def test_multi_image_trigger_shape(det):
    det.cam.num_images.put(4)
    assert det.describe()["mte_image"]["shape"] == [4, 8, 6]
    det.stage()
    det.trigger().wait(5)
    path = det._writer.path
    det.unstage()
    with h5py.File(path, "r") as f:
        assert f["/entry/data/data"].shape == (1, 4, 8, 6)
    assert det.cam.array_counter.get() == 4


def test_unstaged_trigger_produces_no_docs_or_files(det, tmp_path):
    det.trigger().wait(5)
    assert det.last_image.shape == (8, 6)
    assert list(det.collect_asset_docs()) == []
    assert not (tmp_path / "data").exists()


def test_live_view_updates_array_data(det):
    seen = []
    det.image.array_data.subscribe(lambda value, **kw: seen.append(value.shape), run=False)
    det.cam.image_mode.put("Multiple")
    det.cam.num_images.put(3)
    det.cam.acquire.put(1)
    _wait(lambda: det.cam.acquire.get() == 0 and det.cam.array_counter.get() == 3)
    assert seen == [(8, 6)] * 3


def test_continuous_free_run_and_stop(det):
    states = []
    det.cam.acquire.subscribe(lambda value, **kw: states.append(value), run=False)
    det.cam.image_mode.put(2)
    det.cam.acquire.put(1)
    _wait(lambda: det.cam.array_counter.get() >= 5)
    det.cam.acquire.put(0)
    _wait(lambda: det.cam.acquire.get() == 0)
    assert states == [1, 0]
    assert det.cam.detector_state.get() == 0


def test_second_acquisition_refused_while_busy(det):
    det.driver.gate = threading.Event()
    st = det.trigger()
    with pytest.raises(RuntimeError):
        det.trigger()
    det.driver.gate.set()
    st.wait(5)


def test_roi_locked_while_staged(det):
    det.stage()
    with pytest.raises(RuntimeError):
        det.set_roi(0, 0, 4, 4)
    with pytest.raises(RuntimeError):
        det.cam.bin_x.put(2)


def test_online_read_uses_cache_during_acquisition(det):
    assert det.cam.temperature.get() == 20.0
    drv = det.driver
    reads = drv.online_reads
    drv.gate = threading.Event()
    st = det.trigger()
    _wait(lambda: drv.acquiring)
    drv.params["Sensor Temperature Reading"] = -40.0
    assert det.cam.temperature.get() == 20.0          # cached, hardware not touched
    assert drv.online_reads == reads
    drv.gate.set()
    st.wait(5)
    assert det.cam.temperature.get() == -40.0


def test_release_is_sticky_and_connect_reopens(det):
    det.driver  # open
    assert det.camera_connected and det.cam.detector_state.get() == 0
    det.release_camera()
    assert not det.camera_connected and det.released
    assert det.cam.detector_state.get() == 9
    assert not det.cam.temperature.connected
    with pytest.raises(DisconnectedError):
        det.cam.temperature.get()                     # no silent lazy reopen
    assert FakePicam.instances == 1
    det.connect_camera()
    assert det.camera_connected and FakePicam.instances == 2
    assert det.cam.temperature.get() == 20.0


def test_release_refused_while_staged(det):
    det.stage()
    with pytest.raises(RuntimeError):
        det.release_camera()


def test_poller_publishes_temperature(tmp_path, fake):
    d = PIMTEDetector(name="mte", write_dir=tmp_path, poll_period=0.05)
    seen = []
    d.cam.temperature.subscribe(lambda value, **kw: seen.append(value), run=False)
    d.connect_camera()
    _wait(lambda: 20.0 in seen)
    d.driver.params["Sensor Temperature Reading"] = 5.0
    _wait(lambda: 5.0 in seen)
    d.close()


def test_destroy_releases_camera(tmp_path, fake):
    d = PIMTEDetector(name="mte", write_dir=tmp_path, poll_period=0.05)
    d.connect_camera()
    drv = d._driver
    d.destroy()                      # what Lightfall calls when the device is disabled
    assert not drv.is_open and d._driver is None


def test_dropping_last_reference_releases_camera(tmp_path, fake):
    import gc

    d = PIMTEDetector(name="mte", write_dir=tmp_path, poll_period=0.05)
    d.connect_camera()
    drv, poller = d._driver, d._poll_thread
    assert poller.is_alive()
    del d
    gc.collect()
    assert not drv.is_open           # __del__ ran despite the running poller
    poller.join(2)
    assert not poller.is_alive()


@pytest.mark.parametrize("read_dir, prefix", [
    ("/mnt/beamline/data/mte", "file:///mnt/beamline/data/mte/"),
    (r"\\fileserver\share\mte", "file://fileserver/share/mte/"),
    (r"D:\data\mte", "file:///D:/data/mte/"),
])
def test_read_dir_uri_mapping(tmp_path, read_dir, prefix):
    d = PIMTEDetector(name="mte", write_dir=tmp_path, read_dir=read_dir)
    local, uri = d._file_paths()
    assert uri.startswith(prefix) and uri.endswith(local.name)
    assert local.is_relative_to(tmp_path.resolve())


def test_packaged_happi_entry_instantiates_without_hardware():
    happi = pytest.importorskip("happi")
    from happi.backends.json_db import JSONBackend

    path = resources.files("lightfall_endstation_12021.devices").joinpath("devices.json")
    assert json.loads(path.read_text())["mte"]["device_class"].endswith("PIMTEDetector")
    client = happi.Client(database=JSONBackend(str(path)))
    dev = client.load_device(name="mte")
    assert isinstance(dev, PIMTEDetector)
    assert dev._driver is None            # camera not opened by instantiation
    assert dev._serial == "02062413"


def test_lightfall_classifies_as_detector():
    happi_backend = pytest.importorskip("lightfall.devices.backends.happi")
    detector = happi_backend.DeviceCategory.DETECTOR
    assert happi_backend._guess_category_from_mro(PIMTEDetector) == detector


def test_manifest_entry_point_resolves():
    eps = [ep for ep in entry_points(group="lightfall.plugins") if ep.name == "lightfall_endstation_12021"]
    if not eps:
        pytest.skip("package not installed")
    manifest = eps[0].load()
    for entry in manifest.plugins:
        mod, _, attr = entry.import_path.partition(":")
        assert getattr(__import__(mod, fromlist=[attr]), attr)
