"""Hardware inventory/configuration uses fixtures, never opens a live stream."""
import json
import threading
import urllib.error
import urllib.request
from unittest.mock import patch

from joker.audio_routes import RouteConflict
from joker.backends import ModelError
from joker.panel import make_server
from joker.readiness import report
from test_next_stage import Fixture


class AudioRouteTests(Fixture):
    def setUp(self):
        super().setUp()
        self.routes = self.app.voice.routes
        self.devices = [dict(name="Fixture Cable", hostapi=0, max_input_channels=0, max_output_channels=2),
                        dict(name="Fixture Call", hostapi=0, max_input_channels=1, max_output_channels=0),
                        dict(name="Fixture Cable", hostapi=1, max_input_channels=0, max_output_channels=2)]
        self.hosts = [{"name": "Windows WASAPI"}, {"name": "MME"}]
        self.query = patch("sounddevice.query_devices", side_effect=lambda: self.devices)
        self.host = patch("sounddevice.query_hostapis", return_value=self.hosts)
        self.query.start()
        self.host.start()
        self.addCleanup(self.query.stop)
        self.addCleanup(self.host.stop)
        for name in ("check_input_settings", "check_output_settings"):
            check = patch("sounddevice." + name)
            check.start()
            self.addCleanup(check.stop)

    def select(self):
        devices = self.routes.inventory()
        return self.routes.save(0, devices[0]["id"], devices[1]["id"])

    def test_inventory_and_setup_report_never_open_capture_or_playback(self):
        with patch("sounddevice.RawInputStream") as capture, patch("sounddevice.RawOutputStream") as output:
            self.assertEqual(len(self.routes.report()["devices"]), 3)
            data = report(self.app)
        capture.assert_not_called()
        output.assert_not_called()
        self.assertFalse(data["configured"])
        self.assertFalse(next(c for c in data["checks"] if c["id"] == "output")["ready"])
        self.assertEqual(self.model.calls, 0)

    def test_selection_survives_index_reordering_and_persists_host_not_number(self):
        saved = self.select()
        self.assertEqual(saved["version"], 1)
        stored = self.app.store.get("audio_routes")
        self.assertEqual(stored["output"], {"name": "Fixture Cable", "hostapi": "Windows WASAPI"})
        self.devices.reverse()
        self.assertEqual(self.routes.resolve("output")["index"], 2)
        self.assertEqual(self.routes.resolve("output")["hostapi"], "Windows WASAPI")

    def test_removed_device_does_not_fall_back_to_same_name_on_another_host(self):
        self.select()
        self.devices.pop(0)
        with self.assertRaises(ModelError):
            self.routes.resolve("output")
        self.assertFalse(self.routes.report()["routes"]["output"]["available"])

    def test_ambiguous_names_and_wrong_direction_are_rejected(self):
        self.settings.voice_device = "Fixture Cable"
        with self.assertRaises(ModelError):
            self.routes.resolve("output")
        devices = self.routes.inventory()
        with self.assertRaises(RouteConflict):
            self.routes.save(0, devices[1]["id"], None)
        self.devices.append(dict(self.devices[0]))
        with self.assertRaises(RouteConflict):
            self.routes.save(0, devices[0]["id"], None)
        self.assertIsNone(self.app.store.get("audio_routes"))

    def test_legacy_numeric_route_is_checked_and_explicit_disable_overrides_env(self):
        self.settings.voice_device = "0"
        self.assertEqual(self.routes.resolve("output")["index"], 0)
        self.routes.save(0, None, None)
        self.assertFalse(self.routes.configured("output"))
        with self.assertRaises(ModelError):
            self.routes.resolve("output")
        self.assertFalse(self.app.voice.status()["output_device_configured"])

    def test_stale_or_live_configuration_changes_are_rejected(self):
        self.select()
        with self.assertRaises(RouteConflict):
            self.routes.save(0, None, None)
        self.app.voice.enabled = True
        with self.assertRaises(RouteConflict):
            self.routes.save(1, None, None)
        self.assertEqual(self.app.store.get("audio_routes")["version"], 1)

    def test_saving_routes_cancels_pending_previews_without_enabling_voice(self):
        cancel = self.app.voice.cancel
        self.select()
        self.assertTrue(cancel.is_set())
        self.assertFalse(self.app.voice.enabled)

    def test_wasapi_checks_synthesized_rate_with_shared_system_conversion(self):
        self.select()
        with patch("sounddevice.WasapiSettings", return_value="fixture-conversion") as wasapi, \
                patch("sounddevice.check_output_settings") as check:
            args = self.routes.stream_args("output", 22050, 1)
        wasapi.assert_called_once_with(auto_convert=True)
        self.assertEqual(args["device"], 0)
        self.assertEqual(args["samplerate"], 22050)
        self.assertEqual(args["extra_settings"], "fixture-conversion")
        check.assert_called_once_with(**args)

    def test_other_host_receives_no_wasapi_option_and_format_failure_is_actionable(self):
        self.settings.voice_device = "2"
        with patch("sounddevice.WasapiSettings") as wasapi, patch("sounddevice.check_output_settings"):
            self.assertNotIn("extra_settings", self.routes.stream_args("output", 16000, 1))
        wasapi.assert_not_called()
        with patch("sounddevice.check_output_settings", side_effect=RuntimeError("fixture-driver")), self.assertRaises(ModelError):
            self.routes.stream_args("output", 16000, 1)

    def test_tts_preflight_never_checks_or_opens_optional_input(self):
        self.select()
        with patch("sounddevice.check_input_settings") as check, patch("sounddevice.RawInputStream") as capture:
            self.assertIsNone(self.routes.preflight("tts"))
        check.assert_not_called()
        capture.assert_not_called()

    def test_portaudio_driver_failure_is_reported_without_a_traceback(self):
        import sounddevice
        with patch("sounddevice.query_devices", side_effect=sounddevice.PortAudioError("fixture")), \
                self.assertRaises(ModelError):
            self.routes.report()
        self.select()
        with patch("sounddevice.check_output_settings", side_effect=sounddevice.PortAudioError("fixture")), \
                self.assertRaises(ModelError):
            self.routes.stream_args("output", 22050, 1)

    def test_missing_output_rejected_before_voice_workers_or_tts_requests(self):
        self.select()
        self.devices.pop(0)
        with patch.object(self.app.voice, "tts_status", return_value={"ready": True}), \
                patch("joker.voice.threading.Thread") as workers, self.assertRaises(ModelError):
            self.app.voice.start("tts")
        workers.assert_not_called()
        self.assertFalse(self.app.voice.enabled)

    def test_readiness_distinguishes_optional_stt_and_live_checks(self):
        self.select()
        self.app.voice.mode = "tts"
        with patch.object(self.app.voice, "tts_status", return_value={"ready": True, "selected_voice": "fixture"}):
            data = report(self.app)
            self.assertTrue(data["configured"])
            self.assertTrue(next(c for c in data["checks"] if c["id"] == "stt")["optional"])
            self.assertFalse(next(c for c in data["checks"] if c["id"] == "live")["ready"])
            self.app.voice.mode = "conversation"
            self.assertFalse(report(self.app)["configured"])

    def test_routes_endpoint_requires_csrf_and_rejects_stale_version(self):
        (self.root / "joker").mkdir(exist_ok=True)
        (self.root / "joker/panel.html").write_text('<script>const key=__CSRF__;</script>', encoding="utf-8")
        server = make_server(self.app, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(base + "/api/audio/devices") as response:
                device = json.load(response)["devices"][0]["id"]
            request = urllib.request.Request(base + "/api/audio/routes", data=json.dumps(
                {"version": 0, "output": device, "input": None}).encode(), headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(request)
            self.assertEqual(denied.exception.code, 403)
            with urllib.request.urlopen(base) as page:
                key = json.loads(page.read().decode().split("const key=", 1)[1].split(";", 1)[0])
            request.add_header("X-Joker-Key", key)
            with urllib.request.urlopen(request) as response:
                self.assertEqual(json.load(response)["routes"]["version"], 1)
            with self.assertRaises(urllib.error.HTTPError) as stale:
                urllib.request.urlopen(request)
            self.assertEqual(stale.exception.code, 409)
            with urllib.request.urlopen(base + "/api/readiness") as response:
                self.assertIn("checks", json.load(response))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
