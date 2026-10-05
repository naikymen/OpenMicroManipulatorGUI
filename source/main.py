# --------------------------------------------------------------------------------------
# Project: OpenMicroManipulator
# License: MIT (see LICENSE file for full description)
#          All text in here must be included in any redistribution.
# Author:  M. S. (diffraction limited)
# --------------------------------------------------------------------------------------

import os
import signal

# Disable scaling
os.environ['QT_SCALE_FACTOR'] = '1'
os.environ['QT_AUTO_SCREEN_SCALE_FACTOR'] = '0'
os.environ['GDK_SCALE'] = '1'
os.environ['GDK_DPI_SCALE'] = '1'

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from hardware.open_micro_stage_api import OpenMicroStageInterface
from mainwindow import DeviceControlMainWindow


def main():
    oms = OpenMicroStageInterface(show_communication=False, show_log_messages=True)
    app = QApplication()
    app.setOrganizationName("OpenMicroManipulator")
    app.setApplicationName("OpenMicroManipulatorGUI")
    gui = DeviceControlMainWindow(oms)
    gui.show()

    shutdown_requested = False

    def request_shutdown(signum, frame):
        nonlocal shutdown_requested
        shutdown_requested = True

    def process_interrupt():
        if shutdown_requested:
            interrupt_timer.stop()
            # Closing the windows invokes the main window's existing cleanup.
            app.closeAllWindows()

    previous_sigint_handler = signal.signal(signal.SIGINT, request_shutdown)
    # Periodically return control to Python so it can dispatch pending signals
    # even when the Qt event loop has no user input or camera frames.
    interrupt_timer = QTimer(app)
    interrupt_timer.timeout.connect(process_interrupt)
    interrupt_timer.start(100)
    try:
        app.exec()
    finally:
        interrupt_timer.stop()
        signal.signal(signal.SIGINT, previous_sigint_handler)
        oms.disconnect()

if __name__ == "__main__":
    main()
