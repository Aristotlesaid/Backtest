# Backtest

基于 `causis_api` 的 Wheel 策略回测项目，当前实现为 **AZYC001001 单 ETF 事件驱动版**。策略逻辑与产物对齐 `PLAN.md`：
- 状态机：`FLAT -> SHORT_PUT -> LONG_ETF_SHORT_CALL -> FLAT`
- 信号分钟触发、下一根 bar 的 `OPEN` 成交
- 持仓期采用“事件驱动 + 日频估值”加速，而不是全分钟扫描
- 输出订单、持仓、日频损益、指标和图表

## 项目结构

```text
Backtest/
├── PLAN.md
├── README.md
└── AZYC001001/
    ├── AZYC001001_demo.ipynb
    └── Modules/
        ├── run_backtest.py      # CLI 入口
        ├── backtest_engine.py   # 回测主引擎
        ├── data_loader.py       # causis_api 数据适配与缓存
        ├── contract_selector.py # Put/Call 选约
        ├── state_machine.py     # 状态与仓位结构
        ├── execution.py         # 手续费/滑点/现金流计算
        ├── analyze.py           # 指标和图表
        ├── io_store.py          # parquet/csv 落盘
        └── config.yaml          # 回测配置
```

## 功能概览

### 1) 回测入口

- Python API：`run_backtest(config_path: str) -> dict`
- CLI：

```bash
python -m AZYC001001.Modules.run_backtest --config AZYC001001/Modules/config.yaml
```

执行后会返回并落盘：
- `orders`
- `positions`
- `daily_pnl`
- `metrics`
- 图像：`equity_curve.png`、`drawdown.png`

### 2) 核心策略规则

- **卖 Put 开仓**：目标执行价 `S-0.2`
- **持有现货后卖 Call**：目标执行价 `S+0.2`
- **到期处理**：
  - Put ITM：按执行价接货 ETF
  - Call ITM：按执行价卖出 ETF
  - OTM：期权价值归零
- **流动性过滤**：信号 bar 与成交 bar 都要求 `VOLUME > 20`
- **覆盖比例**：100% covered call

### 3) 事件驱动提速

- 空仓（`FLAT`）按分钟扫描入场
- 持仓状态不再全分钟扫描策略信号
- 聚焦关键事件（到期结算）并按日收盘记账
- 在保证策略规则一致的前提下降低回测复杂度

## 环境依赖

建议 Python 3.10+。

核心依赖：
- `pandas`
- `pyyaml`
- `matplotlib`
- `pyarrow`（parquet 读写）
- `causis_api`（行情与合约数据）

如果你还没有依赖，可先手动安装（示例）：

```bash
pip install pandas pyyaml matplotlib pyarrow
```

> `causis_api` 通常由内部环境提供，请按你的数据环境配置。

## 配置说明（`AZYC001001/Modules/config.yaml`）

| 字段 | 说明 |
|---|---|
| `strategy_id` | 策略编号 |
| `etf_symbol` | ETF 标的代码 |
| `option_code` | 期权链过滤代码 |
| `start_date` / `end_date` | 回测区间 |
| `frequency` | 数据频率（如 `minute1`） |
| `fixed_contracts` | 每次卖出合约张数 |
| `put_offset_abs` / `call_offset_abs` | Put/Call 目标偏移 |
| `expiry_rule` | 到期筛选规则 |
| `min_volume_signal` / `min_volume_fill` | 流动性门槛 |
| `cost_option_fee_per_contract` | 期权每张手续费 |
| `cost_option_slippage_ticks` | 期权滑点（tick） |
| `cost_etf_fee_rate` | ETF 手续费率 |
| `cost_etf_slippage_bps` | ETF 滑点（bps） |
| `option_prefetch_chunk_size` | 期权行情预拉取分块大小 |
| `option_prefetch_max_symbols` | 每个方向每个到期预拉取的最大合约数（按接近ATM优先） |
| `output_dir` | 输出目录 |

## 运行步骤

1. 按数据环境准备 `causis_api`。
2. 根据需要修改 `AZYC001001/Modules/config.yaml`。
3. 运行回测：

```bash
python -m AZYC001001.Modules.run_backtest --config AZYC001001/Modules/config.yaml
```

4. 查看输出目录（默认 `AZYC001001/outputs` 的相对路径）中的：
   - `orders.parquet`
   - `positions.parquet`
   - `daily_pnl.parquet`
   - `metrics.parquet` / `metrics.csv`
   - `equity_curve.png`
   - `drawdown.png`

## 结果字段（简版）

### orders
关键列：
`order_id, ts_signal, ts_fill, symbol, side, effect, qty, price, fee, slippage, status, reason`

### positions
关键列：
`ts, state, option_symbol, option_qty, strike, expiry, etf_qty, cash_ledger, equity_ledger`

### daily_pnl
关键列：
`date, cash_eod, option_mv_eod, etf_mv_eod, equity_eod, daily_pnl, total_pnl_cum, drawdown`

## 注意事项

- 当前版本聚焦 **单 ETF 首版可运行框架**，不含保证金/资金占用/强平等账户约束。
- 默认不做提前展期。
- 若当日 `15:00` 数据缺失，会回退到当日最后可用 bar 做估值与结算。

---
如需下一步，可继续补充：
1) `requirements.txt/pyproject.toml`，2) 最小可复现实验配置，3) 分析报告模板。
