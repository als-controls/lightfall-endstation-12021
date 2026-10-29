"""PIMTEDetector document/file behaviour, using a fake PICam driver (no hardware)."""
from __future__ import annotations

import json
import threading
from importlib import resources
from importlib.metadata import entry_points

import h5py
import numpy as np
import pytest
from event_model import DocumentNames, StreamRange, compose_run, schema_validators

from lightfall_endstation_12021.devices import PIMTEDetector


class FakePicam:
    model, serial, sensor = "PI-MTE: 2048B", "FAKE0001", "fake sensor"

    def __init__(self, shape=(8, 6)):
        self._shape = shape
        self.params = {"Exposure Time": 100.0, "Sensor Temperature Reading": 20.0,
                       "Sensor Temperature Set Point": 25.0, "Sensor Temperature Status": "Unlocked",
                       "ADC Speed": 1.0, "Shutter Timing Mode": "Normal",
                       "Trigger Response": "No Response", "Readout Time Calculation": 4478.0}
        self.acquiring = False
        self.online_reads = 0
        self.n_acquired = 0
        self.gate: threading.Event | None = None

    def get(self, name, online=False):
        self.online_reads += online
        return self.params[name]

    def set(self, name, value, commit=True):
        self.params[name] = value

    def image_shape(self):
        return self._shape

    def acquire(self, n=1):
        self.acquiring = True
        try:
            if self.gate is not None:
                self.gate.wait(5)
            base = self.n_acquired
            self.n_acquired += n
            return (np.arange(n * self._shape[0] * self._shape[1], dtype="<u2")
                    .reshape(n, *self._shape) + base)
        finally:
            self.acquiring = False

    def stop(self):
        pass

    def close(self):
        pass


@pytest.fixture
def det(tmp_path):
    d = PIMTEDetector(name="mte", write_dir=tmp_path / "data")
    d._driver = FakePicam()
    yield d
    if d._writer is not None:
        d.unstage()


def test_image_is_external_stream_and_not_in_read(det):
    desc = det.describe()["mte_image"]
    assert desc["external"] == "STREAM:"
    assert desc["shape"] == [8, 6] and desc["dtype_numpy"] == "<u2"
    assert "mte_image" not in det.read()
    assert "mte_temperature" in det.read()


def test_acquire_time_is_seconds(det):
    assert det.acquire_time.get() == pytest.approx(0.1)
    det.acquire_time.set(0.5).wait(1)
    assert det._driver.params["Exposure Time"] == pytest.approx(500.0)


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

    names = [n for n, _ in docs]
    assert names == ["stream_resource", "stream_datum", "stream_datum", "stream_datum"]
    sres = docs[0][1]
    assert sres["mimetype"] == "application/x-hdf5"
    assert sres["data_key"] == "mte_image"
    assert sres["parameters"] == {"dataset": "/entry/data/data", "chunk_shape": [1, 8, 6], "swmr": True}
    assert sres["uri"] == path.as_uri()
    assert [d["indices"] for _, d in docs[1:]] == [{"start": i, "stop": i + 1} for i in range(3)]

    with h5py.File(path, "r") as f:
        ds = f["/entry/data/data"]
        assert ds.shape == (3, 8, 6) and ds.dtype == np.dtype("<u2") and ds.chunks == (1, 8, 6)
        assert ds.attrs["serial"] == "FAKE0001"
        np.testing.assert_array_equal(ds[2], det.last_image)


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
    assert cons.get_data_source().parameters["dataset"] == "/entry/data/data"


def test_multi_image_trigger_shape(det):
    det.num_images.put(4)
    assert det.describe()["mte_image"]["shape"] == [4, 8, 6]
    det.stage()
    det.trigger().wait(5)
    path = det._writer.path
    det.unstage()
    with h5py.File(path, "r") as f:
        assert f["/entry/data/data"].shape == (1, 4, 8, 6)


def test_unstaged_trigger_produces_no_docs_or_files(det, tmp_path):
    det.trigger().wait(5)
    assert det.last_image.shape == (8, 6)
    assert list(det.collect_asset_docs()) == []
    assert not (tmp_path / "data").exists()


def test_roi_locked_while_staged(det):
    det.stage()
    with pytest.raises(RuntimeError):
        det.set_roi(0, 0, 4, 4)


def test_online_read_uses_cache_during_acquisition(det):
    assert det.temperature.get() == 20.0
    reads = det._driver.online_reads
    det._driver.gate = threading.Event()
    st = det.trigger()
    for _ in range(100):           # wait until the worker is inside acquire()
        if det._driver.acquiring:
            break
        threading.Event().wait(0.01)
    det._driver.params["Sensor Temperature Reading"] = -40.0
    assert det.temperature.get() == 20.0          # cached, hardware not touched
    assert det._driver.online_reads == reads
    det._driver.gate.set()
    st.wait(5)
    assert det.temperature.get() == -40.0


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


def test_manifest_entry_point_resolves():
    eps = [ep for ep in entry_points(group="lightfall.plugins") if ep.name == "lightfall_endstation_12021"]
    if not eps:
        pytest.skip("package not installed")
    manifest = eps[0].load()
    for entry in manifest.plugins:
        mod, _, attr = entry.import_path.partition(":")
        assert getattr(__import__(mod, fromlist=[attr]), attr)
