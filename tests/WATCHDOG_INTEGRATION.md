# Watchdog integration evidence (local HLS)

Environment: Linux, system ffmpeg, local HTTP HLS with 3 static segments and `ENDLIST`. This validates lifecycle, not a real platform.

## Scenario

- `input_mode=snapshot`
- Pre-created `_0001.ts` to verify max+1
- Watchdog start → source snapshot → start ffmpeg
- Stop watchdog → restart → verify next segment

## Observed

1. First start:
   - `source saved for pandalive:u1: 227 bytes`
   - `started seg>1`
   - produced `_0002.ts`
2. Restart:
   - `started seg>2`
   - produced `_0003.ts`
   - `_0001/_0002` unchanged
3. Another restart:
   - `started seg>3`
   - produced `_0004.ts`
   - old files unchanged
4. Static playlist reaches `ENDLIST`; ffmpeg exits 0; watchdog treats it as failure and schedules retry. For real live playlists (no ENDLIST) ffmpeg stays running.

## Unit tests

```bash
python3 -m unittest discover -s tests -v
# 8 tests OK
```

Covered: m3u8 absolutization, snapshot reuse inside refresh window, LKG quality selection, segment max+1, PID identity, redaction.
