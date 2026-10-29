"""Plugin manifest for 12.0.2.1 endstation.

Defines all Lightfall plugins provided by this package:
- Device backend plugin shipping the endstation happi database (PI-MTE camera, ...)
"""

from lightfall.plugins.manifest import PluginEntry, PluginManifest

manifest = PluginManifest(
    name="lightfall-endstation-12021",
    version="0.1.0",
    description="Lightfall plugins for ALS Beamline 12.0.2.1 endstation",
    plugins=[
        # Device backend: packaged happi database (devices/devices.json)
        PluginEntry(
            type_name="device_backend",
            name="endstation_12021_devices",
            import_path="lightfall_endstation_12021.devices.backend:Endstation12021Devices",
        ),
    ],
)
