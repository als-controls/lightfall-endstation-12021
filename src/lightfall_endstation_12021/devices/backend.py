"""Device backend plugin: ships the 12.0.2.1 endstation happi database with this package."""

from __future__ import annotations

from lightfall.plugins.happi_database_plugin import HappiDatabasePlugin


class Endstation12021Devices(HappiDatabasePlugin):
    """Adds the packaged ``devices.json`` happi database to Lightfall's device catalog.

    Disable with the ``device_plugin_endstation_12021_devices_enabled`` preference.
    """

    database_resource = ("lightfall_endstation_12021.devices", "devices.json")
    beamline = "12.0.2.1"
    # Devices are instantiated in the background; PIMTEDetector opens the camera
    # lazily, so instantiation alone never takes the camera away from LightField.
    instantiate = "background"

    @property
    def name(self) -> str:
        return "endstation_12021_devices"

    @property
    def description(self) -> str:
        return "12.0.2.1 endstation devices (packaged happi database)"
