# AZYC001001 Wheel 回测首版计划（单ETF，事件驱动提速）

## Summary
1. 目标：基于你现有 `causis_api` 数据接口，先落地可跑通、可配置的 Wheel 回测框架。
2. 范围：单ETF、分钟级信号与下一bar `OPEN` 成交、不做账户约束、输出 `order/position` 并计算日频PNL与分析。
3. 关键优化：有持仓时不逐分钟盯盘，改为“事件驱动跳时 + 日收盘记录”，显著提升回测速度。

## Public API / 接口定义
1. 主入口：`run_backtest(config_path: str) -> dict`
2. 返回结构：`{"orders": df, "positions": df, "daily_pnl": df, "metrics": df, "artifacts_path": str}`
3. 分析入口：`analyze(result_dir: str) -> dict`，只读取落地 parquet 产出指标与图。
4. 配置入口：`config.yaml`（策略参数、成本参数、输出路径、回测区间）。

## 数据与类型约定
1. 期权链：`all_instruments('O', date)` + `instruments(symbols)`，统一字段 `Symbol/OptType/Strike/EndDate/MinTick/Multiplier`。
2. 行情：`get_price(..., frequency='minute1')`，字段 `SYMBOL/CLOCK/OPEN/HIGH/LOW/CLOSE/VOLUME`。
3. 交易日：`get_trading_dates`、`get_next_trading_date`。
4. `orders` 核心列：`order_id, ts_signal, ts_fill, symbol, side, effect, qty, price, fee, slippage, status, reason`。
5. `positions` 核心列：`ts, state, option_symbol, option_qty, strike, expiry, multiplier, etf_qty, etf_avg_cost, cash_ledger, equity_ledger`。
6. `daily_pnl` 核心列：`date, cash_eod, option_mv_eod, etf_mv_eod, equity_eod, daily_pnl, total_pnl_cum, drawdown`。

## 策略与执行规则（已定）
1. 状态机：`FLAT -> SHORT_PUT -> LONG_ETF_SHORT_CALL -> FLAT`，单腿并发。
2. Put选约：目标执行价 `S-0.2`，优先方向满足且最近；无候选时放宽方向取最近。
3. Call选约：目标执行价 `S+0.2`，同上规则。
4. 到期：统一次近月；若不可得则回退到可交易最近可用到期。
5. 成交：信号后下一bar `OPEN`。
6. 流动性过滤：信号bar与成交bar都需 `VOLUME > 20`。
7. 到期处理：用到期日 `15:00` 收盘价判定行权。
8. Put ITM：被指派接货ETF；Call ITM：被行权卖出ETF；OTM作废。
9. 覆盖比例：100% covered call。
10. 成本：手续费/滑点可配置，首版默认0。

## 速度优化实现（本次新增重点）
1. `FLAT` 状态：分钟级扫描入场机会。
2. 持仓状态（`SHORT_PUT` / `LONG_ETF_SHORT_CALL`）：不再逐分钟扫描策略。
3. 开仓后直接跳到“下一个关键事件时点”（到期日15:00，及日收盘估值点）。
4. `position/pnl` 在持仓期间仅按日收盘记录（你确认的口径）。
5. 这样把复杂度从“全区间全分钟扫描”降为“空仓分钟扫描 + 持仓日频估值 + 事件结算”。

## 落地结构与产物
1. 代码目录：`Wheel_backtest/AZYC001001/`（引擎、选约、执行、分析、入口）。
2. 配置文件：`Wheel_backtest/AZYC001001/config.yaml`（首跑参数从配置读取）。
3. 输出目录：`outputs/AZYC001001/`。
4. 固定产物：
5. `orders.parquet`
6. `positions.parquet`
7. `daily_pnl.parquet`
8. `metrics.parquet`
9. `equity_curve.png`
10. `drawdown.png`

## 测试与验收场景
1. 数据完整性：期权链与分钟行情字段齐全，`Multiplier/MinTick` 缺失时能回退拉取。
2. 选约正确性：Put/Call 正常场景与“放宽方向”场景都可选出合约。
3. 撮合正确性：下一bar成交、双`VOLUME>20`过滤、生效与拒单路径正确。
4. 状态流转：完整 `FLAT -> SHORT_PUT -> LONG_ETF_SHORT_CALL -> FLAT` 可复现。
5. 到期逻辑：Put接货、Call卖出、OTM作废三种结果正确。
6. PNL一致性：现金账+持仓估值与日频总PNL一致。
7. 性能验收：相较“全分钟扫描版本”，持仓期耗时明显下降且结果一致（在同一规则下）。

## Assumptions / 默认假设
1. 首版只跑单ETF。
2. 不考虑保证金、资金占用、强平等账户层约束。
3. 不做提前展期，仅预留扩展点。
4. 分析以日频收益、回撤、交易统计为主，不做Greeks风险归因。
5. 若日内15:00数据缺失，估值回退到当日最后可用bar。
