# Muse 独立看门狗（muse_watchdog）

## 为什么需要它

Muse 会自动重置/重启。录制任务不能跟着 Muse 的进程生命周期走，否则：

- Muse 重启 → 录制中断；
- 重复启动 → 同一主播多份录制；
- 重启后不知道旧文件/段号 → 覆盖或另起新系列。

`tools/muse_watchdog.py` 是**独立进程**：

- 自己持有 ffmpeg 子进程；
- 自己持久化 `tasks.json`；
- 用 OS 文件锁保证单实例；
- Muse 重启后，新看门狗读取状态，认领仍存活的 ffmpeg，或从 `max+1` 续录。

## 作用

1. **每 10 分钟获取并保存 m3u8**
   - `refresh_seconds` 默认 600；
   - 快照写入 `state/sources/<room>.m3u8`；
   - m3u8 内相对地址会绝对化，ffmpeg 可直接读本地快照；
   - 快照未超过 10 分钟时，Muse 重启不会重复请求。

2. **启动后顺序拉起任务**
   - 读取 `tasks.json` / 快照；
   - 按 `rooms[]` 顺序逐个启动；
   - 每个任务间隔 `start_gap_seconds`，避免请求风暴；
   - 已有同房间进程存活 → 只附着监控，不再启动第二个。

3. **崩溃/重启续录**
   - 从已有文件计算 `max+1`；
   - 旧文件不覆盖；
   - 失败快速退避，超过 `fast_retry_max` 后每 5 分钟慢速探测；
   - 明确源失效不冒充下播。

4. **单实例**
   - `state/watchdog.lock` 使用 OS 文件锁；
   - 已有看门狗运行时，新实例直接退出。

## 运行

```bash
python3 tools/muse_watchdog.py --config config/muse.rooms.json
```

只刷新源、不启动录制：

```bash
python3 tools/muse_watchdog.py --config config/muse.rooms.json --refresh-only
```

配置示例：`config/muse.rooms.example.json`。

## 输入模式

| input_mode | 行为 | 适用 |
|---|---|---|
| `snapshot` | ffmpeg 读本地保存的 m3u8；相对地址已绝对化 | 公开 HLS、无需自定义请求头 |
| `remote` | ffmpeg 读每次解析出的远程 URL；快照仍保存作 LKG | 需要 Origin/Referer/Cookie 等头的平台 |

注意：本地快照走 `file` 协议，ffmpeg 会拒绝 `-user_agent/-headers/-reconnect` 等 HTTP 选项；需要请求头时请用 `input_mode=remote`。

## 源类型

| type | 说明 |
|---|---|
| `m3u8` | 配置里的静态 m3u8 URL |
| `command` | 运行 argv 列表，stdout 第一行输出新 m3u8 URL（不使用 shell） |
| `lkg` | 读取 Live.Monitor 的 `lkg-sources.json`，按目标画质/带宽选一个有效变体 |

`command` 适合由平台专用脚本获取新签名源；不要把 Cookie/token 写进仓库。

## 状态与检查点

```text
state/
├── watchdog.lock
├── tasks.json
├── watchdog.log
└── sources/
    ├── pandalive_exampleA.m3u8
    └── pandalive_exampleA.json
```

- `tasks.json`：base、segIndex、pid、proc_start、lastMediaAt、streak、nextRetryAt。
- `sources/*.json`：抓取时间、sha256、headers、quality；不输出到报告。
- 日志脱敏，不含完整签名 URL/Cookie/token。

## 实测（本地 HLS）

- 第 1 次启动：源保存 → 启动 ffmpeg → 写出 `_0002.ts`（预置 `_0001.ts` 用于验证续段）。
- 停止看门狗后重启：读取状态，`seg>2`，写出 `_0003.ts`，旧文件未覆盖。
- 单实例锁与状态文件均生效。

## 与 Live.Monitor 的关系

- 如果 Live.Monitor 已经在对同一主播录制，**不要让看门狗重复启动同一房间**。
- 看门狗可以作为：
  - Muse 值守环境下的独立录制器；
  - 或在 Live.Monitor 不运行时，基于其 LKG 快照做恢复录制。
- `lkg` 类型只复用已保存的源；不绕过粉丝/密码/付费/DRM/授权。

## 已知边界

- 看门狗不实现平台登录/取源协议；`m3u8`/`command` 类型的源获取由外部负责。
- 不同平台的 Cookie/Headers 由配置或外部命令提供，仓库不存凭据。
- 长时间 24h/72h 仍需单独压测。
- Windows 进程启动时刻读取有限；跨平台恢复以文件锁 + 文件段号为主。
