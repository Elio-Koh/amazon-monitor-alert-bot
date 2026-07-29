# Pangolin / Xingshang 采集异常优化方案

> **Status:** Implemented on branch `codex/pangolin-xingshang-collection-optimization`.
> **Date:** 2026-07-29
> **Scope:** Daily ASIN monitor, Pangolin API calls, xingshang MCP inventory collection, parent-child candidate generation, and report wording.

## 实施结果

已完成以下改动：

- `previous_inventory_payload()` 已限制为当前父 ASIN 的历史 `child_asins ∪ inventory_only_asins`。
- Pangolin 采集已增加单轮缓存，同一轮相同 ASIN 最多实际请求一次。
- Pangolin `2001`、鉴权失败、账户过期等终止性错误已增加熔断；熔断后只记录跳过指标，不继续实际请求。
- 已新增 `PANGOLIN_MAX_CALLS_PER_RUN`，GitHub Actions 默认值为 `250`。
- xingshang MCP 候选但 Pangolin 未确认时，不再写成 `不可售/404`，统一展示为 `xingshang mcp 候选，但 pangolin 前台侧未确认`。
- 文本报告已增加唯一候选 ASIN、父子关系候选行、正常前台确认子体、未确认候选数和数据源健康摘要。
- `requirements.txt` 已直接声明 `httpx`。

验证命令：

```bash
/Users/dylan.wang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m unittest discover -s tests -v
```

## 结论

本次异常不是 1,133 个真实子体，而是 103 个历史候选子体被错误地扩散到 11 个父 ASIN 下，形成了 `103 * 11 = 1,133` 条父子关系候选行。

同时，Pangolin 返回 `2001: 积分余额不足` 后没有熔断，系统继续按候选逐个请求，导致调用量被放大。xingshang 侧报错本质是 xingshang MCP 运行失败，而不是业务上确认库存为空。

## 已确认问题点

### 1. Pangolin 调用量被候选扩散放大

当前每日 workflow 配置了两个 GitHub Actions 定时触发：

- `15 1 * * *`，北京时间 09:15。
- `30 1 * * *`，北京时间 09:30，作为备份触发。

正常情况下，第二次触发会被同日发送保护跳过，不应重复采集。但 GitHub Actions schedule 是 best-effort，实际开始时间可能延迟。

本次异常链路下的调用量：

- 父 ASIN：11 个。
- 唯一候选子 ASIN：103 个。
- 错误扩散后的父子候选关系：1,133 条。
- 最终 Pangolin 尝试量约为 `11 + 1,133 = 1,144` 次。

因此，1,133 是父子关系候选行数，不是唯一子 ASIN 数。

### 2. xingshang MCP 失败被当成库存侧候选来源

排查中提到的“星商库存接口失败”，指的是 xingshang MCP 调用失败。已见错误是运行依赖缺失导致的 `No module named 'httpx'`，不是星商业务接口返回“无库存”。

当前 `requirements.txt` 未直接声明 `httpx`，但 `monitor.py` 的 MCP 调用路径需要它。依赖缺失会使库存侧采集失败，并触发 previous snapshot fallback。

### 3. previous snapshot fallback 存在父 ASIN 作用域污染

`previous_inventory_payload(previous, parent_asin)` 先取当前父 ASIN 的 `child_asins`，随后又把 `previous["children"]` 里的所有历史子体都加入当前父 ASIN 候选集合。

结果是：任一父 ASIN 只要触发 previous snapshot fallback，就可能拿到全局历史子体集合，进而把其他父 ASIN 的子体扩散到自己名下。

这就是 103 个候选子体被重复挂到 11 个父 ASIN 下的核心原因。

### 4. Pangolin 余额不足后没有熔断

Pangolin 返回 `2001: 积分余额不足` 属于终止性错误。当前逻辑会记录错误，但仍继续遍历后续父 ASIN 和候选子 ASIN。

这导致余额不足时仍然产生大量无效调用尝试，也放大了错误报告。

### 5. 数据源失败被误分类为前台不可售或 404

当前子体详情请求失败后，代码会将候选写成：

- `front_status: "不可售/404"`
- `source: "xingshang_inventory_only"`
- Excel 行类型：`库存侧异常子体`

这会把“Pangolin 前台侧未验证”误表达成“前台确认不可售/404”。目标文案应改为：

```text
xingshang mcp 候选，但 pangolin 前台侧未确认
```

这个文案只表达来源和验证状态，不再暗示前台已经确认 404。

### 6. 报告口径混淆了唯一子体和父子关系行

报告里出现的 1,133 条，是父子关系候选行数；真实唯一候选子 ASIN 数是 103。后续报告需要同时展示：

- 唯一候选子 ASIN 数。
- 父子关系候选行数。
- 正常前台确认子体数。
- xingshang mcp 候选但 pangolin 前台侧未确认的行数。

## 优化方案

### P0: 先修正确性

1. 显式补齐 xingshang MCP 依赖。
   - 在 `requirements.txt` 直接加入 `httpx`。
   - 在 CI 中增加 import smoke test，确保 MCP 运行路径不会因为隐式依赖缺失而失败。

2. 修复 previous snapshot fallback 的父 ASIN 作用域。
   - fallback 只能使用当前父 ASIN 过去的 `child_asins ∪ inventory_only_asins`。
   - `previous["children"]` 只能作为详情值仓库，不能作为当前父 ASIN 的成员关系来源。

3. 拆分子体状态语义。
   - `front_confirmed_child`: Pangolin 前台确认正常子体。
   - `xingshang_candidate_unconfirmed`: xingshang mcp 候选，但 pangolin 前台侧未确认。
   - `source_failed_unverified`: 数据源失败，无法判断前台或库存业务状态。

4. 数据源失败时保留上一次可信父子关系。
   - Pangolin 或 xingshang 失败时，不生成 404 事件。
   - 不生成子体新增或移除事件。
   - 报告中显示数据源异常和数据新鲜度，而不是业务状态变化。

### P1: 控制调用量和故障扩散

1. 为 Pangolin 终止性错误增加熔断。
   - 识别 `2001`、鉴权失败、余额不足等错误码。
   - 同一轮任务中首次命中终止性错误后，后续 Pangolin 请求全部跳过并记录 `circuit_skipped`。

2. 增加单轮缓存和去重。
   - 同一轮内相同 ASIN 的 Pangolin 详情最多请求一次。
   - 不同父 ASIN 引用同一个候选 ASIN 时复用详情结果或失败结果。

3. 增加每轮调用上限。
   - 新增 `PANGOLIN_MAX_CALLS_PER_RUN`。
   - 超过上限后跳过后续 Pangolin 请求，并在报告中标记未验证数量。

4. 增加调用指标。
   - planned calls。
   - attempted calls。
   - successful calls。
   - failed calls。
   - cache hits。
   - circuit skipped。
   - max-call skipped。

预期调用量：

- 健康情况下，上限应接近 `父 ASIN 数 + 唯一候选子 ASIN 数`。
- 按本次数据，去重后的理论上限约为 `11 + 103 = 114`。
- 如果第一个 Pangolin 请求就返回 `2001`，理想实际请求数应为 1，后续全部熔断跳过。

### P2: 优化报告表达

1. 报告中拆分唯一子体数和父子关系候选行数。
   - 避免把 1,133 条关系候选理解为 1,133 个真实子体。

2. 将 Excel 行类型从 `库存侧异常子体` 调整为：

```text
xingshang mcp 候选，但 pangolin 前台侧未确认
```

3. 增加数据源健康摘要。
   - Pangolin 是否成功。
   - Pangolin 是否熔断。
   - xingshang MCP 是否成功。
   - 是否使用 previous snapshot fallback。
   - 本次报告中有多少行属于未验证候选。

## 不做范围

本轮不处理“更换外部调度器以保证精确 09:15 执行”。GitHub Actions 的 09:15 / 09:30 双触发继续保留，备份触发仍依赖同日发送保护避免重复采集。

## 验收标准

1. 多父 ASIN previous snapshot fallback 不再交叉污染。
   - 父 ASIN A 只能得到 A 自己历史上的 `child_asins ∪ inventory_only_asins`。
   - 父 ASIN B 的历史子体不会出现在 A 的候选集合里。

2. Pangolin `2001` 余额不足时单轮最多产生 1 次实际 Pangolin 请求。
   - 后续请求计入 `circuit_skipped`。
   - 报告显示 Pangolin 熔断原因。

3. xingshang MCP 或 Pangolin 数据源失败时，不生成前台 404 结论。
   - 不生成虚假的子体新增。
   - 不生成虚假的子体移除。
   - 不把未验证候选写成前台不可售。

4. 同一轮相同 ASIN 最多请求一次 Pangolin 详情。
   - 多个父 ASIN 引用同一 ASIN 时复用缓存。

5. Excel 和文本报告能同时看清：
   - 唯一候选子 ASIN 数。
   - 父子关系候选行数。
   - 正常前台确认子体数。
   - `xingshang mcp 候选，但 pangolin 前台侧未确认` 行数。

## 建议实施顺序

1. 先补测试。
   - 多父 ASIN fallback 隔离测试。
   - Pangolin `2001` 熔断测试。
   - 同 ASIN 单轮缓存测试。
   - 数据源失败不写 404 测试。
   - 报告口径和新行类型文案测试。

2. 修 P0 正确性问题。
   - 补 `httpx`。
   - 修父 ASIN 作用域。
   - 拆分未验证候选状态。
   - 数据源失败时保留上次可信关系。

3. 加 P1 调用控制。
   - Pangolin 终止错误熔断。
   - 单轮缓存。
   - 调用上限和指标。

4. 做 P2 报告调整。
   - 改 Excel 行类型文案。
   - 增加唯一数、关系行数和数据源健康摘要。

## 相关文件

- `.github/workflows/daily-monitor.yml`: 定时触发、依赖安装、报告 artifact 上传。
- `requirements.txt`: Python 运行依赖。
- `monitor.py`: Pangolin 调用、xingshang MCP 调用、previous snapshot fallback、父子候选生成、Excel 报告行类型。
- `tests/test_monitor.py`: 需要补充上述回归测试。
