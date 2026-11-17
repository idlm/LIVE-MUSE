#!/usr/bin/env python3
"""Independent watchdog for Muse/Live.Monitor-style HLS recording.

Why a separate process:
  Muse may reset/restart at any time. This watchdog owns ffmpeg processes and
  persists state on disk, so a Muse restart does not lose tasks or duplicate them.

Core behavior:
  - single instance via an OS file lock
  - refresh and save m3u8 snapshots at most once per refresh_seconds (default 600)
  - on startup, start enabled rooms strictly sequentially
  - resume from max existing segment + 1, never overwrite
  - fast retry, then slow retry; explicit source failure is not "offline"
  - no cookie/token/signed-URL output in logs

Source types:
  m3u8   : static URL in config
  command: run an argv list (no shell) that prints a fresh m3u8 URL on stdout
  lkg    : read lkg-sources.json produced by Live.Monitor and pick a valid variant

Usage:
  python3 tools/muse_watchdog.py --config config/muse.rooms.json
  python3 tools/muse_watchdog.py --config ... --refresh-only
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
ERR_RE = re.compile(
    r"(?i)no space left|enospc|read-only file system|erofs|permission denied|eacces"
)


def now_ms() -> int:
    return int(time.time() * 1000)


def redact(s: str) -> str:
    s = re.sub(r"https?://[^\s\"']+", "<redacted-url>", str(s or ""))
    s = re.sub(
        r"(?i)\b(token|access_token|authorization|cookie|sesskey|password|ticket)=([^\s&;\"']+)",
        r"\1=<redacted>",
        s,
    )
    s = re.sub(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b", "<redacted-jwt>", s)
    return s


def safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f%]', "", str(value or "")).strip(" .")
    return (cleaned or "unknown")[:120]


def atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(data, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def absolutize_playlist(text: str, base_url: str) -> str:
    """Make relative URIs absolute so ffmpeg can read a local snapshot."""
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not raw.strip():
            out.append(raw)
            continue
        if raw.startswith("#"):
            # #EXT-X-KEY:URI="...", #EXT-X-MAP:URI="...", #EXT-X-MEDIA:...,URI="..."
            def repl(m: re.Match[str]) -> str:
                uri = m.group(2)
                try:
                    return f'{m.group(1)}{urllib.parse.urljoin(base_url, uri)}"'
                except Exception:
                    return m.group(0)

            out.append(re.sub(r'(URI=")([^"]*)"', repl, raw))
            continue
        try:
            out.append(urllib.parse.urljoin(base_url, raw.strip()))
        except Exception:
            out.append(raw)
    return "\n".join(out) + "\n"


def fetch_text(url: str, headers: dict[str, str], timeout: float) -> str:
    req = urllib.request.Request(url, headers={**headers, "User-Agent": headers.get("User-Agent", DEFAULT_UA)})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if getattr(resp, "status", 200) >= 400:
            raise RuntimeError(f"http-{resp.status}")
        data = resp.read(2 * 1024 * 1024)
    text = data.decode("utf-8", errors="replace")
    if "#EXTM3U" not in text:
        raise ValueError("not-m3u8")
    return text


def choose_lkg_variant(lkg_file: Path, platform: str, user_id: str, quality: str) -> tuple[str, dict[str, str]] | None:
    raw = load_json(lkg_file, {})
    rooms = raw.get("rooms", {}) if isinstance(raw, dict) else {}
    entry = rooms.get(f"{platform}:{user_id}")
    if not isinstance(entry, dict):
        return None
    variants = entry.get("variants") if isinstance(entry.get("variants"), list) else []
    candidates = [v for v in variants if isinstance(v, dict) and isinstance(v.get("url"), str)]
    now = time.time()
    valid = [v for v in candidates if not (isinstance(v.get("expiresAt"), (int, float)) and v["expiresAt"] > 0 and v["expiresAt"] < now)]
    if not valid:
        valid = candidates
    if not valid:
        url = entry.get("m3u8")
        return (url, entry.get("dlHeaders") or {}) if isinstance(url, str) and url else None
    wanted = str(quality or "highest")
    if wanted != "highest":
        match = [v for v in valid if str(v.get("resolution") or "") == wanted]
        if match:
            return match[0]["url"], entry.get("dlHeaders") or {}
    best = max(valid, key=lambda v: float(v.get("bandwidth") or 0))
    return best["url"], entry.get("dlHeaders") or {}


class Room:
    def __init__(self, raw: dict[str, Any], index: int):
        self.room_id = str(raw.get("id") or f"room{index}")
        self.platform = str(raw.get("platform") or "pandalive")
        self.user_id = str(raw.get("userId") or self.room_id.split(":", 1)[-1])
        self.nick = str(raw.get("nick") or self.user_id)
        self.enabled = bool(raw.get("enabled", True))
        self.quality = str(raw.get("quality") or "highest")
        self.source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
        self.extra_headers = self.source.get("headers") if isinstance(self.source.get("headers"), dict) else {}
        self.key = f"{self.platform}:{self.user_id}"

    def snapshot_path(self, state_dir: Path) -> Path:
        return state_dir / "sources" / f"{safe_name(self.key)}.m3u8"

    def meta_path(self, state_dir: Path) -> Path:
        return state_dir / "sources" / f"{safe_name(self.key)}.json"


class SourceManager:
    def __init__(self, config: dict[str, Any], state_dir: Path, log):
        self.config = config
        self.state_dir = state_dir
        self.log = log
        self.refresh_seconds = int(config.get("refresh_seconds", 600))
        self.timeout = float(config.get("fetch_timeout_seconds", 20))

    def resolve_url(self, room: Room) -> tuple[str, dict[str, str]] | None:
        src = room.source
        kind = str(src.get("type") or "m3u8")
        try:
            if kind == "m3u8":
                url = str(src.get("url") or "")
                return (url, room.extra_headers) if url else None
            if kind == "command":
                cmd = src.get("command")
                if not isinstance(cmd, list) or not cmd:
                    return None
                p = subprocess.run([str(x) for x in cmd], capture_output=True, text=True,
                                   timeout=float(src.get("timeout", self.timeout)))
                if p.returncode != 0:
                    self.log(f"source command failed for {room.key}: exit={p.returncode}")
                    return None
                url = p.stdout.strip().splitlines()[0].strip() if p.stdout.strip() else ""
                return (url, room.extra_headers) if url else None
            if kind in {"lkg", "live_monitor"}:
                lkg_file = Path(str(src.get("lkg_file") or Path(str(src.get("data_dir") or "")) / "lkg-sources.json"))
                hit = choose_lkg_variant(lkg_file, room.platform, room.user_id, room.quality)
                if not hit:
                    return None
                url, headers = hit
                return url, {**room.extra_headers, **(headers or {})}
        except Exception as e:
            self.log(f"source resolve failed for {room.key}: {type(e).__name__}")
        return None

    def snapshot_age(self, room: Room) -> float | None:
        meta = load_json(room.meta_path(self.state_dir), {})
        at = meta.get("fetched_at_ms") if isinstance(meta, dict) else None
        return None if not isinstance(at, (int, float)) else max(0, (now_ms() - at) / 1000)

    def refresh(self, room: Room) -> tuple[Path | None, dict[str, str], bool, str]:
        """Return (snapshot, headers, stale). Reuses a snapshot younger than refresh_seconds."""
        age = self.snapshot_age(room)
        snap = room.snapshot_path(self.state_dir)
        if age is not None and age <= self.refresh_seconds and snap.exists():
            meta = load_json(room.meta_path(self.state_dir), {})
            return snap, meta.get("headers") or {}, False, ""
        hit = self.resolve_url(room)
        if not hit:
            if snap.exists():
                self.log(f"source refresh failed for {room.key}; using stale snapshot ({int(age or 0)}s old)")
                meta = load_json(room.meta_path(self.state_dir), {})
                return snap, meta.get("headers") or {}, True, ""
            self.log(f"source refresh failed for {room.key}: no snapshot")
            return None, {}, True, ""
        url, headers = hit
        try:
            text = fetch_text(url, headers, self.timeout)
            body = absolutize_playlist(text, url)
            atomic_write(snap, body)
            atomic_write(room.meta_path(self.state_dir), json.dumps({
                "room": room.key, "fetched_at_ms": now_ms(),
                "sha256": hashlib.sha256(body.encode()).hexdigest(),
                "headers": headers, "quality": room.quality,
            }, ensure_ascii=False, indent=1))
            self.log(f"source saved for {room.key}: {len(body)} bytes")
            return snap, headers, False, url
        except Exception as e:
            if snap.exists():
                self.log(f"source fetch failed for {room.key} ({type(e).__name__}); using stale snapshot")
                meta = load_json(room.meta_path(self.state_dir), {})
                return snap, meta.get("headers") or {}, True, ""
            self.log(f"source fetch failed for {room.key}: {type(e).__name__}")
            return None, {}, True, ""


class Task:
    def __init__(self, room: Room, root: Path, segment_seconds: int, stall_seconds: int):
        self.room = room
        self.root = root
        self.segment_seconds = max(60, int(segment_seconds))
        self.stall_seconds = max(10, int(stall_seconds))
        self.dir = root / room.platform / f"{safe_name(room.nick)}({safe_name(room.user_id)})"
        self.base = ""
        self.seg_index = 0
        self.pid = 0
        self.proc_start = 0
        self.proc: subprocess.Popen | None = None
        self.last_bytes = 0
        self.last_media_at = 0
        self.streak = 0
        self.next_retry_at = 0
        self.last_error = ""

    def scan(self) -> tuple[list[Path], int]:
        if not self.dir.exists():
            return [], 0
        files = sorted([p for p in self.dir.iterdir() if p.is_file() and re.search(r"_\d{4,}\.(ts|mp4)$", p.name)])
        max_idx = 0
        for p in files:
            m = re.search(r"_(\d{4,})\.(ts|mp4)$", p.name)
            if m:
                max_idx = max(max_idx, int(m.group(1)))
        return files, max_idx

    def ensure_base(self) -> None:
        if self.base:
            return
        files, max_idx = self.scan()
        if files:
            first = files[0].name
            self.base = re.sub(r"_\d{4,}\.(ts|mp4)$", "", first)
            self.seg_index = max_idx
        else:
            self.base = f"{safe_name(self.room.nick)}_{safe_name(self.room.user_id)}_{time.strftime('%Y%m%d_%H%M%S')}"
            self.seg_index = 0
            self.dir.mkdir(parents=True, exist_ok=True)

    def pattern(self) -> Path:
        return self.dir / f"{self.base}_%04d.ts"

    def start(self, snapshot: Path, headers: dict[str, str], ffmpeg: str, user_agent: str, proxy: str = "", source_url: str = "") -> bool:
        self.ensure_base()
        self.dir.mkdir(parents=True, exist_ok=True)
        files, max_idx = self.scan()
        self.seg_index = max(self.seg_index, max_idx)
        hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items() if isinstance(v, str))
        # A local .m3u8 snapshot is opened through the file protocol: ffmpeg rejects
        # HTTP-only options such as -user_agent/-headers on it. Those options are
        # only valid when the input itself is remote. Segment URLs inside the
        # snapshot are absolute and are fetched by the HLS demuxer.
        input_target = source_url if source_url else str(snapshot)
        remote_input = input_target.lower().startswith(("http://", "https://"))
        args = [
            ffmpeg, "-y", "-loglevel", "error", "-hide_banner",
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-fflags", "+discardcorrupt", "-analyzeduration", "20000000",
            "-probesize", "10000000", "-thread_queue_size", "512",
        ]
        # HTTP-only options must not be attached to a local file input.
        if remote_input:
            args += [
                "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_at_eof", "1",
                "-reconnect_delay_max", "15", "-rw_timeout", "50000000",
                "-user_agent", user_agent,
            ]
            if hdr:
                args += ["-headers", hdr]
            if proxy and not re.match(r"^https?://(127\.0\.0\.1|localhost|\[::1\])", str(snapshot)):
                args += ["-http_proxy", proxy]
        args += [
            "-i", input_target, "-map", "0", "-c", "copy",
            "-max_muxing_queue_size", "1024", "-correct_ts_overflow", "1", "-avoid_negative_ts", "1",
            "-f", "segment", "-segment_time", str(self.segment_seconds), "-segment_format", "mpegts",
            "-segment_start_number", str(self.seg_index + 1), "-reset_timestamps", "1",
            str(self.pattern()),
        ]
        try:
            self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE, start_new_session=True)
        except OSError as e:
            self.last_error = type(e).__name__
            return False
        self.pid = int(self.proc.pid or 0)
        self.proc_start = read_start_ms(self.pid) or 0
        self.last_media_at = now_ms()
        self.last_bytes = 0
        self.streak = 0
        self.next_retry_at = 0
        return True

    def current_bytes(self) -> int:
        total = 0
        for p in self.scan()[0]:
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return total

    def check(self) -> str:
        """Return recording|stalled|exited|external."""
        external = False
        if self.proc is not None:
            if self.proc.poll() is not None:
                self.last_error = f"ffmpeg exit {self.proc.returncode}"
                return "exited"
        elif self.pid and pid_alive(self.pid):
            external = True
        elif self.pid:
            self.last_error = "process gone"
            return "exited"
        size = self.current_bytes()
        if size > self.last_bytes:
            self.last_bytes = size
            self.last_media_at = now_ms()
        if self.last_media_at and now_ms() - self.last_media_at > self.stall_seconds * 1000:
            return "stalled"
        return "external" if external else "recording"

    def stop(self, timeout: float = 10.0) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.write(b"q")
                    self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=timeout)
                return
            except subprocess.TimeoutExpired:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        elif self.pid and same_process(self.pid, self.proc_start):
            try:
                os.kill(self.pid, signal.SIGINT)
            except OSError:
                pass


def read_start_ms(pid: int) -> int:
    if not pid:
        return 0
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            end = stat.rfind(")")
            fields = stat[end + 2:].split()
            ticks = int(fields[19])
            btime = 0
            for line in Path("/proc/stat").read_text().splitlines():
                if line.startswith("btime "):
                    btime = int(line.split()[1])
                    break
            hz = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
            return int(btime * 1000 + ticks * 1000 / hz)
        except Exception:
            return 0
    if sys.platform == "darwin":
        try:
            out = subprocess.check_output(["ps", "-o", "lstart=", "-p", str(pid)], text=True, timeout=3).strip()
            return int(time.mktime(time.strptime(out, "%a %b %d %H:%M:%S %Y")) * 1000)
        except Exception:
            return 0
    return 0


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        return e.errno == errno.EPERM


def same_process(pid: int, start_ms: int) -> bool:
    if not pid_alive(pid):
        return False
    if not start_ms:
        # We cannot verify; do not start a duplicate.
        return True
    now = read_start_ms(pid)
    return bool(now) and abs(now - start_ms) <= 5000


class Watchdog:
    def __init__(self, config: dict[str, Any], config_path: Path, state_dir: Path):
        self.config = config
        self.config_path = config_path
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log_path = state_dir / "watchdog.log"
        self.lock_path = state_dir / "watchdog.lock"
        self.state_path = state_dir / "tasks.json"
        self.sources = SourceManager(config, state_dir, self.log)
        self.rooms = [Room(r, i) for i, r in enumerate(config.get("rooms") or []) if isinstance(r, dict)]
        rec = config.get("recording") or {}
        self.root = Path(str(rec.get("root") or (state_dir / "recordings")))
        self.segment_seconds = int(rec.get("segment_seconds", 600))
        self.stall_seconds = int(config.get("stall_seconds", 60))
        self.refresh_seconds = int(config.get("refresh_seconds", 600))
        # Check snapshot age frequently, but only fetch when it exceeds refresh_seconds.
        # This keeps "about every 10 minutes" even after a Muse restart mid-window.
        self.source_check_seconds = max(10, min(60, int(config.get("source_check_seconds", 30))))
        # snapshot: ffmpeg reads the saved m3u8 (no HTTP headers; best for public HLS)
        # remote:   ffmpeg reads the freshly resolved URL (supports headers; snapshot remains LKG fallback)
        self.input_mode = str(config.get("input_mode", "snapshot")).lower()
        self.start_gap = float(config.get("start_gap_seconds", 3))
        self.fast_max = int(config.get("fast_retry_max", 10))
        self.fast_base = float(config.get("fast_retry_base_seconds", 10))
        self.fast_cap = float(config.get("fast_retry_cap_seconds", 300))
        self.slow_seconds = float(config.get("slow_retry_seconds", 300))
        self.slow_max = int(config.get("slow_retry_max", 288))
        self.ffmpeg = str(config.get("ffmpeg") or "ffmpeg")
        self.user_agent = str(config.get("user_agent") or DEFAULT_UA)
        self.proxy = str(config.get("proxy") or "")
        self.tasks: dict[str, Task] = {}
        self.stop_flag = False
        self._lock_fd = None
        self._state_loaded = False

    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {redact(msg)}"
        print(line, flush=True)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def acquire_lock(self) -> bool:
        try:
            import fcntl
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                self.log("another watchdog instance holds the lock; exiting")
                return False
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps({"pid": os.getpid(), "started_at": now_ms()}).encode())
            self._lock_fd = fd
            return True
        except ImportError:
            # Windows fallback: exclusive create; stale lock only acceptable when PID is gone.
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                os.write(fd, json.dumps({"pid": os.getpid(), "started_at": now_ms()}).encode())
                self._lock_fd = fd
                return True
            except FileExistsError:
                old = load_json(self.lock_path, {})
                if isinstance(old, dict) and pid_alive(int(old.get("pid") or 0)):
                    self.log("another watchdog instance appears alive; exiting")
                    return False
                try:
                    self.lock_path.unlink()
                except OSError:
                    pass
                return self.acquire_lock()

    def load_state(self) -> None:
        raw = load_json(self.state_path, {})
        for key, data in (raw.get("tasks") or {}).items():
            room = next((r for r in self.rooms if r.key == key), None)
            if not room:
                continue
            t = Task(room, self.root, self.segment_seconds, self.stall_seconds)
            for f in ("base", "seg_index", "pid", "proc_start", "last_bytes", "last_media_at",
                      "streak", "next_retry_at", "last_error"):
                if f in data:
                    setattr(t, f, data[f])
            self.tasks[key] = t
        self._state_loaded = True

    def save_state(self) -> None:
        data = {"version": 1, "updated_at": now_ms(), "tasks": {}}
        for key, t in self.tasks.items():
            data["tasks"][key] = {f: getattr(t, f) for f in (
                "base", "seg_index", "pid", "proc_start", "last_bytes", "last_media_at",
                "streak", "next_retry_at", "last_error")}
        atomic_write(self.state_path, json.dumps(data, ensure_ascii=False, indent=1))

    def refresh_all(self, force: bool = False) -> None:
        for room in self.rooms:
            if not room.enabled:
                continue
            if force:
                room.snapshot_path(self.state_dir).unlink(missing_ok=True)
            snap, headers, stale, _url = self.sources.refresh(room)
            if snap:
                self.tasks.setdefault(room.key, Task(room, self.root, self.segment_seconds, self.stall_seconds))
                self.tasks[room.key].last_error = "source-stale" if stale else ""

    def start_one(self, room: Room) -> bool:
        t = self.tasks.setdefault(room.key, Task(room, self.root, self.segment_seconds, self.stall_seconds))
        if same_process(t.pid, t.proc_start):
            self.log(f"{room.key}: previous ffmpeg still alive; attach without starting a duplicate")
            return True
        snap, headers, stale, source_url = self.sources.refresh(room)
        if not snap:
            t.last_error = "no-source"
            self.log(f"{room.key}: no m3u8 snapshot, skip")
            self.handle_failure(room.key)
            return False
        use_url = source_url if self.input_mode == "remote" else ""
        ok = t.start(snap, headers, self.ffmpeg, self.user_agent, self.proxy, use_url)
        self.log(f"{room.key}: {'started' if ok else 'start-failed'} seg>{t.seg_index} stale={stale}")
        if not ok:
            self.handle_failure(room.key)
        self.save_state()
        return ok

    def start_all_sequential(self) -> None:
        for room in self.rooms:
            if not room.enabled:
                continue
            self.start_one(room)
            time.sleep(self.start_gap)

    def next_delay(self, streak: int) -> float:
        if streak <= self.fast_max:
            return min(self.fast_cap, self.fast_base * (2 ** max(0, streak - 1)))
        return self.slow_seconds

    def handle_failure(self, key: str) -> None:
        t = self.tasks[key]
        if t.proc is not None:
            try:
                t.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            t.proc = None
        t.streak += 1
        if t.streak > self.fast_max + self.slow_max:
            t.next_retry_at = 0
            t.last_error = "retry-exhausted"
            self.log(f"{key}: retry exhausted after {t.streak} attempts")
            return
        delay = self.next_delay(t.streak)
        t.next_retry_at = now_ms() + int(delay * 1000)
        self.log(f"{key}: failure ({t.last_error}); retry in {int(delay)}s (streak {t.streak})")
        self.save_state()

    def loop(self) -> int:
        if not self.acquire_lock():
            return 1
        self.load_state()
        self.refresh_all()
        self.start_all_sequential()
        next_refresh = time.time() + self.source_check_seconds
        try:
            while not self.stop_flag:
                for key, t in list(self.tasks.items()):
                    if not t.room.enabled:
                        t.stop()
                        self.tasks.pop(key, None)
                        continue
                    state = t.check()
                    if state in {"stalled", "exited"}:
                        if state == "stalled":
                            self.log(f"{key}: stalled; stopping current ffmpeg")
                        t.stop()
                        self.handle_failure(key)
                    elif state == "external":
                        # adopt a surviving process; keep polling files
                        pass
                now = time.time()
                if now >= next_refresh:
                    self.refresh_all()
                    next_refresh = now + self.source_check_seconds
                for key, t in self.tasks.items():
                    if t.next_retry_at and now_ms() >= t.next_retry_at:
                        t.next_retry_at = 0
                        self.start_one(t.room)
                self.save_state()
                time.sleep(2)
        finally:
            for t in self.tasks.values():
                t.stop()
            self.save_state()
            if self._lock_fd is not None:
                try:
                    os.close(self._lock_fd)
                except OSError:
                    pass
                try:
                    self.lock_path.unlink()
                except OSError:
                    pass
        return 0


def load_config(path: Path) -> dict[str, Any]:
    data = load_json(path, None)
    if not isinstance(data, dict):
        raise ValueError(f"config not found or invalid: {path}")
    rooms = data.get("rooms")
    if not isinstance(rooms, list) or not rooms:
        raise ValueError("config.rooms must be a non-empty list")
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Muse independent HLS watchdog")
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--state", type=Path, default=None)
    ap.add_argument("--refresh-only", action="store_true", help="refresh/save m3u8 then exit")
    args = ap.parse_args(argv)
    config_path = args.config.expanduser().resolve()
    try:
        config = load_config(config_path)
    except Exception as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    state_dir = (args.state or (config_path.parent / "state")).expanduser().resolve()
    wd = Watchdog(config, config_path, state_dir)
    if args.refresh_only:
        if not wd.acquire_lock():
            return 1
        wd.refresh_all(force=True)
        return 0
    def on_signal(_sig, _frame):
        wd.stop_flag = True
        wd.log("signal received; stopping")
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    wd.log(f"watchdog start: {len(wd.rooms)} room(s), state={state_dir}")
    return wd.loop()


if __name__ == "__main__":
    raise SystemExit(main())
