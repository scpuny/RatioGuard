# 钉钉推送模板（Markdown 消息）

渲染实现：`src/qsignal/notify/notifier.py::render_signal_message`
消息类型 `msgtype=markdown`，`markdown.title = "WorkBuddy 半自动量化信号 · 观测记录"`。
加签：`build_signed_url(webhook, secret)` 使用 HMAC-SHA256 + Base64 + URL-encode。
环境变量优先：`QSIGNAL_DINGTALK_WEBHOOK` / `QSIGNAL_DINGTALK_SECRET`。
限流：同类消息 `notify_dedup_sec`（默认 1800 秒）内去重；钉钉侧上限 20 条/分钟。

所有模板末尾由代码强制拼接 `core.disclaimer.DISCLAIMER` 全文，并经过
`assert_compliant()` 校验（完整免责 + 无诱导性话术）。下列模板中展示的免责全文
须与 `src/qsignal/core/disclaimer.py::DISCLAIMER` 逐字一致（人工阅读副本见
`templates/disclaimer.txt`）。模板中禁止出现
"建议交易 / 买入信号 / 推荐买入 / 稳赚" 等表述。

---

## 模板 1 · signal_qualified（信号已记录 · 历史统计达标）

```
### 信号已记录 · 历史统计达标

- 信号时间：{signal_ts}
- 信号类型：{signal_type}
- 触发比值：{ratio}
- 引用快照：{freeze_date} 15:10 冻结快照
- 历史样本：N={n}，胜率={win_rate}，95% CI=[{ci_low}, {ci_high}]，含成本盈亏平衡胜率={break_even}
- 说明：以上为历史样本的描述性统计，本信号尚未纳入统计样本。
- 系统动作：已入库，等待结算闭环。

⚠️ 统计结果仅基于当前已收集样本，是历史样本快照，不代表未来行情概率分布，市场结构变化会直接失效；半自动模式，请人工复核行情、流动性、ETF 申赎状态、券源与交易成本后再决定是否操作，不构成投资建议。本信号未纳入统计样本，历史统计不构成对其结果的推断。
```

## 模板 2 · signal_insufficient（信号已记录 · 置信不足，仅观测）

```
### 信号已记录 · 置信不足，仅观测

- 信号时间：{signal_ts}
- 信号类型：{signal_type}
- 判定原因：{qualified_reason}（样本量 / 交易日跨度 / 置信区间下限 / 漂移暂停）
- 系统动作：仅入库记录，等待结算闭环，不推送交易类消息。

⚠️ 统计结果仅基于当前已收集样本，是历史样本快照，不代表未来行情概率分布，市场结构变化会直接失效；半自动模式，请人工复核行情、流动性、ETF 申赎状态、券源与交易成本后再决定是否操作，不构成投资建议。本信号未纳入统计样本，历史统计不构成对其结果的推断。
```

## 模板 3 · drift_alert（策略分布漂移）

```
### 策略分布漂移 · 暂停交易推送，继续收集样本

- 检验方式：互斥窗口两比例检验（W1 最近 {window_size} 笔，W2 其前 {window_size} 笔）
- 检验结果：z={z_stat}，临界值={z_critical}，效应量={effect_size}，最小效应门槛={min_effect_size}
- 窗口胜率：p1={p1}（n1={n1}），p2={p2}（n2={n2}）
- 系统动作：pause_trade_alert=True；之后所有新信号仅入库记录，不推送交易类消息。
- 恢复方式：人工复核后执行 /resume，操作写入日志。

⚠️ 统计结果仅基于当前已收集样本，是历史样本快照，不代表未来行情概率分布，市场结构变化会直接失效；半自动模式，请人工复核行情、流动性、ETF 申赎状态、券源与交易成本后再决定是否操作，不构成投资建议。本信号未纳入统计样本，历史统计不构成对其结果的推断。
```

---

## 分发规则

| 条件 | 动作 |
| --- | --- |
| `pause_trade_alert=True` 且消息为信号类 | 不发送，仅写 `run_log: notify_suppressed` |
| 同类消息在去重窗口内 | 不发送，返回 `reason=dedup` |
| 未配置 webhook / channel=none | 生成文案但不发送，返回 `reason=dry_run`（文案仍通过合规校验） |
| 漂移告警 | 发送，并置 `pause_trade_alert=True`；恢复需人工 `/resume` |

渠道缺失、网络失败均不得阻断采集与结算主流程。
