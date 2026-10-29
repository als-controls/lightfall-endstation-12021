"""Thin ctypes driver for Princeton Instruments cameras via the PICam C API.

Loads ``Picam.dll`` from the PICam runtime that ships with LightField (override the
location with the ``PICAM_RUNTIME`` environment variable). Only one process may own a
camera, so LightField must be closed while this driver holds it.

Kept free of ophyd so the transport can be swapped (e.g. an asynchronous
StartAcquisition/WaitForAcquisitionUpdate backend) without touching the device class.
"""

from __future__ import annotations

import ctypes as C
import logging
import os
import threading

import numpy as np

logger = logging.getLogger(__name__)

PICAM_RUNTIME = os.environ.get(
    "PICAM_RUNTIME", r"C:\Program Files\Common Files\Princeton Instruments\Picam\Runtime"
)

# PicamValueType / PicamEnumeratedType values used here (from picam.h)
_VT_INT, _VT_FLOAT, _VT_BOOL, _VT_ENUM, _VT_ROIS, _VT_LARGE = 1, 2, 3, 4, 5, 6
_ET_ERROR, _ET_MODEL, _ET_IFACE, _ET_PARAM = 1, 2, 3, 6


class PicamError(RuntimeError):
    pass


class _CameraID(C.Structure):
    _fields_ = [("model", C.c_int), ("computer_interface", C.c_int),
                ("sensor_name", C.c_char * 64), ("serial_number", C.c_char * 64)]


class _AvailableData(C.Structure):
    _fields_ = [("initial_readout", C.c_void_p), ("readout_count", C.c_longlong)]


class _Roi(C.Structure):
    _fields_ = [(n, C.c_int) for n in ("x", "width", "x_binning", "y", "height", "y_binning")]


class _Rois(C.Structure):
    _fields_ = [("roi_array", C.POINTER(_Roi)), ("roi_count", C.c_int)]


# --------------------------------------------------------------------------- library
class _PicamLib:
    """Process-wide Picam.dll handle with reference-counted Initialize/Uninitialize."""

    _lock = threading.Lock()
    _dll = None
    _refs = 0

    @classmethod
    def acquire(cls):
        with cls._lock:
            if cls._dll is None:
                os.add_dll_directory(PICAM_RUNTIME)
                cls._dll = C.WinDLL(os.path.join(PICAM_RUNTIME, "Picam.dll"))
            if cls._refs == 0:
                err = cls._dll.Picam_InitializeLibrary()
                if err:
                    raise PicamError(f"Picam_InitializeLibrary failed ({err})")
            cls._refs += 1
            return cls._dll

    @classmethod
    def release(cls):
        with cls._lock:
            cls._refs -= 1
            if cls._refs == 0 and cls._dll is not None:
                cls._dll.Picam_UninitializeLibrary()


# --------------------------------------------------------------------------- driver
class PicamCamera:
    """Thin, thread-safe wrapper around one open PICam camera handle."""

    def __init__(self, serial: str | None = None):
        self._dll = _PicamLib.acquire()
        self._lock = threading.RLock()
        self._handle = C.c_void_p()
        self._acquiring = False
        try:
            self.camera_id = self._find_camera(serial)
            self._check(self._dll.Picam_OpenCamera(C.byref(self.camera_id), C.byref(self._handle)),
                        "Picam_OpenCamera")
            self.params = self._build_param_map()
        except Exception:
            _PicamLib.release()
            raise
        self.model = self.enum_string(_ET_MODEL, self.camera_id.model)
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
            raise PicamError(f"{what}: {self.enum_string(_ET_ERROR, err)} (error {err})")

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
            raise PicamError("Camera is in use by another process -- close LightField and retry.")
        raise PicamError(f"No PICam camera found (serial={serial!r}).")

    def _build_param_map(self) -> dict[str, int]:
        plist, n = C.POINTER(C.c_int)(), C.c_int()
        self._check(self._dll.Picam_GetParameters(self._handle, C.byref(plist), C.byref(n)),
                    "Picam_GetParameters")
        params = {self.enum_string(_ET_PARAM, plist[i]): plist[i] for i in range(n.value)}
        self._dll.Picam_DestroyParameters(plist)
        return params

    def _pid(self, name: str) -> int:
        try:
            return self.params[name]
        except KeyError:
            raise PicamError(f"Camera does not support parameter {name!r}") from None

    @staticmethod
    def _vtype(pid: int) -> int:
        return (pid >> 16) & 0xFF

    # ---- enum support
    def enum_type(self, name: str) -> int:
        et = C.c_int()
        self._check(self._dll.Picam_GetParameterEnumeratedType(self._handle, self._pid(name),
                                                                C.byref(et)), name)
        return et.value

    def enum_choices(self, name: str) -> dict[str, int]:
        et = self.enum_type(name)
        out = {}
        for v in range(0, 64):
            s = self.enum_string(et, v)
            if not s.startswith("<"):
                out[s] = v
        return out

    # ---- parameter access
    def get(self, name: str, online: bool = False):
        """Return parameter value. ``online=True`` reads live from hardware."""
        pid = self._pid(name)
        vt = self._vtype(pid)
        prefix = "Picam_ReadParameter" if online else "Picam_GetParameter"
        with self._lock:
            if vt in (_VT_INT, _VT_BOOL, _VT_ENUM):
                x = C.c_int()
                self._check(getattr(self._dll, prefix + "IntegerValue")(self._handle, pid, C.byref(x)), name)
                if vt == _VT_ENUM:
                    return self.enum_string(self.enum_type(name), x.value)
                return bool(x.value) if vt == _VT_BOOL else x.value
            if vt == _VT_FLOAT:
                x = C.c_double()
                self._check(getattr(self._dll, prefix + "FloatingPointValue")(self._handle, pid, C.byref(x)), name)
                return x.value
            if vt == _VT_LARGE:
                x = C.c_longlong()
                self._check(self._dll.Picam_GetParameterLargeIntegerValue(self._handle, pid, C.byref(x)), name)
                return x.value
        raise PicamError(f"{name}: unsupported value type {vt}")

    def set(self, name: str, value, commit: bool = True):
        pid = self._pid(name)
        vt = self._vtype(pid)
        with self._lock:
            if self._acquiring:
                raise PicamError(f"Cannot set {name!r} while acquiring")
            if vt == _VT_ENUM and isinstance(value, str):
                choices = self.enum_choices(name)
                if value not in choices:
                    raise ValueError(f"{name}: {value!r} not in {list(choices)}")
                value = choices[value]
            if vt in (_VT_INT, _VT_BOOL, _VT_ENUM):
                err = self._dll.Picam_SetParameterIntegerValue(self._handle, pid, C.c_int(int(value)))
            elif vt == _VT_FLOAT:
                err = self._dll.Picam_SetParameterFloatingPointValue(self._handle, pid, C.c_double(value))
            elif vt == _VT_LARGE:
                err = self._dll.Picam_SetParameterLargeIntegerValue(self._handle, pid, C.c_longlong(int(value)))
            else:
                raise PicamError(f"{name}: unsupported value type {vt}")
            self._check(err, f"set {name}={value!r}")
            if commit:
                self.commit()

    def commit(self):
        with self._lock:
            failed, n = C.POINTER(C.c_int)(), C.c_int()
            err = self._dll.Picam_CommitParameters(self._handle, C.byref(failed), C.byref(n))
            bad = [self.enum_string(_ET_PARAM, failed[i]) for i in range(n.value)]
            if n.value:
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
            r = rois.contents.roi_array[0]
            out = {f: getattr(r, f) for f, _ in _Roi._fields_}
            self._dll.Picam_DestroyRois(rois)
        return out

    def set_roi(self, x=0, y=0, width=None, height=None, x_binning=1, y_binning=1):
        width = width or self.get("Sensor Active Width") - x
        height = height or self.get("Sensor Active Height") - y
        roi = _Roi(x, width, x_binning, y, height, y_binning)
        rois = _Rois(C.pointer(roi), 1)
        with self._lock:
            self._check(self._dll.Picam_SetParameterRoisValue(self._handle, self._pid("ROIs"),
                                                              C.byref(rois)), "set ROIs")
            self.commit()

    def image_shape(self) -> tuple[int, int]:
        r = self.get_roi()
        return r["height"] // r["y_binning"], r["width"] // r["x_binning"]

    # ---- acquisition
    def acquire(self, n: int = 1, timeout_s: float | None = None) -> np.ndarray:
        """Blocking acquisition of ``n`` frames. Returns a uint16 copy of shape (n, H, W)."""
        with self._lock:
            self.commit()
            h, w = self.image_shape()
            stride = self.get("Readout Stride")
            if timeout_s is None:
                timeout_s = (self.get("Exposure Time") + self.get("Readout Time Calculation")) / 1e3 * 2 + 10
            data, errors = _AvailableData(), C.c_int()
            self._acquiring = True
        try:
            # Picam_Acquire blocks; run outside the lock so stop()/status reads can get in.
            err = self._dll.Picam_Acquire(self._handle, C.c_longlong(n), C.c_int(int(timeout_s * 1e3)),
                                          C.byref(data), C.byref(errors))
        finally:
            self._acquiring = False
        self._check(err, "Picam_Acquire")
        if errors.value:
            raise PicamError(f"Acquisition error mask 0x{errors.value:x}")
        if data.readout_count != n:
            raise PicamError(f"Expected {n} readouts, got {data.readout_count}")
        raw = C.string_at(data.initial_readout, stride * n)   # copy out of PICam's buffer
        frames = np.frombuffer(raw, dtype="<u2").reshape(n, stride // 2)[:, : h * w]
        return frames.reshape(n, h, w).copy()

    @property
    def acquiring(self) -> bool:
        return self._acquiring

    def stop(self):
        if self._acquiring:
            self._dll.Picam_StopAcquisition(self._handle)

    def close(self):
        with self._lock:
            if self._handle:
                self._dll.Picam_CloseCamera(self._handle)
                self._handle = C.c_void_p()
                _PicamLib.release()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
