"""Beamline data directories keyed on Lightfall's ``tiled_beamline`` preference.

The data mover (shussebora) copies files relative to its source root, so writing
to ``<data_root>/<beamline>/<detector>/`` on this PC lands them at
``/data/<beamline>/<detector>/`` on bcgtiled, the location the Tiled server reads
(als-tiled requires assets under ``/data/<beamline>/``). Same scheme as
``lightfall_endstation_7011.devices.paths``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

TILED_ROOT = "/data"

_warned = False


def _beamline() -> str:
    """The ``tiled_beamline`` preference (e.g. ``"12.0.2.1"``), or ``""``."""
    try:
        from lightfall.ui.preferences.manager import PreferencesManager

        return str(PreferencesManager.get_instance().get("tiled_beamline", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def beamline_dirs(detector: str, data_root: str) -> tuple[str, str]:
    """Return ``(write_dir, read_dir)`` for ``detector``.

    ``write_dir`` is ``<data_root>/<beamline>/<detector>`` (local) and ``read_dir``
    is ``/data/<beamline>/<detector>`` (as the Tiled server sees it). With the
    ``tiled_beamline`` preference unset, the beamline level is left out (warning
    once).
    """
    global _warned
    root = str(data_root).rstrip("/\\")
    beamline = _beamline()
    if not beamline:
        if not _warned:
            _warned = True
            logger.warning(
                "tiled_beamline preference is unset; %s files will be written "
                "without a beamline directory under %s",
                detector,
                root,
            )
        return f"{root}/{detector}", f"{TILED_ROOT}/{detector}"
    return f"{root}/{beamline}/{detector}", f"{TILED_ROOT}/{beamline}/{detector}"
