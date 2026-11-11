---
name: S5-notify
description: 告警推送 SOP。说明客观文案、完整免责声明、钉钉加签、去重、漂移暂停与人工恢复。仅作人工 SOP，不参与运行约束。
---

# S5 · 告警推送（SOP）

> **约束声明**：本文件是**人工 SOP 说明**，不是运行约束。文案模板、
> 合规校验、加签、去重、暂停开关均以 `src/qsignal/notify/notifier.py`、
> `src/qsignal/core/disclaimer.py` 及 `tests/test_m3_notify.py` 为准。

## 1. 对应实现

| 项目 | 位置 |
| --- | --- |
| 文案渲染 / 分发 | `notify/notifier.py::render_signal_message` / `dispatch` |
| 免责与话术校验 | `core/disclaimer.py`（`DISCLAIMER` / `assert_compliant`） |
| 钉钉加签 | `notifier.build_signed_url`（HMAC-SHA256 + Base64 + URL-encode） |
| 模板 | `templates/dingtalk.md` |
| 运行入口 | 服务自动 / `qctl.py drift` / `qctl.py pause` / `qctl.py resume` |
| 开关 | `config.yaml::enable_notify`、`notify_channel` |
| 验收 | M3 + M4 |

## 2. 三类消息（只用客观描述）

| kind | 触发条件 | 标题 |
| --- | --- | --- |
| `signal_qualified` | 新信号 + 最近冻结快照 `qualified=True` | 信号已记录 · 历史统计达标 |
| `signal_insufficient` | 新信号 + 无冻结快照 / 快照未达标 | 信号已记录 · 置信不足，仅观测 |
| `drift_alert` | 互斥窗口检验告警 | 策略分布漂移 · 暂停交易推送，继续收集样本 |

**禁止话术**（`PERSUASIVE_PHRASES`）：建议交易 / 建议买入 / 建议卖出 /
交易参考信号 / 买入信号 / 卖出信号 / 推荐买入 / 稳赚 等。
`render_signal_message()` 在返回前调用 `assert_compliant()`，
缺免责或含诱导话术直接抛 `ComplianceError`。

## 3. 放行规则（硬分支）

- 唯一依据 = `get_latest_frozen()`（最近一次 15:10 冻结快照）；
- 盘中**不刷新**快照来放行信号；
- `pause_trade_alert=true` 时，两类信号消息一律不发，仅写
  `run_log(action=notify_suppressed)`；`drift_alert` 本身仍发送；
- 同类消息在 `notify_dedup_sec`（默认 1800s）内去重；
- 未配置 webhook / `channel=none` → `dry_run`：生成并通过合规校验文案，但不发送；
- 渠道失败不阻断采集与结算主流程。

## 4. 漂移暂停与恢复

`run_drift_monitor()` 检出告警 → `pause_trade_alert()` 落盘 `system_state`
→ 之后所有新信号只入库记录，不推送交易类消息。

恢复**必须人工执行**，操作写入 `run_log`：

```bash
python qctl.py resume --reason "人工复核后恢复"
```

> 暂停状态存在 SQLite，不放在内存变量里，进程 / 会话重启不丢失。

## 5. 环境变量

| 变量 | 说明 |
| --- | --- |
| `QSIGNAL_DINGTALK_WEBHOOK` | 钉钉机器人 Webhook（优先于 config） |
| `QSIGNAL_DINGTALK_SECRET` | 加签密钥（优先于 config） |

---

⚠️ 统计结果仅基于当前已收集样本，是历史样本快照，不代表未来行情概率分布，市场结构变化会直接失效；半自动模式，请人工复核行情、流动性、ETF 申赎状态、券源与交易成本后再决定是否操作，不构成投资建议。本信号未纳入统计样本，历史统计不构成对其结果的推断。
