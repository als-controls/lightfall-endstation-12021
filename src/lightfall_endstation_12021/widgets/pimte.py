"""PI-MTE camera controller plugin for the 12.0.2.1 endstation.

Builds on Lightfall's plan-based camera widget (live view, TV mode, dark frames,
acquisition settings) and adds:
- the generic temperature panel (sensor reading + setpoint),
- a PI-MTE panel: camera connection (Connect / Release, to share the camera with
  LightField), temperature lock status, trigger response, ADC speed, binning,
  readout time, and the "allow cooling" vacuum interlock.

Matches only :class:`~lightfall_endstation_12021.devices.PIMTEDetector` instances
(priority 160, above 7011's name-pattern PIMTE plugin at 150).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from lightfall.plugins.controller_plugin import ControllerPlugin
from PySide6.QtWidgets import QWidget

if TYPE_CHECKING:
    from lightfall.ui.models.device_tree import DeviceTreeItem

BINNINGS = (1, 2, 4, 8)


def _is_pimte(obj: Any) -> bool:
    from lightfall_endstation_12021.devices.pimte import PIMTEDetector

    return isinstance(obj, PIMTEDetector)


def _make_mte_panel():
    from lightfall.epics.widgets.ophyd_combobox import OphydComboBox
    from lightfall.epics.widgets.ophyd_label import OphydLabel
    from lightfall.utils.threads import QThreadFuture
    from PySide6.QtCore import Signal
    from PySide6.QtWidgets import (
        QCheckBox,
        QComboBox,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QMessageBox,
        QPushButton,
    )

    class MTEPanel(QGroupBox):
        """PI-MTE specific controls."""

        connection_changed = Signal(bool)

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__("PI-MTE", parent)
            self._device: Any = None
            self._thread: QThreadFuture | None = None
            self._updating = False

            grid = QGridLayout(self)
            grid.setSpacing(6)
            r = 0
            grid.addWidget(QLabel("Camera:"), r, 0)
            row = QHBoxLayout()
            self._conn_label = QLabel("---")
            self._conn_btn = QPushButton("Release")
            self._conn_btn.setToolTip(
                "Release closes the camera so LightField can use it; Connect takes it back.")
            self._conn_btn.clicked.connect(self._on_conn_clicked)
            row.addWidget(self._conn_label, 1)
            row.addWidget(self._conn_btn)
            grid.addLayout(row, r, 1)

            r += 1
            grid.addWidget(QLabel("Temp. lock:"), r, 0)
            self._lock_label = OphydLabel(show_units=False)
            grid.addWidget(self._lock_label, r, 1)

            r += 1
            grid.addWidget(QLabel("Trigger:"), r, 0)
            self._trigger_combo = OphydComboBox()
            grid.addWidget(self._trigger_combo, r, 1)

            r += 1
            grid.addWidget(QLabel("ADC speed:"), r, 0)
            self._adc_combo = QComboBox()
            self._adc_combo.currentIndexChanged.connect(self._on_adc_changed)
            grid.addWidget(self._adc_combo, r, 1)

            r += 1
            grid.addWidget(QLabel("Binning:"), r, 0)
            self._bin_combo = QComboBox()
            for b in BINNINGS:
                self._bin_combo.addItem(f"{b} x {b}", b)
            self._bin_combo.currentIndexChanged.connect(self._on_bin_changed)
            grid.addWidget(self._bin_combo, r, 1)

            r += 1
            grid.addWidget(QLabel("Image:"), r, 0)
            self._size_label = QLabel("---")
            grid.addWidget(self._size_label, r, 1)

            r += 1
            grid.addWidget(QLabel("Readout:"), r, 0)
            self._readout_label = OphydLabel(precision=2)
            grid.addWidget(self._readout_label, r, 1)

            r += 1
            self._cool_check = QCheckBox("Allow cooling (chamber under vacuum)")
            self._cool_check.setToolTip(
                "Setpoints below 20 °C are refused unless this is checked. Only cool the "
                "in-vacuum MTE when the chamber is under vacuum.")
            self._cool_check.toggled.connect(self._on_cool_toggled)
            grid.addWidget(self._cool_check, r, 0, 1, 2)

        # ---- binding
        def set_device(self, device: Any) -> None:
            self._device = device
            self._bind()

        def _bind(self) -> None:
            dev = self._device
            live = dev is not None and dev.camera_connected
            for w in (self._lock_label, self._trigger_combo, self._readout_label):
                w.signal = None
            if live:
                self._lock_label.signal = dev.cam.temperature_status
                self._trigger_combo.signal = dev.cam.trigger_response
                self._readout_label.signal = dev.cam.readout_time
            self._refresh_static()
            for w in (self._adc_combo, self._bin_combo, self._cool_check):
                w.setEnabled(live)
            self._update_conn_state()

        def _refresh_static(self) -> None:
            dev = self._device
            self._updating = True
            try:
                self._adc_combo.clear()
                if dev is None or not dev.camera_connected:
                    self._size_label.setText("---")
                    return
                drv = dev.driver
                speeds = drv.collection("ADC Speed") or [drv.get("ADC Speed")]
                current = drv.get("ADC Speed")
                for s in sorted(speeds, reverse=True):
                    label = f"{s:g} MHz" if s >= 1 else f"{s * 1000:g} kHz"
                    self._adc_combo.addItem(label, s)
                    if abs(s - current) < 1e-9:
                        self._adc_combo.setCurrentIndex(self._adc_combo.count() - 1)
                roi = drv.get_roi()
                idx = self._bin_combo.findData(roi["x_binning"])
                self._bin_combo.setCurrentIndex(max(idx, 0))
                h, w = drv.image_shape()
                self._size_label.setText(f"{w} x {h} px")
                self._cool_check.setChecked(dev.allow_cooling)
                dev.cam.readout_time.get()
            except Exception as exc:  # noqa: BLE001
                self._size_label.setText(f"error: {exc}")
            finally:
                self._updating = False

        def _update_conn_state(self) -> None:
            dev = self._device
            busy = self._thread is not None and self._thread.running
            if dev is None:
                self._conn_label.setText("No device")
                self._conn_btn.setEnabled(False)
                return
            self._conn_btn.setEnabled(not busy)
            if dev.camera_connected:
                drv = dev.driver
                self._conn_label.setText(f"Connected (s/n {drv.serial})")
                self._conn_btn.setText("Release")
            else:
                self._conn_label.setText("Released" if dev.released else "Not connected")
                self._conn_btn.setText("Connect")

        # ---- actions
        def _on_conn_clicked(self) -> None:
            dev = self._device
            if dev is None:
                return
            if dev.camera_connected:
                try:
                    dev.release_camera()
                except Exception as exc:  # noqa: BLE001
                    QMessageBox.warning(self, "Release failed", str(exc))
                self._bind()
                self.connection_changed.emit(False)
                return
            self.connect_async()

        def connect_async(self) -> None:
            dev = self._device
            if dev is None or (self._thread is not None and self._thread.running):
                return
            self._conn_label.setText("Connecting...")
            self._conn_btn.setEnabled(False)

            def done(_):
                self._thread = None
                self._bind()
                self.connection_changed.emit(True)

            def failed(err):
                self._thread = None
                self._bind()
                self.connection_changed.emit(False)
                self._conn_label.setText(f"Connect failed: {err}")
                self._conn_label.setToolTip(str(err))

            self._thread = QThreadFuture(dev.connect_camera, callback_slot=done,
                                         except_slot=failed, name="pimte-connect")
            self._thread.start()

        def _on_adc_changed(self, index: int) -> None:
            if self._updating or self._device is None or index < 0:
                return
            try:
                self._device.cam.adc_speed.put(self._adc_combo.itemData(index))
                self._device.cam.readout_time.get()
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "ADC speed", str(exc))
                self._refresh_static()

        def _on_bin_changed(self, index: int) -> None:
            if self._updating or self._device is None or index < 0:
                return
            b = self._bin_combo.itemData(index)
            try:
                self._device.set_roi(0, 0, None, None, b, b)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "Binning", str(exc))
            self._refresh_static()

        def _on_cool_toggled(self, checked: bool) -> None:
            if self._updating or self._device is None:
                return
            if checked:
                ok = QMessageBox.question(
                    self, "Allow cooling",
                    "Is the MTE chamber under vacuum?\n\nCooling the CCD at atmosphere can "
                    "cause condensation damage.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                if ok != QMessageBox.StandardButton.Yes:
                    self._updating = True
                    self._cool_check.setChecked(False)
                    self._updating = False
                    return
            self._device.allow_cooling = checked

        # ---- API / cleanup
        def get_introspection_data(self) -> dict[str, Any]:
            dev = self._device
            return {
                "widget_type": "MTEPanel",
                "connected": bool(dev and dev.camera_connected),
                "released": bool(dev and dev.released),
                "binning": self._bin_combo.currentData(),
                "adc_speed": self._adc_combo.currentData(),
                "image_size": self._size_label.text(),
                "allow_cooling": bool(dev and dev.allow_cooling),
                "available_actions": [
                    {"name": "release_camera", "description": "Close the camera for LightField"},
                    {"name": "connect_camera", "description": "Re-open the camera"},
                ],
            }

        def close(self) -> None:
            for w in (self._lock_label, self._trigger_combo, self._readout_label):
                w.signal = None
            super().close()

    return MTEPanel


class _PIMTECameraWidget:
    """Factory: subclass Lightfall's camera widget at runtime (keeps imports lazy)."""

    def __new__(cls, parent: QWidget | None = None):
        from lightfall.ui.widgets.camera.panels.temperature import TemperaturePanel
        from lightfall.ui.widgets.camera.plan_based import PlanBasedCameraControlWidget

        MTEPanel = _make_mte_panel()

        class _Widget(PlanBasedCameraControlWidget):
            """PI-MTE camera widget: base camera controls + temperature + MTE panel."""

            display_name: ClassVar[str] = "PI-MTE Camera"
            priority: ClassVar[int] = 160
            supported_tags: ClassVar[set[str]] = {"pimte"}
            supported_classes: ClassVar[set[str]] = {"PIMTEDetector"}

            def __init__(self, parent: QWidget | None = None) -> None:
                self._temp_panel: TemperaturePanel | None = None
                self._mte_panel: MTEPanel | None = None
                super().__init__(parent)

            def _create_device_panels(self):
                panels = super()._create_device_panels()
                self._temp_panel = TemperaturePanel()
                self._mte_panel = MTEPanel()
                self._mte_panel.connection_changed.connect(self._on_camera_connection_changed)
                panels.extend([self._temp_panel, self._mte_panel])
                return panels

            def set_items(self, items: list) -> None:
                dev = items[0].ophyd_obj if items and len(items) == 1 else None
                if _is_pimte(dev) and not dev.camera_connected and not dev.released:
                    # Open the camera off the UI thread; connection_changed rebinds.
                    self._items = items
                    self._mte_panel.set_device(dev)
                    self._mte_panel.connect_async()
                    return
                self._bind_items(items)

            def _bind_items(self, items: list) -> None:
                super().set_items(items)
                dev = self._device if _is_pimte(self._device) else None
                if self._mte_panel is not None:
                    self._mte_panel.set_device(dev)
                if self._temp_panel is not None:
                    self._temp_panel.set_device(dev if dev is not None and dev.camera_connected else None)

            def _on_camera_connection_changed(self, connected: bool) -> None:
                # Rebind the base controls/temperature panel to the new connection state.
                self._bind_items(getattr(self, "_items", []))

            def get_introspection_data(self) -> dict[str, Any]:
                data = super().get_introspection_data()
                if self._temp_panel is not None:
                    data["temperature"] = self._temp_panel.get_introspection_data()
                if self._mte_panel is not None:
                    data["pimte"] = self._mte_panel.get_introspection_data()
                return data

            def closeEvent(self, event) -> None:
                if self._temp_panel is not None:
                    self._temp_panel.close()
                if self._mte_panel is not None:
                    self._mte_panel.close()
                super().closeEvent(event)

        return _Widget(parent)


class PIMTEControllerPlugin(ControllerPlugin):
    """Controller plugin for the PICam-driven PI-MTE detector."""

    @property
    def name(self) -> str:
        return "pimte_picam_camera"

    @property
    def display_name(self) -> str:
        return "PI-MTE Camera"

    @property
    def description(self) -> str:
        return "PI-MTE camera (PICam) control: live view, temperature, connection, binning"

    def can_control(self, items: list[DeviceTreeItem]) -> int | None:
        if len(items) != 1:
            return None
        item = items[0]
        if _is_pimte(item.ophyd_obj):
            return 160
        cls = (item.device_info.device_class if item.device_info else "") or ""
        if cls.endswith("PIMTEDetector") and "lightfall_endstation_12021" in cls:
            return 160
        return None

    def create_widget(self, parent: QWidget | None = None) -> QWidget:
        return _PIMTECameraWidget(parent)
