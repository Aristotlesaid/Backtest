# ResearchIV

当前功能（缓存优先）：
- 输入日期默认按回退方式对齐到最近交易日（on/before）
- 选出当日近月、次近月各 4 档最接近平值执行价
- 对每份合约（Call/Put 分开）动态计算：`IV, Delta, Gamma, Theta, Vega`
- 同步叠加标的波动率：`HV30, HV60`
- 新增日内 IV 板块：每个合约一张图，观察从起始日到到期前的分钟级 IV 走势，并叠加 HV30/HV60
- 新增选约模式参数：
  - `selection_mode="near_next"`：沿用输入日近月+次近月
  - `selection_mode="expired_lifecycle"`：回看最近已到期月份，支持 `lifecycle_start_month_offsets=[3,6]` 这类生命周期窗口
- 计算后写入缓存，后续同参数优先读取缓存

主入口 Notebook：`ResearchIV_main.ipynb`

## Notebook 一行查看

```python
from Modules import notebook_show_term_atm_greeks

res = notebook_show_term_atm_greeks(
    input_date="2026-02-20",
    etf_symbol="S.CN.SZSE.159915",
    lookback_days=240,
    trade_date_align="backward",
    selection_mode="expired_lifecycle",
    strikes_per_term=4,
    lifecycle_start_month_offsets=[3, 6],
    lifecycle_anchor_expiry=None,
    include_intraday_iv=True,
)
```

这会直接展示：
- 选中合约表
- Greeks 面板尾部数据
- 近月/次近月图
- 总览图
- 每个合约一张的日内 IV + HV 图

## 脚本调用

```python
from Modules import build_term_atm_greeks_hv_snapshot

res = build_term_atm_greeks_hv_snapshot(
    input_date="2026-02-20",
    etf_symbol="S.CN.SZSE.159915",
    lookback_days=240,
    trade_date_align="backward",
    selection_mode="expired_lifecycle",
    strikes_per_term=4,
    lifecycle_start_month_offsets=[3, 6],
    lifecycle_anchor_expiry=None,
    include_intraday_iv=True,
    risk_free_rate=0.015,
    dividend_yield=0.0,
    use_calc_cache=True,
)

print(res["trade_date"], res["from_cache"])
panel = res["greeks_panel"]
```

兼容旧接口：`build_term_atm_iv_hv_snapshot(...)` 仍可用，内部映射到新实现。

## 输出目录

`ResearchIV/outputs/term_atm_greeks_hv_YYYYMMDD/`

- `selected_contracts.csv`
- `greeks_panel.csv`
- `greeks_panel.parquet`
- `NearTerm_Expiry_*.csv`
- `NextTerm_Expiry_*.csv`
- `NearTerm_Expiry_*.png`
- `NextTerm_Expiry_*.png`
- `term_atm_greeks_hv_overview.png`
- `intraday_iv/intraday_iv_panel.csv`
- `intraday_iv/intraday_iv_panel.parquet`
- `intraday_iv/<symbol>.csv`
- `intraday_iv/<symbol>.png`

## 缓存目录

`DataCache/research_iv/<cache_key>/`

- `meta.json`
- `selected_contracts.parquet`
- `greeks_panel.parquet`
- `intraday_iv_panel.parquet`
