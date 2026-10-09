# Open Micro-Manipulator GUI

**Open Micro-Manipulator GUI** is a small graphical user interface for controlling the [Open Micro-Manipulator](https://github.com/0x23/MicroManipulatorStepper), featuring a live microscope camera view for real-time feedback.
You  can find the Open Micro-Manipulator repository.
<br><br>

<div style="display: flex; gap: 10%;">
  <img src="images/gcode_runner.jpg" alt="G-Code runner interface" width="49%">
  <img src="images/grain_manipulation_1.jpg" alt="Grain manipulation example" width="49%">
</div>

## ✨New Version 0.1.3

The version incudes several general improvements, controlls for the tool output and changes to the g-code parser to support the **Fiber Immersion Micro 3D-Printing Method**.


## ⬇️ Installation

1. Clone the repository and navigate into the project directory.
2. Install the required Python dependencies: `pip install -r requirements.txt`
3. Run the application: `python source/main.py`

Make sure you are using a compatible Python version and that your hardware is properly connected before launching the GUI.

## 🔧 Device Selection

The GUI now lists available serial devices and cameras directly in the interface. Use the `Refresh`, `Connect`, and `Disconnect` controls to manage the micromanipulator and camera at runtime.

## Axis calibration

In the Advanced tab, the full-width axis selector controls both **Home Axis**
and **Calibrate Axis** in the row below, alongside the **Save** checkbox.
**All axes** is selected by default and calibrates all three joints in order.
The individual choices are X (Axis 1 / J0 / G28 A), Y (Axis 2 / J1 / G28 B),
and Z (Axis 3 / J2 / G28 C). They calibrate or home only the selected
motor/encoder pair; A, B and C are the respective homing command letters.
**Save** is checked by default and writes the new
calibration to the controller's persistent storage. Uncheck it to apply the
new calibration only in memory without replacing the saved calibration; the
plot dialog also shows whether saving was requested.
Hardware axis numbers are 1-based; firmware joint numbers are 0-based.

**Home Axis** homes all axes or only the selected joint. **Save** affects
calibration only, not homing.
The original Home button still homes all axes, regardless of this selection.
Both homing buttons keep the same realtime-control guard and controller error
handling. Stop realtime mouse control before homing.

## Jog and realtime motion rejection

Cartesian coordinate bounds in the GUI are not actuator travel limits. Use
firmware that validates calibrated joint ranges for both planned (`G0`/`G1`)
and realtime (`G24`) motion.

Jog buttons refresh the controller's last accepted target (`M50`) before each
step, and update their cached target only after an `OK` reply. Rejected moves
display the controller error. Each click sends the selected displacement once;
the GUI does not retry, subdivide, or replace a rejected move with a shorter
one. Jogging is blocked while realtime mouse control is active. Realtime control
stops and displays an error when a target is rejected, retaining the last
accepted pose rather than accumulating unreachable targets.
Restart it to move back into the usable workspace. Restart the GUI after updating
its Python source; an already-running instance will not pick up these changes.

The Home button checks the controller's reply. Failed homing or a refused servo
restart displays `Home Failed` and does not update the cached position as though
Home succeeded. Stop realtime control before homing. A physical stop search and
backoff can finish while the controller still refuses feedback restart.

`M50` is an accepted target, not proof that the motors reached it. Calibration
limits do not replace physical stops, verified geometry or encoder-fault handling.

With the dependency virtual environment activated, run the offline Qt regressions
from this GUI directory:

```bash
QT_QPA_PLATFORM=offscreen python tests/test_motion_rejection.py -v
QT_QPA_PLATFORM=offscreen python tests/test_axis_calibration.py -v
```

The tests mock stage and camera access and never open a hardware connection.

## 📷 Camera

The program can display a live camera feed using the open-cv image capturing framework. For best experience I recommend a camera capable of capturing 60 frames per second (also make sure you are not limited by the cameras shutter time).

## 3-Point Alignment

Computes a workspace transformation that aligns three points to the XY plane. This is useful for microscopy to get the sample plane to stay in focus in case of slight misalignments.
Usage: Set three wayoints spanning a triangle (points must not be on a line) in the XY plane and set the Z height (e.g. so that the sample is in focus at each point). Then press the '3-Point Alignment button'.

## 🧾 Running G-Code

The G-Code runner supports **simple absolute movement commands** of the form: `G0 X Y Z F`. All other commands are ignored.
You may press the 'Set Origin' button to set the current device loaction as zero position for running the G-Code.

> ⚠️ **Warning**  
> Running G-Code that exceeds the machine’s physical limits may cause rapid and uncontrolled movements. Use with caution.

### Scaling
A custom scaling directive can be added at the beginning of a G-Code file: `SCALE=0.123`.
This is useful when running G-Code generated by external software, such as 3D-printing slicers.
