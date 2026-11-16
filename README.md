# LIVE-MUSE

`LIVE-MUSE` 是 **Live.Monitor 的录制值守 agent（Muse）工作仓库**。

它不重复实现录制器，而是把 `idlm/Live-Monitor` 已经实现的连续录制逻辑，转成 Muse 可以准确理解、执行和汇报的规则、剧本与只读工具。

- 录制软件：<https://github.com/idlm/Live-Monitor>
- 最新 Release：<https://github.com/idlm/Live-Monitor/releases/tag/v2.0.0-continuity.3>
- 上游基线：Joftal/SODALive-Monitor v2.0.0（MIT）

## Muse 的职责

Muse 是**值守/监督 agent**，不是第二个录制器：

- 周期读取 Live.Monitor 的检查点、日志、磁盘、录像文件增长；
- 判断当前录制是否健康、是否在退避/慢速探测、是否有缺口；
- 发现异常时按 `docs/OPERATIONS.md` 的剧本处置或升级；
- 输出可核验的状态报告；
- **不重复启动录制器、不删除录像、不绕过平台限制、不泄露 Cookie/token。**

## 先读什么

Muse 开始工作前必须按顺序阅读：

1. `MUSE_PROMPT.md` —— 可直接粘贴给 Muse 的任务提示词
2. `docs/INVARIANTS.md` —— 不可违背的连续录制原则
3. `docs/OPERATIONS.md` —— 值守循环、异常剧本、汇报模板
4. `AGENTS.md` —— 仓库内行为约束

## 只读状态工具

```bash
python3 tools/muse_status.py --data "/path/to/Live.Monitor/data" --json --sample 5
```

- `--data` 指向 Live.Monitor 的 `data/` 目录（Linux 默认 `~/.config/pandalive-monitor/data`）
- `--sample 5` 采样 5 秒，计算录像文件增长
- 只读：不启动/停止录制，不修改检查点，不读取/输出 Cookie、token、完整签名 URL

输出包含：

- checkpoints（phase、段号、lastMediaAge、nextRetry、缺口、房型、令牌到期）
- 录像文件增长、磁盘剩余
- 最近 WARN/ERROR 日志（已脱敏）
- LKG 条目数量（不输出地址）
- `alerts`：stalled / retrying / disk low / no media growth 等

## 仓库结构

```
LIVE-MUSE/
├── MUSE_PROMPT.md          # 给 Muse 的完整提示词
├── AGENTS.md               # 仓库行为约束
├── docs/
│   ├── INVARIANTS.md       # 连续录制不可违背原则
│   └── OPERATIONS.md       # 值守手册与异常剧本
├── tools/
│   └── muse_status.py      # 只读状态工具(脱敏)
├── tests/
│   └── test_muse_status.py # 脱敏/状态/告警回归
└── config/
    └── muse.example.json   # 配置示例(超时/阈值/轮询)
```

## 独立看门狗（推荐）

Muse 会重置/重启，独立看门狗负责持有 ffmpeg 和持久化任务：

```bash
python3 tools/muse_watchdog.py --config config/muse.rooms.json
```

- 每 10 分钟抓取/保存 m3u8 快照（10 分钟内复用，不重复请求）
- 启动后按顺序拉起任务，单实例锁，已有进程只附着不重复启动
- 失败快速退避，之后每 5 分钟慢速探测
- 从已有文件 `max+1` 续录，旧文件不覆盖

详见 [docs/WATCHDOG.md](docs/WATCHDOG.md)。

## 快速使用

1. 在值守机器上安装并配置 Live.Monitor。
2. 确认 `data/` 路径。
3. 运行：

```bash
python3 tools/muse_status.py --data "$DATA_DIR" --json --sample 5
```

4. 把 `MUSE_PROMPT.md` 交给 Muse，并让它先读 `docs/INVARIANTS.md` 与 `docs/OPERATIONS.md`。
5. Muse 按“异常才行动、无事只汇总”的节奏值守。

## 原则摘要

- 媒体健康优先于入口状态：粉丝房/密码房提示本身不能让健康录制停止。
- 超时/DNS/TLS/5xx 不等于下播。
- 先快速退避，再每 5 分钟慢速探测；明确下播才结束。
- LKG 复用前必须验证媒体；600 秒不是失效期限。
- 一个主播最多一个 owner；旧文件不覆盖；恢复从 max+1。
- 不绕过粉丝/密码/付费/DRM/成人/地区限制；旧源只在已授权且仍健康时复用。
- 请求要节制，不制造风暴；Cookie/token/签名 URL 不写入报告。

## License

本仓库文档与工具为 MIT。Live.Monitor 基于 Joftal/SODALive-Monitor v2.0.0（MIT）。

## systemd（Muse 重启不影响看门狗）

示例单元：`deploy/live-muse-watchdog.service`

```bash
sudo cp deploy/live-muse-watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now live-muse-watchdog
```

`KillMode=process` 只重启看门狗主进程；ffmpeg 子进程由新看门狗认领，`max+1` 续录。
