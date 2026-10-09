"""Shutting the camera preview down must not outlive its own join budget.

A stalled Pi camera stream holds the worker thread inside a single blocking
OpenCV read, so ``CameraStreamWorker.stop()`` has to allow at least the camera's
own read deadline. When it does not, the worker is still alive after the join
times out and Qt aborts the process if the last reference to the QThread is
dropped. No camera and no network is touched here.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

import mainwindow

CameraStreamWorker = mainwindow.CameraStreamWorker


class CameraStreamWorkerStopTests(unittest.TestCase):
    def _stop_timeout(self, camera):
        worker = CameraStreamWorker(camera)
        self.addCleanup(worker.deleteLater)
        return worker.stop_timeout_ms()

    def test_join_budget_covers_the_camera_read_deadline(self):
        camera = Mock()
        camera.stream_read_timeout_ms = 2000
        self.assertGreaterEqual(
            self._stop_timeout(camera),
            2000 + CameraStreamWorker.STOP_TIMEOUT_MARGIN_MS,
        )

    def test_join_budget_grows_with_a_slower_read_deadline(self):
        camera = Mock()
        for read_timeout in (500, 2000, 8000):
            with self.subTest(read_timeout=read_timeout):
                camera.stream_read_timeout_ms = read_timeout
                self.assertEqual(
                    self._stop_timeout(camera),
                    read_timeout + CameraStreamWorker.STOP_TIMEOUT_MARGIN_MS,
                )

    def test_cameras_without_a_declared_deadline_use_the_default(self):
        camera = Mock(spec=[])  # no stream_read_timeout_ms attribute at all
        self.assertEqual(
            self._stop_timeout(camera),
            CameraStreamWorker.DEFAULT_STOP_TIMEOUT_MS,
        )

    def test_a_nonsense_deadline_falls_back_instead_of_raising(self):
        """stop() runs on every camera switch, so it must never raise."""
        camera = Mock()
        for bad in (Mock(), "soon", None, -5, 0, float("nan")):
            with self.subTest(deadline=repr(bad)):
                camera.stream_read_timeout_ms = bad
                self.assertEqual(
                    self._stop_timeout(camera),
                    CameraStreamWorker.DEFAULT_STOP_TIMEOUT_MS,
                )

    def test_a_real_pi_camera_declares_its_read_deadline(self):
        """The fix only works if PiCamera actually publishes its deadline.

        PiCamera is not instantiated here: its constructor opens a network
        stream. The attribute is set before that happens, so inspect the
        constructor's code object instead of reaching for the hardware.
        """
        from hardware import camera_pi

        names = camera_pi.PiCamera.__init__.__code__.co_names
        self.assertIn("stream_read_timeout_ms", names)
        self.assertIn("STREAM_READ_TIMEOUT_MS", names)
        self.assertGreater(camera_pi.STREAM_READ_TIMEOUT_MS, 0)


class StopTimeoutIsUsedByTheWorkerTests(unittest.TestCase):
    def test_stop_waits_for_the_declared_budget(self):
        camera = Mock()
        camera.stream_read_timeout_ms = 2000
        worker = CameraStreamWorker(camera)
        self.addCleanup(worker.deleteLater)

        seen = []
        worker.wait = lambda timeout: seen.append(timeout) or True

        self.assertTrue(worker.stop())
        self.assertEqual(
            seen, [2000 + CameraStreamWorker.STOP_TIMEOUT_MARGIN_MS]
        )
        self.assertFalse(worker.running)
        camera.stop_grabbing.assert_called_once()

    def test_stop_reports_whether_the_thread_actually_finished(self):
        camera = Mock()
        worker = CameraStreamWorker(camera)
        self.addCleanup(worker.deleteLater)

        worker.wait = lambda timeout: False
        self.assertFalse(worker.stop())
        worker.wait = lambda timeout: True
        self.assertTrue(worker.stop())

    def test_stop_survives_a_camera_that_refuses_to_stop(self):
        camera = Mock()
        camera.stop_grabbing.side_effect = RuntimeError("already closed")
        worker = CameraStreamWorker(camera)
        self.addCleanup(worker.deleteLater)
        worker.wait = lambda timeout: True

        self.assertTrue(worker.stop())


class AbandonedWorkerIsKeptAliveTests(unittest.TestCase):
    """A worker that outlives its join must stay referenced by the window.

    Qt calls std::terminate when a QThread is destroyed while still running, so
    dropping the last Python reference is a hard crash rather than a leak.
    """

    def setUp(self):
        self.settings_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.settings_dir.cleanup)
        from PySide6.QtCore import QSettings

        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(
            QSettings.Format.IniFormat,
            QSettings.Scope.UserScope,
            self.settings_dir.name,
        )
        from PySide6.QtWidgets import QApplication

        self.app = QApplication.instance() or QApplication([])

    def _window(self):
        stage = Mock()
        stage.is_connected.return_value = False
        from unittest.mock import patch

        with patch.object(mainwindow, "list_camera_devices", return_value=[]), \
                patch.object(mainwindow, "list_serial_devices", return_value=[]):
            window = mainwindow.DeviceControlMainWindow(stage)
        self.addCleanup(window.deleteLater)
        return window

    def test_a_finished_worker_is_discarded(self):
        window = self._window()
        worker = Mock()
        worker.stop.return_value = True
        window.camera_stream_worker = worker

        window.stop_camera_stream()

        self.assertIsNone(window.camera_stream_worker)
        self.assertEqual(window.abandoned_camera_stream_workers, [])

    def test_a_worker_that_did_not_finish_is_retained(self):
        window = self._window()
        worker = Mock()
        worker.stop.return_value = False
        window.camera_stream_worker = worker

        window.stop_camera_stream()

        self.assertIsNone(window.camera_stream_worker)
        self.assertEqual(window.abandoned_camera_stream_workers, [worker])


if __name__ == "__main__":
    unittest.main()
