# Spec：訊號研究迴路與策略層重構

狀態：草案 · 建立於 2026-09-11 · 基準 commit `fe4801e`
前置文件：[code-review-2026-09-11.md](code-review-2026-09-11.md)（工程面）

---

## 0. 背景與這份 spec 要解決的問題

2026-09-11 對 38.2 小時、46 檔、632,500 個 symbol-觀測做了離線重建與回測，
重建值對照 `app.scoring.activity_score` 與 `app.trade_signal.calculate_trade_signal`
在 4,000 個抽樣點上**零誤差**，因此以下結論成立於現行程式碼：

| 發現 | 數據 |
|---|---|
| 進場門檻 `entry_activity_score: 80` 從未被觸及 | 632,500 個觀測中 **0** 次；p99.9 只有 ~45 |
| 分數對「方向」沒有資訊 | symbol-demean 後的擇時 alpha：1m +0.0、5m −0.5、15m −0.4、60m −3.3 bps，全部 \|t\| < 1.8 |
| 障礙尺寸錯誤 | TP 2% 超過 60 分鐘移動的 p95（1.75%）；回測中絕大多數以 TIME_STOP 出場 |
| 掃過 11 個門檻 × 6 組障礙幾何 | 沒有任何組合有正期望值 |
| 每筆期望值 | 擇時 alpha −3.3 bps − 成本 11.4 bps = **−14.7 bps** |
| **分數確實預測「波動」** | 同一檔內部，score 到自身 p99 時下一分鐘 \|報酬\| 為平常的 **1.6 倍**（IC +0.017，45 檔中 30 檔為正） |

**核心診斷**：測量層是好的，它能偵測「這裡正在發生事情」；但它對「往哪個方向」零資訊。
現行策略把一個**波動偵測器**當成**方向下注**在用。

**這份 spec 不要求把策略做到賺錢。** 它要求建立能誠實回答「有沒有邊際」的機制，
並在證據出現之前，不允許再往策略層堆東西。

---

## 1. 不做什麼（給執行 agent 的硬性限制）

以下行為會讓這份 spec 失敗，即使測試全綠：

1. **不得為了讓回測數字變好而調整門檻、障礙、權重。** 參數若要改，理由必須是
   「量綱錯誤」或「與定義不符」，不能是「這樣回測比較賺」。
2. **不得刪除或放寬既有測試來讓新程式碼通過。** 既有 104 個測試是資料層正確性的護欄。
3. **不得在 Phase 2 產出證據之前實作任何新的進場策略。** Phase 4 有明確的啟動條件。
4. **不得把研究程式碼寫進 `app/` 的執行路徑。** 研究模組是唯讀的旁路。
5. **不得改動 `app/models.py` 的欄位語意**（`timestamp` = 來源時間、`received_at` = 本地接收時間）。
   離線重建能對得上線上，靠的就是這個。
6. 任何「我認為這樣比較好」的設計偏離，寫進 PR 描述，不要靜默實作。

---

## 2. Phase 0 — 修正會污染研究結論的資料缺口

**目標**：讓 Phase 1 之後的所有統計建立在密度一致的資料上。

### 0.1 查明並修復 Binance perp 訂單簿的資料稀疏

實測（2026-09-09 ~ 09-11）：

| feed | 每檔列數 | 觀測間隔中位數 |
|---|---:|---:|
| binance spot | 24,591 | 2.80s |
| okx spot | 25,007 | 2.70s |
| okx perp | 24,910 | 2.71s |
| **binance perp** | **3,165** | **15.50s**（p99 = 169s、最大 323s） |

Binance perp 是 `_preferred_price` 的第一順位、也是 `_execution_book` 的第一順位執行場所，
卻是密度最低的 feed。這同時污染參考價、脆弱度百分位與所有回測。

**任務**：
- 加一個診斷腳本 `backend/research/feed_health.py`，輸出每個
  (exchange, market) 的寫入間隔分位數、缺口 > 60s 的次數與時間分布。
- 判斷成因並修復。候選（依可能性排序）：
  1. perp chunk 的 WebSocket 任務反覆斷線／曾經永久死亡（`fe4801e` 已加監管，需重新量測確認）
  2. `_available_book` 的 10 秒 staleness 規則把 perp 書擋掉（檢查 `received_at` 與 `E`/`T` 欄位的實際落差）
  3. `_market_watermarks` 的來源時間戳沒有推進（檢查 Binance futures depth 事件的 `E` 是否被正確取用）
- **驗收**：`fe4801e` 之後跑滿 6 小時，binance perp 的寫入間隔中位數與其他三個 feed
  的差距在 2 倍以內；缺口 > 60s 的次數為 0。若成因是交易所端限制而非 bug，
  寫進 `docs/data-quality.md` 並在 `_preferred_price` 改為偏好密度較高的 feed。

### 0.2 universe 清理

- `USDT` 正規化為 `USDTUSDT`，兩所皆不存在，浪費一個名額 → 移除或 `enabled: false`。
- `USDC`、`DAI` 是穩定幣，深度極深、價差趨近 0，會成為
  `liquidity_fragility_scores` 跨幣百分位的固定極值，系統性推高其餘 43 檔的分數。
  → `UniverseAsset` 增加 `stablecoin: bool = False`，在 `liquidity_fragility_scores`
  的取樣母體中排除，但仍照常監控與顯示。
- 驗證 Binance 的 `1000X` 合約命名（`1000PEPEUSDT` / `1000SHIBUSDT`）。
  若屬實，在 `BinanceAdapter._parse_perp` 加 alias：辨識 `1000`/`1000000` 前綴，
  symbol 正規化回 `PEPEUSDT`，並把 `base_quantity_multiplier` 設為對應倍數。
  用 `python -m app discover-live-markets` 對線上驗證，不要憑記憶。
- **驗收**：`discover-live-markets` 回報的 perp 市場數，binance 與 okx 的差距 ≤ 2。

---

## 3. Phase 1 — 建立研究迴路（本 spec 的核心交付）

**目標**：讓「這個訊號能不能預測什麼」變成一條指令就能回答的問題。

新增 `backend/research/`（唯讀模組，不得被 `app/` import）：

```
backend/research/
  __init__.py
  panel.py        # 從 DB 重建特徵面板
  forward.py      # 前瞻報酬、symbol-demean、區塊叢集統計
  evaluate.py     # IC / 響應曲線 / 三重障礙回測
  feed_health.py  # Phase 0.1
  cli.py          # python -m research <command>
```

### 1.1 `panel.py` — 特徵面板重建

```python
def build_panel(
    session_factory, *, start: datetime, end: datetime,
    step_seconds: int = 10, fresh_tolerance_seconds: int = 30,
) -> Panel: ...
```

`Panel` 是一個帶 `grid`（epoch 秒）、`symbols`、以及每個特徵一個
`(T, S)` `float32` 陣列的容器。必須重建的欄位，且**必須沿用 `app/` 的真實函式**
（不得複製一份邏輯）：

- `activity_score`（呼叫 `app.scoring.activity_score`，或提供向量化版本 + 見 1.4 的保真度測試）
- `bias`（`app.trade_signal.calculate_trade_signal` 的語意）
- `liquidity_fragility`、`move_type`、`cross_exchange_state`
- 各成分：`pressure_component`、`flow_component`、`delta_ratio`、`oi_change_5m`、`funding`
- `price`（依 `_preferred_price` 的順位）、`tradable`（是否有可執行的 perp 簿）

聚合規則必須鏡射 `runtime._persist_flow`：某交易所只有在該時點**有新鮮的簿**時
才貢獻成交量，深度是新鮮簿的 2% 深度總和。

### 1.2 `forward.py` — 前瞻報酬與正確的統計

```python
def forward_returns(panel, horizons_seconds) -> dict[str, np.ndarray]   # bps
def demean_by_symbol(returns, tradable) -> np.ndarray                   # 剝除市場/個股漂移
def block_stats(values, time_index, block_size) -> BlockStat            # mean, t, n, k
```

三個統計上的必要條件，缺一不可：

1. **symbol-demean**：樣本期間可能是單邊行情（實測那 38 小時是 43 檔中 39 檔下跌、
   中位數 −6.02%）。不 demean 就會把 beta 誤認為 alpha。
2. **非重疊區塊**：60 分鐘水平在 10 秒網格上重疊 360 倍，naive 標準誤會被高估數十倍。
   以固定區塊 `t // horizon` 切分，**所有子集共用同一組區塊**（否則 LONG/SHORT/合計
   會互相矛盾 —— 這在初次分析中實際發生過，一個 t=+2.48 的格子在改用共用區塊後變成 t=−1.26）。
3. **時間叢集**：46 檔同時同向移動，跨 symbol 高度相關。先對每個區塊取跨 symbol 平均，
   再對區塊序列做 t 檢定。
4. `BlockStat` 必須同時回報 `k`（獨立區塊數）。**`k < 30` 的 t 值一律標記為不可用。**

### 1.3 `evaluate.py` — 三種評估

```python
def response_curve(panel, feature, horizons, buckets) -> DataFrame
def rank_ic(panel, feature, horizons, *, within_symbol: bool = True) -> DataFrame
def barrier_backtest(panel, config: BacktestConfig) -> BacktestResult
```

**`rank_ic` 的 `within_symbol` 預設為 True。** pooled IC 會被跨幣波動差異汙染 ——
實測中 pooled IC 為 −0.049 但 within-symbol 為 **+0.017**，結論完全相反。

`barrier_backtest` 必須：
- 完整重現 `PaperTradingEngine` 的狀態機（ARMED/TRIGGERED/COOLDOWN、
  `signal_persistence_seconds`、`max_open_positions` 的跨 symbol 全域上限）
- **停損先於停利判斷**（移除現行的樂觀 tie-break）
- 扣除成本：`2 × fee_bps + 2 × 該檔的中位半價差`
- 同時產出一組**對照組**：每筆真實訊號，配一筆同檔、同方向、隨機時點的假想交易。
  報告必須並列「訊號組 vs 對照組」。這是防止自欺最有效的單一機制。

### 1.4 保真度測試（**必須**，這是整個研究迴路的信任基礎）

新增 `backend/tests/test_research_fidelity.py`：

- 建構一組合成的 market/flow/derivative 列，餵進 `build_panel`
- 對同樣的輸入直接呼叫 `app.scoring.activity_score` 與
  `app.trade_signal.calculate_trade_signal`
- 斷言**每一點**的 score 差 < 0.01、bias 完全相等
- 這個測試若失敗，代表研究結論與線上行為脫節 → CI 必須紅燈

**Phase 1 驗收**：

```sh
python -m research evaluate --feature activity_score \
    --horizons 1m,5m,15m,60m --start ... --end ...
```

輸出響應曲線、within-symbol IC、障礙回測（含對照組），並且
`test_research_fidelity` 通過。**此階段不要求任何正報酬。**

---

## 4. Phase 2 — 把分數重新定義成它實際上的東西

**目標**：`activity_score` 現在是一個量綱錯誤的任意配分。實證上它是
**同一檔內部的異常/波動偵測器**。照這個事實重新設計。

### 2.1 現行配分的量綱錯誤（Phase 1 完成後才動）

```python
10 * _bounded_magnitude(oi_change)              # oi 是比例：5% -> 0.5/10 分
5  * _bounded_magnitude(funding, scale=1_000)   # funding 1e-4 -> 0.5/5 分
```

OI 5 分鐘變動 5%（劇烈加倉）只拿到 0.5 分；要拿滿 10 分需要 5 分鐘內 OI 變動 100%。
這 15 分實務上只拿得到 ~1 分。加上 flow 那 30 分需要成交 100% 單邊才滿分，
**實際天花板約 78，門檻 80 在天花板之上。**

### 2.2 改為 per-symbol 滾動百分位

不要只是換一組魔術數字。改成：每個成分對**該檔自身的滾動分布**取百分位。

```python
@dataclass(frozen=True)
class AbnormalityScore:
    """每個成分 = 相對於該 symbol 自身 24h 滾動分布的百分位 (0..1)。"""
    pressure_pct: float
    flow_imbalance_pct: float
    oi_change_pct: float
    funding_pct: float
    spread_pct: float
```

- 滾動視窗長度可設定，預設 24h；暖機期不足時回傳 `None` 而非 0。
- 總分是各成分百分位的加權平均 × 100，權重可設定但**預設等權**
  （沒有證據支持任何特定權重，不要假裝有）。
- 好處：自動修正量綱、跨幣可比、跨時間可比、門檻有意義
  （「這檔自己的 p99」而不是「80 分」）。
- **命名要誠實**：改名為 `abnormality_score` 或保留 `activity_score` 但在 docstring
  明確寫「這衡量市場狀態的異常程度，實證上與後續**波動**相關、與**方向**無關」。

### 2.3 門檻改為百分位

`config/scoring.yaml`：

```yaml
paper_trading:
  entry_percentile: 0.99      # 取代 entry_activity_score: 80
  rearm_percentile: 0.80      # 取代 rearm_activity_score: 65
```

並在啟動時做一次健全性檢查：若配置的門檻在暖機後 1 小時內觸發次數為 0，
記 `logger.warning`。**現行系統缺的就是這個 —— 一個打不到的門檻活了好幾個版本沒人發現。**

**Phase 2 驗收**：
- 用 Phase 1 的工具重跑同一段歷史，新分數的 within-symbol IC（對 \|報酬\|）
  不低於現行的 +0.017
- 新門檻在歷史資料上的觸發頻率落在每檔每天 0.5 ~ 5 次的量級（可實際檢定的頻率）
- `docs/scoring.md` 記錄每個成分的定義、單位、與實證關聯

---

## 5. Phase 3 — 重做 paper trading 為「假說檢定工具」

**目標**：現在的 paper trading 是一個沒有 edge 的策略。改成一個能**誠實檢定任意假說**的框架。

### 3.1 障礙必須波動率標準化

固定 2%/1% 對 BTC（60m 波動 ~0.3%）幾乎必定 TIME_STOP，對小市值 alt 則是純噪音。
實測 60 分鐘 \|報酬\| 的 p50 = 0.40%、p95 = 1.75% —— TP 2% 在 p95 之外。

```yaml
paper_trading:
  take_profit_atr: 1.5        # 取代 take_profit_pct
  stop_loss_atr: 1.0          # 取代 stop_loss_pct
  atr_window_minutes: 60
```

ATR 由 `market_metrics` 的價格序列在 runtime 內滾動計算（每檔一個
`RollingVolatility`，與 `RollingTradeFlow` 同構）。

### 3.2 修掉三個同向的樂觀偏誤

| 偏誤 | 現況 | 要求 |
|---|---|---|
| tie-break | TP 判斷排在 SL 之前 | SL 先判斷 |
| 取樣 | 1 秒快照會漏掉盤中觸及 SL 後回彈 | 用已在追蹤的 `max_adverse_excursion_pct` 檢查區間內是否已觸及 SL |
| 成交 | `simulate_market_fill` 假設零延遲、靜態簿子 | 加入可設定的延遲（預設 250ms）：以 `t + latency` 的簿子成交；若該時點無新鮮簿則記為 `SKIPPED` |

### 3.3 每筆訊號都要有對照組

`PaperTradeRow` 增加 `control_*` 欄位，或另建 `paper_control_trades` 表：
每筆真實進場，同時記錄一筆「同檔、同方向、同障礙、但進場時點隨機」的假想交易。
`/api/paper/stats` 並列兩者。

**若訊號組與對照組的差異在統計上不顯著，UI 必須明白顯示「無證據支持此訊號」。**

### 3.4 計入資金費率

持倉跨越結算時點（00:00 / 08:00 / 16:00 UTC）就扣一次 funding。
現在 `entry_funding` 有存但從不計費。

### 3.5 出場價用部位自己的場所

`_maintain_position` 目前用 `detail["price"]`（偏好 Binance perp 的聚合中價），
但部位可能開在 OKX。改為讀
`detail["orderbooks"][position["exchange"]][position["market"]]` 的中價。

### 3.6 `_execution_book` 簡化

現行是 48 行的遞迴走訪，處理 `runtime` 從不產生的別名（`perpetual`/`swap`）。
輸入形狀恆為 `_public_books()` 的 `{exchange: {market: {...} | None}}`。
改成直接查表（約 10 行）。好處：runtime 輸出格式改變時會**直接壞掉並被測試抓到**，
而不是靜默回傳 `None`（表現為「永遠不進場」—— 最難查的那種 bug）。

**Phase 3 驗收**：
- 用 Phase 1 的 `barrier_backtest` 與線上 paper trading 跑同一段歷史，
  兩者的成交筆數與 P&L 差異 < 5%（離線與線上一致，這是 Phase 1 保真度的延伸）
- 對照組機制上線並在 `/api/paper/stats` 呈現

---

## 6. Phase 4 — 方向性訊號（**閘門**）

**啟動條件**：Phase 1 的工具在**至少 2 週、涵蓋上漲與下跌兩種情境**的資料上，
找到某個特徵在某個水平上滿足全部三項：

1. symbol-demean 後的擇時 alpha **> 15 bps**（成本地板為 11.4 bps）
2. 區塊叢集 t 值 **|t| > 2.5**，且獨立區塊數 **k > 50**
3. 在樣本外的另一段時間維持同號

**條件未滿足前，不得實作任何新的進場邏輯。** 若 4 週後仍無特徵通過，
正確結論是「以目前的資料頻率與成本結構，此系統不支援方向性交易」，
把它定位成監控與研究平台，並在 README 明說。

### 若要探索，優先順序

現行 `TradeSignal` 只讀 `*_5m`，而 `_market_pressure` 還把 5m 權重設到 0.55。
訂單流失衡的可預測期是**秒到 1 分鐘**，5 分鐘水平已在衰減曲線的另一側。

1. 先在**秒級**找：把 `WINDOWS_SECONDS` 的 10s 窗接進 `TradeSignal`，
   用 Phase 1 的工具測 10s/30s/60s 水平的 IC。
2. 成本地板 11.4 bps 意味著吃單無法存活。若秒級真有訊號，
   下一步是掛單成交模型，不是加大部位。
3. 脆弱度是目前唯一有實證價值的輸出（波動預測）。它適合當
   **部位大小的分母**或**風險閘門**，不是進場訊號。

---

## 7. 執行順序與各階段的獨立價值

| Phase | 內容 | 前置 | 就算後續全部放棄也有價值？ |
|---|---|---|---|
| 0 | 資料缺口 | — | 是，資料品質 |
| 1 | 研究迴路 | 0 | **是，這是最高價值的單一產出** |
| 2 | 分數重新定義 | 1 | 是，監控/告警品質 |
| 3 | paper trading 改為檢定工具 | 1 | 是，避免自欺 |
| 4 | 方向性訊號 | 1、2、3 + **證據閘門** | 未知，這正是要檢定的 |

Phase 1 建議獨立成一個 PR 先合併，之後每個 Phase 一個 PR。

---

## 8. 給執行 agent 的檢查清單

每個 PR 都要能回答：

- [ ] 既有 104 個測試仍然全過，且沒有測試被刪除或放寬
- [ ] `ruff check` 與 `ruff format --check` 通過
- [ ] `test_research_fidelity` 通過（Phase 1 之後）
- [ ] 沒有任何參數是為了讓回測數字變好而改的；每個參數變更在 PR 描述中有量綱或定義上的理由
- [ ] 新增的統計都有回報獨立樣本數 `k`，且 `k < 30` 時標記不可用
- [ ] `app/` 沒有 import `research/`
- [ ] 若偏離本 spec，理由寫在 PR 描述

---

## 附錄：可直接參考的既有分析腳本

2026-09-11 的分析腳本（`build.py` / `analyze.py` / `analyze2.py` / `analyze3.py` /
`analyze4.py`）已驗證可行，包含面板重建、保真度驗證、symbol-demean、
共用區塊叢集統計、within-symbol IC、三重障礙回測與對照組。
Phase 1 應以此為藍本整理成正式模組，而非從零開始。
