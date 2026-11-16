#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "muse_status.py"


class MuseStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        (self.data / "logs").mkdir(parents=True)
        media = Path(self.tmp.name) / "rec"
        media.mkdir()
        self.media = media / "seg_0001.ts"
        self.media.write_bytes(b"x" * 4096)
        now = int(time.time() * 1000)
        db = {
            "settings": {
                "splitSeconds": 600,
                "autoRetryRecord": True,
                "restrictedRoomFallback": True,
                "maxConcurrentRecordings": 5,
                "diskLimitGb": 1,
                "savePath": str(media),
            },
            "anchors": [{"platform": "pandalive", "userId": "u1"}],
        }
        (self.data / "db.json").write_text(json.dumps(db), encoding="utf-8")
        cp = {
            "v": 1,
            "rooms": {
                "pandalive:u1": {
                    "version": 1,
                    "room": "pandalive:u1",
                    "platform": "pandalive",
                    "userId": "u1",
                    "nick": "U1",
                    "title": "t",
                    "dirPath": str(media),
                    "baseName": "U1",
                    "startedAt": now - 60000,
                    "updatedAt": now,
                    "segIndex": 1,
                    "files": [str(self.media)],
                    "bytes": 4096,
                    "lastMediaAt": now - 60_000,
                    "auto": True,
                    "phase": "retrying",
                    "retryStreak": 12,
                    "nextRetryAt": now + 120_000,
                    "gapStart": now - 60_000,
                    "gapReason": "interrupted",
                    "lastVariantUrl": "https://cdn.example/live.m3u8?token=SUPER_SECRET",
                    "dlHeaders": {"Cookie": "COOKIE_SECRET"},
                    "roomType": "fan",
                    "sourceExpiresAt": 1,
                }
            },
        }
        (self.data / "rec-checkpoints.json").write_text(json.dumps(cp), encoding="utf-8")
        (self.data / "lkg-sources.json").write_text(json.dumps({
            "rooms": {"pandalive:u1": {"variants": [{"url": "https://secret.example/x?token=SECRET"}], "expiresAt": 1}}
        }), encoding="utf-8")
        (self.data / "logs" / "app-20260101.log").write_text(
            "[x] [WARN] [rec] GET https://secret.example/live.m3u8?token=SUPER_SECRET failed\n"
            "[x] [ERROR] [rec] Cookie=COOKIE_SECRET Authorization: Bearer eyJaaaaaaaaaa.bbbbbbbbbb.cccccccccc\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, *extra):
        p = subprocess.run(
            [sys.executable, str(TOOL), "--data", str(self.data), "--json", "--sample", "0", *extra],
            capture_output=True, text=True, timeout=30,
        )
        return p, json.loads(p.stdout)

    def test_status_and_redaction(self):
        p, status = self.run_tool()
        self.assertEqual(p.returncode, 0)
        self.assertEqual(len(status["checkpoints"]), 1)
        c = status["checkpoints"][0]
        self.assertEqual(c["phase"], "retrying")
        self.assertEqual(c["health"], "retrying")
        self.assertEqual(c["retryStreak"], 12)
        self.assertEqual(c["roomType"], "fan")
        self.assertGreaterEqual(c["gapCount"], 1)
        self.assertTrue(any(a["code"] == "slow-probe" for a in status["alerts"]))
        self.assertEqual(status["settings"]["anchors"], 1)
        self.assertEqual(status["lkg"]["rooms"], 1)
        blob = json.dumps(status, ensure_ascii=False)
        for secret in ("SUPER_SECRET", "COOKIE_SECRET", "secret.example", "eyJaaaaaaaaaa"):
            self.assertNotIn(secret, blob)
        self.assertNotIn("lastVariantUrl", blob)
        self.assertNotIn("dlHeaders", blob)

    def test_strict_exit_on_stalled(self):
        cp = json.loads((self.data / "rec-checkpoints.json").read_text())
        cp["rooms"]["pandalive:u1"]["phase"] = "recording"
        cp["rooms"]["pandalive:u1"]["lastMediaAt"] = int(time.time() * 1000) - 400_000
        (self.data / "rec-checkpoints.json").write_text(json.dumps(cp), encoding="utf-8")
        p, status = self.run_tool("--strict")
        self.assertEqual(p.returncode, 2)
        self.assertTrue(any(a["code"] == "stalled" for a in status["alerts"]))

    def test_missing_data(self):
        missing = Path(self.tmp.name) / "missing"
        p = subprocess.run(
            [sys.executable, str(TOOL), "--data", str(missing), "--json", "--sample", "0", "--strict"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(p.returncode, 2)
        status = json.loads(p.stdout)
        self.assertTrue(any(a["code"] == "data-missing" for a in status["alerts"]))


if __name__ == "__main__":
    unittest.main()
