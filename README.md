# Amazon Monitor Alert Bot

每日采集 Amazon 父/子 ASIN 快照，对比上次成功状态，并把需要人工处理的重点变化发送到飞书群。

## 默认飞书策略

- `--daily-report` 默认只发送 1 条飞书消息。
- 群消息只展开 P0/P1 事件，不展示 P2/低优先级变化数量。
- 群消息不包含完整报告里的“监控范围”和明细大段文本。
- 完整日报写入 `FULL_REPORT_OUTPUT`，父 ASIN 可筛选明细写入 `FULL_REPORT_XLSX_OUTPUT`，在 GitHub Actions 中一起上传为 `asin-full-daily-report` artifact。
- Excel 第一张表 `父体筛选明细` 的 `父 ASIN` 列可直接筛选；筛选后会保留该父体行、正常子体行和库存侧异常子体行。
- LD / BD 等 Deal 的具体折扣百分比会写入 `Deal 折扣百分比` 列，并在完整 txt 明细中显示为 `Deal折扣`。
- 去重历史写在加密 state 的 `alert_dedupe` 字段里，避免窗口期内重复刷同一事件。

## 关键配置

| Env | Default | 用途 |
| --- | --- | --- |
| `FEISHU_MESSAGE_MODE` | `card` | `card` 发送交互卡片，失败时兜底 text；`text` 只发文本。 |
| `FULL_REPORT_OUTPUT` | empty | 完整日报输出路径；Actions 默认 `state-report.txt`。 |
| `FULL_REPORT_XLSX_OUTPUT` | empty | 父 ASIN 可筛选 Excel 输出路径；Actions 默认 `state-report.xlsx`。 |
| `FULL_REPORT_URL` | empty | 摘要末尾显示的完整报告入口；Actions 默认本次 run URL。 |
| `ALERT_MIN_SEVERITY` | `P1` | 飞书摘要最低展示等级。 |
| `ALERT_MAX_SUMMARY_ITEMS` | `10` | P0/P1 摘要最多展开条数。 |
| `ALERT_DEDUPE_WINDOW_DAYS` | `1` | 同一事件重复提醒抑制窗口。 |
| `ALERT_SEND_NO_CHANGE` | `false` | 没有 P0/P1 时是否仍发送无变化摘要。 |
| `ALERT_PRICE_PCT_THRESHOLD` | `5` | P1 价格变化百分比阈值。 |
| `ALERT_CRITICAL_PRICE_PCT_THRESHOLD` | `10` | P0 价格变化百分比阈值。 |
| `ALERT_PRICE_ABS_THRESHOLD` | `1` | P1 价格变化绝对金额阈值。 |
| `ALERT_RANK_PCT_THRESHOLD` | `20` | P1 排名变化百分比阈值。 |
| `ALERT_LOW_INVENTORY_THRESHOLD` | `5` | P1 低库存阈值。 |
| `ALERT_DELIVERY_DAYS_THRESHOLD` | `2` | P1 配送变慢天数阈值。 |

## 本地验证

使用仓库要求的 Python 3.11+ 运行：

```bash
python3.11 -m unittest discover -s tests -v
```

daily dry-run 会打印将发送到飞书的单条 primary payload JSON；完整报告仍写入 `FULL_REPORT_OUTPUT`：

```bash
FEISHU_MESSAGE_MODE=card \
FULL_REPORT_OUTPUT=state-report.txt \
FULL_REPORT_XLSX_OUTPUT=state-report.xlsx \
FULL_REPORT_URL=https://example.invalid/actions/runs/local \
python3.11 monitor.py --daily-report --dry-run --force-daily-report \
  --state state/latest.enc.json \
  --output state/latest.enc.json \
  --delivery-state state/delivery.enc.json
```

## 快速回滚

- 卡片模式异常：设置 `FEISHU_MESSAGE_MODE=text`。
- 摘要过滤过严：设置 `ALERT_MIN_SEVERITY=P2`。
- 去重过严：设置 `ALERT_DEDUPE_WINDOW_DAYS=0`。
- 暂停 daily 高级提醒：从定时 workflow 参数中移除 `--daily-report`，或临时设置 `ALERT_MIN_SEVERITY=P0` 且 `ALERT_SEND_NO_CHANGE=false`。
