# AI Stock 自动化交易骨架

这是一个面向同花顺桌面 App 的自动化交易骨架。当前默认进入 `sim_run` 阶段，使用同花顺模拟账户完成模拟交易；真实账户仍被禁用。

## 当前配置

- 股票代码：`588330`
- 交易时间：
  - 上午 `09:15-11:30`
  - 下午 `13:00-15:15`
- 默认执行：
  - `execution.stage`: `sim_run`
  - `execution.mode`: `ths_applescript`
  - 进入同花顺模拟账户，先同步资金/持仓，字段和截图校验通过后提交模拟买卖单
- 监控频率：`poll_seconds=60`，每 1 分钟获取一次行情并检查策略
- 初始资金：`50000`
- 买入策略：
  - 不再按第几次买入判断，改为按当前仓位状态判断
  - 仓位为 0%：连续 3 个交易日上涨，且 `MA5 > MA10 > MA20`，买入至 50% 仓位
  - 仓位大于等于 50%：当天价格大于最新买入价，`MA5 > MA10 > MA20 > MA60`，买入至 85% 仓位
  - 仓位大于等于 85%：当天价格大于最新买入价，`MA5 > MA10 > MA20 > MA60`，买入至 100% 仓位
  - 每次买入后，将成交价记录为最新买入价
  - 买入数量按 100 股一手取整
  - 买入目标仓位如果不够整手，取大于目标仓位的最小可买整手数量；受可用现金约束
- 卖出策略：
  - 当前亏损达到 3% 时，直接清仓
  - 持仓后记录第 1 段最高浮盈
  - 当前浮盈从当前段最高浮盈回撤达到 20% 时，卖出 50% 持仓
  - 第 1 次卖出后，将卖出时的当前浮盈设置为第 2 段最高浮盈
  - 第 2 次卖出后，将卖出时的当前浮盈设置为第 3 段最高浮盈
  - 第 3 次卖出基于第 3 段最高浮盈触发，触发时直接清仓
- 风控：
  - 每天最多执行 1 次交易
  - 交易白名单默认只有 `588330`
  - 支持 `STOP_TRADING` 一键停止文件
  - 非交易日和非交易时段默认不下单
  - 小资金实盘阶段默认买入金额上限 `5000`
  - 状态保存在 `portfolio.json`
- 执行层：
  - `DryRunExecutor`：只记录模拟订单
  - `ManualConfirmExecutor`：人工确认占位，尚不提交真实订单
  - `ThsAppleScriptExecutor`：同花顺安全执行框架，优先使用 macOS Accessibility，且始终要求截图/OCR 字段校验
- 行情层：
  - `market_data.py`：负责实时行情和日 K 数据
  - `trading_engine.py` 持续运行时会先记录目标标的实时行情，再调用策略判断
  - 所有抓取到的行情数据统一保存到项目根目录的 `market_data.sqlite3`
- 回测成本与诊断：
  - 佣金、最低手续费、印花税、滑点会计入资金曲线
  - 报告包含最大回撤、胜率、盈亏比、换手率、停牌/涨跌停检查、未来函数提示、幸存者偏差提示

## 快速开始

```bash
python3 trading_engine.py --once
```

忽略交易时间，仅测试策略：

```bash
python3 trading_engine.py --once --ignore-hours
```

周末或节假日仅做 dry-run 测试：

```bash
python3 trading_engine.py --once --ignore-hours --ignore-trade-day
```

`--ignore-trade-day` 只能搭配 `execution.mode=dry_run` 使用。

持续运行：

```bash
python3 trading_engine.py
```

持续运行入口是 `trading_engine.py`。它会按照 `config.json` 中的 `poll_seconds` 循环监控目标股票，当前为每 1 分钟一次；行情获取逻辑由 `market_data.py` 提供。持续运行时日志会显示在当前终端，按 `Ctrl+C` 可以立即停止。

如果想额外打开实时日志窗口：

```bash
python3 trading_engine.py --open-log
```

查看状态、停止和恢复：

```bash
python3 trading_engine.py --status
python3 trading_engine.py --stop
python3 trading_engine.py --clear-stop
python3 trading_engine.py --cleanup-screenshots
```

`--stop` 会写入 `STOP_TRADING`，持续运行中的交易引擎会在睡眠期间每秒检查一次并尽快退出；下次重新运行前先执行 `--clear-stop`。
`--cleanup-screenshots` 会按 `runtime.screenshots_cleanup` 配置立即清理 `screenshots/` 中的历史 `.png/.json` 文件；交易引擎正常启动时也会自动执行一次清理。

检查配置：

```bash
python3 trading_engine.py --check-config
```

运行单元测试：

```bash
python3 -m unittest discover -s tests
```

## 项目结构与工作顺序

核心工作流：

1. `trading_engine.py` 启动并进入循环。
2. `config.json` 提供股票代码、交易时段、风控和执行阶段配置。
3. `market_data.py` 获取目标股票实时行情和日 K 数据。
4. `trading_strategy.py` 根据行情、均线、持仓和最新买入价生成买卖信号。
5. `trading_engine.py` 的风控模块检查白名单、交易时段、交易日、仓位、金额和停止文件。
6. 执行器根据 `execution.mode` 和 `execution.stage` 处理信号；当前默认 `gui_simulation` 只填单和校验，不点击最终确认。
7. 信号、订单生命周期和账户变化分别写入 `signals.csv`、`runtime_state.json` 和本地 `TradingLog.csv`。

文件职责：

- `trading_engine.py`：长期运行入口，负责调度、风控、执行、通知和日志。
- `market_data.py`：行情数据层，负责实时行情和日 K。
- `market_data_store.py`：行情 SQLite 落库层，统一维护 `market_data.sqlite3`。
- `trading_strategy.py`：策略层，只负责生成交易信号。
- `backtest.py`：回测层，只负责历史回测、回测撮合、指标和报告。
- `config.json`：配置中心。
- `portfolio.json`：本地模拟资金和持仓状态，运行时自动创建，不上传 GitHub。
- `signals.csv`：信号和执行审计，运行时自动追加，不上传 GitHub。
- `TradingLog.csv`：订单状态、系统成交和外部/人工交易的追加式审计记录，不上传 GitHub。
- `market_data.sqlite3`：行情数据库，保存回测日 K、回测 1 分钟 K 和实时轮询行情，不上传 GitHub。
- `trading_engine.log`：交易引擎运行日志，不上传 GitHub。
- `trading_engine.monitor.log`：后台托管运行时建议使用的监控日志，不上传 GitHub。
- `tests/`：本地安全测试和基础解析测试，不上传 GitHub。

## GitHub 上传范围

为了保持仓库通用、轻量且不包含本地运行状态，GitHub 只上传源码、配置模板、脚本和文档；本地测试目录不上传。

会上传：

- 源码：`trading_engine.py`、`trading_strategy.py`、`market_data.py`、`market_data_store.py`、`backtest.py`
- 配置与依赖：`config.json`、`requirements.txt`
- 启停脚本：`Trading_Engine.command`、`automation_start_trading_engine.sh`、`automation_stop_trading_engine.sh`
- 文档与忽略规则：`README.md`、`agent.md`、`.gitignore`

不会上传：

- 回测结果：`backtest_*.html`、`backtest_*.png`、`backtest_trades_*.csv`
- 本地测试：`tests/`
- 行情数据库：`market_data.sqlite3`
- 本地运行状态：`portfolio.json`、`runtime_state.json`、`signals.csv`、`TradingLog.csv`、`STOP_TRADING`
- 日志：`*.log`
- GUI 自动化临时产物：`screenshots/*.png`、`screenshots/latest_order_intent.json`、`screenshots/latest_verified_order.json`
- Python 缓存、虚拟环境和 IDE 配置：`__pycache__/`、`.venv/`、`venv/`、`.idea/`、`.vscode/`

后台运行建议：

```bash
launchctl submit -l com.aistock.tradingengine -- /usr/bin/env zsh -lc 'cd "$1" && exec "$(command -v python3)" trading_engine.py >> trading_engine.monitor.log 2>&1' zsh "$(pwd)"
```

查看后台日志：

```bash
tail -f trading_engine.monitor.log
```

停止后台运行：

```bash
launchctl remove com.aistock.tradingengine
```

## 执行阶段

`config.json` 中的 `execution.stage` 控制准入闸门：

- `dry_run`：只能使用 `execution.mode=dry_run`。
- `gui_simulation`：生成订单意图后等待受控 GUI 自动化完成同花顺界面填单和字段校验；`final_confirm_enabled=false` 时不会最终提交。
- `sim_run`：使用同花顺模拟账户执行买入/卖出；必须保持 `execution.ths_account_mode=simulation`，并通过截图/OCR/字段校验。
- `small_live`：预留小资金实盘阶段，默认买入金额上限 `5000`。
- `full_live`：预留完整额度阶段，默认买入金额上限 `50000`。

`config.json` 中的 `execution.mode` 控制执行器：

- `dry_run`：只记录计划动作，不触碰同花顺。
- `manual_confirm`：人工确认占位，当前不会提交真实订单。
- `ths_applescript`：同花顺 AppleScript 执行器。后台进程生成订单意图，由项目内 AppleScript + Accessibility bridge 操作 App，并使用 Apple Vision OCR 独立校验。当前默认只允许模拟账户交易，实盘账户仍被阻断。

同花顺 Mac 版交易界面必须先使用 App 内的“模拟”交易选项完成调试。`execution.ths_account_mode` 默认是 `simulation`，`execution.live_account_enabled=false`；在这个状态下，如果 GUI 校验识别到实盘/普通交易界面，系统会阻断执行。

同花顺 App 交互采用以下分层：

1. `macOS Accessibility` 是常规交互主路径。按钮通过名称和 `AXPress` 操作；证券代码栏通过附近语义标签定位后逐字符键入，禁止粘贴或直接设置 `AXValue`，以确保 App 触发市场代码匹配；价格和数量可设置 `AXValue` 并立即回读。禁止依赖 `child[n]` 之类会随界面变化的控件序号。
2. 截图和 Apple Vision OCR 是独立的安全复核层。即使 Accessibility 写入和回读成功，也必须再次验证模拟账户、方向、代码、价格、数量和确认/回执页面，并保留截图凭证。
3. OCR 坐标与 `AppBridge_UIMap.py` 只作为 Accessibility 无法识别自绘控件、WebView 或特殊弹窗时的受控兜底。UI Map 不参与常规导航或填单。

相关配置：

- `execution.ths_interaction_mode=accessibility_first`：先使用 Accessibility。
- `execution.ths_visual_fallback_enabled=false`：普通版当前默认关闭坐标兜底，Accessibility 失败时直接安全停止。只有完成普通版页面坐标校准和安全点击验证后才应改为 `true`。
- `execution.require_screenshot_verification=true`：必须保持开启，不因 Accessibility 可用而跳过视觉复核。
- `execution.ths_minimize_when_idle=true`：启动账户同步或成功交易后自动最小化同花顺；需要 App 操作时恢复并置前。确认弹窗或任何失败状态下保持可见。
- `execution.ths_app_ready_timeout_seconds=30`：等待同花顺窗口恢复、置前且 bounds 稳定的最长时间。

每次截图、OCR、Accessibility 操作和坐标点击前都会确认同花顺是未最小化的最前端窗口。截图后会保存前台进程、窗口 ID、bounds、截图尺寸和捕获方式；如果焦点、窗口 ID 或 bounds 在点击前发生变化，旧 OCR 坐标立即失效，桥接会停止并要求重新截图，不会继续点击。空闲最小化不是周期任务，用户之后手动打开同花顺不会被引擎再次强制最小化。

正常启动、账户同步和订单流程均禁用坐标兜底。“交易”侧栏如果没有 Accessibility 名称，只允许 OCR 识别语义锚点后对最近的匿名 `AXButton` 执行 `AXPress`；“模拟”“持仓”和订单控件使用命名 UI 元素。坐标点击只保留给显式 `visual_only` 诊断流程。

`execution.gui_bridge_command` 配置订单 AppleScript bridge；账户同步默认使用项目内置的 AppleScript + Apple Vision OCR bridge。交易引擎会写出 `screenshots/latest_order_intent.json`，然后等待并读取 `screenshots/latest_verified_order.json`；桥接实现必须写入账户模式、订单字段、提交状态、截图路径和可复盘日志。没有特殊要求时不使用 Computer Use 操作同花顺。

`gui_simulation` 阶段只要求完成填单并写回 `submitted=false` 的校验文件。`sim_run` 阶段在字段校验通过且 `execution.final_confirm_enabled=true` 后，可由受控 GUI 自动化点击最终的“买入(模拟账户)”或“卖出(模拟账户)”按钮，并写回 `submitted=true`；后台引擎只认校验文件和截图凭证，不假设具体由哪种工具点击。`execution.applescript_bridge_timeout_seconds` 控制引擎等待回写结果的最长时间。

当前安全配置为 `execution.stage=gui_simulation` 且 `execution.final_confirm_enabled=false`。恢复模拟账户自动最终提交前，必须先通过订单生命周期、部分成交、跨重启幂等和外部交易对账测试。

### 订单生命周期与交易日志

订单状态统一为 `VALIDATED`、`SUBMITTED`、`PARTIAL`、`FILLED`、`UNFILLED`、`CANCELLED` 和 `REJECTED`。只有已确认的非零成交量才会更新“今日已交易”、每日交易次数及连续卖出次数；`SUBMITTED` 和 `UNFILLED` 不再当作成交。

待处理订单保存在 `runtime_state.json`。同一标的存在待处理订单时，系统继续监控行情，但禁止重复提交，并按 `runtime.pending_order_alert_interval_minutes` 限频预警。系统不会自动撤单或重试。

人工在同花顺确认订单已经撤销或被拒绝后，可关闭本地待处理状态；该操作不会伪造成交，也不会更新交易次数：

```bash
python3 trading_engine.py --resolve-pending-order THS-合同号 --resolution-status CANCELLED --resolution-note "已在同花顺人工撤单并复核"
```

账户持仓变化优先匹配待处理系统订单。无法匹配的变化写入 `TradingLog.csv`，来源标记为 `EXTERNAL_OR_MANUAL`，只记录和预警，不中断系统。外部买入会重置连续卖出次数；外部卖出不计入策略连续卖出次数。委托价不会写成成交价；从账户成本或资金差额推算的价格会明确标记为估算值及置信度。

当前 AppleScript GUI bridge 已接入模拟账户买入和卖出路径，默认 `execution.gui_bridge_command` 会根据订单 intent 自动选择方向，并以 Accessibility 为主路径：

```bash
python3 AppBridge_AppleScript.py --action order --intent screenshots/latest_order_intent.json --verification screenshots/latest_verified_order.json
```

第一次调用使用 Accessibility 填入对应的模拟买入/卖出表单，逐字段回读后停在委托确认弹窗，再由 OCR 独立校验方向、代码、数量和价格并写回 `submitted=false`；卖出还会在打开确认弹窗前校验界面的可卖数量。当交易引擎在 `sim_run` 且 `final_confirm_enabled=true` 时，第二次调用会复用字段完全匹配的确认弹窗，通过 `AXSheet` 内按钮的 `AXPress` 完成最终确认。确认后固定使用 UI 元素先打开“委托”页、再打开“成交”页并保存截图；即时提交回执、匹配的委托记录或匹配的成交记录均可作为 `submitted=true` 的审计证据。

`AppBridge_UIMap.py` 保留用于采集、诊断和校准 Accessibility 未暴露的界面区域。它不会被交易引擎的正常订单流程自动调用；需要视觉兜底时，应先用安全页面验证坐标转换和目标页面，再允许桥接脚本使用该路径。

模拟账户可通过 `execution.simulation_allow_repeated_symbol_trades=true` 允许同一标的一日内正常多次买卖，但仍受 `risk.max_orders_per_day` 的每日总次数上限约束。实盘账户不使用该放宽规则。

不要把交易密码写入 `config.json`、脚本或任何会提交到 GitHub 的文件。需要输入交易密码时，只能通过受控交互按需输入，不写入仓库。

账户同步由受控 GUI 自动化读取同花顺模拟账户界面后写入 `screenshots/latest_account_snapshot.json`，格式类似：

```json
{
  "account_mode": "simulation",
  "total_assets": 50123.45,
  "available_cash": 12345.67,
  "cash_balance": 20000.0,
  "market_value": 30123.45,
  "profit_loss": -88.5,
  "source": "apple_vision_ocr",
  "submitted": false
}
```

也可以使用 Apple 原生只读快照桥进行模拟账户诊断：

```bash
python3 AppBridge_AppleScript.py
python3 apple_account_snapshot.py
python3 apple_account_snapshot.py --write-latest
```

`AppBridge_AppleScript.py` 会打开同花顺并导航到 App 内“模拟交易”的持仓页，写出 `screenshots/latest_applescript_bridge_holdings.json` 作为导航校验凭证；它不会填单或提交订单。导航校验与标准账户快照复用同一次窗口截图和 Apple Vision OCR，避免在同一持仓页重复识别；快照没有 `warnings` / `validation_errors` 时交易引擎才会接受。`apple_account_snapshot.py` 继续作为独立诊断工具，可单独通过 CoreGraphics、`screencapture` 和 Apple Vision OCR 读取账户页。Swift 窗口查询和 OCR helper 会按源码哈希编译并缓存到 `.cache/app_bridge/`，源码更新后自动失效。

`screenshots/` 自动清理策略在 `config.json` 的 `runtime.screenshots_cleanup` 中配置。默认启动时清理超过 7 天的历史截图/诊断 JSON，并在历史文件超过 300 个时优先删除最旧文件；`latest_*` 当前状态文件默认保留，避免误删正在被交易引擎读取的订单意图、校验结果或账户快照。

`trading_engine.py` 每次打开同花顺 App 后，都会先进入“交易 → 模拟 → 持仓”并重新读取账户快照，再同步 `portfolio.json` 的可用金额、总资产和目标标的持仓；导航或快照校验失败时会停止启动，不会进入策略轮询。持续轮询行情和策略时不会重复做账户验证。真正下单前、下单后仍会再次强制重新读取账户。

如果快照不存在、过期、无效或账户模式不是 `simulation`，引擎会调用内置 AppleScript + Apple Vision OCR bridge 重新生成账户快照并等待校验结果；超时后停止本次运行，避免用过期的本地资金/持仓继续判断交易。

这里的资金口径必须区分：

- `total_assets` / 总资产：只用于账户跟踪和仓位占比计算，会随行情波动，不作为下单资金依据。
- `cash` / `available_cash` / 可用金额：交易引擎里的“金额”口径，买入下单和风控只使用这个字段。

当策略生成买入/卖出信号后，交易引擎会在执行前强制重新读取同花顺模拟账户，刷新 `portfolio.json`，并用最新可用金额和持仓重新生成/校验信号。买入金额必须小于等于最新账户可用金额，卖出数量必须小于等于最新账户持仓。订单提交成功后，引擎会再次强制读取账户快照，确认交易后的资金和持仓状态。

执行限价口径：买入指令使用实时行情中的涨停价作为限价，卖出指令使用实时行情中的跌停价作为限价；如果行情源没有提供对应涨跌停价，本轮执行会被阻断。

同花顺 GUI 校验字段可以通过 `execution.verification_fields_path` 指向一个 JSON 文件，格式示例：

```json
{
  "account_mode": "simulation",
  "symbol": "588330",
  "side": "BUY",
  "quantity": 100,
  "limit_price": 1.234
}
```

如果未读取到这些字段，且 `require_screenshot_verification=true`，系统会阻断下单。

## 回测报告

默认回测区间为 `2025-01-01` 至今天，生成可交互网页报告，不生成 PNG，并自动用 Microsoft Edge 打开报告。成交价默认使用 `mootdx` 1 分钟 K 的分时价格，分时数据保存在本地 SQLite 数据库 `market_data.sqlite3`，后续运行只按数据库最新时间增量抓取新分钟线：

```bash
python3 backtest.py
```

输出文件：

- `backtest_588330_YYYYMMDD_YYYYMMDD.html`

如果只想生成文件、不打开浏览器：

```bash
python3 backtest.py --no-open
```

如果要回退到旧的日收盘价成交假设：

```bash
python3 backtest.py --no-open --execution-price close
```

如果要指定分时成交时间：

```bash
python3 backtest.py --no-open --execution-time 09:31
```

如果要忽略数据库最新时间并重新回补可获取的分钟线：

```bash
python3 backtest.py --no-open --refresh-minute-cache
```

如需额外导出 PNG：

```bash
python3 backtest.py --png
```

## 行情数据保存

项目根目录的 `market_data.sqlite3` 是统一行情数据库：

- `daily_bars`：日 K 数据。回测抓取和实时策略检查抓取的日 K 都会写入这里。
- `minute_bars`：1 分钟 K 数据。回测使用 `mootdx` 抓取的分时数据会写入这里。
- `realtime_quotes`：实时轮询行情。`trading_engine.py` 每次获取目标标的实时行情后会追加一条记录。

查看数据库表：

```bash
sqlite3 market_data.sqlite3 ".tables"
```

## 数据源约定

已安装 `a-stock-data` skill。后续 A 股行情、历史 K 线、交易日等数据获取，优先按照该 skill 的要求和数据源流程执行。

## 下一步需要补充

1. 真实可用资金和真实持仓：当前 `portfolio.json` 使用本地模拟状态。
2. 同花顺 App 自动化方式：按钮坐标、快捷键流程，或其他可用接口。
3. 卖出规则确认：当前按“分段最高浮盈回撤 20%”理解。

## 重要说明

真实交易前请先长时间 dry-run、GUI 模拟和 sim-run。自动化交易可能因为行情延迟、网络、App 弹窗、坐标偏移等原因产生错误操作。当前代码只允许在同花顺模拟账户下提交订单，不会绕过截图/字段校验直接点击真实最终确认。

## 更新记录

- 更新日期：2026-09-24
- 更新内容：同花顺侧栏交易入口在 OCR 未识别标签时改用已知位置定位无名称的辅助功能按钮，并过滤尚未展开的小窗口；启动和停止脚本默认使用项目虚拟环境的 Python。

- 更新日期：2026-08-26
- 更新内容：为同花顺 CoreGraphics 窗口 ID 获取增加短等待，复用 App 启动剩余时间，降低窗口刚出现但尚未进入窗口列表时的截图失败风险。

- 更新日期：2026-08-25
- 更新内容：收紧同花顺 CoreGraphics 窗口匹配条件，账户快照和 AppleScript 桥接只接受精确进程名，降低误选相近窗口的风险。

- 更新日期：2026-08-20
- 更新内容：修复已提交订单被误记为成交及连续卖出次数无法递增的问题；新增待处理订单、账户差额对账与本地 `TradingLog.csv` 审计；系统外交易仅跟踪预警；自动最终确认暂时切回 GUI 模拟阶段。

- 更新日期：2026-08-17
- 更新内容：收紧交易频率为每日仅允许一次买入或卖出；回测买入上涨条件固定为最近两日连续上涨；增强同花顺账户快照解析，支持 OCR 漏识别红色 0 的可用余额兜底，并记录模拟成交均价用于本地持仓成本更新。

- 更新日期：2026-08-11
- 更新内容：优化同花顺启动和窗口生命周期：统一 App 打开入口、复用持仓 OCR 快照、增加前台焦点/窗口证据保护、Swift helper 缓存、硬超时、阶段耗时日志及空闲安全最小化。

- 更新日期：2026-08-11
- 更新内容：更新 AppleScript 模拟交易桥：卖出前改用已验证账户快照校验可卖数量，确认弹窗字段校验限定在居中原生弹窗区域，并在账户快照解析中补充成本价。

- 更新日期：2026-08-10
- 更新内容：补充 agent 上传规范：每次上传 GitHub 前必须同步更新本节“更新内容”，并在 `agent.md` / `AGENTS.md` 中记录该要求。

- 更新日期：2026-06-30
- 更新内容：准备上传 GitHub：补充 GitHub 上传范围说明，排除回测报告、回测交易明细、行情数据库、运行状态、日志和 GUI 临时产物。

- 更新日期：2026-06-24
- 更新内容：补充同花顺 Mac 模拟交易桥接、安全账户模式校验、GUI 填单验证、行情数据节流与股票代码规范化，并更新自动化启动脚本。

- 更新日期：2026-06-18
- 更新内容：review README 并同步项目更新；交易入口调整为 `trading_engine.py`，补充实时行情层、持续运行/停止命令、项目结构、仓位目标买入、3% 止损、回测输出和测试说明。

- 更新日期：2026-06-14
- 更新内容：同步现有项目到 GitHub，补充 README 更新日期和更新内容说明。
