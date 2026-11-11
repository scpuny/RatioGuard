---
name: S6-report
description: 报表与日志 SOP。说明 Markdown 复盘报表、CSV 备份、动态大屏 JSON 四项新口径与漂移复核。仅作人工 SOP，不参与运行约束。
---

# S6 · 报表与日志（SOP）

> **约束声明**：本文件是**人工 SOP 说明**，不是运行约束。报表字段、
> 大屏 JSON 结构、免责注入均以 `src/qsignal/report/renderer.py`、
> `src/qsignal/stats/drift.py` 及 `tests/test_m3_notify.py` /
> `tests/test_m4_drift.py` 为准。

## 1. 对应实现

| 项目 | 位置 |
| --- | --- |
| Markdown 报表 | `report/renderer.py::render_markdown_report` |
| CSV 备份 | `renderer.export_csv` |
| 动态大屏 JSON | `renderer.build_dashboard_payload` / `write_dashboard` |
| 漂移复核 | `stats/drift.py::run_drift_monitor` |
| 全链路日志 | `core/audit.py::log_run` |
| 运行入口 | `qctl.py report` / `qctl.py drift` / 服务 15:10 自动 |
| 验收 | M3 + M4 + M5 |

## 2. 输出物

| 文件 | 内容 |
| --- | --- |
| `reports/report_YYYY-MM-DD.md` | 信号清单、三张快照对比表、漂移记录、完整免责声明 |
| `reports/signals_YYYY-MM-DD.csv` | `signal_table` 全量离线备份（UTF-8 BOM） |
| `reports/dashboard.json` | 动态大屏指标（结构见 `templates/dashboard_schema.json`） |

报表中的 live 快照**仅用于展示**；放行依据始终且仅为冻结快照。

## 3. 动态大屏四项新口径

```json
{
  "config_version_tag": "pair_ratio_v1@<config_hash>",
  "quality": {
    "ambiguity_rate": 0.0,
    "hold_rate": 0.0,
    "ambiguous_cnt": 0,
    "hold_cnt": 0
  },
  "drift": {
    "z_stat": null,
    "z_critical": 2.2414,
    "effect_size": null,
    "min_effect_size": 0.10,
    "status": "insufficient"
  },
  "disclaimer": "<完整免责声明>"
}
```

- `quality.ambiguity_rate`：路径歧义样本占有效样本比例；
- `quality.hold_rate`：收盘 hold 占全部闭环样本比例；
- `drift.z_stat`：互斥窗口两比例检验 z 值（含 Bonferroni 校正临界值）；
- `config_version_tag`：策略版本 + `config_hash`，标识统计口径。

## 4. 漂移复核（代码化）

- W1 = 最近 W 笔有效样本，W2 = **紧邻其前 W 笔**，二者完全不重叠
  （修复原「滚动窗口 vs 全量」子集包含导致差值恒为 0 的缺陷）；
- 两比例 z 检验，`z_critical = z(1 - alpha / drift_comparisons)`（Bonferroni）；
- 告警条件：`|z| > z_critical` **且** `effect_size >= min_effect_size`；
- 双窗口任一不足 `min_window_samples` → `status=insufficient`，
  **不视为无漂移**；
- 参数变更写 `status=config_change`，查询时排除，不误判为漂移。

## 5. 日志可复现

`run_log` 每条指标计算都带 `input_json`（含全部输入参数）与 `output_json`，
可按 `module/action` 检索复现，例如 `snapshot.compute`、`drift.compute`、
`service.signal_gate`、`service.eod_routine`。

## 6. 人工操作

```bash
python qctl.py report --out-dir reports   # 立即生成三类报表
python qctl.py drift                       # 立即复核漂移
python qctl.py status                      # 运行态概览
```

---

⚠️ 统计结果仅基于当前已收集样本，是历史样本快照，不代表未来行情概率分布，市场结构变化会直接失效；半自动模式，请人工复核行情、流动性、ETF 申赎状态、券源与交易成本后再决定是否操作，不构成投资建议。本信号未纳入统计样本，历史统计不构成对其结果的推断。
