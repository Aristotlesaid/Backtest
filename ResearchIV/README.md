# ResearchIV

当前功能（缓存优先）：
- 输入日期自动对齐到下一个交易日
- 选出当日近月、次近月各 4 档最接近平值执行价
- 对每份合约（Call/Put 分开）动态计算：`IV, Delta, Gamma, Theta, Vega`
- 同步叠加标的波动率：`HV30, HV60`
- 计算后写入缓存，后续同参数优先读取缓存

主入口 Notebook：`ResearchIV_main.ipynb`

## Notebook 一行查看

```python
from Modules import notebook_show_term_atm_greeks

res = notebook_show_term_atm_greeks(
    input_date="2026-02-20",
    etf_symbol="S.CN.SZSE.159915",
    lookback_days=240,
)
```

这会直接展示：
- 选中合约表
- Greeks 面板尾部数据
- 近月/次近月图
- 总览图

## 脚本调用

```python
from Modules import build_term_atm_greeks_hv_snapshot

res = build_term_atm_greeks_hv_snapshot(
    input_date="2026-02-20",
    etf_symbol="S.CN.SZSE.159915",
    lookback_days=240,
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

## 缓存目录

`DataCache/research_iv/<cache_key>/`

- `meta.json`
- `selected_contracts.parquet`
- `greeks_panel.parquet`
