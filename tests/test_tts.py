"""Tests for spoken announcements (tts.py): wording, the Wyoming client against a fake Piper, the cache.
Run: python3 -m unittest discover -s tests -v"""
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import wave

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import tts  # noqa: E402


def fake_piper(requests):
    """A Wyoming server that answers synthesize with two audio chunks, and describe with info."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)

    def send(c, etype, data, payload=b""):
        body = json.dumps(data).encode()
        h = {"type": etype, "data_length": len(body)}
        if payload:
            h["payload_length"] = len(payload)
        c.sendall(json.dumps(h).encode() + b"\n" + body + payload)

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            f = c.makefile("rb")
            h = json.loads(f.readline())
            data = json.loads(f.read(h["data_length"])) if h.get("data_length") else {}
            requests.append((h["type"], data))
            if h["type"] == "describe":
                send(c, "info", {"tts": [{"name": "piper", "voices": [{"name": "en_US-lessac-medium"}]}]})
            else:
                send(c, "audio-start", {"rate": 22050, "width": 2, "channels": 1})
                send(c, "audio-chunk", {"rate": 22050, "width": 2, "channels": 1}, b"\x01\x00" * 1000)
                send(c, "audio-chunk", {"rate": 22050, "width": 2, "channels": 1}, b"\x02\x00" * 500)
                send(c, "audio-stop", {})
            c.close()
    threading.Thread(target=serve, daemon=True).start()
    return srv


class TTS(unittest.TestCase):
    def test_phrases(self):
        self.assertEqual(tts.phrase("Person", "Front door"), "Person at the Front door")
        self.assertEqual(tts.phrase("Doorbell", "Front door"), "Someone's at the Front door")
        self.assertEqual(tts.phrase("Vehicle", "Driveway", "{object} in the {camera}"), "Vehicle in the Driveway")
        self.assertEqual(tts.phrase("Animal", "Back", "{oops}"), "Animal at the Back")       # bad template: fallback
        with self.assertRaises(ValueError):
            tts.check_template("{object} at {where}")
        tts.check_template("Heads up: {object} by the {camera}")

    def test_wyoming_and_cache(self):
        reqs = []
        srv = fake_piper(reqs)
        port = srv.getsockname()[1]
        try:
            data = tts.synthesize("Person at the Front door", "en_US-amy-medium", port=port)
            w = wave.open(io.BytesIO(data))
            self.assertEqual((w.getframerate(), w.getsampwidth(), w.getnchannels(), w.getnframes()), (22050, 2, 1, 1500))
            self.assertEqual(reqs[0], ("synthesize", {"text": "Person at the Front door", "voice": {"name": "en_US-amy-medium"}}))
            self.assertEqual(tts.status(port=port), {"ok": True, "voices": ["en_US-lessac-medium"]})
            with tempfile.TemporaryDirectory() as stack:
                synth = lambda text, voice: tts.synthesize(text, voice, port=port)  # noqa: E731
                n = len(reqs)
                a = tts.wav(stack, "Hello", "v", synth)
                b = tts.wav(stack, "Hello", "v", synth)                   # second time from the cache
                self.assertEqual(a, b)
                self.assertEqual(len(reqs), n + 1)
                cams = [{"name": "Front door", "doorbell": True}, {"name": "Driveway"}]
                made = tts.prewarm(stack, cams, {"sounds": {"doorbell": True, "person": True}, "voice": "v"}, synth)
                self.assertEqual(made, 3)                                 # doorbell + person at Front door, person at Driveway
        finally:
            srv.close()
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        unused = free.getsockname()[1]
        free.close()
        self.assertEqual(tts.status(port=unused)["ok"], False)                # nobody listening: not running

    def test_profile_and_voice_in_env(self):
        import solar_setup as ss
        with tempfile.TemporaryDirectory() as stack:
            os.makedirs(os.path.join(stack, "state"))
            self.assertNotIn("tts", ss.env_profiles(stack))
            json.dump({"speak": True, "voice": "en_US-ryan-medium"}, open(os.path.join(stack, "state", "display.json"), "w"))
            self.assertIn("tts", ss.env_profiles(stack))
            self.assertEqual(ss.env_extra(stack), ["TTS_VOICE=en_US-ryan-medium"])
            json.dump({"speak": True, "voice": "x; rm -rf /"}, open(os.path.join(stack, "state", "display.json"), "w"))
            self.assertEqual(ss.env_extra(stack), [])


if __name__ == "__main__":
    unittest.main()


class Progress(unittest.TestCase):
    def test_pull_progress_from_docker_stream(self):
        import io
        lines = [{"status": "Pulling fs layer", "id": "a"}, {"status": "Pulling fs layer", "id": "b"},
                 {"status": "Downloading", "id": "a", "progressDetail": {"current": 10, "total": 100}},
                 {"status": "Downloading", "id": "b", "progressDetail": {"current": 50, "total": 300}},
                 {"status": "Download complete", "id": "a"}, {"status": "Extracting", "id": "a", "progressDetail": {"current": 5, "total": 100}},
                 {"status": "Downloading", "id": "b", "progressDetail": {"current": 300, "total": 300}}, {"status": "Pull complete", "id": "b"}]

        class Resp(io.BytesIO):
            status = 200
        orig = tts._docker
        tts._docker = lambda m, p, timeout=30: Resp(b"\n".join(json.dumps(x).encode() for x in lines) + b"\n")
        seen = []
        try:
            tts.pull_image("rhasspy/wyoming-piper:2.5.4", lambda d, t, x: seen.append((d, t, x)))
        finally:
            tts._docker = orig
        self.assertEqual(seen[3], (60, 400, False))       # after both layers started downloading
        self.assertEqual(seen[-1], (400, 400, True))
        tts._docker = lambda m, p, timeout=30: Resp(json.dumps({"error": "manifest unknown"}).encode() + b"\n")
        try:
            with self.assertRaisesRegex(OSError, "manifest unknown"):
                tts.pull_image("x:1", lambda *a: None)
        finally:
            tts._docker = orig

    def test_prewarm_reports_progress(self):
        with tempfile.TemporaryDirectory() as stack:
            got = []
            tts.prewarm(stack, [{"name": "Driveway"}, {"name": "Front door", "doorbell": True}],
                        {"sounds": {"person": True, "doorbell": True}, "voice": "v"}, synth=lambda t, v: b"RIFF",
                        progress=lambda m, t: got.append((m, t)))
            self.assertEqual(got, [(1, 3), (2, 3), (3, 3)])

    def test_bar_html(self):
        import solar_web
        h = solar_web.speech_progress_html({"active": True, "step": "Downloading the voice", "pct": 42, "detail": "26 MB of about 63 MB", "error": ""})
        self.assertIn("<progress max='100' value='42'", h)
        self.assertIn("/speech/progress", h)
        self.assertIn("stopped: no network", solar_web.speech_progress_html({"active": False, "error": "no network", "step": "", "pct": None, "detail": ""}))
