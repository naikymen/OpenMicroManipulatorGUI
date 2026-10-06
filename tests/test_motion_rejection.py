"""Offline Qt/API regressions. No serial port or camera is opened."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

import numpy as np
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
import mainwindow
from gui_components.realtime_controller_widget import UpdateWorker
from hardware.open_micro_stage_api import OpenMicroStageInterface, SerialInterface

Status = SerialInterface.ReplyStatus


class FakeStage:
    def __init__(self):
        self.connected = False
        self.position = [1.0, 2.0, 3.0]
        self.status = Status.OK
        self.last_motion_error = "invalid or out-of-range Cartesian path"
        self.last_home_error = "homing or servo restart failed; inspect HOME GUARD logs"
        self.moves = []

    def is_connected(self):
        return self.connected

    def read_current_position(self, _):
        return list(self.position)

    def move_to(self, x, y, z, feedrate):
        self.moves.append((x, y, z, feedrate))
        if self.status == Status.OK:
            self.position = [x, y, z]
        return self.status

    def home(self):
        return self.status


class MotionRejectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.settings_dir = tempfile.TemporaryDirectory()
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, cls.settings_dir.name)

    @classmethod
    def tearDownClass(cls):
        cls.settings_dir.cleanup()

    def setUp(self):
        self.stage = FakeStage()
        # Patch discovery before constructing the actual window, not hardware.
        with patch.object(mainwindow, "list_camera_devices", return_value=[]), \
             patch.object(mainwindow, "list_serial_devices", return_value=[]):
            self.window = mainwindow.DeviceControlMainWindow(self.stage)
        self.stage.connected = True
        self.window.update_connection_state()
        self.warnings = []
        self.warning_patch = patch.object(mainwindow.QMessageBox, "warning",
                                         side_effect=lambda *args: self.warnings.append(args))
        self.warning_patch.start()

    def tearDown(self):
        self.warning_patch.stop()
        self.window.deleteLater()
        self.app.processEvents()

    def test_buttons_send_the_correct_axis_and_refresh_stale_cache(self):
        for name, axis, direction in [("btn_x_plus", 0, 1), ("btn_x_minus", 0, -1),
                                      ("btn_y_plus", 1, 1), ("btn_y_minus", 1, -1),
                                      ("btn_z_plus", 2, 1), ("btn_z_minus", 2, -1)]:
            self.stage.position = [1.0, 2.0, 3.0]
            self.window.current_pos = [-9.0, -9.0, -9.0]
            getattr(self.window, name).click()
            expected = [1.0, 2.0, 3.0]
            expected[axis] += direction*0.1
            self.assertEqual(self.stage.moves[-1], (*expected, 5.0))
            self.assertEqual(self.window.current_pos, expected)

    def test_rejection_never_advances_cache(self):
        for status in (Status.ERROR, Status.BUSY, Status.TIMEOUT):
            self.stage.status = status
            moves_before = len(self.stage.moves)
            self.window.move_axis(1, 1)
            self.assertEqual(len(self.stage.moves), moves_before + 1)
            self.assertEqual(self.stage.moves[-1], (1.0, 2.1, 3.0, 5.0))
            self.assertEqual(self.window.current_pos, [1.0, 2.0, 3.0])
            self.assertIn("out-of-range", self.warnings[-1][2])

    def test_failed_home_is_reported_and_does_not_refresh_cache(self):
        self.window.current_pos = [-1.0, -2.0, -3.0]
        for status in (Status.ERROR, Status.BUSY, Status.TIMEOUT):
            self.stage.status = status
            with patch.object(self.stage, "read_current_position", side_effect=AssertionError("Home failed")):
                self.window.home()
            self.assertEqual(self.window.current_pos, [-1.0, -2.0, -3.0])
            self.assertEqual(self.warnings[-1][1], "Home Failed")
            self.assertIn("HOME GUARD", self.warnings[-1][2])

    def test_successful_home_refreshes_cache(self):
        self.window.current_pos = [-1.0, -2.0, -3.0]
        self.window.home()
        self.assertEqual(self.window.current_pos, self.stage.position)
        self.assertEqual(self.warnings, [])

    def test_missing_position_or_active_mouse_prevents_jog(self):
        self.stage.position = [None, None, None]
        self.window.move_axis(1, 1)
        self.assertEqual(self.stage.moves, [])
        self.stage.position = [1.0, 2.0, 3.0]
        with patch.object(self.window.realtime_control_widget, "is_running", return_value=True):
            self.window.move_axis(1, 1)
        self.assertEqual(self.stage.moves, [])

    def test_realtime_retains_only_accepted_pose_and_stops_on_error(self):
        self.stage.position = [0.0, 0.0, 0.0]
        commands = []
        def set_pose(*pose):
            commands.append(pose)
            return Status.OK if len(commands) == 1 else Status.ERROR
        self.stage.set_pose = set_pose
        worker = UpdateWorker(self.stage, [0, 0, 0], [1, 1, 1], lowpass_strength=0.5)
        worker.relative_device_pos[1] = 0.1
        errors = []
        worker.motion_failed.connect(errors.append)
        worker.run()
        self.assertFalse(worker.running)
        self.assertEqual(len(commands), 2)
        np.testing.assert_allclose(worker.get_current_pose(), commands[0])
        self.assertNotEqual(commands[0][1], commands[1][1])
        self.assertEqual(errors, [self.stage.last_motion_error])

    def test_api_preserves_controller_error_for_both_motion_paths(self):
        interface = OpenMicroStageInterface(show_communication=False)
        class FakeSerial:
            _response_error_msg = "invalid or out-of-range Cartesian pose"
            def send_command(self, command, *_, **__):
                self.command = command
                return Status.ERROR, ""
        interface.serial = FakeSerial()
        self.assertEqual(interface.move_to(1, 2, 3, 5), Status.ERROR)
        self.assertIn("out-of-range", interface.last_motion_error)
        self.assertTrue(interface.serial.command.startswith("G0 "))
        self.assertEqual(interface.set_pose(1, 2, 3), Status.ERROR)
        self.assertIn("out-of-range", interface.last_motion_error)
        self.assertTrue(interface.serial.command.startswith("G24 "))
        self.assertEqual(interface.home(), Status.ERROR)
        self.assertIn("out-of-range", interface.last_home_error)
        self.assertTrue(interface.serial.command.startswith("G28 "))


if __name__ == "__main__":
    unittest.main()
