# Muse 值守手册

## 1. 启动检查

1. 确认 Live.Monitor 已运行、`data/` 可读。
2. 运行：

```bash
python3 tools/muse_status.py --data "$DATA_DIR" --json --sample 5
```

3. 检查：
   - `checkpoints` 是否为空；
   - `diskFreeGb` 是否高于阈值；
   - `alerts` 是否有 critical；
   - 日志是否有 `disk`/`403`/`stall`/`retry` 关键行。

## 2. 周期值守

| 频率 | 动作 |
|---|---|
| 60 秒 | 读取 `--json`，只看 `alerts` 是否有 critical |
| 5 分钟 | 完整检查所有 checkpoint、磁盘、日志摘要 |
| 30 分钟 | 输出汇总（无异常只报正常） |
| 异常时 | 按下方剧本 |

## 3. 状态解释

- `phase=recording` + 文件增长：健康。
- `phase=recording` + `lastMediaAgeSec > 120`：观察。
- `lastMediaAgeSec > 300`：停滞风险，检查是否已 retrying。
- `phase=retrying`：正常退避，不要启动第二任务。
- `nextRetryInMs`：下一次尝试时间。
- `gapCount > 0`：已有缺口，记录原因，不要补录假数据。
- `sourceExpiresAt`：令牌到期；过期候选会被跳过。
- `roomType=fan/paid/password`：入口受限，不代表要停止健康媒体。

## 4. 异常剧本

### 4.1 媒体停滞

**观测**：`lastMediaAgeSec > 300`，文件不增长。

**动作**：

1. 看 `phase` 是否已 `retrying`；
2. 若已 retrying：等待下一次退避/慢速探测；
3. 若仍 recording 且 App 未动作：记录证据，升级用户；
4. 不重复启动；不杀进程。

### 4.2 FFmpeg 退出/中断

**观测**：`phase=retrying`，日志 `interrupted`。

**动作**：

- 让 App 自动续录；
- 检查 LKG/源模式；
- 连续失败超过快速上限后进入慢速探测，不要干预；
- 明确下播则等下一场。

### 4.3 粉丝房/密码房/付费房

**观测**：`roomType=fan/paid/password`，平台拒绝新源。

**动作**：

- 如果 `phase=recording` 且媒体增长：继续；
- 如果 LKG 旧源健康且之前已授权：允许继续；
- 如果旧源失效 + 无授权：停止并升级；
- 不猜密码、不换账号、不伪造 token。

### 4.4 401/403/IP 封禁/风控

**观测**：日志 `403`、`block`、`needLogin`、`token expired`。

**动作**：

- 不循环重试；
- 不换代理/账号绕过；
- 记录平台原话（脱敏）、时间、房间；
- 升级用户。

### 4.5 分片冻结

**观测**：清单 200、序列不前进；`stalled`/周期探针告警。

**动作**：

- 让 App 的周期探针/续录处理；
- 若持续无动作，记录证据并升级；
- 不重复启动。

### 4.6 磁盘/写入失败

**观测**：`diskFreeGb` 低于阈值，日志 `ENOSPC/Read-only/Permission denied`。

**动作**：

- 停止启动新任务；
- 不删除已有录像；
- 报告路径、剩余空间、失败文件；
- 等用户清理/修复。

### 4.7 崩溃/重启

**观测**：`rec-checkpoints.json` 有残留。

**动作**：

- 让 App 恢复；
- 检查 `phase/segIndex/files`；
- 旧文件是否保留；
- 进程身份不可核实 → 不并发写，升级。

### 4.8 同一主播疑似两个任务

**观测**：检查点/日志出现同房间两个 id。

**动作**：

- 立即停止第二个（仅在用户授权下）；
- 保留第一个；
- 记录证据；
- 检查是否绕过单实例/手工重复启动。

## 4.9 看门狗重启

- Muse 重启后：重启看门狗进程；它自己获取锁并恢复。
- 检查 `state/tasks.json` 的 `segIndex`、`pid`、`nextRetryAt`。
- 若看门狗报 "another watchdog instance holds the lock"：说明旧实例仍在，不要强杀；先确认。
- 若旧 ffmpeg 存活但无法核实身份：不要手动启动第二个，升级用户。

## 5. 汇报模板

### 正常

```text
[时间] Live.Monitor 值守正常
- 录制中: 2（roomA 段 12，roomB 段 5）
- 等待续录: 1（roomC，下次 120s）
- 磁盘: 42 GB
- 最近异常: 无
```

### 异常

```text
[时间] 异常: roomX (pandalive)
- 状态: stalled
- 证据: lastMediaAge=420s, phase=recording, segIndex=8, disk=12GB
- 已做: 只读检查，未重复启动
- 未做: 未杀进程/未删文件
- 建议: 等 App 退避或人工确认源
```

## 6. 每日/每周

每日：

- 录制成功率/缺口清单；
- 异常次数与原因；
- 磁盘趋势；
- 是否有重复任务/幽灵恢复。

每周：

- 长断流样本；
- LKG 命中率/失败原因；
- 令牌过期统计；
- 需要用户处理的授权/IP 问题。

## 7. 只读工具字段

`tools/muse_status.py --json` 输出：

- `checkpoints[]`: room/nick/phase/segIndex/lastMediaAgeSec/nextRetryInMs/gapCount/roomType/sourceExpiresAt/health/fileGrowthBytes
- `diskFreeGb`
- `alerts[]`: severity/room/code/message
- `recentLogs[]`: 已脱敏 WARN/ERROR
- `lkg`: 房间数/档数（不输出 URL）
- `settings`: 分段/自动续录/受限房回退/并发上限（不输出密钥）

不要输出 `lastVariantUrl` / `dlHeaders` / Cookie / token。
