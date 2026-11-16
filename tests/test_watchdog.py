#!/usr/bin/env python3
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("muse_watchdog", ROOT / "tools" / "muse_watchdog.py")
wd = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(wd)


class WatchdogUnitTests(unittest.TestCase):
    def test_absolutize_playlist(self):
        body = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:2\n"
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
            "#EXTINF:2,\nseg1.ts\n#EXTINF:2,\n../seg2.ts\n"
        )
        out = wd.absolutize_playlist(body, "https://cdn.example/live/index.m3u8")
        self.assertIn("https://cdn.example/live/seg1.ts", out)
        self.assertIn("https://cdn.example/seg2.ts", out)
        self.assertIn('URI="https://cdn.example/live/key.bin"', out)

    def test_safe_name_and_segment_index(self):
        self.assertEqual(wd.safe_name('a/b:c*'), "abc")
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "Show(u1)_0001.ts").write_bytes(b"a")
            (d / "Show(u1)_0003.ts").write_bytes(b"bb")
            (d / "Show(u1).mp4").write_bytes(b"c")
            room = wd.Room({"id": "pandalive:u1", "platform": "pandalive", "userId": "u1", "nick": "Show"}, 0)
            t = wd.Task(room, d.parent, 600, 60)
            t.dir = d
            t.ensure_base()
            self.assertEqual(t.seg_index, 3)
            self.assertEqual(t.base, "Show(u1)")

    def test_lkg_quality_selection(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "lkg-sources.json"
            p.write_text(json.dumps({"rooms": {"pandalive:u1": {
                "variants": [
                    {"url": "https://a/1080.m3u8", "bandwidth": 3000, "resolution": "1920x1080"},
                    {"url": "https://a/720.m3u8", "bandwidth": 2000, "resolution": "1280x720"},
                ],
                "dlHeaders": {"Referer": "https://www.pandalive.co.kr/"},
            }}}), encoding="utf-8")
            hit = wd.choose_lkg_variant(p, "pandalive", "u1", "1280x720")
            self.assertIsNotNone(hit)
            self.assertEqual(hit[0], "https://a/720.m3u8")
            high = wd.choose_lkg_variant(p, "pandalive", "u1", "highest")
            self.assertEqual(high[0], "https://a/1080.m3u8")

    def test_process_identity(self):
        self.assertTrue(wd.pid_alive(os.getpid()))
        self.assertGreater(wd.read_start_ms(os.getpid()), 0)


class SourceManagerTests(unittest.TestCase):
    def test_refresh_saves_absolutized_snapshot_and_reuses_it(self):
        count = {"n": 0}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                count["n"] += 1
                body = b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nseg.ts\n"
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        srv = HTTPServer(("127.0.0.1", 0), Handler)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                state = Path(td)
                url = f"http://127.0.0.1:{srv.server_port}/live.m3u8?token=SECRET"
                cfg = {"refresh_seconds": 600, "fetch_timeout_seconds": 5}
                rooms = [wd.Room({"id": "pandalive:u1", "platform": "pandalive", "userId": "u1", "nick": "U1",
                                  "source": {"type": "m3u8", "url": url}}, 0)]
                sm = wd.SourceManager(cfg, state, lambda _m: None)
                snap, headers, stale, _url = sm.refresh(rooms[0])
                self.assertIsNotNone(snap)
                self.assertFalse(stale)
                body = snap.read_text()
                self.assertIn(f"http://127.0.0.1:{srv.server_port}/seg.ts", body)
                self.assertEqual(count["n"], 1)
                # Fresh snapshot inside 600s is reused; no second upstream request.
                snap2, _, stale2, _url2 = sm.refresh(rooms[0])
                self.assertEqual(snap2, snap)
                self.assertFalse(stale2)
                self.assertEqual(count["n"], 1)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
