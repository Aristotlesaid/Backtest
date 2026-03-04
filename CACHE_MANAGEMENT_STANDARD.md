# Backtest 数据缓存管理规范

本规范用于统一 `Backtest/` 下各模块（如 `AZYC001001`、`ResearchIV`、`SharedData`）的缓存设计、命名、读写流程和清理策略。

## 1. 设计目标

- 保证同参数任务可复现、可复用，避免重复请求和重复计算。
- 保证缓存命中逻辑明确：先查缓存，未命中再计算/拉取。
- 保证缓存可追踪：每个缓存单元都能看到参数、版本、产出行数等元信息。

## 2. 目录约定

- 原始行情缓存：`DataCache/price/`、`DataCache/option_chain/`
- 研究计算缓存：`DataCache/research_iv/`（可扩展为 `DataCache/<module_name>/`）

建议结构：

```text
DataCache/
  price/
  option_chain/
  research_iv/
    <cache_key>/
      meta.json
      *.parquet
```

## 3. 缓存 Key 规范

- 采用“参数字典 -> JSON（sort_keys=True）-> SHA1 截断”方式生成 `cache_key`。
- `cache_key` 必须包含以下要素：
  - 计算引擎版本（如 `engine=term_atm_greeks_hv_v1`）
  - 时间参数（如 `input_date`、`trade_date`、`lookback_days`）
  - 标的参数（如 `etf_symbol`、`option_code`）
  - 定价参数（如 `risk_free_rate`、`dividend_yield`）
  - 合约选择结果（避免同日期不同选约逻辑冲突）

## 4. 读写流程规范（强制）

统一流程：

1. 先构建参数载荷并生成 `cache_key`
2. 优先检查缓存文件完整性
3. 命中则直接加载并返回（`from_cache=True`）
4. 未命中则执行计算
5. 计算成功后写入缓存并落地输出（`from_cache=False`）

## 5. 缓存文件规范

每个 `<cache_key>/` 下最少包含：

- `meta.json`：记录参数、版本、时间、行数、关键统计
- 结果明细 parquet（如 `greeks_panel.parquet`）
- 必要中间/索引 parquet（如 `selected_contracts.parquet`）

要求：

- 表格数据优先使用 `parquet`
- 若需要人工查看可额外输出 `csv`
- `meta.json` 编码统一 `utf-8`

## 6. 版本与失效策略

- 计算逻辑发生“口径变化”（字段、公式、选约、过滤规则）必须升级 `engine` 版本号。
- 版本升级后不强制删除旧缓存，允许并行存在。
- 若缓存损坏（文件缺失/读取异常），视为未命中并触发重算。

## 7. 清理策略

- 默认不自动删除缓存，避免误删有效结果。
- 清理建议按模块目录定向执行（例如只清 `DataCache/research_iv/`）。
- 生产使用可引入“按最后访问时间 + 保留天数”策略，避免无限增长。

## 8. 最佳实践

- API 原始数据缓存与研究结果缓存分层管理，不混放。
- 函数返回结构应包含：`from_cache`、`cache_key`、`cache_dir`，便于审计。
- Notebook 场景下即使命中缓存，也应重新生成可视化输出文件，保证“打开即看”。
