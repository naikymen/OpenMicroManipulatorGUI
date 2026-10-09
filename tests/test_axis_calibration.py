"""Offline calibration controls: no serial port or camera is opened."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QLabel
import mainwindow

CalibrationPlotDialog = mainwindow.CalibrationPlotDialog


class AxisCalibrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.settings_dir = tempfile.TemporaryDirectory()
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                          cls.settings_dir.name)

    @classmethod
    def tearDownClass(cls):
        cls.settings_dir.cleanup()

    def setUp(self):
        self.stage = Mock()
        self.stage.is_connected.return_value = False
        self.stage.calibrate_joint.return_value = (
            mainwindow.SerialInterface.ReplyStatus.OK, [[0.1], [0.2], [100]]
        )
        self.stage.home.return_value = mainwindow.SerialInterface.ReplyStatus.OK
        self.stage.read_current_position.return_value = [1.0, 2.0, 3.0]
        with patch.object(mainwindow, "list_camera_devices", return_value=[]), \
             patch.object(mainwindow, "list_serial_devices", return_value=[]):
            self.window = mainwindow.DeviceControlMainWindow(self.stage)
        self.plot_patch = patch.object(mainwindow, "CalibrationPlotDialog")
        self.plot = self.plot_patch.start()
        self.warning_patch = patch.object(mainwindow.QMessageBox, "warning")
        self.warning = self.warning_patch.start()
        self.stage.is_connected.return_value = True
        self.window.update_connection_state()

    def tearDown(self):
        self.plot_patch.stop()
        self.warning_patch.stop()
        self.window.deleteLater()
        self.app.processEvents()

    def test_default_button_calibrates_all_joints_and_saves(self):
        self.assertEqual(self.window.axis_selector_combo.currentIndex(), 0)
        self.assertEqual(self.window.axis_selector_combo.currentText(),
                         "All axes (1–3 / J0–J2 / G28 A B C)")
        self.assertTrue(self.window.save_calibration_checkbox.isChecked())
        self.window.btn_calibrate_axis.click()
        self.assertEqual(self.stage.calibrate_joint.call_args_list,
                         [call(j, save_result=True) for j in range(3)])
        self.assertEqual([j for j, _ in self.plot.call_args.args[0]], [0, 1, 2])
        self.assertEqual(self.plot.call_args.kwargs, {"save_result": True})
        self.plot.return_value.show.assert_called_once()

    def test_each_axis_calibrates_only_its_joint_and_saves(self):
        for selection, axis in enumerate(("X", "Y", "Z"), start=1):
            with self.subTest(axis=axis):
                self.stage.calibrate_joint.reset_mock()
                self.window.axis_selector_combo.setCurrentIndex(selection)
                label = self.window.axis_selector_combo.currentText()
                letter = "ABC"[selection - 1]
                self.assertEqual(label, f"{axis} (Axis {selection} / J{selection - 1} / G28 {letter})")
                self.window.btn_calibrate_axis.click()
                self.stage.calibrate_joint.assert_called_once_with(selection - 1, save_result=True)
                self.assertEqual([j for j, _ in self.plot.call_args.args[0]], [selection - 1])

    def test_checkbox_controls_saving_for_all_and_individual_axes(self):
        for selection in range(4):
            for save_result in (False, True):
                with self.subTest(selection=selection, save_result=save_result):
                    self.stage.calibrate_joint.reset_mock()
                    self.window.axis_selector_combo.setCurrentIndex(selection)
                    self.window.save_calibration_checkbox.setChecked(save_result)
                    self.window.btn_calibrate_axis.click()
                    indices = range(3) if selection == 0 else (selection - 1,)
                    self.assertEqual(self.stage.calibrate_joint.call_args_list,
                                     [call(j, save_result=save_result) for j in indices])
                    self.assertEqual(self.plot.call_args.kwargs, {"save_result": save_result})

    def test_plot_dialog_describes_the_persistence_choice(self):
        for save_result, expected in (
            (True, "Calibration saved to device"),
            (False, "Calibration not saved to persistent storage"),
        ):
            with self.subTest(save_result=save_result):
                dialog = CalibrationPlotDialog([(1, [[0.1], [0.2], [100]])],
                                               save_result=save_result)
                self.assertIn(expected, [label.text() for label in dialog.findChildren(QLabel)])
                dialog.deleteLater()

    def test_explicit_unsaved_calibration_still_supported(self):
        self.window.axis_selector_combo.setCurrentIndex(2)
        self.window.run_axis_calibration(save_result=False)
        self.stage.calibrate_joint.assert_called_once_with(1, save_result=False)

    def test_disconnected_controls_do_not_calibrate(self):
        self.stage.is_connected.return_value = False
        self.window.update_connection_state()
        self.assertFalse(self.window.axis_selector_combo.isEnabled())
        self.assertFalse(self.window.btn_calibrate_axis.isEnabled())
        self.assertFalse(self.window.save_calibration_checkbox.isEnabled())
        self.assertFalse(self.window.btn_home_axis.isEnabled())
        self.window.btn_calibrate_axis.click()
        self.window.btn_home_axis.click()
        self.stage.calibrate_joint.assert_not_called()
        self.stage.home.assert_not_called()

    def test_default_homing_button_homes_all_axes(self):
        self.assertEqual(self.window.axis_selector_combo.currentIndex(), 0)
        self.window.btn_home_axis.click()
        self.stage.home.assert_called_once_with()
        self.assertEqual(self.window.current_pos, [1.0, 2.0, 3.0])

    def test_each_axis_homes_only_its_joint(self):
        for selection, axis in enumerate(("X", "Y", "Z"), start=1):
            with self.subTest(axis=axis):
                self.stage.home.reset_mock()
                self.window.axis_selector_combo.setCurrentIndex(selection)
                letter = "ABC"[selection - 1]
                self.assertEqual(self.window.axis_selector_combo.currentText(),
                                 f"{axis} (Axis {selection} / J{selection - 1} / G28 {letter})")
                self.window.btn_home_axis.click()
                self.stage.home.assert_called_once_with(axis_list=(selection - 1,))
                self.assertEqual(self.window.current_pos, [1.0, 2.0, 3.0])
        self.stage.calibrate_joint.assert_not_called()

    def test_both_buttons_use_the_shared_selection(self):
        for selection in range(4):
            with self.subTest(selection=selection):
                self.window.axis_selector_combo.setCurrentIndex(selection)
                self.stage.home.reset_mock()
                self.stage.calibrate_joint.reset_mock()
                self.window.btn_home_axis.click()
                self.window.btn_calibrate_axis.click()
                indices = (0, 1, 2) if selection == 0 else (selection - 1,)
                self.assertEqual(self.stage.calibrate_joint.call_args_list,
                                 [call(j, save_result=True) for j in indices])
                if selection == 0:
                    self.stage.home.assert_called_once_with()
                else:
                    self.stage.home.assert_called_once_with(axis_list=indices)

    def test_original_home_button_ignores_axis_selection(self):
        self.window.axis_selector_combo.setCurrentIndex(2)
        self.window.btn_home.click()
        self.stage.home.assert_called_once_with()

    def test_individual_homing_is_blocked_during_realtime_control(self):
        self.window.axis_selector_combo.setCurrentIndex(2)
        with patch.object(self.window.realtime_control_widget, "is_running", return_value=True):
            self.window.btn_home_axis.click()
        self.stage.home.assert_not_called()
        self.assertEqual(self.warning.call_args.args[1], "Realtime Control Active")

    def test_failed_individual_homing_does_not_refresh_position(self):
        self.window.axis_selector_combo.setCurrentIndex(2)
        self.window.current_pos = [-1.0, -2.0, -3.0]
        for status in (mainwindow.SerialInterface.ReplyStatus.ERROR,
                       mainwindow.SerialInterface.ReplyStatus.BUSY,
                       mainwindow.SerialInterface.ReplyStatus.TIMEOUT):
            with self.subTest(status=status):
                self.stage.home.return_value = status
                self.stage.home.reset_mock()
                self.window.btn_home_axis.click()
                self.stage.home.assert_called_once_with(axis_list=(1,))
                self.stage.read_current_position.assert_not_called()
                self.assertEqual(self.window.current_pos, [-1.0, -2.0, -3.0])
                self.assertEqual(self.warning.call_args.args[1], "Home Failed")

    def test_selector_fills_first_row_with_actions_in_order_below(self):
        self.window.show()
        self.window.main_tabs.setCurrentWidget(self.window.AdvancedTab)
        self.app.processEvents()
        combo = self.window.axis_selector_combo
        button = self.window.btn_calibrate_axis
        home_button = self.window.btn_home_axis
        checkbox = self.window.save_calibration_checkbox
        row = self.window.axis_actions_layout.geometry()
        self.assertEqual(combo.width(), row.width())
        self.assertEqual(combo.height(), 40)
        self.assertEqual(button.height(), 40)
        self.assertEqual(home_button.height(), 40)
        self.assertEqual(combo.x(), row.x())
        self.assertEqual(combo.geometry().right(), row.right())
        self.assertGreater(home_button.y(), combo.geometry().bottom())
        self.assertEqual(home_button.y(), button.y())
        self.assertEqual(checkbox.geometry().center().y(), button.geometry().center().y())
        self.assertLessEqual(abs(home_button.width() - button.width()), 1)
        self.assertEqual(home_button.x(), row.x())
        self.assertGreater(button.x(), home_button.geometry().right())
        self.assertGreater(checkbox.x(), button.geometry().right())
        self.assertEqual(checkbox.geometry().right(), row.right())
        self.assertEqual([row_item.widget() for row_item in
                          (self.window.axis_actions_layout.itemAt(i) for i in range(3))],
                         [home_button, button, checkbox])

    def test_api_uses_joint_homing_words_and_optional_save_flag(self):
        interface = mainwindow.OpenMicroStageInterface(show_communication=False)
        interface.serial = Mock()
        interface.serial._response_error_msg = ""
        interface.serial.send_command.return_value = (mainwindow.SerialInterface.ReplyStatus.OK, "")
        for joint_index, word in enumerate(("A", "B", "C")):
            with self.subTest(joint_index=joint_index):
                interface.home(axis_list=(joint_index,))
                interface.serial.send_command.assert_called_with(f"G28 {word}\n", 30)
                for save_result in (False, True):
                    interface.calibrate_joint(joint_index, save_result=save_result)
                    expected = f"M56 J{joint_index} P" + (" S" if save_result else "")
                    interface.serial.send_command.assert_called_with(expected, 30)


if __name__ == "__main__":
    unittest.main()
