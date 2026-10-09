# Changelog

## Add the Raspberry Pi HQ camera as a selectable camera

- Add `source/hardware/camera_pi.py`, an `AbstractCamera` implementation that
  presents the Pi's MJPEG preview as a normal camera, so the HQ camera behind a
  40x objective appears in the existing camera dropdown, streams into the
  existing worker, and answers the existing exposure, gain and white balance
  controls without changing the measurement, calibration or motion paths.
- Discover the Pi by probing the address used last, the built-in defaults, and
  any address typed into the camera box, accepting `host`, `host:port`,
  `http://host:port` and mDNS names alike. Skip a network scan on purpose: a
  camera that does not answer simply does not appear in the list, and a typed
  address that fails produces a dialog naming the exact address instead of an
  empty dropdown.
- Map the GUI's exposure, gain and white balance controls onto libcamera
  semantics rather than passing values through: a non-positive value re-enables
  automatic control instead of forcing an invalid setting, gain is converted
  from decibels to a linear factor, and requested values are clamped into the
  range the sensor reports.
- Report the real problem when the preview cannot be reached. Pass explicit
  open and read timeouts to `cv2.VideoCapture`, because OpenCV otherwise blocks
  for its internal 30 seconds and froze the GUI while switching cameras; an
  unreachable address now fails in 3 seconds and a refused connection in 0.02
  seconds. Note that these two properties are honoured only as constructor
  arguments, since `cap.set()` returns `False` and silently has no effect.
- Recover control ranges when the daemon was still starting during the initial
  probe, so the sliders are not left disabled for the session, and widen a
  degenerate range instead of reporting a range no value can satisfy.
- Align the client's default port with the service. The client previously
  defaulted to 8080 while the service, its CLI and its systemd unit all used
  8000, so a user following the deployment instructions would start a service
  the GUI could never find.
- Add 59 offline regressions in `tests/test_camera_pi.py` covering address
  parsing, probing, discovery, frame decoding, streaming, disconnections,
  stream timeouts, controls, still capture and dark-image capture. The suite
  serves genuine JPEG bytes from a real `ThreadingHTTPServer` on a loopback
  port so actual OpenCV decoding is exercised, and asserts the colour of a
  decoded frame against a known source value so a channel swap cannot pass.
- Size the preview worker's join budget from the camera's own read deadline
  instead of a fixed 1500 ms, because the explicit OpenCV read timeout above
  lets a stalled stream hold that thread for up to 2 seconds. Keep hold of a
  worker that outlives its join rather than dropping the last reference, since
  Qt aborts the process when a running `QThread` is destroyed. Add 10 offline
  regressions, verified to fail against the previous fixed budget.

## Disable motors from the main controls

- Add Disable Motors next to Set Origin, using the existing motor-disable API
  (`M18`) without changing the origin, calibration or firmware.
- Stop realtime control and waypoint playback and request G-code playback to
  stop before disabling, so the GUI stops producing new motion commands.
  Disable the button when disconnected and report rejected, timed-out or
  failed requests instead of assuming power was removed.
- Document the loss of holding torque and that this serial control is not an
  emergency stop. Add seven offline button, failure, layout and API regressions.

## Individual axis homing and calibration controls

- Add a full-width All/X/Y/Z selector above Home Axis, Calibrate Axis and Save.
  Both actions use the same selection, with labels showing hardware axes 1–3,
  firmware joints J0–J2 and homing command words A/B/C to identify the intended
  motor/encoder pair. Keep All axes as the default and the original Home
  button's all-axis behavior.
- Add a checked-by-default Save checkbox to preserve calibration persistence
  by default while permitting temporary in-memory calibrations. Prevent the
  button's Qt checked flag from overriding the saving choice and reflect the
  chosen persistence mode in the plot dialog.
- Preserve homing's realtime-control guard and controller-error handling.
  Include documentation and 14 offline Qt/API regressions for shared selection,
  defaults, persistence, individual homing, rejection and the requested layout.

## Allow the complete homing sequence to finish

- Increase the G28 reply timeout from 10 to 30 seconds to accommodate the
  end-stop search, measured backoff, guarded feedback handover and final move
  into the usable range. A successful full sequence should not be reported
  as timed out merely because it takes longer than the previous allowance.
- Preserve command selection and controller-error handling; this changes only
  how long the GUI API waits for the reply, not firmware homing behavior.

## Report unsuccessful Home commands

- Preserve Home's controller error and show `Home Failed` when stop detection
  or servo restart fails, rather than updating the GUI cache as though all
  joints were ready.
- Block Home during active realtime control and warn if a successful Home's
  target cannot be read. Add offline successful/failed Home regressions.
- This changes error handling only; it does not weaken firmware guards or
  automatically retry homing/calibration.

## Handle rejected jog and realtime targets

- Refresh each jog's starting target from the controller and update the GUI
  cache only on an accepted move, preventing stale or rejected targets from
  accumulating after a limit is reached.
- Show controller motion errors, stop realtime control on rejection, and block
  jog buttons while realtime control is active so command sources do not compete.
- Retain the default step sizes and feedrates; actuator limits are enforced by
  the matching firmware, not by the GUI's nominal Cartesian box.
- Send the selected jog exactly once. Do not retry, subdivide, or silently
  shorten a controller-rejected displacement.
- Add seven offline Qt/API regressions with mocked hardware, including correct
  Y-button wiring, exact single-request rejection handling, Home errors and
  realtime accepted-pose retention.

## Close the GUI cleanly on terminal Ctrl+C

- Handle SIGINT as a shutdown request and use a 100 ms Qt timer to dispatch
  Python signals even while the application is idle.
- Close the application windows through the existing cleanup path, disconnect
  the serial interface on exit, and restore the previous signal handler.
- This makes terminal Ctrl+C responsive without interrupting window cleanup.
  Verified with a real SIGINT in an offscreen Qt run with mocked hardware.
