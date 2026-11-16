# HANDOFF — LIVE-MUSE

## 当前状态

- 本仓库是 **Muse 值守仓库**，不是 Live.Monitor 源码。
- 录制软件：`idlm/Live-Monitor`，Release `v2.0.0-continuity.3`。
- 只读状态工具：`tools/muse_status.py`。
- 值守规则：`docs/INVARIANTS.md`、`docs/OPERATIONS.md`。
- 给 Muse 的提示词：`MUSE_PROMPT.md`。

## 下一步

1. 在值守机器上安装/配置 Live.Monitor，确认 `data/` 路径。
2. 把 `MUSE_PROMPT.md` 交给 Muse，让它先读 invariants/operations。
3. 首次运行：
   ```bash
   python3 tools/muse_status.py --data "$DATA_DIR" --json --sample 5
   ```
4. 按 60s/5min/30min 节奏值守；异常按剧本升级。
5. 如果 Live.Monitor 的数据目录/文件名变化，同步更新 `tools/muse_status.py` 的字段读取。

## 不要做

- 不要在本仓库放入 Cookie/token/真实签名 URL。
- 不要让 Muse 重复启动录制器。
- 不要把“监控正常”当成“长稳验收”；长稳仍需 2h/24h/72h 证据。
