"""ophyd device for Princeton Instruments PI-MTE cameras via the PICam C API.

Talks to the camera directly through ``Picam.dll`` (see :mod:`.picam`) -- no EPICS IOC
and no LightField. PICam allows a single owner per camera, so LightField must be closed
while this device holds it; :meth:`PIMTEDetector.release_camera` hands it back.

The component layout follows ophyd's areaDetector naming so Lightfall's camera widgets
and plans (``simple_acquire``, TV mode, dark frames) work unchanged::

    det = PIMTEDetector(name="mte", serial="02062413", data_root="D:/data")
    det.cam.acquire_time.set(0.5)      # seconds
    RE(bp.count([det], num=3))

By default files go to ``<data_root>/<tiled_beamline>/pi_mte`` on this PC and are
referenced as ``/data/<tiled_beamline>/pi_mte`` (the Tiled server's view, where
shussebora copies them); explicit ``write_dir`` / ``read_dir`` override that. After each
file is closed (``unstage``), shussebora is told through
``lightfall.services.shussebora_files.notify_files_closed``.

Images are never put inline in events. Each stage() opens one HDF5 file (SWMR, dataset
``/entry/data/data``, one chunk per trigger) and emits a ``StreamResource``; each
trigger() appends and emits a ``StreamDatum``. The descriptor marks ``<name>_image`` as
``external: "STREAM:"`` for TiledWriter's HDF5Consolidator. ``read_dir`` must be a
location the Tiled server can read (and be within its readable_storage).

Components:
    cam.*            acquisition/camera settings (PICam-backed), areaDetector names
    image.array_data latest frame for live display (omitted from documents)

Outside a staged run, ``cam.acquire.put(1)`` free-runs per ``cam.image_mode``
(Single / Multiple / Continuous) for live view; no files or documents are produced.

Cooling: setpoints below ``SAFE_SETPOINT`` (20 degC) are refused unless
``det.allow_cooling`` is True. Only cool an in-vacuum MTE when it is under vacuum.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
import weakref
from collections import deque
from pathlib import Path, PurePosixPath, PureWindowsPath

import h5py
import numpy as np
from bluesky.protocols import Triggerable
from event_model import StreamRange, compose_stream_resource
from ophyd import Component as Cpt
from ophyd import Device, Signal
from ophyd.status import DeviceStatus
from ophyd.utils import DisconnectedError, ReadOnlyError

from lightfall_endstation_12021.devices.paths import beamline_dirs
from lightfall_endstation_12021.devices.picam import (
    ABSENT,
    PicamCamera,
    PicamError,
    camera_presence,
)

logger = logging.getLogger(__name__)

#: Main-thread answer before the first presence probe has finished (see
#: PIMTEDetector._camera_presence): treated as present.
PRESENT_UNKNOWN = "unknown"

DETECTOR_DIR = "pi_mte"   # per-detector directory under <data_root>/<beamline>/


def _notify_files_closed(paths: list[str]) -> None:
    """Tell shussebora the files are complete. Never raises.

    Older Lightfall versions lack the helper; then shussebora's catch-up moves the files.
    """
    try:
        from lightfall.services.shussebora_files import notify_files_closed
    except ImportError:
        logger.debug("lightfall.services.shussebora_files unavailable; not notifying shussebora")
        return
    try:
        notify_files_closed(paths)
    except Exception as exc:  # noqa: BLE001
        logger.warning("shussebora file.closed notification failed: %s", exc)


SAFE_SETPOINT = 20.0   # degC; below this requires allow_cooling
DETECTOR_IDLE, DETECTOR_ACQUIRE, DETECTOR_ERROR, DETECTOR_DISCONNECTED = 0, 1, 6, 9
IMAGE_MODES = ["Single", "Multiple", "Continuous"]


def _changed(old, new) -> bool:
    try:
        return bool(old != new)
    except Exception:   # numpy arrays etc.
        return True


class _StateSignal(Signal):
    """Soft signal the device updates internally (always notifies on change)."""

    def _update(self, value):
        old = self._readback
        self._readback = value
        self._metadata["timestamp"] = time.time()
        if _changed(old, value):
            self._run_subs(sub_type=self.SUB_VALUE, old_value=old, value=value,
                           timestamp=self._metadata["timestamp"])


class _EnumSoftSignal(Signal):
    """Soft integer signal with ``enum_strs`` (accepts index or label on put)."""

    def __init__(self, *, enum_strs, **kwargs):
        self._enum_strs = tuple(enum_strs)
        super().__init__(**kwargs)

    @property
    def enum_strs(self):
        return self._enum_strs

    def put(self, value, **kwargs):
        if isinstance(value, str):
            value = self._enum_strs.index(value)
        super().put(int(value), **kwargs)

    def describe(self):
        desc = super().describe()
        desc[self.name]["enum_strs"] = list(self._enum_strs)
        return desc


class PicamParamSignal(Signal):
    """Signal backed by a PICam parameter on the root detector's driver.

    ``enum=True`` exposes an enumerated parameter as an integer index into
    ``enum_strs`` (the camera's allowed choices), like an EPICS mbbo.
    """

    def __init__(self, *, param: str, scale: float = 1.0, online: bool = False,
                 read_only: bool = False, units: str | None = None, enum: bool = False,
                 **kwargs):
        self._param, self._scale, self._online = param, scale, online
        self._read_only, self._units, self._enum = read_only, units, enum
        self._enum_cache: tuple[str, ...] | None = None
        super().__init__(**kwargs)

    @property
    def connected(self) -> bool:
        return self.root.camera_present   # opens lazily on first read; never here

    @property
    def enum_strs(self):
        if not self._enum:
            return None
        if self._enum_cache is None:
            self._enum_cache = tuple(self.root.driver.enum_choices(self._param))
        return self._enum_cache

    def _raw_to_value(self, raw):
        if self._enum:
            return self.enum_strs.index(raw) if raw in self.enum_strs else raw
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and self._scale != 1.0:
            return raw * self._scale
        return raw

    def get(self, **kwargs):
        det = self.root
        drv = det.driver
        if self._online and drv.acquiring and self._readback is not None:
            return self._readback      # cached while the camera is busy
        val = self._raw_to_value(drv.get(self._param, online=self._online))
        old = self._readback
        self._readback = val
        self._metadata["timestamp"] = time.time()
        if _changed(old, val):
            self._run_subs(sub_type=self.SUB_VALUE, old_value=old, value=val,
                           timestamp=self._metadata["timestamp"])
        return val

    def put(self, value, **kwargs):
        if self._read_only:
            raise ReadOnlyError(f"{self.name} is read-only")
        if self._enum:
            raw = value if isinstance(value, str) else self.enum_strs[int(value)]
        elif isinstance(value, (int, float)) and self._scale != 1.0:
            raw = value / self._scale
        else:
            raw = value
        self.root._check_put(self, raw)
        self.root.driver.set(self._param, raw)
        self.get()                   # publish the value the camera actually accepted

    def describe(self):
        desc = super().describe()
        if self._units:
            desc[self.name]["units"] = self._units
        if self._enum:
            desc[self.name]["enum_strs"] = list(self.enum_strs)
        return desc


class _RoiSignal(PicamParamSignal):
    """One field of the (single) PICam ROI."""

    def __init__(self, *, field: str, **kwargs):
        self._field = field
        super().__init__(param="ROIs", **kwargs)

    def get(self, **kwargs):
        val = self.root.driver.get_roi()[self._field]
        old = self._readback
        self._readback = val
        self._metadata["timestamp"] = time.time()
        if _changed(old, val):
            self._run_subs(sub_type=self.SUB_VALUE, old_value=old, value=val,
                           timestamp=self._metadata["timestamp"])
        return val

    def put(self, value, **kwargs):
        if self._read_only:
            raise ReadOnlyError(f"{self.name} is read-only")
        det = self.root
        if det._writer is not None:
            raise RuntimeError("Cannot change ROI while staged")
        roi = det.driver.get_roi()
        roi[self._field] = int(value)
        det.driver.set_roi(**roi)
        det._refresh_roi_signals()


class _AcquireSignal(_StateSignal):
    """``cam.acquire``: put 1 to free-run per image_mode, 0 to stop."""

    def put(self, value, **kwargs):
        det = self.root
        if int(value):
            det._start_free_run()
        else:
            det._request_stop()


class _ShutterControlSignal(Signal):
    """``cam.shutter_control`` as used by Lightfall dark-frame plans: 0 closed, 1 normal."""

    @property
    def connected(self) -> bool:
        return self.root.camera_present   # opens lazily on first read; never here

    def get(self, **kwargs):
        closed = self.root.driver.get("Shutter Timing Mode") == "Always Closed"
        self._readback = 0 if closed else 1
        return self._readback

    def put(self, value, **kwargs):
        mode = "Always Closed" if int(value) == 0 else "Normal"
        self.root.cam.shutter_mode.put(mode)
        super().put(int(bool(int(value))), **kwargs)


class PIMTECam(Device):
    """Camera settings, named after ophyd's AreaDetectorCam where an equivalent exists."""

    acquire = Cpt(_AcquireSignal, value=0, kind="omitted")
    detector_state = Cpt(_StateSignal, value=DETECTOR_DISCONNECTED, kind="omitted")
    array_counter = Cpt(_StateSignal, value=0, kind="omitted")
    image_mode = Cpt(_EnumSoftSignal, enum_strs=IMAGE_MODES, value=0, kind="config")
    num_images = Cpt(Signal, value=1, kind="config")

    acquire_time = Cpt(PicamParamSignal, param="Exposure Time", scale=1e-3, units="s", kind="config")
    readout_time = Cpt(PicamParamSignal, param="Readout Time Calculation", scale=1e-3,
                       read_only=True, units="s", kind="config")
    adc_speed = Cpt(PicamParamSignal, param="ADC Speed", units="MHz", kind="config")
    shutter_mode = Cpt(PicamParamSignal, param="Shutter Timing Mode", enum=True, kind="config")
    shutter_control = Cpt(_ShutterControlSignal, value=1, kind="omitted")
    trigger_response = Cpt(PicamParamSignal, param="Trigger Response", enum=True, kind="config")

    temperature = Cpt(PicamParamSignal, param="Sensor Temperature Reading", online=True,
                      read_only=True, units="degC", kind="normal")
    temperature_setpoint = Cpt(PicamParamSignal, param="Sensor Temperature Set Point",
                               units="degC", kind="config")
    temperature_status = Cpt(PicamParamSignal, param="Sensor Temperature Status", online=True,
                             read_only=True, kind="config")

    min_x = Cpt(_RoiSignal, field="x", kind="config")
    min_y = Cpt(_RoiSignal, field="y", kind="config")
    size_x = Cpt(_RoiSignal, field="width", kind="config")
    size_y = Cpt(_RoiSignal, field="height", kind="config")
    bin_x = Cpt(_RoiSignal, field="x_binning", kind="config")
    bin_y = Cpt(_RoiSignal, field="y_binning", kind="config")
    max_size_x = Cpt(PicamParamSignal, param="Sensor Active Width", read_only=True, kind="config")
    max_size_y = Cpt(PicamParamSignal, param="Sensor Active Height", read_only=True, kind="config")


class PIMTEImage(Device):
    """Live-view image (latest frame); never part of event documents."""

    array_data = Cpt(_StateSignal, value=np.zeros((1, 1), dtype="<u2"), kind="omitted")


class _HDF5FrameWriter:
    """One SWMR HDF5 file per staged run, plus the matching StreamResource/StreamDatum docs."""

    def __init__(self, path: Path, uri: str, data_key: str, frame_shape: tuple[int, ...],
                 dtype: np.dtype, dataset: str, attrs: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.frame_shape = path, frame_shape
        self._file = h5py.File(path, "w", libver="latest")
        self._ds = self._file.create_dataset(
            dataset, shape=(0, *frame_shape), maxshape=(None, *frame_shape),
            chunks=(1, *frame_shape), dtype=dtype)
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
    write_dir : directory this PC writes HDF5 files into (YYYY/MM/DD subfolders are added);
                default ``<data_root>/<tiled_beamline>/pi_mte``, resolved at stage time
    read_dir : the same directory as seen by the Tiled server (POSIX or Windows path);
               default ``/data/<tiled_beamline>/pi_mte``, or ``write_dir`` when only
               ``write_dir`` is given
    data_root : local root that shussebora moves to bcgtiled's ``/data``
    dataset : HDF5 dataset path for the frames
    poll_period : seconds between temperature polls while connected (0 disables)
    """

    cam = Cpt(PIMTECam, "", kind="normal")
    image = Cpt(PIMTEImage, "", kind="omitted")

    def __init__(self, *args, serial: str | None = None,
                 write_dir: str | os.PathLike | None = None,
                 read_dir: str | None = None, data_root: str | os.PathLike = "D:/data",
                 dataset: str = "/entry/data/data",
                 poll_period: float = 2.0, **kwargs):
        self._serial = serial
        self._driver: PicamCamera | None = None
        self._released = False
        self._open_lock = threading.Lock()
        # camera_presence() cache: (result, time.monotonic() when probed)
        self._presence: tuple[str, float] | None = None
        self._presence_lock = threading.Lock()
        self._presence_thread: threading.Thread | None = None
        self.data_root = str(data_root)
        self.write_dir = write_dir
        self.read_dir = read_dir
        self.dataset = dataset
        self.poll_period = poll_period
        self.allow_cooling = False
        self._writer: _HDF5FrameWriter | None = None
        self._acq_thread: threading.Thread | None = None
        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self.last_image: np.ndarray | None = None
        super().__init__(*args, **kwargs)

    # ---- camera ownership
    @property
    def camera_connected(self) -> bool:
        return self._driver is not None and self._driver.is_open

    #: Seconds a presence probe result is reused (ophyd polls ``connected`` on every
    #: signal, every 50 ms, while waiting for a connection).
    PRESENCE_TTL = 10.0

    @property
    def camera_present(self) -> bool:
        """True if the camera is open here, or plugged in (even if LightField has it).

        This is what the signals' ``connected`` reports, so an unplugged camera shows
        as disconnected rather than connected-but-every-read-fails. It never opens the
        camera: PICam allows one owner, and LightField may be that owner. False once
        released (:meth:`release_camera`).
        """
        if self._released:
            return False
        if self.camera_connected:
            return True
        return self._camera_presence() != ABSENT

    def _camera_presence(self) -> str:
        """Cached :func:`camera_presence`. On the main (GUI) thread never blocks: a
        stale result is refreshed in the background and the last result is returned
        meanwhile. Before the first probe the answer there is "present" (the old,
        optimistic behaviour): ophyd's ``read()`` checks ``connected`` first, so
        answering ABSENT would fail a good camera's first read. In practice the
        connection check (a worker thread) probes first."""
        cached = self._presence
        if cached is not None and time.monotonic() - cached[1] < self.PRESENCE_TTL:
            return cached[0]
        if threading.current_thread() is threading.main_thread():
            self._refresh_presence_in_background()
            return cached[0] if cached is not None else PRESENT_UNKNOWN
        return self._probe_presence()

    def _probe_presence(self) -> str:
        with self._presence_lock:
            cached = self._presence
            if cached is not None and time.monotonic() - cached[1] < self.PRESENCE_TTL:
                return cached[0]  # another thread just probed
            result = camera_presence(self._serial)
            self._presence = (result, time.monotonic())
            return result

    def _refresh_presence_in_background(self):
        thread = self._presence_thread
        if thread is not None and thread.is_alive():
            return
        self._presence_thread = threading.Thread(
            target=self._probe_presence, daemon=True, name=f"{self.name}-presence")
        self._presence_thread.start()

    @property
    def released(self) -> bool:
        return self._released

    @property
    def driver(self) -> PicamCamera:
        """The open camera. Opens lazily unless released via :meth:`release_camera`."""
        if self._driver is None:
            if self._released:
                raise DisconnectedError(
                    f"{self.name}: camera released; call connect_camera() to reconnect")
            self._open()
        return self._driver

    def _open(self):
        with self._open_lock:
            if self._driver is not None:
                return
            self._driver = PicamCamera(self._serial)
            for sig in (self.cam.shutter_mode, self.cam.trigger_response):
                sig._enum_cache = None
            self.cam.detector_state._update(DETECTOR_IDLE)
            if self.poll_period:
                # Fresh stop event per poller; the thread holds only a weak reference so
                # it never keeps the detector alive (lets __del__ release the camera).
                self._poll_stop = threading.Event()
                self._poll_thread = threading.Thread(
                    target=_poll_loop, args=(weakref.ref(self), self._poll_stop, self.poll_period),
                    daemon=True, name=f"{self.name}-poll")
                self._poll_thread.start()

    def connect_camera(self):
        """(Re)connect to the camera, e.g. after LightField has been closed."""
        self._released = False
        self._open()

    def release_camera(self):
        """Stop, close the camera and stay disconnected until :meth:`connect_camera`."""
        if self._writer is not None:
            raise RuntimeError("Cannot release the camera while staged")
        self._released = True
        self._close_driver()

    def _close_driver(self):
        self._poll_stop.set()
        if self._driver is not None:
            self._driver.close()
            self._driver = None
        if self._acq_thread is not None:
            self._acq_thread.join(30)
        self.cam.acquire._update(0)
        self.cam.detector_state._update(DETECTOR_DISCONNECTED)

    def _check_put(self, sig, raw):
        if sig is self.cam.temperature_setpoint and float(raw) < SAFE_SETPOINT and not self.allow_cooling:
            raise PermissionError(
                f"Setpoint {raw} degC is below {SAFE_SETPOINT} degC. Cooling an in-vacuum MTE "
                f"is only safe under vacuum; set {self.name}.allow_cooling = True to permit it.")

    # ---- geometry
    @property
    def image_key(self) -> str:
        return f"{self.name}_image"

    def set_roi(self, x=0, y=0, width=None, height=None, x_binning=1, y_binning=1):
        if self._writer is not None:
            raise RuntimeError("Cannot change ROI while staged")
        self.driver.set_roi(x, y, width, height, x_binning, y_binning)
        self._refresh_roi_signals()

    def _refresh_roi_signals(self):
        for attr in ("min_x", "min_y", "size_x", "size_y", "bin_x", "bin_y"):
            getattr(self.cam, attr).get()

    def frame_shape(self) -> tuple[int, ...]:
        """Shape of the data produced by one trigger()."""
        if self._writer is not None:
            return self._writer.frame_shape
        n = int(self.cam.num_images.get())
        hw = self.driver.image_shape()
        return (n, *hw) if n > 1 else hw

    # ---- file / document bookkeeping
    @property
    def write_dir(self) -> Path:
        """Local write directory: the explicit one, else from ``tiled_beamline`` now."""
        if self._write_dir is not None:
            return Path(self._write_dir)
        return Path(beamline_dirs(DETECTOR_DIR, self.data_root)[0])

    @write_dir.setter
    def write_dir(self, value: str | os.PathLike | None):
        self._write_dir = value

    @property
    def read_dir(self) -> str | None:
        """Tiled-server view of :attr:`write_dir` (``None``: same as ``write_dir``)."""
        if self._read_dir is not None:
            return str(self._read_dir)
        if self._write_dir is not None:
            return None   # only write_dir given: reference the local file as before
        return beamline_dirs(DETECTOR_DIR, self.data_root)[1]

    @read_dir.setter
    def read_dir(self, value: str | None):
        self._read_dir = value

    def _file_paths(self) -> tuple[Path, str]:
        parts = (time.strftime("%Y"), time.strftime("%m"), time.strftime("%d"),
                 f"{self.name}_{uuid.uuid4().hex[:12]}.h5")
        local = self.write_dir.joinpath(*parts).resolve()
        read_dir = self.read_dir
        if read_dir is None:
            return local, local.as_uri()
        rd = str(read_dir)
        base = PureWindowsPath(rd) if PureWindowsPath(rd).drive else PurePosixPath(rd)
        return local, base.joinpath(*parts).as_uri()

    def stage(self):
        staged = super().stage()
        try:
            local, uri = self._file_paths()
            d = self.driver
            self._writer = _HDF5FrameWriter(
                local, uri, self.image_key, self.frame_shape(), d.dtype, self.dataset,
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
            path = self._writer.path
            self._writer.close()
            self._writer = None
            _notify_files_closed([str(path)])
        return super().unstage()

    def describe(self):
        desc = super().describe()
        desc[self.image_key] = {
            "source": f"PICAM:{self.driver.serial}",
            "dtype": "array",
            "shape": list(self.frame_shape()),
            "dtype_numpy": self.driver.dtype.str,
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
    def _launch(self, n: int | None, writer: _HDF5FrameWriter | None) -> DeviceStatus:
        """Run an acquisition of ``n`` frames (None = continuous) in a worker thread."""
        if self._acq_thread is not None and self._acq_thread.is_alive():
            raise RuntimeError(f"{self.name} is already acquiring")
        drv = self.driver
        status = DeviceStatus(self)
        per_trigger = int(self.cam.num_images.get()) if writer is not None else 1
        batch: list[np.ndarray] = []

        def on_frame(img: np.ndarray, i: int):
            self.last_image = img
            self.image.array_data._update(img)
            self.cam.array_counter._update(self.cam.array_counter.get() + 1)
            if writer is not None:
                batch.append(img)
                if len(batch) == per_trigger:
                    writer.write(batch[0] if per_trigger == 1 else np.stack(batch))
                    batch.clear()

        def run():
            self.cam.acquire._update(1)
            self.cam.detector_state._update(DETECTOR_ACQUIRE)
            state = DETECTOR_IDLE
            try:
                got = drv.run(n, on_frame)
                if n is not None and got < n:
                    raise PicamError(f"Acquisition stopped after {got} of {n} frames")
                status.set_finished()
            except Exception as exc:
                state = DETECTOR_ERROR if self._driver is not None else DETECTOR_DISCONNECTED
                logger.error("%s acquisition failed: %s", self.name, exc)
                status.set_exception(exc)
            finally:
                self.cam.acquire._update(0)
                self.cam.detector_state._update(state)

        self.cam.array_counter._update(0)
        self._acq_thread = threading.Thread(target=run, name=f"{self.name}-acquire", daemon=True)
        self._acq_thread.start()
        return status

    def trigger(self):
        return self._launch(int(self.cam.num_images.get()), self._writer)

    def _start_free_run(self):
        mode = int(self.cam.image_mode.get())
        n = {0: 1, 1: int(self.cam.num_images.get()), 2: None}[mode]
        self._launch(n, None)

    def _request_stop(self):
        if self._driver is not None:
            self._driver.request_stop()

    def stop(self, *, success=False):
        self._request_stop()
        super().stop(success=success)

    def close(self):
        """Release resources (camera handle, open file). Lazily reopens on next use."""
        if self._writer is not None:
            self.unstage()
        self._close_driver()

    def destroy(self):
        """ophyd teardown (called by Lightfall when the device is disabled/removed)."""
        self.close()
        super().destroy()

    def __del__(self):
        # Last-resort release of the camera if the object is dropped without
        # destroy()/close(). Must never raise.
        try:
            if self._driver is not None:
                self._poll_stop.set()
                self._driver.request_stop()
                self._driver.close()
                self._driver = None
            if self._writer is not None:
                self._writer.close()
                self._writer = None
        except Exception:  # noqa: BLE001
            pass


def _poll_loop(det_ref, stop: threading.Event, period: float):
    """Temperature poller; exits when stopped, the camera closes, or the device is gone."""
    while not stop.wait(period):
        det = det_ref()
        if det is None:
            return
        drv = det._driver
        if drv is None or not drv.is_open:
            return
        try:
            det.cam.temperature.get()
            det.cam.temperature_status.get()
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s poll failed: %s", det.name, exc)
        del det
