"""ophyd device for Princeton Instruments PI-MTE cameras via the PICam C API.

Talks to the camera directly through ``Picam.dll`` with ctypes -- no EPICS IOC and
no LightField. LightField must be CLOSED while this device holds the camera
(PICam allows a single owner per camera).

Layout mirrors an areaDetector-style detector so it slots into Bluesky plans::

    det = PIMTEDetector(name="mte", serial="02062413",
                        write_dir=r"D:\\data\\mte",            # where this PC writes
                        read_dir="/mnt/beamline/data/mte")   # same dir as the Tiled server sees it
    det.acquire_time.set(0.5)          # seconds
    RE(bp.count([det], num=3))

Images are never put inline in events. Each stage() opens one HDF5 file
(SWMR, dataset ``/entry/data/data``, one chunk per frame) and emits a
``StreamResource``; each trigger() appends a frame and emits a ``StreamDatum``.
The descriptor marks ``<name>_image`` as ``external: "STREAM:"`` so TiledWriter's
HDF5Consolidator registers the file with Tiled. ``read_dir`` must be a location the
Tiled server can read (and be within its readable_storage).

Outside a staged run (e.g. a quick look from a panel) trigger() just acquires and
leaves the frame(s) in ``det.last_image``; no file or documents are produced.

Data keys:
    <name>_image           external STREAM:, uint16 (H, W) or (N, H, W) if num_images > 1

Signals (kind):
    temperature            normal  sensor temperature reading [degC] (online read)
    acquire_time           config  exposure [s]
    num_images             config  frames per trigger
    temperature_setpoint   config  [degC]  -- WARNING: lowering this starts cooling;
                                    only cool an in-vacuum MTE when it is under vacuum.
    temperature_status     config  "Locked"/"Unlocked"
    adc_speed              config  [MHz]
    shutter_mode           config  "Normal", "Always Closed", "Always Open", ...
    trigger_response       config  "No Response", "Readout Per Trigger", ...
    readout_time           config  calculated readout time [s] (read-only)
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path, PurePosixPath, PureWindowsPath

import h5py
import numpy as np
from bluesky.protocols import Triggerable
from event_model import StreamRange, compose_stream_resource
from ophyd import Component as Cpt
from ophyd import Device, Signal
from ophyd.status import DeviceStatus
from ophyd.utils import ReadOnlyError

from lightfall_endstation_12021.devices.picam import PicamCamera

logger = logging.getLogger(__name__)


class PicamParamSignal(Signal):
    """Signal backed by a PICam parameter on the parent detector's driver."""

    def __init__(self, *, param: str, scale: float = 1.0, online: bool = False,
                 read_only: bool = False, units: str | None = None, **kwargs):
        self._param, self._scale, self._online = param, scale, online
        self._read_only, self._units = read_only, units
        super().__init__(**kwargs)

    @property
    def _driver(self) -> PicamCamera:
        return self.root.driver

    def get(self, **kwargs):
        drv = self._driver
        if self._online and drv.acquiring and self._readback is not None:
            # don't poke the hardware mid-readout; report the last value
            return self._readback
        val = drv.get(self._param, online=self._online)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and self._scale != 1.0:
            val = val * self._scale
        self._readback = val
        self._metadata["timestamp"] = time.time()
        return val

    def put(self, value, **kwargs):
        if self._read_only:
            raise ReadOnlyError(f"{self.name} is read-only")
        raw = value / self._scale if isinstance(value, (int, float)) and self._scale != 1.0 else value
        self._driver.set(self._param, raw)
        super().put(self.get(), **kwargs)   # publish the value the camera actually accepted

    def describe(self):
        desc = super().describe()
        if self._units:
            desc[self.name]["units"] = self._units
        return desc


class _HDF5FrameWriter:
    """One SWMR HDF5 file per staged run, plus the matching StreamResource/StreamDatum docs."""

    def __init__(self, path: Path, uri: str, data_key: str, frame_shape: tuple[int, ...],
                 dataset: str, attrs: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.frame_shape = path, frame_shape
        self._file = h5py.File(path, "w", libver="latest")
        self._ds = self._file.create_dataset(
            dataset, shape=(0, *frame_shape), maxshape=(None, *frame_shape),
            chunks=(1, *frame_shape), dtype="<u2")
        for k, v in attrs.items():
            self._ds.attrs[k] = v
        self._file.swmr_mode = True
        bundle = compose_stream_resource(
            mimetype="application/x-hdf5", uri=uri, data_key=data_key,
            parameters={"dataset": dataset, "chunk_shape": [1, *frame_shape], "swmr": True})
        self._compose_datum = bundle.compose_stream_datum
        self.pending: deque = deque([("stream_resource", bundle.stream_resource_doc)])
        self.count = 0

    def write(self, data: np.ndarray):
        if data.shape != self.frame_shape:
            raise RuntimeError(f"Frame shape {data.shape} != staged shape {self.frame_shape}; "
                               "ROI/binning/num_images must not change while staged")
        i = self.count
        self._ds.resize(i + 1, axis=0)
        self._ds[i] = data
        self._ds.flush()
        self.count = i + 1
        self.pending.append(
            ("stream_datum", self._compose_datum(indices=StreamRange(start=i, stop=i + 1))))

    def close(self):
        if self._file:
            self._file.close()
            self._file = None


class PIMTEDetector(Device, Triggerable):
    """Princeton Instruments PI-MTE (or other PICam camera) as an ophyd detector.

    ``Triggerable`` is listed explicitly so Lightfall's MRO-based category inference
    classifies this as a detector (plan forms filter detector choices by category).

    Parameters
    ----------
    serial : camera serial number (None = first free PICam camera)
    write_dir : directory this PC writes HDF5 files into (YYYY/MM/DD subfolders are added)
    read_dir : the same directory as seen by the Tiled server (POSIX or Windows path);
               defaults to ``write_dir``
    dataset : HDF5 dataset path for the frames
    """

    temperature = Cpt(PicamParamSignal, param="Sensor Temperature Reading", online=True,
                      read_only=True, units="degC", kind="normal")

    acquire_time = Cpt(PicamParamSignal, param="Exposure Time", scale=1e-3, units="s", kind="config")
    num_images = Cpt(Signal, value=1, kind="config")
    temperature_setpoint = Cpt(PicamParamSignal, param="Sensor Temperature Set Point",
                               units="degC", kind="config")
    temperature_status = Cpt(PicamParamSignal, param="Sensor Temperature Status", online=True,
                             read_only=True, kind="config")
    adc_speed = Cpt(PicamParamSignal, param="ADC Speed", units="MHz", kind="config")
    shutter_mode = Cpt(PicamParamSignal, param="Shutter Timing Mode", kind="config")
    trigger_response = Cpt(PicamParamSignal, param="Trigger Response", kind="config")
    readout_time = Cpt(PicamParamSignal, param="Readout Time Calculation", scale=1e-3,
                       read_only=True, units="s", kind="config")

    def __init__(self, *args, serial: str | None = None,
                 write_dir: str | os.PathLike = Path.home() / "lightfall" / "data" / "pi_mte",
                 read_dir: str | None = None,
                 dataset: str = "/entry/data/data", **kwargs):
        self._serial = serial
        self._driver: PicamCamera | None = None
        self.write_dir = Path(write_dir)
        self.read_dir = read_dir
        self.dataset = dataset
        self._writer: _HDF5FrameWriter | None = None
        self._acq_thread: threading.Thread | None = None
        self.last_image: np.ndarray | None = None
        super().__init__(*args, **kwargs)

    # ---- camera
    @property
    def driver(self) -> PicamCamera:
        """Open the camera lazily on first use so instantiation never grabs it from LightField."""
        if self._driver is None:
            self._driver = PicamCamera(self._serial)
        return self._driver

    @property
    def image_key(self) -> str:
        return f"{self.name}_image"

    def set_roi(self, x=0, y=0, width=None, height=None, x_binning=1, y_binning=1):
        if self._writer is not None:
            raise RuntimeError("Cannot change ROI while staged")
        self.driver.set_roi(x, y, width, height, x_binning, y_binning)

    def frame_shape(self) -> tuple[int, ...]:
        """Shape of the data produced by one trigger()."""
        if self._writer is not None:
            return self._writer.frame_shape
        n = int(self.num_images.get())
        hw = self.driver.image_shape()
        return (n, *hw) if n > 1 else hw

    # ---- file / document bookkeeping
    def _file_paths(self) -> tuple[Path, str]:
        parts = (time.strftime("%Y"), time.strftime("%m"), time.strftime("%d"),
                 f"{self.name}_{uuid.uuid4().hex[:12]}.h5")
        local = self.write_dir.joinpath(*parts).resolve()
        if self.read_dir is None:
            return local, local.as_uri()
        rd = str(self.read_dir)
        base = PureWindowsPath(rd) if PureWindowsPath(rd).drive else PurePosixPath(rd)
        return local, base.joinpath(*parts).as_uri()

    def stage(self):
        staged = super().stage()
        try:
            local, uri = self._file_paths()
            d = self.driver
            self._writer = _HDF5FrameWriter(
                local, uri, self.image_key, self.frame_shape(), self.dataset,
                attrs={"model": d.model, "serial": d.serial, "sensor": d.sensor})
            logger.info("%s writing %s", self.name, local)
        except Exception:
            super().unstage()
            raise
        return staged

    def unstage(self):
        if self._acq_thread is not None:
            self._acq_thread.join()
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        return super().unstage()

    def describe(self):
        desc = super().describe()
        desc[self.image_key] = {
            "source": f"PICAM:{self.driver.serial}",
            "dtype": "array",
            "shape": list(self.frame_shape()),
            "dtype_numpy": "<u2",
            "external": "STREAM:",
        }
        return desc

    def collect_asset_docs(self):
        if self._writer is None:
            return
        pending = self._writer.pending
        while pending:
            yield pending.popleft()

    # ---- acquisition
    def trigger(self):
        status = DeviceStatus(self)
        n = int(self.num_images.get())
        writer = self._writer

        def _run():
            try:
                frames = self.driver.acquire(n)
                data = frames[0] if n == 1 else frames
                self.last_image = data
                if writer is not None:
                    writer.write(data)
                status.set_finished()
            except Exception as exc:
                logger.exception("PI-MTE acquisition failed")
                status.set_exception(exc)

        self._acq_thread = threading.Thread(target=_run, name=f"{self.name}-acquire", daemon=True)
        self._acq_thread.start()
        return status

    def stop(self, *, success=False):
        if self._driver is not None:
            self._driver.stop()
        super().stop(success=success)

    def close(self):
        """Release the camera (e.g. to hand it back to LightField)."""
        if self._writer is not None:
            self.unstage()
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def destroy(self):
        self.close()
        super().destroy()
