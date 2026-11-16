#!/usr/bin/env python3
"""Read-only status probe for Live.Monitor. No writes, no network, no secrets.

Usage:
  python3 tools/muse_status.py --data /path/to/Live.Monitor/data --json --sample 5

The output intentionally omits:
  - lastVariantUrl / dlHeaders / cookies / tokens / full signed URLs
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

REC_FAST_RETRY_MAX = 10
DISK_WARN_GB = 2.0
DISK_CRITICAL_GB = 0.5
STALL_WATCH_SEC = 120
STALL_CRITICAL_SEC = 300


def now_ms() -> int:
    return int(time.time() * 1000)


def default_data_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "pandalive-monitor" / "data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "pandalive-monitor" / "data"
    return Path.home() / ".config" / "pandalive-monitor" / "data"


def read_json(path: Path, default):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def redact(text: str) -> str:
    s = str(text or "")
    # full signed URL -> scheme/host + redacted path
    s = re.sub(r"https?://[^\s]+", "<redacted-url>", s, flags=re.I)
    # query/token/cookie style values
    s = re.sub(
        r"(?i)\b(token|access_token|authorization|cookie|sesskey|password|passwd|ticket|authticket|userticket)=([^\s&;]+)",
        r"\1=<redacted>",
        s,
    )
    # JWT-like strings
    s = re.sub(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b", "<redacted-jwt>", s)
    return s


def disk_free_gb(path: Path) -> float | None:
    try:
        probe = path if path.exists() else path.parent
        usage = shutil.disk_usage(str(probe))
        return round(usage.free / (1024 ** 3), 2)
    except Exception:
        return None


def file_stats(files: list[str]) -> tuple[int, int]:
    total = 0
    existing = 0
    for f in files:
        try:
            st = Path(f).stat()
            if st.st_size >= 0:
                total += st.st_size
                existing += 1
        except OSError:
            continue
    return total, existing


def latest_log_file(data_dir: Path) -> Path | None:
    logs = data_dir / "logs"
    if not logs.is_dir():
        return None
    candidates = sorted(logs.glob("app-*.log"), key=lambda p: p.stat().st_mtime if p.exists() else 0)
    return candidates[-1] if candidates else None


def read_recent_logs(data_dir: Path, limit: int) -> list[str]:
    p = latest_log_file(data_dir)
    if not p:
        return []
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-max(1, limit):]
    except OSError:
        return []
    out: list[str] = []
    for line in lines:
        line = line.rstrip("\n\r")
        if re.search(r"\[(WARN|ERROR)\]", line):
            out.append(redact(line))
    return out[-max(1, limit // 2):]


def checkpoint_rows(data_dir: Path, sample_sec: int) -> tuple[list[dict], list[dict]]:
    raw = read_json(data_dir / "rec-checkpoints.json", {})
    rooms = raw.get("rooms", {}) if isinstance(raw, dict) else {}
    rows: list[dict] = []
    alerts: list[dict] = []
    now = now_ms()
    for key, cp in rooms.items():
        if not isinstance(cp, dict):
            continue
        files = [str(f) for f in (cp.get("files") or []) if isinstance(f, str)]
        size1, existing1 = file_stats(files)
        if sample_sec > 0:
            time.sleep(sample_sec)
            size2, existing2 = file_stats(files)
            growth = size2 - size1
        else:
            size2, existing2 = size1, existing1
            growth = 0
        last = int(cp.get("lastMediaAt") or 0)
        age = max(0, (now - last) // 1000) if last else None
        phase = str(cp.get("phase") or "unknown")
        if phase == "retrying":
            health = "retrying"
        elif phase == "recording":
            if age is None or age > STALL_CRITICAL_SEC:
                health = "stalled"
            elif age > STALL_WATCH_SEC:
                health = "watch"
            elif sample_sec > 0 and growth <= 0:
                health = "no-growth"
            else:
                health = "recording"
        else:
            health = phase
        next_at = int(cp.get("nextRetryAt") or 0)
        next_in = max(0, next_at - now) if next_at else 0
        gaps = cp.get("gaps") if isinstance(cp.get("gaps"), list) else []
        gap_count = len(gaps) or (1 if cp.get("gapStart") else 0)
        row = {
            "room": key,
            "platform": cp.get("platform"),
            "userId": cp.get("userId"),
            "nick": cp.get("nick"),
            "phase": phase,
            "health": health,
            "segIndex": cp.get("segIndex"),
            "files": existing2,
            "bytes": size2,
            "fileGrowthBytes": growth,
            "lastMediaAgeSec": age,
            "retryStreak": cp.get("retryStreak") or 0,
            "nextRetryInMs": next_in,
            "gapCount": gap_count,
            "roomType": cp.get("roomType") or "unknown",
            "sourceExpiresAt": cp.get("sourceExpiresAt"),
            "diskFreeGb": disk_free_gb(Path(str(cp.get("dirPath") or data_dir))),
        }
        rows.append(row)
        if health == "stalled":
            alerts.append({"severity": "critical", "room": key, "code": "stalled",
                           "message": f"no media progress for {age if age is not None else 'unknown'}s"})
        elif health == "no-growth":
            alerts.append({"severity": "warning", "room": key, "code": "no-file-growth",
                           "message": "recording phase but file size did not grow during sample"})
        if phase == "retrying":
            if int(cp.get("retryStreak") or 0) > REC_FAST_RETRY_MAX:
                alerts.append({"severity": "info", "room": key, "code": "slow-probe",
                               "message": "fast retry cap exceeded; slow probe (every 5 min)"})
            if next_at and next_in > 6 * 60_000:
                alerts.append({"severity": "warning", "room": key, "code": "retry-delayed",
                               "message": f"next retry in {next_in // 1000}s"})
        exp = cp.get("sourceExpiresAt")
        if isinstance(exp, (int, float)) and exp > 0 and exp * 1000 < now:
            alerts.append({"severity": "warning", "room": key, "code": "token-expired",
                           "message": "current media token looks expired"})
        if row["diskFreeGb"] is not None and row["diskFreeGb"] < DISK_CRITICAL_GB:
            alerts.append({"severity": "critical", "room": key, "code": "disk-critical",
                           "message": f"free disk {row['diskFreeGb']}GB"})
        elif row["diskFreeGb"] is not None and row["diskFreeGb"] < DISK_WARN_GB:
            alerts.append({"severity": "warning", "room": key, "code": "disk-low",
                           "message": f"free disk {row['diskFreeGb']}GB"})
    return rows, alerts


def lkg_summary(data_dir: Path) -> dict:
    raw = read_json(data_dir / "lkg-sources.json", {})
    rooms = raw.get("rooms", {}) if isinstance(raw, dict) else {}
    now = int(time.time())
    expired = 0
    tiers = 0
    for entry in rooms.values():
        if not isinstance(entry, dict):
            continue
        variants = entry.get("variants") if isinstance(entry.get("variants"), list) else []
        tiers += len(variants)
        exp = entry.get("expiresAt")
        if isinstance(exp, (int, float)) and exp > 0 and exp < now:
            expired += 1
    return {"rooms": len(rooms), "tiers": tiers, "expiredEntries": expired}


def settings_summary(data_dir: Path) -> dict:
    db = read_json(data_dir / "db.json", {})
    settings = db.get("settings", {}) if isinstance(db, dict) else {}
    anchors = db.get("anchors", []) if isinstance(db, dict) else []
    return {
        "anchors": len(anchors) if isinstance(anchors, list) else 0,
        "splitSeconds": settings.get("splitSeconds"),
        "autoRetryRecord": settings.get("autoRetryRecord"),
        "restrictedRoomFallback": settings.get("restrictedRoomFallback"),
        "maxConcurrentRecordings": settings.get("maxConcurrentRecordings"),
        "diskLimitGb": settings.get("diskLimitGb"),
        "savePathSet": bool(settings.get("savePath")),
    }


def build_status(data_dir: Path, sample_sec: int, log_lines: int) -> dict:
    data_dir = data_dir.expanduser().resolve()
    alerts: list[dict] = []
    if not data_dir.exists():
        alerts.append({"severity": "critical", "room": "-", "code": "data-missing",
                       "message": f"data directory not found: {data_dir}"})
        cps: list[dict] = []
        logs: list[str] = []
    else:
        cps, cp_alerts = checkpoint_rows(data_dir, sample_sec)
        alerts.extend(cp_alerts)
        logs = read_recent_logs(data_dir, log_lines)
        for line in logs:
            if re.search(r"(?i)ENOSPC|No space left|Read-only file system|Permission denied|EACCES", line):
                alerts.append({"severity": "critical", "room": "-", "code": "disk-write",
                               "message": line[:300]})
    return {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S%z"),
        "dataDir": str(data_dir),
        "sampleSeconds": sample_sec,
        "diskFreeGb": disk_free_gb(data_dir),
        "checkpoints": cps,
        "lkg": lkg_summary(data_dir) if data_dir.exists() else {"rooms": 0, "tiers": 0, "expiredEntries": 0},
        "settings": settings_summary(data_dir) if data_dir.exists() else {},
        "alerts": alerts,
        "recentLogs": logs,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only Live.Monitor status for Muse")
    ap.add_argument("--data", type=Path, default=None, help="Live.Monitor data directory")
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--sample", type=int, default=5, help="file-growth sample seconds (0=skip)")
    ap.add_argument("--log-lines", type=int, default=200, help="tail lines to scan")
    ap.add_argument("--strict", action="store_true", help="exit 2 if any critical alert")
    args = ap.parse_args(argv)
    data_dir = args.data or default_data_dir()
    sample = max(0, min(60, int(args.sample)))
    status = build_status(data_dir, sample, max(10, int(args.log_lines)))
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        print(f"Live.Monitor status @ {status['generatedAt']}")
        print(f"data: {status['dataDir']}  diskFree: {status['diskFreeGb']} GB")
        print(f"checkpoints: {len(status['checkpoints'])}  alerts: {len(status['alerts'])}")
        for c in status["checkpoints"]:
            print(f"- {c['room']} {c['health']} seg={c['segIndex']} "
                  f"lastMediaAge={c['lastMediaAgeSec']}s growth={c['fileGrowthBytes']}B "
                  f"retry={c['retryStreak']} next={c['nextRetryInMs']}ms gaps={c['gapCount']} "
                  f"room={c['roomType']}")
        for a in status["alerts"]:
            print(f"! [{a['severity']}] {a['code']} {a['room']}: {a['message']}")
        if status["recentLogs"]:
            print("recent warnings/errors:")
            for line in status["recentLogs"][-20:]:
                print("  " + line)
    if args.strict and any(a.get("severity") == "critical" for a in status["alerts"]):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
