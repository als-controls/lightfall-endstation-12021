"""Thin ctypes driver for Princeton Instruments cameras via the PICam C API.

Loads ``Picam.dll`` from the PICam runtime that ships with LightField (override the
location with the ``PICAM_RUNTIME`` environment variable). Only one process may own a
camera, so LightField must be closed while this driver holds it.

Acquisition is asynchronous: the driver supplies its own circular buffer
(``PicamAdvanced_SetAcquisitionBuffer``), starts with ``Picam_StartAcquisition`` and
polls ``Picam_WaitForAcquisitionUpdate``. This gives a working stop, streaming of
frames as they arrive, and continuous (TV-mode) acquisition.

Kept free of ophyd so it can be tested and swapped independently of the device class.
"""

from __future__ import annotations

import ctypes as C
import logging
import os
import threading
import time
from collections.abc import Callable

import numpy as np

logger = logging.getLogger(__name__)

PICAM_RUNTIME = os.environ.get(
    "PICAM_RUNTIME", r"C:\Program Files\Common Files\Princeton Instruments\Picam\Runtime"
)

# PicamValueType
VT_INT, VT_FLOAT, VT_BOOL, VT_ENUM, VT_ROIS, VT_LARGE = 1, 2, 3, 4, 5, 6
# PicamConstraintType
CT_NONE, CT_RANGE, CT_COLLECTION = 1, 2, 3
# PicamEnumeratedType (subset)
ET_ERROR, ET_MODEL, ET_IFACE, ET_PARAM = 1, 2, 3, 6
# PicamConstraintCategory
CAT_CAPABLE, CAT_REQUIRED = 1, 2
# PicamError (subset)
ERR_NONE, ERR_TIMEOUT = 0, 32
# PicamAcquisitionErrorsMask
ACQ_ERRORS = {0x01: "DataLost", 0x02: "ConnectionLost", 0x04: "DataNotArriving",
              0x08: "ShutterOverheated", 0x10: "CameraFaulted"}
ACQ_FATAL = 0x02 | 0x10

PicamHandle = C.c_void_p
pibln = C.c_int


class PicamError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class _CameraID(C.Structure):
    _fields_ = [("model", C.c_int), ("computer_interface", C.c_int),
                ("sensor_name", C.c_char * 64), ("serial_number", C.c_char * 64)]


class _AvailableData(C.Structure):
    _fields_ = [("initial_readout", C.c_void_p), ("readout_count", C.c_longlong)]


class _AcquisitionStatus(C.Structure):
    _fields_ = [("running", pibln), ("errors", C.c_int), ("readout_rate", C.c_double)]


class _AcquisitionBuffer(C.Structure):
    _fields_ = [("memory", C.c_void_p), ("memory_size", C.c_longlong)]


class _Roi(C.Structure):
    _fields_ = [(n, C.c_int) for n in ("x", "width", "x_binning", "y", "height", "y_binning")]


class _Rois(C.Structure):
    _fields_ = [("roi_array", C.POINTER(_Roi)), ("roi_count", C.c_int)]


class _RangeConstraint(C.Structure):
    _fields_ = [("scope", C.c_int), ("severity", C.c_int), ("empty_set", pibln),
                ("minimum", C.c_double), ("maximum", C.c_double), ("increment", C.c_double),
                ("excluded_values_array", C.POINTER(C.c_double)), ("excluded_values_count", C.c_int),
                ("outlying_values_array", C.POINTER(C.c_double)), ("outlying_values_count", C.c_int)]


class _CollectionConstraint(C.Structure):
    _fields_ = [("scope", C.c_int), ("severity", C.c_int),
                ("values_array", C.POINTER(C.c_double)), ("values_count", C.c_int)]


_P = C.POINTER
_SIGNATURES = {
    "Picam_InitializeLibrary": [],
    "Picam_UninitializeLibrary": [],
    "Picam_IsLibraryInitialized": [_P(pibln)],
    "Picam_GetVersion": [_P(C.c_int)] * 4,
    "Picam_GetEnumerationString": [C.c_int, C.c_int, _P(C.c_char_p)],
    "Picam_DestroyString": [C.c_char_p],
    "Picam_GetAvailableCameraIDs": [_P(_P(_CameraID)), _P(C.c_int)],
    "Picam_GetUnavailableCameraIDs": [_P(_P(_CameraID)), _P(C.c_int)],
    "Picam_DestroyCameraIDs": [_P(_CameraID)],
    "Picam_OpenCamera": [_P(_CameraID), _P(PicamHandle)],
    "Picam_CloseCamera": [PicamHandle],
    "Picam_IsCameraConnected": [PicamHandle, _P(pibln)],
    "PicamAdvanced_GetCameraDevice": [PicamHandle, _P(PicamHandle)],
    "PicamAdvanced_SetAcquisitionBuffer": [PicamHandle, _P(_AcquisitionBuffer)],
    "PicamAdvanced_HasAcquisitionBufferOverrun": [PicamHandle, _P(pibln)],
    "Picam_GetParameters": [PicamHandle, _P(_P(C.c_int)), _P(C.c_int)],
    "Picam_DestroyParameters": [_P(C.c_int)],
    "Picam_CanReadParameter": [PicamHandle, C.c_int, _P(pibln)],
    "Picam_GetParameterEnumeratedType": [PicamHandle, C.c_int, _P(C.c_int)],
    "Picam_GetParameterIntegerValue": [PicamHandle, C.c_int, _P(C.c_int)],
    "Picam_ReadParameterIntegerValue": [PicamHandle, C.c_int, _P(C.c_int)],
    "Picam_SetParameterIntegerValue": [PicamHandle, C.c_int, C.c_int],
    "Picam_GetParameterFloatingPointValue": [PicamHandle, C.c_int, _P(C.c_double)],
    "Picam_ReadParameterFloatingPointValue": [PicamHandle, C.c_int, _P(C.c_double)],
    "Picam_SetParameterFloatingPointValue": [PicamHandle, C.c_int, C.c_double],
    "Picam_GetParameterLargeIntegerValue": [PicamHandle, C.c_int, _P(C.c_longlong)],
    "Picam_SetParameterLargeIntegerValue": [PicamHandle, C.c_int, C.c_longlong],
    "Picam_GetParameterRoisValue": [PicamHandle, C.c_int, _P(_P(_Rois))],
    "Picam_SetParameterRoisValue": [PicamHandle, C.c_int, _P(_Rois)],
    "Picam_DestroyRois": [_P(_Rois)],
    "Picam_GetParameterRangeConstraint": [PicamHandle, C.c_int, C.c_int, _P(_P(_RangeConstraint))],
    "Picam_DestroyRangeConstraints": [_P(_RangeConstraint)],
    "Picam_GetParameterCollectionConstraint": [PicamHandle, C.c_int, C.c_int,
                                               _P(_P(_CollectionConstraint))],
    "Picam_DestroyCollectionConstraints": [_P(_CollectionConstraint)],
    "Picam_CommitParameters": [PicamHandle, _P(_P(C.c_int)), _P(C.c_int)],
    "Picam_StartAcquisition": [PicamHandle],
    "Picam_StopAcquisition": [PicamHandle],
    "Picam_IsAcquisitionRunning": [PicamHandle, _P(pibln)],
    "Picam_WaitForAcquisitionUpdate": [PicamHandle, C.c_int, _P(_AvailableData),
                                       _P(_AcquisitionStatus)],
}


# --------------------------------------------------------------------------- library
class _PicamLib:
    """Process-wide Picam.dll handle with reference-counted Initialize/Uninitialize."""

    _lock = threading.Lock()
    _dll = None
    _refs = 0
    _owns_init = False   # False if PICam was already initialized in-process by someone else

    @classmethod
    def acquire(cls):
        with cls._lock:
            if cls._dll is None:
                os.add_dll_directory(PICAM_RUNTIME)
                dll = C.WinDLL(os.path.join(PICAM_RUNTIME, "Picam.dll"))
                for name, argtypes in _SIGNATURES.items():
                    fn = getattr(dll, name)
                    fn.argtypes, fn.restype = argtypes, C.c_int
                cls._dll = dll
            if cls._refs == 0:
                inited = pibln()
                cls._dll.Picam_IsLibraryInitialized(C.byref(inited))
                if inited.value:
                    cls._owns_init = False      # borrow; don't uninitialize on release
                else:
                    err = cls._dll.Picam_InitializeLibrary()
                    if err:
                        raise PicamError(f"Picam_InitializeLibrary failed ({err})", err)
                    cls._owns_init = True
            cls._refs += 1
            return cls._dll

    @classmethod
    def release(cls):
        with cls._lock:
            if cls._refs <= 0:
                return
            cls._refs -= 1
            if cls._refs == 0 and cls._dll is not None and cls._owns_init:
                cls._dll.Picam_UninitializeLibrary()
                cls._owns_init = False


# --------------------------------------------------------------------------- driver
class PicamCamera:
    """One open PICam camera: parameters, constraints, ROI and asynchronous acquisition.

    All calls are serialized by an internal lock except the acquisition wait loop,
    which runs in the caller's thread (see :meth:`run`). :meth:`request_stop` may be
    called from any thread.
    """

    def __init__(self, serial: str | None = None):
        self._dll = _PicamLib.acquire()
        self._lock = threading.RLock()
        self._handle = PicamHandle()
        self._device = PicamHandle()
        self._acquiring = False
        self._stop_requested = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._buffer = None
        try:
            self.camera_id = self._find_camera(serial)
            self._check(self._dll.Picam_OpenCamera(C.byref(self.camera_id), C.byref(self._handle)),
                        "Picam_OpenCamera")
            self._check(self._dll.PicamAdvanced_GetCameraDevice(self._handle, C.byref(self._device)),
                        "PicamAdvanced_GetCameraDevice")
            self.params = self._build_param_map()
            self._readable = {name: self._can_read(pid) for name, pid in self.params.items()}
        except Exception:
            if self._handle:
                self._dll.Picam_CloseCamera(self._handle)
                self._handle = PicamHandle()
            _PicamLib.release()
            raise
        self.model = self.enum_string(ET_MODEL, self.camera_id.model)
        self.serial = self.camera_id.serial_number.decode()
        self.sensor = self.camera_id.sensor_name.decode()
        logger.info("Opened %s s/n %s (%s)", self.model, self.serial, self.sensor)

    # ---- helpers
    def enum_string(self, etype: int, value: int) -> str:
        s = C.c_char_p()
        if self._dll.Picam_GetEnumerationString(etype, value, C.byref(s)):
            return f"<{value}>"
        out = s.value.decode()
        self._dll.Picam_DestroyString(s)
        return out

    def _check(self, err: int, what: str):
        if err:
            raise PicamError(f"{what}: {self.enum_string(ET_ERROR, err)} (error {err})", err)

    def _find_camera(self, serial):
        found = []
        for fn, in_use in (("Picam_GetAvailableCameraIDs", False),
                           ("Picam_GetUnavailableCameraIDs", True)):
            arr, n = C.POINTER(_CameraID)(), C.c_int()
            self._check(getattr(self._dll, fn)(C.byref(arr), C.byref(n)), fn)
            for i in range(n.value):
                cid = _CameraID.from_buffer_copy(arr[i])
                if serial is None or cid.serial_number.decode() == serial:
                    found.append((cid, in_use))
            self._dll.Picam_DestroyCameraIDs(arr)
        free = [c for c, in_use in found if not in_use]
        if free:
            return free[0]
        if found:
            raise PicamError("Camera is in use by another process (LightField?) -- close it and retry.")
        raise PicamError(f"No PICam camera found (serial={serial!r}).")

    def _build_param_map(self) -> dict[str, int]:
        plist, n = C.POINTER(C.c_int)(), C.c_int()
        self._check(self._dll.Picam_GetParameters(self._handle, C.byref(plist), C.byref(n)),
                    "Picam_GetParameters")
        params = {self.enum_string(ET_PARAM, plist[i]): plist[i] for i in range(n.value)}
        self._dll.Picam_DestroyParameters(plist)
        return params

    def _can_read(self, pid: int) -> bool:
        r = pibln()
        return not self._dll.Picam_CanReadParameter(self._handle, pid, C.byref(r)) and bool(r.value)

    def _pid(self, name: str) -> int:
        try:
            return self.params[name]
        except KeyError:
            raise PicamError(f"Camera does not support parameter {name!r}") from None

    @staticmethod
    def value_type(pid: int) -> int:
        return (pid >> 16) & 0xFF

    @staticmethod
    def constraint_type(pid: int) -> int:
        return (pid >> 24) & 0xFF

    # ---- connection
    @property
    def is_open(self) -> bool:
        return bool(self._handle)

    def is_connected(self) -> bool:
        if not self._handle:
            return False
        r = pibln()
        return not self._dll.Picam_IsCameraConnected(self._handle, C.byref(r)) and bool(r.value)

    # ---- enums & constraints
    def enum_type(self, name: str) -> int:
        et = C.c_int()
        self._check(self._dll.Picam_GetParameterEnumeratedType(self._handle, self._pid(name),
                                                                C.byref(et)), name)
        return et.value

    def enum_choices(self, name: str) -> dict[str, int]:
        """Allowed enum values {label: value}, from the Required collection constraint."""
        et = self.enum_type(name)
        values = self.collection(name) if self.constraint_type(self._pid(name)) == CT_COLLECTION else None
        if values is None:
            values = [v for v in range(64) if not self.enum_string(et, v).startswith("<")]
        return {self.enum_string(et, int(v)): int(v) for v in values}

    def collection(self, name: str, category: int = CAT_REQUIRED) -> list[float] | None:
        """Allowed values of a collection-constrained parameter (None if not a collection)."""
        pid = self._pid(name)
        if self.constraint_type(pid) != CT_COLLECTION:
            return None
        c = C.POINTER(_CollectionConstraint)()
        with self._lock:
            self._check(self._dll.Picam_GetParameterCollectionConstraint(
                self._handle, pid, category, C.byref(c)), f"collection {name}")
            vals = [c.contents.values_array[i] for i in range(c.contents.values_count)]
            self._dll.Picam_DestroyCollectionConstraints(c)
        return vals

    def range(self, name: str, category: int = CAT_REQUIRED) -> dict | None:
        """{minimum, maximum, increment} of a range-constrained parameter (None if not a range)."""
        pid = self._pid(name)
        if self.constraint_type(pid) != CT_RANGE:
            return None
        c = C.POINTER(_RangeConstraint)()
        with self._lock:
            self._check(self._dll.Picam_GetParameterRangeConstraint(
                self._handle, pid, category, C.byref(c)), f"range {name}")
            r = c.contents
            out = {"minimum": r.minimum, "maximum": r.maximum, "increment": r.increment,
                   "empty": bool(r.empty_set)}
            self._dll.Picam_DestroyRangeConstraints(c)
        return out

    # ---- parameter access
    def get(self, name: str, online: bool = False):
        """Return a parameter value. ``online=True`` reads live from hardware when the
        parameter is readable; enums are returned as their label."""
        pid = self._pid(name)
        vt = self.value_type(pid)
        live = online and self._readable.get(name, False)
        prefix = "Picam_ReadParameter" if live else "Picam_GetParameter"
        with self._lock:
            if vt in (VT_INT, VT_BOOL, VT_ENUM):
                x = C.c_int()
                self._check(getattr(self._dll, prefix + "IntegerValue")(self._handle, pid, C.byref(x)), name)
                if vt == VT_ENUM:
                    return self.enum_string(self.enum_type(name), x.value)
                return bool(x.value) if vt == VT_BOOL else x.value
            if vt == VT_FLOAT:
                x = C.c_double()
                self._check(getattr(self._dll, prefix + "FloatingPointValue")(self._handle, pid, C.byref(x)), name)
                return x.value
            if vt == VT_LARGE:   # no Read* variant exists for 64-bit values
                x = C.c_longlong()
                self._check(self._dll.Picam_GetParameterLargeIntegerValue(self._handle, pid, C.byref(x)), name)
                return x.value
        raise PicamError(f"{name}: unsupported value type {vt}")

    def set(self, name: str, value, commit: bool = True):
        pid = self._pid(name)
        vt = self.value_type(pid)
        with self._lock:
            if self._acquiring:
                raise PicamError(f"Cannot set {name!r} while acquiring")
            if vt == VT_ENUM and isinstance(value, str):
                choices = self.enum_choices(name)
                if value not in choices:
                    raise ValueError(f"{name}: {value!r} not in {list(choices)}")
                value = choices[value]
            if vt in (VT_INT, VT_BOOL, VT_ENUM):
                err = self._dll.Picam_SetParameterIntegerValue(self._handle, pid, int(value))
            elif vt == VT_FLOAT:
                err = self._dll.Picam_SetParameterFloatingPointValue(self._handle, pid, float(value))
            elif vt == VT_LARGE:
                err = self._dll.Picam_SetParameterLargeIntegerValue(self._handle, pid, int(value))
            else:
                raise PicamError(f"{name}: unsupported value type {vt}")
            self._check(err, f"set {name}={value!r}")
            if commit:
                self.commit()

    def commit(self):
        with self._lock:
            failed, n = C.POINTER(C.c_int)(), C.c_int()
            err = self._dll.Picam_CommitParameters(self._handle, C.byref(failed), C.byref(n))
            bad = [self.enum_string(ET_PARAM, failed[i]) for i in range(n.value)]
            if failed:
                self._dll.Picam_DestroyParameters(failed)
            if bad:
                raise PicamError(f"Commit rejected parameters: {bad}")
            self._check(err, "Picam_CommitParameters")

    # ---- ROI / geometry
    def get_roi(self) -> dict:
        rois = C.POINTER(_Rois)()
        with self._lock:
            self._check(self._dll.Picam_GetParameterRoisValue(self._handle, self._pid("ROIs"),
                                                              C.byref(rois)), "get ROIs")
            try:
                if rois.contents.roi_count != 1:
                    raise PicamError(f"{rois.contents.roi_count} ROIs configured; only one is supported")
                r = rois.contents.roi_array[0]
                return {f: getattr(r, f) for f, _ in _Roi._fields_}
            finally:
                self._dll.Picam_DestroyRois(rois)

    def set_roi(self, x=0, y=0, width=None, height=None, x_binning=1, y_binning=1):
        width = width or self.get("Sensor Active Width") - x
        height = height or self.get("Sensor Active Height") - y
        roi = _Roi(x, width, x_binning, y, height, y_binning)
        rois = _Rois(C.pointer(roi), 1)
        with self._lock:
            if self._acquiring:
                raise PicamError("Cannot change ROI while acquiring")
            self._check(self._dll.Picam_SetParameterRoisValue(self._handle, self._pid("ROIs"),
                                                              C.byref(rois)), "set ROIs")
            self.commit()

    def image_shape(self) -> tuple[int, int]:
        r = self.get_roi()
        return r["height"] // r["y_binning"], r["width"] // r["x_binning"]

    @property
    def dtype(self) -> np.dtype:
        bits = self.get("Pixel Bit Depth")
        return np.dtype("<u2") if bits <= 16 else np.dtype("<u4")

    # ---- acquisition
    @property
    def acquiring(self) -> bool:
        return self._acquiring

    def frame_timeout(self) -> float | None:
        """Seconds to wait for one frame: exposure + readout + margin, or None (forever)
        when the camera waits for an external trigger."""
        if self.get("Trigger Response") != "No Response":
            return None
        return (self.get("Exposure Time") + self.get("Readout Time Calculation")) / 1e3 * 1.5 + 10

    def run(self, n: int | None, on_frame: Callable[[np.ndarray, int], None],
            frame_timeout: float | None = -1.0, buffer_readouts: int = 4) -> int:
        """Acquire ``n`` frames (``None`` = continuous until :meth:`request_stop`).

        Blocks in the calling thread; ``on_frame(image, index)`` is called with a copy of
        each frame (shape (H, W)) as it arrives. Returns the number of frames delivered.
        ``frame_timeout``: seconds without a new frame before failing; -1 = automatic,
        None = wait forever.
        """
        with self._lock:
            if self._acquiring:
                raise PicamError("Acquisition already in progress")
            self.commit()
            h, w = self.image_shape()
            dtype = self.dtype
            frame_px = h * w
            fpr = self.get("Frames per Readout")
            frame_stride = self.get("Frame Stride")
            readout_stride = self.get("Readout Stride")
            if frame_timeout == -1.0:
                frame_timeout = self.frame_timeout()
            n_readouts = None if n is None else -(-n // fpr)
            nbuf = max(2, min(buffer_readouts, n_readouts or buffer_readouts))
            self._buffer = C.create_string_buffer(readout_stride * nbuf)
            base = C.addressof(self._buffer)
            ab = _AcquisitionBuffer(base, readout_stride * nbuf)
            self._check(self._dll.PicamAdvanced_SetAcquisitionBuffer(self._device, C.byref(ab)),
                        "SetAcquisitionBuffer")
            self.set("Readout Count", 0 if n is None else n_readouts, commit=True)
            self._stop_requested.clear()
            self._idle.clear()
            self._check(self._dll.Picam_StartAcquisition(self._handle), "Picam_StartAcquisition")
            self._acquiring = True

        delivered = readouts = 0
        avail, status = _AvailableData(), _AcquisitionStatus()
        deadline = None if frame_timeout is None else time.monotonic() + frame_timeout
        stop_sent = False
        try:
            while True:
                if self._stop_requested.is_set() and not stop_sent:
                    self._dll.Picam_StopAcquisition(self._handle)
                    stop_sent = True
                err = self._dll.Picam_WaitForAcquisitionUpdate(self._handle, 100, C.byref(avail),
                                                               C.byref(status))
                if err not in (ERR_NONE, ERR_TIMEOUT):
                    self._check(err, "Picam_WaitForAcquisitionUpdate")
                if err == ERR_NONE and status.errors:
                    names = [v for k, v in ACQ_ERRORS.items() if status.errors & k]
                    raise PicamError(f"Acquisition errors: {names} (mask 0x{status.errors:x})",
                                     status.errors)
                if err == ERR_NONE and avail.initial_readout and avail.readout_count:
                    expected = base + (readouts % nbuf) * readout_stride
                    if avail.initial_readout != expected:
                        raise PicamError("Acquisition buffer overrun (frames were lost)")
                    for k in range(avail.readout_count):
                        addr = base + ((readouts + k) % nbuf) * readout_stride
                        for f in range(fpr):
                            if n is not None and delivered >= n:
                                break
                            raw = C.string_at(addr + f * frame_stride, frame_px * dtype.itemsize)
                            on_frame(np.frombuffer(raw, dtype=dtype).reshape(h, w).copy(), delivered)
                            delivered += 1
                    readouts += avail.readout_count
                    if deadline is not None:
                        deadline = time.monotonic() + frame_timeout
                if err == ERR_NONE and not status.running:
                    break
                if deadline is not None and time.monotonic() > deadline and not stop_sent:
                    raise PicamError(f"No frame within {frame_timeout:.1f} s", ERR_TIMEOUT)
        finally:
            self._finish_acquisition(stop_sent)
        return delivered

    def _finish_acquisition(self, stop_sent: bool):
        """Stop (if still running) and drain until PICam reports not running."""
        running = pibln(1)
        avail, status = _AvailableData(), _AcquisitionStatus()
        try:
            self._dll.Picam_IsAcquisitionRunning(self._handle, C.byref(running))
            if running.value:
                if not stop_sent:
                    self._dll.Picam_StopAcquisition(self._handle)
                t_end = time.monotonic() + 30
                while running.value and time.monotonic() < t_end:
                    self._dll.Picam_WaitForAcquisitionUpdate(self._handle, 100, C.byref(avail),
                                                            C.byref(status))
                    self._dll.Picam_IsAcquisitionRunning(self._handle, C.byref(running))
                if running.value:
                    logger.error("PICam acquisition did not stop within 30 s")
        finally:
            with self._lock:
                empty = _AcquisitionBuffer(None, 0)
                self._dll.PicamAdvanced_SetAcquisitionBuffer(self._device, C.byref(empty))
                self._buffer = None
                self._acquiring = False
                self._idle.set()

    def acquire(self, n: int = 1, frame_timeout: float | None = -1.0) -> np.ndarray:
        """Blocking acquisition of ``n`` frames; returns an array of shape (n, H, W)."""
        frames: list[np.ndarray] = []
        self.run(n, lambda img, i: frames.append(img), frame_timeout=frame_timeout)
        if len(frames) != n:
            raise PicamError(f"Acquisition stopped after {len(frames)} of {n} frames")
        return np.stack(frames)

    def request_stop(self):
        """Ask a running :meth:`run` to stop (thread-safe, returns immediately)."""
        self._stop_requested.set()

    def stop(self, timeout: float = 30.0) -> bool:
        """Stop a running acquisition and wait until it has finished."""
        self.request_stop()
        return self._idle.wait(timeout)

    def close(self):
        self.stop()
        with self._lock:
            if self._handle:
                self._dll.Picam_CloseCamera(self._handle)
                self._handle = PicamHandle()
                self._device = PicamHandle()
                _PicamLib.release()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
