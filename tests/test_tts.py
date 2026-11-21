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
