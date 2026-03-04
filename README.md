# Backtest

鍩轰簬 `causis_api` 鐨?Wheel 绛栫暐鍥炴祴椤圭洰锛屽綋鍓嶅疄鐜颁负 **AZYC001001 鍗?ETF 浜嬩欢椹卞姩鐗?*銆傜瓥鐣ラ€昏緫涓庝骇鐗╁榻?`PLAN.md`锛?
- 鐘舵€佹満锛歚FLAT -> SHORT_PUT -> LONG_ETF_SHORT_CALL -> FLAT`
- 淇″彿鍒嗛挓瑙﹀彂銆佷笅涓€鏍?bar 鐨?`OPEN` 鎴愪氦
- 鎸佷粨鏈熼噰鐢ㄢ€滀簨浠堕┍鍔?+ 鏃ラ浼板€尖€濆姞閫燂紝鑰屼笉鏄叏鍒嗛挓鎵弿
- 杈撳嚭璁㈠崟銆佹寔浠撱€佹棩棰戞崯鐩娿€佹寚鏍囧拰鍥捐〃

## 椤圭洰缁撴瀯

```text
Backtest/
鈹溾攢鈹€ PLAN.md
鈹溾攢鈹€ README.md
鈹斺攢鈹€ AZYC001001/
    鈹溾攢鈹€ AZYC001001_demo.ipynb
    鈹斺攢鈹€ Modules/
        鈹溾攢鈹€ run_backtest.py      # CLI 鍏ュ彛
        鈹溾攢鈹€ backtest_engine.py   # 鍥炴祴涓诲紩鎿?
        鈹溾攢鈹€ data_loader.py       # causis_api 鏁版嵁閫傞厤涓庣紦瀛?
        鈹溾攢鈹€ contract_selector.py # Put/Call 閫夌害
        鈹溾攢鈹€ state_machine.py     # 鐘舵€佷笌浠撲綅缁撴瀯
        鈹溾攢鈹€ execution.py         # 鎵嬬画璐?婊戠偣/鐜伴噾娴佽绠?
        鈹溾攢鈹€ analyze.py           # 鎸囨爣鍜屽浘琛?
        鈹溾攢鈹€ io_store.py          # parquet/csv 钀界洏
        鈹斺攢鈹€ config.yaml          # 鍥炴祴閰嶇疆
```

## 鍔熻兘姒傝

### 1) 鍥炴祴鍏ュ彛

- Python API锛歚run_backtest(config_path: str) -> dict`
- CLI锛?

```bash
python -m AZYC001001.modules.run_backtest --config AZYC001001/modules/config.yaml
```

鎵ц鍚庝細杩斿洖骞惰惤鐩橈細
- `orders`
- `positions`
- `daily_pnl`
- `metrics`
- 鍥惧儚锛歚equity_curve.png`銆乣drawdown.png`

### 2) 鏍稿績绛栫暐瑙勫垯

- **鍗?Put 寮€浠?*锛氱洰鏍囨墽琛屼环 `S-0.2`
- **鎸佹湁鐜拌揣鍚庡崠 Call**锛氱洰鏍囨墽琛屼环 `S+0.2`
- **鍒版湡澶勭悊**锛?
  - Put ITM锛氭寜鎵ц浠锋帴璐?ETF
  - Call ITM锛氭寜鎵ц浠峰崠鍑?ETF
  - OTM锛氭湡鏉冧环鍊煎綊闆?
- **娴佸姩鎬ц繃婊?*锛氫俊鍙?bar 涓庢垚浜?bar 閮借姹?`VOLUME > 20`
- **瑕嗙洊姣斾緥**锛?00% covered call

### 3) 浜嬩欢椹卞姩鎻愰€?

- 绌轰粨锛坄FLAT`锛夋寜鍒嗛挓鎵弿鍏ュ満
- 鎸佷粨鐘舵€佷笉鍐嶅叏鍒嗛挓鎵弿绛栫暐淇″彿
- 鑱氱劍鍏抽敭浜嬩欢锛堝埌鏈熺粨绠楋級骞舵寜鏃ユ敹鐩樿璐?
- 鍦ㄤ繚璇佺瓥鐣ヨ鍒欎竴鑷寸殑鍓嶆彁涓嬮檷浣庡洖娴嬪鏉傚害

## 鐜渚濊禆

寤鸿 Python 3.10+銆?

鏍稿績渚濊禆锛?
- `pandas`
- `pyyaml`
- `matplotlib`
- `pyarrow`锛坧arquet 璇诲啓锛?
- `causis_api`锛堣鎯呬笌鍚堢害鏁版嵁锛?

濡傛灉浣犺繕娌℃湁渚濊禆锛屽彲鍏堟墜鍔ㄥ畨瑁咃紙绀轰緥锛夛細

```bash
pip install pandas pyyaml matplotlib pyarrow
```

> `causis_api` 閫氬父鐢卞唴閮ㄧ幆澧冩彁渚涳紝璇锋寜浣犵殑鏁版嵁鐜閰嶇疆銆?

## 閰嶇疆璇存槑锛坄AZYC001001/modules/config.yaml`锛?

| 瀛楁 | 璇存槑 |
|---|---|
| `strategy_id` | 绛栫暐缂栧彿 |
| `etf_symbol` | ETF 鏍囩殑浠ｇ爜 |
| `option_code` | 鏈熸潈閾捐繃婊や唬鐮侊紙鍙€夛紝榛樿浠?`etf_symbol` 鑷姩鎻愬彇锛?|
| `start_date` / `end_date` | 鍥炴祴鍖洪棿 |
| `frequency` | 鏁版嵁棰戠巼锛堝 `minute1`锛?|
| `fixed_contracts` | 姣忔鍗栧嚭鍚堢害寮犳暟锛堜粎 `contract_sizing=fixed` 鏃朵娇鐢級 |
| `put_offset_abs` / `call_offset_abs` | Put/Call 鐩爣鍋忕Щ |
| `expiry_rule` | 鍒版湡绛涢€夎鍒?|
| `min_volume_signal` / `min_volume_fill` | 娴佸姩鎬ч棬妲?|
| `cost_option_fee_per_contract` | 鏈熸潈姣忓紶鎵嬬画璐?|
| `cost_option_slippage_ticks` | 鏈熸潈婊戠偣锛坱ick锛?|
| `cost_etf_fee_rate` | ETF 鎵嬬画璐圭巼 |
| `cost_etf_slippage_bps` | ETF 婊戠偣锛坆ps锛?|
| `option_prefetch_chunk_size` | 鏈熸潈琛屾儏棰勬媺鍙栧垎鍧楀ぇ灏?|
| `output_dir` | 杈撳嚭鐩綍 |

## 杩愯姝ラ

1. 鎸夋暟鎹幆澧冨噯澶?`causis_api`銆?
2. 鏍规嵁闇€瑕佷慨鏀?`AZYC001001/modules/config.yaml`銆?
3. 杩愯鍥炴祴锛?

```bash
python -m AZYC001001.modules.run_backtest --config AZYC001001/modules/config.yaml
```

4. 鏌ョ湅杈撳嚭鐩綍锛堥粯璁?`AZYC001001/outputs` 鐨勭浉瀵硅矾寰勶級涓殑锛?
   - `orders.parquet`
   - `positions.parquet`
   - `daily_pnl.parquet`
   - `metrics.parquet` / `metrics.csv`
   - `equity_curve.png`
   - `drawdown.png`

## 缁撴灉瀛楁锛堢畝鐗堬級

### orders
鍏抽敭鍒楋細
`order_id, ts_signal, ts_fill, symbol, side, effect, qty, price, fee, slippage, status, reason`

### positions
鍏抽敭鍒楋細
`ts, state, option_symbol, option_qty, strike, expiry, etf_qty, cash_ledger, equity_ledger`

### daily_pnl
鍏抽敭鍒楋細
`date, cash_eod, option_mv_eod, etf_mv_eod, equity_eod, daily_pnl, total_pnl_cum, drawdown`

## 娉ㄦ剰浜嬮」

- 褰撳墠鐗堟湰鑱氱劍 **鍗?ETF 棣栫増鍙繍琛屾鏋?*锛屼笉鍚繚璇侀噾/璧勯噾鍗犵敤/寮哄钩绛夎处鎴风害鏉熴€?
- 榛樿涓嶅仛鎻愬墠灞曟湡銆?
- 鑻ュ綋鏃?`15:00` 鏁版嵁缂哄け锛屼細鍥為€€鍒板綋鏃ユ渶鍚庡彲鐢?bar 鍋氫及鍊间笌缁撶畻銆?

---
濡傞渶涓嬩竴姝ワ紝鍙户缁ˉ鍏咃細
1) `requirements.txt/pyproject.toml`锛?) 鏈€灏忓彲澶嶇幇瀹為獙閰嶇疆锛?) 鍒嗘瀽鎶ュ憡妯℃澘銆?

