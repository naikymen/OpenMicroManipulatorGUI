# Changelog

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
