# AZYC002001 铁鹰（Iron Condor）回测计划

## Summary
1. 目标：基于现有 `causis_api` 数据接口，落地可运行、可配置的单 ETF 铁鹰策略回测框架。
2. 范围：分钟级信号与下一 bar `OPEN` 成交；四腿同到期；持有到到期；到期按现金结算。
3. 定仓：默认 `contract_sizing=capital`，按单组名义最大亏损估算可开组数（保守，不扣权利金）。

## Public API / 接口定义
1. 主入口：`run_backtest(config_path: str) -> dict`
2. 返回结构：`{"orders": df, "positions": df, "daily_pnl": df, "metrics": df, "artifacts_path": str}`
3. 分析入口：`analyze(result_dir: str) -> dict`（读取 parquet/csv，输出指标与图）
4. 配置入口：`modules/config.yaml`

## 策略规则（本版）
1. 结构：卖出看跌价差 + 卖出看涨价差（四腿同到期，组成一组铁鹰）。
2. 选约口径（绝对价差）：
   - `short_put_strike_target = S - a`
   - `long_put_strike_target = S - b`
   - `short_call_strike_target = S + c`
   - `long_call_strike_target = S + d`
3. 默认参数：`a=0.10, b=0.20, c=0.10, d=0.20`。
4. 翼宽约束：`long_put_strike < short_put_strike`，`long_call_strike > short_call_strike`。
5. 到期筛选：`expiry_rule` 与 AZYC001001 一致（`near_month`/`next_month_2nd`）。
6. 成交规则：信号分钟触发，下一根 bar `OPEN` 成交。
7. 流动性过滤：信号 bar 与成交 bar 都要求 `VOLUME > threshold`。
8. 管理规则：开仓后持有到到期，不做提前止盈止损。
9. 到期结算：现金结算四腿内在价值，不生成 ETF 实物持仓。
10. 开仓节奏：组合到期平掉后，同日不重开，次日再开下一组。

## 定仓与风险口径
1. `fixed` 模式：每次固定开 `fixed_contracts` 组。
2. `capital` 模式：
   - `put_width = short_put_strike - long_put_strike`
   - `call_width = long_call_strike - short_call_strike`
   - `max_loss_per_set = max(put_width, call_width) * Multiplier`
   - `qty = floor(initial_capital / max_loss_per_set)`
3. 该口径偏保守，不使用净权利金抵扣最大亏损。

## 数据与产出
1. 数据源：`SharedData.causis_data_loader.CausisDataLoader`
2. `orders`：新增 `leg_role` 标识四腿（`SHORT_PUT/LONG_PUT/SHORT_CALL/LONG_CALL`）
3. `positions`：记录四腿符号、执行价、翼宽、每组最大亏损等字段
4. `daily_pnl`：日频现金账 + 持仓估值 + 回撤
5. 分析产物：
   - `metrics.parquet/csv`
   - `daily_enriched.parquet/csv`
   - `equity_curve.png`
   - `drawdown.png`
   - `monthly_pnl.png`
   - `rolling_sharpe_60d.png`
   - `excess_vs_benchmark.png`
   - `position_state.png`

## 关键配置项（`modules/config.yaml`）
1. `strategy_id`, `etf_symbol`, `start_date`, `end_date`, `frequency`
2. `short_put_offset_abs`, `long_put_offset_abs`, `short_call_offset_abs`, `long_call_offset_abs`
3. `expiry_rule`, `min_volume_signal`, `min_volume_fill`
4. `contract_sizing`, `initial_capital`, `fixed_contracts`
5. 成本参数：`cost_option_fee_per_contract`, `cost_option_slippage_ticks`
6. 缓存与输出：`use_disk_cache`, `cache_dir`, `output_dir`

## 验收要点
1. 四腿可同时选出且同到期，翼宽方向正确。
2. 开仓成交严格遵守下一 bar + 双成交量过滤。
3. 到期现金结算逻辑正确，结算后仓位清空并闭环周期。
4. `capital` 定仓在不同翼宽下能正确变化。
5. 输出字段完整，图表与指标可正常生成。
