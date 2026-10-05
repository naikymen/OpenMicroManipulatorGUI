# Changelog

## Close the GUI cleanly on terminal Ctrl+C

- Handle SIGINT as a shutdown request and use a 100 ms Qt timer to dispatch
  Python signals even while the application is idle.
- Close the application windows through the existing cleanup path, disconnect
  the serial interface on exit, and restore the previous signal handler.
- This makes terminal Ctrl+C responsive without interrupting window cleanup.
  Verified with a real SIGINT in an offscreen Qt run with mocked hardware.
