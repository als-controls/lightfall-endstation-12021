# lightfall-endstation-12021

Lightfall plugins for the ALS Beamline 12.0.2.1 endstation.

This package extends [Lightfall](https://github.com/als-controls/lightfall) with
beamline-specific functionality, registered via the `lightfall.plugins` entry point:

- **PI-MTE camera** (`devices.PIMTEDetector`) — ophyd device for the Princeton
  Instruments PI-MTE: 2048B, driven directly through the PICam C API with ctypes
  (no EPICS IOC, no LightField). Frames are written to SWMR HDF5 and announced with
  `StreamResource`/`StreamDatum` documents for TiledWriter.
- **Endstation device database** (`devices/devices.json`) — packaged happi database,
  added to Lightfall's device catalog by the `endstation_12021_devices` device-backend
  plugin.

## PI-MTE notes

- Requires the PICam runtime (installed with LightField) at
  `C:\Program Files\Common Files\Princeton Instruments\Picam\Runtime`, or set
  `PICAM_RUNTIME`.
- PICam allows one owner per camera: **close LightField** before using the device.
  The device opens the camera lazily (first read/stage/trigger) and `det.close()`
  releases it.
- `write_dir` is where this PC writes HDF5; `read_dir` is the same location as the
  Tiled server sees it (must be within the server's readable storage).
- Lowering `temperature_setpoint` starts cooling. Only cool the in-vacuum MTE when it
  is under vacuum.

## Installation

```bash
pip install -e .
```

## Development

```bash
pip install -e ".[dev]"
pytest            # unit tests, fake PICam driver
pytest -m hw      # live camera tests (LightField closed)
```

## License

See [LICENSE.md](LICENSE.md) and [LEGAL.md](LEGAL.md).
