"""Offline tests for the Raspberry Pi network camera client.

A fake ``PiCameraService`` runs on localhost so no real Pi is required: it
serves ``/info``, a genuine multipart MJPEG ``/stream.mjpg`` (real JPEG bytes,
so ``cv2.VideoCapture`` can decode them) and the POST endpoints.
"""
import io
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

import cv2
import numpy as np

from hardware import camera_pi
from hardware.camera_pi import (
    DEFAULT_PI_CAMERA_PORT,
    STREAM_OPEN_TIMEOUT_MS,
    PiCamera,
    _split_host_port,
    probe_address,
)


def _isolate_default_hosts():
    """Stop ``enumerate_devices`` probing the module's built-in addresses.

    ``DEFAULT_PI_CAMERA_HOSTS`` contains a real LAN address for the developer's
    Pi, so an unpatched discovery test would reach out to the network (and pass
    or fail depending on whether that Pi happens to be powered on).
    """
    original = camera_pi.DEFAULT_PI_CAMERA_HOSTS
    camera_pi.DEFAULT_PI_CAMERA_HOSTS = ()
    return original

# A saturated primary so a channel swap cannot hide behind a grey scene.
FRAME_RGB = (210, 40, 30)
JPEG_QUALITY = 95


def _make_jpeg(width=160, height=120, rgb=FRAME_RGB):
    """Build a real JPEG whose pixel values are exactly ``rgb`` (R, G, B)."""
    frame_rgb = np.zeros((height, width, 3), dtype=np.uint8)
    frame_rgb[:, :] = rgb
    bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
    ok, buffer = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    assert ok
    return buffer.tobytes()


def _info_payload(width=160, height=120):
    return {
        "service": "PiCameraService",
        "version": "0.2.0",
        "api_version": 1,
        "hostname": "fake-pi",
        "camera": {
            "open": True,
            "model": "imx477",
            "device_id": "/base/soc/i2c0mux/i2c@1/imx477@1a",
            "sensor_resolution": [4056, 3040],
            "warning": None,
        },
        "stream": {
            "width": 1024,
            "height": 768,
            "fps": 25,
            "encoder": "mjpeg",
            "main": {"format": "RGB888", "size": [width, height], "stride": width * 3},
            "boundary": "picamera-frame",
        },
        "controls": {
            "ExposureTime": {"type": "int", "min": 37, "max": 667234896, "default": 20000},
            "AnalogueGain": {"type": "float", "min": 1.0, "max": 22.2608699798584, "default": 1.0},
            "ColourTemperature": {"type": "int", "min": 100, "max": 100000},
        },
        "current_controls": {"ExposureTime": 20000, "AnalogueGain": 1.0},
    }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Injected by the server factory.
    payload = None
    jpeg = None
    stall_after = None
    capture_fails = False
    requests = None
    control_bodies = None

    def log_message(self, *args):  # keep the test output clean
        pass

    def _record(self, method):
        self.requests.append((method, self.path))

    def do_GET(self):
        self._record("GET")
        route = urlparse(self.path).path

        if route == "/info":
            body = json.dumps(self.payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif route == "/stream.mjpg":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=picamera-frame")
            self.send_header("Connection", "close")
            self.end_headers()
            boundary = b"--picamera-frame\r\n"
            try:
                for index in range(200):
                    if self.stall_after is not None and index >= self.stall_after:
                        time.sleep(30)
                        continue
                    self.wfile.write(boundary)
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(self.jpeg)}\r\n\r\n".encode())
                    self.wfile.write(self.jpeg)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                    time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif route.startswith("/captures/"):
            name = Path(route).name
            if name != "still.jpg":
                self.send_error(404)
                return
            body = self.jpeg
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        self._record("POST")
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        if parsed.path.rstrip("/") == "/control":
            controls = json.loads(body.decode() or "{}")
            self.control_bodies.append(controls)
            applied = controls.get("controls", controls)
            self._json({"applied": applied, "controls": applied, "warnings": []})
        elif parsed.path.rstrip("/") == "/capture":
            if self.capture_fails:
                self.send_error(503, "camera unavailable")
                return
            query = parse_qs(parsed.query)
            self._json(
                {
                    "ok": True,
                    "label": (query.get("label") or [None])[0],
                    "raw": (query.get("raw") or ["1"])[0],
                    "files": {
                        "jpeg": {"name": "still.jpg", "url": "/captures/still.jpg", "bytes": len(self.jpeg)},
                    },
                }
            )
        else:
            self.send_error(404)

    def _json(self, value):
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakePiCameraService:
    """A thread-backed stand-in for the daemon on the Pi."""

    def __init__(self, width=160, height=120, jpeg=None):
        self.jpeg = jpeg or _make_jpeg(width, height)
        handler = type(
            "BoundHandler",
            (_Handler,),
            {
                "payload": _info_payload(width, height),
                "jpeg": self.jpeg,
                "stall_after": None,
                "capture_fails": False,
                "requests": [],
                "control_bodies": [],
            },
        )
        self.handler = handler
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server.server_address[1]

    @property
    def address(self):
        return f"127.0.0.1:{self.port}"

    @property
    def requests(self):
        return self.handler.requests

    @property
    def control_bodies(self):
        """Every control set POSTed to /control, in order."""
        return self.handler.control_bodies

    def reset_requests(self):
        self.handler.requests = []
        self.handler.control_bodies = []

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class AddressParsingTests(unittest.TestCase):
    def test_bare_host_gets_the_default_port(self):
        self.assertEqual(_split_host_port("192.168.1.39"), ("192.168.1.39", DEFAULT_PI_CAMERA_PORT))

    def test_host_with_port_is_preserved(self):
        self.assertEqual(_split_host_port("192.168.1.39:9000"), ("192.168.1.39", 9000))

    def test_full_url_is_reduced_to_host_and_port(self):
        self.assertEqual(_split_host_port("http://192.168.1.39:8000/stream.mjpg"), ("192.168.1.39", 8000))

    def test_mdns_name_is_accepted(self):
        self.assertEqual(_split_host_port("picam.local"), ("picam.local", DEFAULT_PI_CAMERA_PORT))

    def test_empty_input_falls_back_to_the_default_port(self):
        self.assertEqual(_split_host_port(""), (None, DEFAULT_PI_CAMERA_PORT))
        self.assertEqual(_split_host_port(None), (None, DEFAULT_PI_CAMERA_PORT))

    def test_garbage_port_falls_back_to_the_default_port(self):
        host, port = _split_host_port("192.168.1.39:not-a-port")
        self.assertEqual(host, "192.168.1.39")
        self.assertEqual(port, DEFAULT_PI_CAMERA_PORT)

    def test_normalize_address_round_trips_to_host_colon_port(self):
        self.assertEqual(PiCamera.normalize_address("http://10.0.0.5:1234/info"), "10.0.0.5:1234")
        self.assertEqual(PiCamera.normalize_address("10.0.0.5"), f"10.0.0.5:{DEFAULT_PI_CAMERA_PORT}")
        self.assertIsNone(PiCamera.normalize_address(""))


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.service = FakePiCameraService()
        self.addCleanup(self.service.stop)

    def test_probe_returns_the_info_payload_with_the_host_injected(self):
        payload = PiCamera._probe(self.service.address)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["service"], "PiCameraService")
        self.assertEqual(payload["_host"], "127.0.0.1")
        self.assertEqual(payload["_port"], self.service.port)

    def test_probe_address_exposes_a_camera_device_dict(self):
        device = probe_address(self.service.address)
        self.assertIsNotNone(device)
        self.assertEqual(device["kind"], "picamera")
        self.assertEqual(device["host"], "127.0.0.1")
        self.assertEqual(device["port"], self.service.port)
        self.assertEqual(device["id"], f"picamera:127.0.0.1:{self.service.port}")

    def test_probe_rejects_a_service_that_is_not_picameraservice(self):
        self.service.handler.payload = {"service": "SomethingElse"}
        self.assertIsNone(PiCamera._probe(self.service.address))

    def test_probe_returns_none_when_nothing_is_listening(self):
        # Port 1 on loopback is never a camera service.
        self.assertIsNone(PiCamera._probe("127.0.0.1:1", timeout_s=0.2))
        self.assertIsNone(probe_address("127.0.0.1:1", timeout_s=0.2))


class EnumerateDevicesTests(unittest.TestCase):
    def setUp(self):
        original = _isolate_default_hosts()
        self.addCleanup(setattr, camera_pi, "DEFAULT_PI_CAMERA_HOSTS", original)
        self.service = FakePiCameraService()
        self.addCleanup(self.service.stop)

    def test_an_extra_address_is_discovered(self):
        devices = PiCamera.enumerate_devices(addresses=[self.service.address])
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["host"], "127.0.0.1")
        self.assertEqual(devices[0]["port"], self.service.port)
        self.assertIn("imx477", devices[0]["label"])
        self.assertIn("160x120", devices[0]["label"])

    def test_dead_addresses_do_not_raise_and_are_skipped(self):
        devices = PiCamera.enumerate_devices(
            addresses=["127.0.0.1:1", "127.0.0.1:2", self.service.address]
        )
        self.assertEqual([d["port"] for d in devices], [self.service.port])

    def test_duplicate_addresses_are_probed_once(self):
        self.service.reset_requests()
        PiCamera.enumerate_devices(addresses=[self.service.address, self.service.address])
        info_hits = [r for r in self.service.requests if r[1] == "/info"]
        self.assertEqual(len(info_hits), 1)

    def test_the_built_in_addresses_are_probed_alongside_the_extras(self):
        """The module's default hosts are the zero-configuration path."""
        camera_pi.DEFAULT_PI_CAMERA_HOSTS = (self.service.address,)
        devices = PiCamera.enumerate_devices()
        self.assertEqual([d["port"] for d in devices], [self.service.port])


class _CameraTestCase(unittest.TestCase):
    """Base class that opens a real ``PiCamera`` against the fake service."""

    def setUp(self):
        self.service = FakePiCameraService()
        self.addCleanup(self.service.stop)
        self.camera = PiCamera(host="127.0.0.1", port=self.service.port, label="Fake Pi")
        self.addCleanup(self.camera.close)

    def _controls_sent(self):
        """Every control dict POSTed to /control, decoded."""
        sent = []
        for method, path in self.service.requests:
            if method == "POST" and path == "/control":
                sent.append(path)
        return sent


class OpenAndInfoTests(_CameraTestCase):
    def test_open_reads_the_negotiated_preview_size(self):
        self.assertTrue(self.camera.is_connected())
        self.assertEqual(self.camera.info["hostname"], "fake-pi")

    def test_exposure_range_comes_from_the_advertised_controls(self):
        self.assertEqual(self.camera.get_exposure_time_range(), (37, 667234896))

    def test_close_releases_the_stream(self):
        self.camera.close()
        self.assertFalse(self.camera.is_connected())
        self.assertIsNone(self.camera.cap)

    def test_unreachable_host_reports_disconnected_instead_of_raising(self):
        camera = PiCamera(host="127.0.0.1", port=1)
        self.addCleanup(camera.close)
        self.assertFalse(camera.is_connected())
        self.assertIsNone(camera.grab_one(timeout_ms=100))


class FrameTests(_CameraTestCase):
    def test_grab_one_returns_an_rgb_uint8_array(self):
        frame = self.camera.grab_one(timeout_ms=3000)
        self.assertIsNotNone(frame)
        self.assertEqual(frame.dtype, np.uint8)
        self.assertEqual(frame.ndim, 3)
        self.assertEqual(frame.shape[2], 3)

    def test_grab_one_delivers_the_colour_that_was_encoded(self):
        """A channel swap would show up as red and blue trading places."""
        frame = self.camera.grab_one(timeout_ms=3000)
        r, g, b = (float(frame[..., i].mean()) for i in range(3))
        self.assertGreater(r, b, "red should be the strongest channel")
        self.assertGreater(r, g)
        self.assertLess(abs(r - FRAME_RGB[0]), 12, "red channel mismatch")
        self.assertLess(abs(g - FRAME_RGB[1]), 12, "green channel mismatch")
        self.assertLess(abs(b - FRAME_RGB[2]), 12, "blue channel mismatch")

    def test_grab_loop_stops_when_the_callback_returns_false(self):
        frames = []

        def callback(frame):
            frames.append(frame)
            return len(frames) < 5

        self.camera.grab_loop(callback, timeout_ms=2000)
        self.assertEqual(len(frames), 5)
        self.assertFalse(self.camera.grabbing)

    def test_grab_loop_survives_a_raising_callback(self):
        frames = []

        def callback(frame):
            frames.append(frame)
            if len(frames) == 1:
                raise ValueError("callback bug")
            return len(frames) < 4

        self.camera.grab_loop(callback, timeout_ms=2000)
        self.assertEqual(len(frames), 4)

    def test_mirror_is_off_by_default(self):
        self.assertFalse(self.camera.mirror)

    def test_mirror_flips_both_axes_when_opted_in(self):
        plain = PiCamera(host="127.0.0.1", port=self.service.port, mirror=False)
        self.addCleanup(plain.close)
        mirrored = PiCamera(host="127.0.0.1", port=self.service.port, mirror=True)
        self.addCleanup(mirrored.close)

        # The fake feed is flat, so compare the shapes of the two code paths
        # instead: mirroring must not trip over the array layout.
        mirrored_frame = mirrored.grab_one(timeout_ms=3000)
        self.assertIsNotNone(mirrored_frame)
        self.assertEqual(mirrored_frame.shape[2], 3)
        self.assertTrue(plain.is_connected())


class DisconnectTests(_CameraTestCase):
    def test_grab_loop_raises_once_frames_stop_arriving(self):
        self.service.handler.stall_after = 3
        self.camera.grab_failure_timeout_s = 0.5
        started = time.monotonic()
        with self.assertRaises(RuntimeError) as ctx:
            self.camera.grab_loop(lambda frame: True, timeout_ms=300)
        self.assertIn("disconnected", str(ctx.exception))
        self.assertFalse(self.camera.grabbing)
        # The stream's own read deadline must bound this, not a 30 second stall.
        self.assertLess(time.monotonic() - started, 15)


class StreamTimeoutTests(unittest.TestCase):
    """A dead host must not freeze the GUI while the stream is opened."""

    def test_a_silent_endpoint_does_not_block_the_constructor(self):
        import socket

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)

        started = time.monotonic()
        camera = PiCamera(host="127.0.0.1", port=listener.getsockname()[1])
        self.addCleanup(camera.close)
        elapsed = time.monotonic() - started

        self.assertFalse(camera.is_connected())
        self.assertLess(
            elapsed,
            (STREAM_OPEN_TIMEOUT_MS / 1000.0) + 3.0,
            "opening the stream must honour STREAM_OPEN_TIMEOUT_MS",
        )

    def test_an_unroutable_address_fails_fast(self):
        started = time.monotonic()
        camera = PiCamera(host="192.0.2.1")  # TEST-NET-1, guaranteed unroutable
        self.addCleanup(camera.close)
        elapsed = time.monotonic() - started

        self.assertFalse(camera.is_connected())
        self.assertLess(elapsed, (STREAM_OPEN_TIMEOUT_MS / 1000.0) + 3.0)


class ControlTests(_CameraTestCase):
    def setUp(self):
        super().setUp()
        self.service.reset_requests()

    def _last_controls(self):
        """The control dict of the most recent POST to /control."""
        self.assertTrue(self.service.control_bodies, "no controls were sent")
        return self.service.control_bodies[-1]["controls"]

    def test_a_manual_exposure_disables_the_aec_and_rounds_the_value(self):
        self.camera.set_exposure_time(12345.7)
        controls = self._last_controls()
        self.assertEqual(controls["ExposureTime"], 12346)
        self.assertIs(controls["AeEnable"], False)

    def test_exposure_is_clamped_into_the_advertised_range(self):
        self.camera.set_exposure_time(10**12)
        self.assertEqual(self._last_controls()["ExposureTime"], 667234896)

        self.camera.set_exposure_time(1)  # below the 37 us floor
        self.assertEqual(self._last_controls()["ExposureTime"], 37)

    def test_zero_exposure_hands_control_back_to_the_aec(self):
        self.camera.set_exposure_time(0)
        controls = self._last_controls()
        self.assertIs(controls["AeEnable"], True)
        self.assertEqual(controls["ExposureTime"], 0)

    def test_negative_exposure_hands_control_back_to_the_aec(self):
        self.camera.set_exposure_time(-50)
        self.assertIs(self._last_controls()["AeEnable"], True)

    def test_gain_converts_decibels_to_a_linear_factor(self):
        self.camera.set_gain(20.0)  # +20 dB is 10x linear.
        controls = self._last_controls()
        self.assertAlmostEqual(controls["AnalogueGain"], 10.0, places=6)
        self.assertIs(controls["AeEnable"], False)

    def test_gain_is_clamped_to_the_sensor_maximum(self):
        self.camera.set_gain(100.0)  # 100 dB is 100000x, far past 22.26x
        self.assertAlmostEqual(self._last_controls()["AnalogueGain"], 22.2608699798584, places=4)

    def test_gain_below_unity_hands_control_back_to_the_aec(self):
        # Anything at or below 0 dB means "auto", so -24 dB must not be treated
        # as a manual 0.063x gain.
        self.camera.set_gain(-24.0)
        controls = self._last_controls()
        self.assertIs(controls["AeEnable"], True)
        self.assertEqual(controls["AnalogueGain"], 0.0)

    def test_zero_gain_hands_control_back_to_the_aec(self):
        self.camera.set_gain(0)
        controls = self._last_controls()
        self.assertIs(controls["AeEnable"], True)
        self.assertEqual(controls["AnalogueGain"], 0.0)

    def test_white_balance_zero_returns_to_auto(self):
        self.camera.set_white_balance(0)
        controls = self._last_controls()
        self.assertIs(controls["AwbEnable"], True)
        self.assertNotIn("ColourTemperature", controls)

    def test_white_balance_none_returns_to_auto(self):
        self.camera.set_white_balance(None)
        self.assertIs(self._last_controls()["AwbEnable"], True)

    def test_white_balance_sets_a_colour_temperature(self):
        self.camera.set_white_balance(4100)
        controls = self._last_controls()
        self.assertEqual(controls["ColourTemperature"], 4100)
        self.assertIs(controls["AwbEnable"], False)

    def test_a_rejected_control_does_not_raise(self):
        self.camera.set_exposure_time(10000)
        self.assertTrue(self.camera.is_connected())

    def test_controls_on_a_closed_camera_are_ignored(self):
        self.camera.close()
        self.service.reset_requests()
        self.camera.set_exposure_time(10000)
        self.camera.set_gain(6.0)
        self.camera.set_white_balance(4000)
        self.assertEqual(self.service.requests, [])
        self.assertEqual(self.service.control_bodies, [])

    def test_control_range_reads_the_advertised_bounds(self):
        self.assertEqual(self.camera._control_range("ExposureTime"), (37, 667234896))
        self.assertEqual(self.camera._control_range("NoSuchControl"), (None, None))

    def test_the_exposure_range_falls_back_when_the_sensor_is_unknown(self):
        """A failed /info must disable the sliders rather than mislabel them."""
        self.camera._exposure_range = (None, None)
        self.assertEqual(self.camera.get_exposure_time_range(), (0, 1))

    def test_a_degenerate_range_is_widened_so_the_slider_stays_usable(self):
        self.camera._exposure_range = (0, 1)
        self.assertEqual(self.camera.get_exposure_time_range(), (0, 1))

    def test_info_is_re_read_when_the_service_was_not_ready_at_connect(self):
        """A daemon still starting up must not leave the sliders dead forever."""
        self.camera.info = {}
        self.camera._exposure_range = (None, None)
        self.camera._gain_range_linear = (None, None)

        minimum, maximum = self.camera.get_exposure_time_range()
        self.assertEqual((minimum, maximum), (37, 667234896))
        self.assertEqual(self.camera._gain_range_linear, (1.0, 22.2608699798584))

    def test_info_is_not_re_read_on_every_call(self):
        self.service.reset_requests()
        self.camera.get_exposure_time_range()
        self.camera.get_exposure_time_range()
        self.assertEqual([r for r in self.service.requests if r[1] == "/info"], [])


class StillCaptureTests(_CameraTestCase):
    def test_capture_still_uses_post_not_get(self):
        """/capture is a POST route; a GET returns 404."""
        self.service.reset_requests()
        result = self.camera.capture_still(label="unit-test", raw=True)
        self.assertIsNotNone(result)
        self.assertTrue(result["ok"])
        capture_methods = [m for m, p in self.service.requests if p.startswith("/capture")]
        self.assertEqual(capture_methods, ["POST"])

    def test_capture_still_returns_the_file_listing(self):
        result = self.camera.capture_still(label="abc", raw=True)
        self.assertEqual(result["label"], "abc")
        self.assertEqual(result["raw"], "1")
        self.assertIn("jpeg", result["files"])

    def test_capture_still_returns_none_when_the_service_reports_failure(self):
        self.service.handler.capture_fails = True
        self.assertIsNone(self.camera.capture_still())

    def test_capture_still_returns_none_when_the_camera_is_closed(self):
        self.camera.close()
        self.assertIsNone(self.camera.capture_still())

    def test_fetch_file_downloads_the_bytes(self):
        data = self.camera.fetch_file("/captures/still.jpg")
        self.assertIsNotNone(data)
        self.assertTrue(data.startswith(b"\xff\xd8"))
        self.assertEqual(data, self.service.jpeg)

    def test_fetch_file_returns_none_for_a_missing_file(self):
        self.assertIsNone(self.camera.fetch_file("/captures/none.jpg"))

    def test_fetch_file_returns_none_for_empty_input(self):
        self.assertIsNone(self.camera.fetch_file(None))
        self.assertIsNone(self.camera.fetch_file(""))


class DarkImageTests(_CameraTestCase):
    def test_dark_image_capture_averages_frames(self):
        self.camera.capture_dark_image(n_frames=3)
        self.assertIsNotNone(self.camera.dark_image)
        self.assertEqual(self.camera.dark_image.shape[2], 3)

    def test_dark_image_subtraction_is_clipped_to_uint8(self):
        self.camera.capture_dark_image(n_frames=2)
        frame = self.camera.grab_one(timeout_ms=3000)
        self.assertIsNotNone(frame)
        self.assertEqual(frame.dtype, np.uint8)
        self.assertLessEqual(frame.max(), 255)
        self.assertGreaterEqual(frame.min(), 0)

    def test_dark_image_on_a_closed_camera_does_nothing(self):
        self.camera.close()
        self.camera.capture_dark_image(n_frames=2)


class LabelTests(unittest.TestCase):
    def test_the_default_label_names_the_endpoint(self):
        camera = PiCamera(host="example.local", port=9000)
        self.assertEqual(camera.label, "Pi Camera (example.local:9000)")

    def test_an_explicit_label_is_kept(self):
        camera = PiCamera(host="example.local", label="My scope")
        self.assertEqual(camera.label, "My scope")

    def test_the_default_port_is_used_when_none_is_given(self):
        camera = PiCamera(host="example.local", port=None)
        self.assertEqual(camera.port, DEFAULT_PI_CAMERA_PORT)
        self.assertEqual(camera._base_url, f"http://example.local:{DEFAULT_PI_CAMERA_PORT}")


if __name__ == "__main__":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    unittest.main()
