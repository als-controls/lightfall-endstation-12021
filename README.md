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
- **PI-MTE camera widget** (`widgets.pimte.PIMTEControllerPlugin`) — Lightfall's
  plan-based camera widget (live view, TV mode, dark frames) plus temperature panel
  and a PI-MTE panel: Connect/Release (to share the camera with LightField), trigger,
  ADC speed, binning, readout time, and the allow-cooling interlock.

## PI-MTE notes

- Requires the PICam runtime (installed with LightField) at
  `C:\Program Files\Common Files\Princeton Instruments\Picam\Runtime`, or set
  `PICAM_RUNTIME`.
- PICam allows one owner per camera: **close LightField** before using the device.
  The device opens the camera lazily (first read/stage/trigger).
  `det.release_camera()` (or **Release** in the widget) closes it and keeps it closed
  until `det.connect_camera()` / **Connect**, so LightField can use it meanwhile.
  Disabling the device in Lightfall's device tree does *not* release the camera.
- Signals follow areaDetector names under `det.cam` (`acquire_time` in seconds,
  `num_images`, `image_mode`, `acquire`, `shutter_mode`, `bin_x`, ...);
  `det.image.array_data` holds the latest frame for live view only.
- Acquisition is asynchronous (own circular buffer + `Picam_StartAcquisition` /
  `Picam_WaitForAcquisitionUpdate`): frames stream as they arrive, continuous mode
  works, and stop takes effect within ~0.1 s even mid-readout.
- `python scripts/evaluate_picam.py` characterizes the driver on the live camera.
- `write_dir` is where this PC writes HDF5; `read_dir` is the same location as the
  Tiled server sees it (must be within the server's readable storage). Defaults,
  resolved at stage time from Lightfall's `tiled_beamline` preference:
  `write_dir = <data_root>/<tiled_beamline>/pi_mte` (`data_root` kwarg, default
  `D:/data`) and `read_dir = /data/<tiled_beamline>/pi_mte`. With the preference
  unset the beamline level is left out (warning logged). Explicit `write_dir` /
  `read_dir` kwargs win; `write_dir` alone keeps the local `file://` URI.
- Lowering `cam.temperature_setpoint` starts cooling. Only cool the in-vacuum MTE when
  it is under vacuum. Setpoints below 20 °C are refused unless `det.allow_cooling` is
  True (the widget's "Allow cooling" checkbox, which asks for confirmation).

## Data movement

[shussebora](https://git.als.lbl.gov/ncs/shussebora) runs as a Windows service on the
beamline PC with source root `D:/data`. It copies each file to bcgtiled under the same
relative path (`/data/12.0.2.1/pi_mte/YYYY/MM/DD/...`), which is the `read_dir` the
StreamResource documents point at, and prunes the local copy after its retention.

There is no EPICS file plugin for shussebora to watch, so the device tells it directly:
`unstage` (which also runs after an abort or stop) closes the HDF5 file and calls
`lightfall.services.shussebora_files.notify_files_closed([path])`. That sends a
`<prefix>.file.closed` NATS request to the shussebora instance whose source root covers
the path. It never blocks or fails the run; a missed notification is picked up by
shussebora's catch-up pass. With an older Lightfall that lacks the helper, nothing is
sent and catch-up alone moves the files.

Planned, not wired yet (waiting on the Crucible project mapping): Lightfall's
`CrucibleIngestor` in external mode with `path_map=[("/data", "D:/data")]`, so its
claims name the local paths shussebora knows and the uploads go through shussebora.

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
