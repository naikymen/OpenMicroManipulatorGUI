# --------------------------------------------------------------------------------------
# Project: OpenMicroManipulator
# License: MIT (see LICENSE file for full description)
#          All text in here must be included in any redistribution.
# Author:  M. S. (diffraction limited)
# --------------------------------------------------------------------------------------

"""Camera backed by the ``PiCameraService`` daemon running on a Raspberry Pi.

The Pi owns the sensor and serves two things over plain HTTP: an MJPEG preview
stream that this class consumes with OpenCV, and a small JSON control API used
to set exposure, gain and white balance.

The preview is meant for *aiming* the manipulator, not for measuring. It is
lossy MJPEG at a reduced resolution. When a real measurement is needed the
service offers ``POST /capture?raw=1``, which writes a full-resolution JPEG and
a raw DNG to the Pi and returns the file names -- see ``PiCamera.capture_still``.

Addressing
----------
The service is reached either through a USB Ethernet gadget (the Pi appears as
a network interface, no router involved) or over a normal network. Because the
address is not fixed in either case, devices are addressed by an editable
``host[:port]`` string, and :meth:`PiCamera.enumerate_devices` is best-effort:
it probes the configured/last-known hosts quickly and falls back to letting the
user type an address. Manual entry is the primary path, not a fallback.
"""

import json
import time
import urllib.parse
import urllib.request

import cv2
import numpy as np

from hardware.abstract_camera import AbstractCamera

DEFAULT_PI_CAMERA_PORT = 8000
# Addresses probed on every camera refresh, cheapest first. The mDNS names cover
# the zero-configuration case; the literal address is the reference build's Pi
# and should be replaced with your own. The GUI also remembers the last address
# it successfully connected to, which is the primary discovery path in practice
# because the service's mDNS advertisement is optional.
DEFAULT_PI_CAMERA_HOSTS = ("raspberrypi.local", "picam.local", "192.168.1.39")

# Opening the stream is slow enough that the GUI must not block on a dead host.
PROBE_TIMEOUT_S = 0.4
API_TIMEOUT_S = 2.0
# A still capture stops the preview, reconfigures the sensor, integrates for the
# requested exposure and writes ~19 MB of DNG, so it needs a much longer budget.
CAPTURE_TIMEOUT_S = 120.0

# OpenCV's FFMPEG backend applies its own 30 second deadline when it cannot
# reach the stream, which would freeze the GUI while a camera is switched on.
# These must be passed to the VideoCapture constructor: cap.set() silently
# refuses to configure an unopened capture, so setting them afterwards does
# nothing and the 30 second stall comes back.
STREAM_OPEN_TIMEOUT_MS = 3000
STREAM_READ_TIMEOUT_MS = 2000


def _split_host_port(address, default_port=DEFAULT_PI_CAMERA_PORT):
    """Turn ``host``, ``host:port`` or a full URL into ``(host, port)``."""
    if not address:
        return None, default_port

    text = str(address).strip()
    if "://" not in text:
        text = f"http://{text}"

    parsed = urllib.parse.urlparse(text)
    host = parsed.hostname
    if not host:
        # A bare "host:port" is parsed as scheme="host" by urlparse when the
        # host is not a valid scheme; recover the pieces by hand.
        host, _, port_text = str(address).strip().partition(":")
        try:
            return host, int(port_text)
        except ValueError:
            return host, default_port

    try:
        port = parsed.port or default_port
    except ValueError:
        port = default_port
    return host, port


class PiCamera(AbstractCamera):
    """A Raspberry Pi HQ camera (or any sensor) served by ``PiCameraService``."""

    def __init__(self, host, port=DEFAULT_PI_CAMERA_PORT, label=None, mirror=False, **kwargs):
        self.host = host
        self.port = int(port) if port else DEFAULT_PI_CAMERA_PORT
        self.label = label or f"Pi Camera ({self.host}:{self.port})"
        self.cap = None
        self.grabbing = False
        self.dark_image = None
        # A single blocking read can stall for up to this long, so a caller
        # joining this camera's grab thread must allow at least this much.
        self.stream_read_timeout_ms = STREAM_READ_TIMEOUT_MS
        # If no frame arrives for this long, treat the camera as disconnected.
        self.grab_failure_timeout_s = 2.0
        # The libcamera sensor is rotated 180 degrees on the reference HQ camera
        # mount, and a microscope image has no fixed "up", so flipping is opt-in
        # rather than always-on like OpenCVCamera.
        self.mirror = bool(mirror)
        self.info = {}
        self._base_url = f"http://{self.host}:{self.port}"

        # cv2's FFMPEG backend is the one that consumes multipart MJPEG-over-HTTP.
        self.cap = cv2.VideoCapture(
            f"{self._base_url}/stream.mjpg",
            cv2.CAP_FFMPEG,
            [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
                STREAM_OPEN_TIMEOUT_MS,
                cv2.CAP_PROP_READ_TIMEOUT_MSEC,
                STREAM_READ_TIMEOUT_MS,
            ],
        )
        # Never buffer more than one frame, or the preview lags behind reality.
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if not self.cap.isOpened():
            print(f"Failed to open Pi camera stream at {self._base_url}/stream.mjpg")
            self.cap.release()
            self.cap = None
            return

        print(f"Using Pi camera at {self._base_url}")

        self.info = self._fetch_json("/info") or {}
        stream = self.info.get("stream") or {}
        main = stream.get("main") or {}
        size = main.get("size")
        if size:
            print(
                f"Pi camera preview: {size[0]}x{size[1]} @ {stream.get('fps')} fps "
                f"({stream.get('encoder')} encoder)"
            )
        warning = (self.info.get("camera") or {}).get("warning")
        if warning:
            print(f"Pi camera warning: {warning}")

        # The exposure and gain sliders should span what this sensor can do.
        self._load_control_ranges()

    def _load_control_ranges(self):
        self._exposure_range = self._control_range("ExposureTime")
        self._gain_range_linear = self._control_range("AnalogueGain")

    def _ensure_info(self):
        """Re-read ``/info`` if the first attempt failed.

        A daemon that is still bringing libcamera up answers the probe but not
        ``/info``, and without this the exposure and gain controls would stay
        silently disabled for the rest of the session.
        """
        if self.info or not self.cap:
            return
        self.info = self._fetch_json("/info") or {}
        if self.info:
            self._load_control_ranges()

    # -- discovery ---------------------------------------------------------- #

    @staticmethod
    def normalize_address(address):
        """Accept ``host``, ``host:port`` or a URL and return ``host:port``."""
        host, port = _split_host_port(address)
        if not host:
            return None
        return f"{host}:{port}"

    @staticmethod
    def _probe(address, timeout_s=PROBE_TIMEOUT_S):
        """Return the ``/info`` payload if a service answers quickly, else None."""
        host, port = _split_host_port(address)
        if not host:
            return None
        url = f"http://{host}:{port}/info"
        try:
            with urllib.request.urlopen(url, timeout=timeout_s) as response:
                if response.status != 200:
                    return None
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            return None

        if payload.get("service") != "PiCameraService":
            return None

        payload["_host"] = host
        payload["_port"] = port
        return payload

    @classmethod
    def enumerate_devices(cls, addresses=None):
        """Best-effort discovery of camera services.

        Discovery is deliberately quick and never authoritative: an unreachable
        or unadvertised Pi simply does not show up, and the user types its
        address into the camera box instead. ``addresses`` lets the caller add
        the last-used and user-configured hosts.
        """
        candidates = []
        for address in list(addresses or []) + list(DEFAULT_PI_CAMERA_HOSTS):
            normalized = cls.normalize_address(address)
            if normalized and normalized not in candidates:
                candidates.append(normalized)

        devices = []
        for address in candidates:
            payload = cls._probe(address)
            if not payload:
                continue

            host, port = _split_host_port(address)
            camera = payload.get("camera") or {}
            stream = payload.get("stream") or {}
            main = stream.get("main") or {}
            size = main.get("size") or [stream.get("width"), stream.get("height")]

            model = camera.get("model") or "unknown sensor"
            if size and size[0]:
                detail = f"{model}, {size[0]}x{size[1]}"
            else:
                detail = model

            devices.append(
                {
                    "kind": "picamera",
                    "id": f"picamera:{host}:{port}",
                    "label": f"Pi Camera {host}:{port} ({detail})",
                    "host": host,
                    "port": port,
                    "info": payload,
                }
            )
        return devices

    # -- HTTP helpers ------------------------------------------------------- #

    def _fetch_json(self, path, timeout=API_TIMEOUT_S):
        """GET ``path`` and decode the JSON body, or None on any failure.

        This is GET-only by design. ``urllib`` silently turns any request that
        carries a body into a POST, so a generic "fetch" helper that accepts one
        would happily send a POST to a GET route (or vice versa). Use
        ``_post_json`` for the routes that mutate camera state.
        """
        url = f"{self._base_url}{path}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            print(f"Pi camera request {path} failed: {exc}")
            return None

    def _post_json(self, path, payload=None, timeout=API_TIMEOUT_S):
        """POST a JSON body to ``path`` and decode the JSON reply, or None."""
        url = f"{self._base_url}{path}"
        body = json.dumps(payload if payload is not None else {}).encode("utf-8")
        request = urllib.request.Request(url, data=body)
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            print(f"Pi camera request POST {path} failed: {exc}")
            return None

    def _post_controls(self, controls):
        """Apply libcamera controls, ignoring rejections the sensor cannot honour."""
        if not controls:
            return True

        result = self._post_json("/control", {"controls": controls})
        if result is None:
            return False
        warnings = result.get("warnings")
        if warnings:
            print(f"Pi camera control warnings: {warnings}")
        return True

    def _control_range(self, name):
        controls = (self.info.get("controls") or {}).get(name) or {}
        return controls.get("min"), controls.get("max")

    # -- AbstractCamera API ------------------------------------------------- #

    def is_connected(self):
        return self.cap is not None

    def get_exposure_time_range(self):
        """Exposure bounds in microseconds, as advertised by the sensor."""
        self._ensure_info()
        return self._bounded_range(self._exposure_range)

    @staticmethod
    def _bounded_range(bounds):
        """Normalize an advertised (min, max) into a pair the GUI can use."""
        minimum, maximum = bounds
        if minimum is None or maximum is None:
            return 0, 1
        # A degenerate span (e.g. a 0-1 placeholder) leaves the slider useless,
        # so hand back something the user can actually drag.
        if maximum <= minimum:
            return 0, max(int(minimum), 1)
        return minimum, maximum

    def set_exposure_time(self, exposure_time_us):
        """Set the exposure in microseconds. ``0`` hands control back to the AEC."""
        if not self.cap:
            return

        self._ensure_info()
        minimum, maximum = self._exposure_range
        if minimum is None:
            return

        value = float(exposure_time_us)
        if value <= 0:
            # libcamera spells "auto exposure" as 0, which is below the minimum.
            self._post_controls({"AeEnable": True, "ExposureTime": 0})
            return

        if maximum is not None:
            value = max(float(minimum), min(value, float(maximum)))
        else:
            value = max(float(minimum), value)
        self._post_controls({"AeEnable": False, "ExposureTime": int(round(value))})

    def set_gain(self, gain_db):
        """Set analogue gain, converting the GUI's decibels to a linear factor."""
        if not self.cap:
            return

        self._ensure_info()
        minimum, maximum = self._gain_range_linear
        if minimum is None:
            return

        if gain_db <= 0:
            self._post_controls({"AeEnable": True, "AnalogueGain": 0.0})
            return

        linear = 10.0 ** (float(gain_db) / 20.0)
        if maximum is not None:
            linear = max(float(minimum), min(linear, float(maximum)))
        else:
            linear = max(float(minimum), linear)
        self._post_controls({"AeEnable": False, "AnalogueGain": float(linear)})

    def set_white_balance(self, wb):
        """Set a fixed colour temperature in kelvin.

        libcamera exposes white balance as a colour temperature, so unlike the
        Basler driver this is not a no-op. Values at or below zero hand control
        back to the auto white balance algorithm.
        """
        if not self.cap:
            return

        if wb is None or float(wb) <= 0:
            self._post_controls({"AwbEnable": True})
            return

        self._post_controls({"AwbEnable": False, "ColourTemperature": int(round(float(wb)))})

    def start_grabbing(self, single_grab=True):
        self.grabbing = True

    def stop_grabbing(self):
        self.grabbing = False

    def grab_single_triggered(self, timeout_ms=1000):
        return self.grab_one(timeout_ms)

    def grab_one(self, timeout_ms=5000):
        if not self.cap:
            return None

        # timeout_ms is advisory: OpenCV's FFMPEG backend applies the read
        # deadline given to the VideoCapture constructor and refuses to change
        # it afterwards, so a stalled stream fails the read in ~2 s rather than
        # blocking the preview thread.
        ok, frame = self.cap.read()
        if not ok or frame is None:
            return None

        frame = self._convert_frame(frame)
        if frame is None:
            return None

        if self.dark_image is not None:
            frame = np.clip(frame.astype(np.float32) - self.dark_image, 0, 255).astype(np.uint8)
        return frame

    def grab_loop(self, callback, timeout_ms=5000):
        if not self.cap:
            return

        frame = np.ones((100, 100, 3), dtype=np.uint8) * 60
        self.start_grabbing(single_grab=False)
        failure_deadline = None
        try:
            while self.grabbing:
                new_frame = self.grab_one(timeout_ms)
                if new_frame is not None:
                    failure_deadline = None
                    frame = new_frame
                    if self.mirror:
                        frame = np.flip(frame, 0)
                        frame = np.flip(frame, 1)
                else:
                    # The preview may be briefly unavailable (a still capture
                    # tears it down and restores it) so allow a grace period
                    # before declaring the camera disconnected.
                    now = time.monotonic()
                    if failure_deadline is None:
                        failure_deadline = now + self.grab_failure_timeout_s
                    elif now >= failure_deadline:
                        raise RuntimeError("Pi camera disconnected (no frames received).")
                    time.sleep(0.05)

                try:
                    if callback(frame) is False:
                        break
                except Exception as exc:
                    print(f"Error in callback: {exc}")
        finally:
            self.stop_grabbing()

    def capture_dark_image(self, n_frames=10):
        """Average frames captured with the shortest possible exposure."""
        if not self.cap:
            return

        previous = (self.info.get("current_controls") or {}).get("ExposureTime")
        minimum, _ = self._exposure_range
        self._post_controls({"AeEnable": False, "ExposureTime": int(minimum or 1)})

        frames = []
        deadline = time.monotonic() + 5.0
        while len(frames) < n_frames and time.monotonic() < deadline:
            frame = self.grab_one(timeout_ms=1000)
            if frame is not None:
                frames.append(frame.astype(np.float32))
            else:
                break

        self.dark_image = np.mean(frames, axis=0) if frames else None

        if previous is None:
            self._post_controls({"AeEnable": True, "ExposureTime": 0})
        else:
            self._post_controls({"AeEnable": False, "ExposureTime": int(previous)})

    @staticmethod
    def _convert_frame(frame):
        """The GUI expects RGB; OpenCV hands back BGR."""
        if frame is None:
            return None

        if len(frame.shape) == 3 and frame.shape[2] == 3:
            return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        return frame

    # -- still captures ----------------------------------------------------- #

    def capture_still(self, label=None, raw=True, timeout_s=CAPTURE_TIMEOUT_S):
        """Ask the service for a full-resolution still.

        This is the measurement path: the MJPEG preview is lossy and must not be
        used for photometry. The service stops the preview, reconfigures the
        sensor for a full-resolution capture, writes the files on the Pi and
        restores the preview before returning.
        """
        if not self.cap:
            return None

        query = {"raw": "1" if raw else "0"}
        if label:
            query["label"] = label
        path = f"/capture?{urllib.parse.urlencode(query)}"
        # /capture is a POST route: taking a still stops the preview, reconfigures
        # the sensor and restores it, so it is not a safe GET.
        result = self._post_json(path, timeout=timeout_s)
        if not result or not result.get("ok"):
            return None
        return result

    def fetch_file(self, remote_path, timeout_s=CAPTURE_TIMEOUT_S):
        """Download a file the service wrote, e.g. ``/captures/<name>.jpg``."""
        if not remote_path:
            return None
        path = remote_path if remote_path.startswith("/") else f"/{remote_path}"
        try:
            with urllib.request.urlopen(f"{self._base_url}{path}", timeout=timeout_s) as response:
                return response.read()
        except Exception as exc:
            print(f"Pi camera download {path} failed: {exc}")
            return None

    def close(self):
        self.stop_grabbing()
        if self.cap:
            self.cap.release()
            self.cap = None

    def __del__(self):
        self.close()


def probe_address(address, timeout_s=PROBE_TIMEOUT_S):
    """Check a user-typed address. Returns the device dict, or None."""
    payload = PiCamera._probe(address, timeout_s=timeout_s)
    if not payload:
        return None

    host, port = _split_host_port(address)
    return {
        "kind": "picamera",
        "id": f"picamera:{host}:{port}",
        "label": f"Pi Camera {host}:{port}",
        "host": host,
        "port": port,
        "info": payload,
    }
