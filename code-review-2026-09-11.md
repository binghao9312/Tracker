# Qtrade 程式碼檢視與修改建議

檢視日期：2026-09-11 · 範圍：`backend/app`（~4,800 行）、`frontend/src`、infra 設定
基準狀態：`pytest` 104 passed · `ruff check` 全過 · commit `bd1bf0d`

整體評價：架構分層清楚（models / exchanges / collectors / 指標引擎 / repository / runtime），
序號校驗、freshness watermark、rate-limit backoff 這些「難的地方」都有認真處理，測試覆蓋也不錯。
問題集中在三塊：**(A) 錯誤處理與任務監管的破口**、**(B) 每秒 cadence 的計算預算已經超支**、
**(C) 幾個指標語意上的不一致**。

---

## P0：正確性缺陷

### 1. `GET /api/paper/trades/{id}` 對「未平倉」交易一律 500（已重現）

[api.py:215](backend/app/api.py:215)

```python
end = _timestamp(trade.get("closed_at")) or datetime.now().astimezone()
```

`_timestamp(None)` 會走到 `datetime.fromisoformat("None")` → `ValueError`。
`or` 永遠沒機會生效，因為 exception 在 `or` 之前就丟出來了。

實測：以 `status="OPEN"`、`closed_at=None` 的 trade 打這個 endpoint → **HTTP 500**。
`test_replay_api.py` 只測了 CLOSED 路徑，所以 CI 綠燈。前端 `TradeTable` 剛好只在
`status === "CLOSED"` 才呼叫 replay，把這個 bug 遮住了，但 API 本身是壞的。

```python
closed_at = trade.get("closed_at")
end = _timestamp(closed_at) if closed_at is not None else datetime.now(UTC)
```

順帶：`datetime.now().astimezone()` 是本機時區，全專案其他地方一律 `datetime.now(UTC)`，
這裡應統一，否則跨時區部署時 `history_range` 的上界會偏移。

**補測試**：`test_replay_api.py` 加一個 OPEN trade 的案例。

---

### 2. 沒有任何任務監管 —— 串流任務死掉不會被發現，也不會重啟

[runtime.py:96](backend/app/runtime.py:96)、[collectors/orderbooks.py:157](backend/app/collectors/orderbooks.py:157)

`LiveRuntime._tasks` 建立後就再也沒被檢查過。而各 manager 的 `run()` 只捕捉窄集合：

```python
except (aiohttp.ClientError, OSError, ValueError, OrderBookSequenceGap) as error:
```

任何落在集合外的例外會直接讓 task 結束，永久停止該 chunk 的訂閱，且**沒有任何 log**
（只有 `stop()` 時的 `gather(return_exceptions=True)` 會默默吞掉）。

這不是理論問題，有具體觸發路徑：[orderbooks.py:473](backend/app/collectors/orderbooks.py:473)

```python
def _integer(data, name) -> int:
    value = data.get(name)
    if isinstance(value, bool):
        raise ValueError(...)
    return int(value)          # value 是 None → TypeError，不是 ValueError
```

呼叫端 [orderbooks.py:320](backend/app/collectors/orderbooks.py:320) 捕捉
`(KeyError, ValueError, InvalidOperation)`，`TypeError` 穿透 → task 永久死亡。
交易所回一則欄位缺漏的訊息，那 20 檔的 order book 就此消失，前端只會看到 `stale` 然後永遠不恢復。

建議三件事：

1. `_integer` 顯式處理：`if not isinstance(value, (int, float, str)): raise ValueError(...)`。
2. `run()` 的 except 改成 `except Exception`（`CancelledError` 已在上一個 clause 攔掉），
   把 unexpected exception 也納入 backoff 重連，並用 `logger.exception` 記錄。
3. 加一個 supervisor：`_run_cadence` 每輪順便檢查 `task.done()`，發現死亡就記錄並重建，
   或至少在 `DashboardState` 曝露一個 `/api/health` 讓外部監控看到 degraded。

---

### 3. OKX 衍生品的全域節流形同虛設

[runtime.py:376](backend/app/runtime.py:376) vs [exchanges/derivatives.py:126](backend/app/exchanges/derivatives.py:126)

```python
# runtime._derivative_task —— 每個 instrument 都 new 一個 provider
provider = OkxDerivativesProvider(AiohttpJsonClient(self._session))

# OkxDerivativesProvider.__init__
self._semaphore = asyncio.Semaphore(1)   # 只保護「自己這一檔」
```

`Semaphore(1)` 是 per-instance 的，而 instance 是 per-instrument 的 → 完全沒有互斥效果。
`await asyncio.sleep(0.08)` 那些也只在單一檔內串行化。實際上約 44 檔 OKX 永續會同時發射，
真正的節流只剩 `initial_delay = (index % 10) * 1.5` 這個開機錯開，跑一陣子就會漂移對齊。

Binance 那條路徑做對了（`BinanceDerivativeScheduler` 由 runtime 建立一次、共享給所有 provider）。
OKX 應比照：建立一個共享的 `OkxDerivativeScheduler`，或直接改用批次端點（見 §8）。

---

### 4. Paper 事件會被靜默丟棄

[api.py:137](backend/app/api.py:137)

```python
queue: asyncio.Queue = asyncio.Queue(maxsize=1)
...
if queue.full():
    queue.get_nowait()      # 丟掉最舊的
queue.put_nowait(message)
```

對 `scanner` / `symbol:*` 這種「最新值快取」語意，drop-oldest 是對的。
但 `paper` channel 送的是**事件流**（TRADE_OPENED / TRADE_CLOSED / SKIP），
只要客戶端稍慢或同一輪 flush 產生兩個事件，就會永久遺失一筆成交紀錄。

建議 `subscribe()` 帶一個 `latest_only: bool` 參數，`paper` 用較大的
`maxsize`（例如 256）且滿了就斷線讓前端重連重抓，而不是無聲丟事件。

---

### 5. 前端 WebSocket 斷線後不會重連

[App.tsx:85](frontend/src/App.tsx:85)

```javascript
socket.onclose = () => setConnection("RECONNECTING");
```

只改了字串，沒有任何重連邏輯。後端重啟或網路抖動之後，UI 會永遠停在
「RECONNECTING」並顯示凍結的舊資料 —— 對盯盤工具來說，**顯示過期資料比顯示斷線更危險**。

建議抽一個 `useReconnectingSocket(url, onMessage)` hook：指數退避（1s → 30s）、
`onopen` 時重新抓一次 REST 快照、`useEffect` cleanup 時清掉 timer。

---

## P1：效能 —— 每秒 cadence 的預算已經超支

我在本機實測了兩個熱點（200 檔 × 200 檔位 order book）：

| 熱點 | 單次 | 每個 cadence（200 markets） |
|---|---|---|
| `RollingTradeFlow.windows()`（每市場 2 萬筆/小時） | 5.33 ms | **~1,067 ms** |
| `calculate_liquidity()` | 0.89 ms | **~177 ms** |

**單這兩項就已經 >1.2 秒，超過 1 秒的 cadence。** 而 BTC/ETH 在 Binance 永續的成交筆數
遠高於每小時 2 萬筆，實際會更糟。後果不只是「慢」：flush 是在 event loop 上同步跑完的，
它超時會一併延遲 WebSocket 讀取 → order book 落後 → `ORDERBOOK_STALE_AFTER_SECONDS`
誤判 → 進場被擋。也就是說，**效能問題會偽裝成資料品質問題**。

### 6. `RollingTradeFlow.windows()` 是最大單一成本

[flow.py:37](backend/app/flow.py:37)

```python
WINDOWS_SECONDS = (10, 60, 300, 900, 3600)

def windows(self, timestamp_ms):
    return {s: self._window(timestamp_ms - s*1000) for s in WINDOWS_SECONDS}

def _window(self, cutoff):
    buy_volume  = sum(... for trade in self._trades if ...)   # 掃完整個 1 小時 deque
    sell_volume = sum(... for trade in self._trades if ...)   # 再掃一次
```

**每次呼叫掃 10 遍完整的一小時 deque**，而 [runtime.py:643](backend/app/runtime.py:643)
只用到 `windows[60]` 和 `windows[300]`。

三個疊加的修法，由淺入深：

1. **只算需要的**：`windows(now, seconds=(60, 300))`。立即省 60%。
2. **反向迭代 + 提早跳出**：deque 是時間有序的，從尾端往前掃到 cutoff 就 break，
   買賣一趟掃完。5 分鐘窗只碰 5 分鐘的資料而非 60 分鐘 → 再省一個數量級。
3. **增量維護**：`add_trade` 時累加、`prune` 時扣減，用分桶（每秒一桶）的 ring buffer
   維護滾動和。`windows()` 變成 O(桶數) 而非 O(成交筆數)。

同時 deque 保留 1 小時但只用到 5 分鐘 —— 若沒有其他消費者，保留期可以縮到 15 分鐘，
記憶體與掃描成本一起降。

### 7. `calculate_liquidity` 算了一堆沒人用的東西

[liquidity.py:33](backend/app/liquidity.py:33)

- `IMPACT_NOTIONALS` 有 6 檔（1k/5k/10k/25k/50k/100k），但只有 **10k 與 50k** 被
  persist 與顯示（[runtime.py:422](backend/app/runtime.py:422)）。
- `capital_to_move_up/down` 算 1%/2%/5%，只用到 1% 與 2%。
- 每次都對 400 個檔位做 `Decimal(str(level.price))` 轉換。

建議：
- 把要計算的 notional / percent 做成參數，預設只算實際用到的那幾個（省 ~55%）。
- `_market_impact` 的 `is_buy` 參數**完全沒用到**（[liquidity.py:101](backend/app/liquidity.py:101)），
  刪掉；讀者會以為買賣方向有差別處理。
- Decimal 的精度在這裡沒有帶來對應價值：深度/衝擊都是統計量，float64 的
  15~16 位有效數字對加密貨幣價格綽綽有餘。改 float 大約還能再快 3~5 倍。
  若要保守，至少把 `OrderBook.bids/asks` 在 `LocalOrderBook.to_model` 就快取排序好的 Decimal，
  避免 `_sorted_levels` 每個 cadence 重排一次（book 內部本來就是 sorted 的 dict view）。

### 8. `flush()` 是 O(symbols × books) 的巢狀掃描

[runtime.py:394](backend/app/runtime.py:394)、[runtime.py:721](backend/app/runtime.py:721)

```python
for symbol in active_symbols:                    # 50
    for (exchange, book_symbol, market), book in self._books.items():   # 200
        if book_symbol != symbol: continue
```

`_collect_liquidities` 與 `_public_books` 都是這個形狀 → 每個 cadence 兩萬次無效比對。
把 `self._books` 改成 `dict[str, dict[tuple[Exchange, MarketType], OrderBook]]`
（以 symbol 為第一層 key），或另外維護一個 `symbol -> keys` 的索引，就變成 O(books)。

### 9. `_dirty_symbols` 是死狀態

[runtime.py:102](backend/app/runtime.py:102)、[runtime.py:198](backend/app/runtime.py:198)、[runtime.py:239](backend/app/runtime.py:239)

`on_trade` / `on_order_book` / `on_derivative` 都很勤勞地維護它，
`_run_cadence` 還特地補上所有 book 的 symbol，然後 `flush()` 第一行：

```python
self._dirty_symbols.clear()      # 清掉，之後完全沒讀
```

要嘛刪掉這四處維護程式碼，要嘛真的用它來跳過沒動過的 symbol（考慮到大多數 cadence
只有少數幾檔有新資料，這其實是最省事的效能改善）。目前這樣是最糟的組合：付出維護成本、
拿不到任何好處，還讓讀者誤以為有增量更新機制。

### 10. `oi_change` 每次都複製整個 deque

[runtime.py:787](backend/app/runtime.py:787)

```python
prior = min(tuple(history)[:-1], key=lambda e: (abs(e.timestamp - target), ...))
```

`tuple(history)[:-1]` 建兩個新序列，而這個函式每個 cadence 被呼叫
4 windows × 2 exchanges × 50 symbols = **400 次**。改用 `itertools.islice(history, 0, len(history)-1)`
即可免除複製；更好的做法是 deque 有序，用 `bisect` 對 timestamp 二分搜尋。

### 11. `_percentile` 每次重新排序

[scoring.py:363](backend/app/scoring.py:363)

```python
def _percentile(values, index, descending=False):
    ordered = sorted(values, reverse=descending)     # 每次呼叫都重排
    return ordered.index(values[index]) / (len(ordered) - 1)
```

5 個 component × N 個 symbol = 250 次「排序 50 元素 + O(n) 的 `.index`」。
把 rank 表在 `liquidity_fragility_scores` 裡算一次傳進來即可。
另外 `.index()` 對重複值一律回傳最小 rank，若多檔深度相同（例如都是 0）會全部拿到同一個
percentile —— 這在 `fmean` 之後是可接受的，但值得寫成註解說明是刻意的。

### 12. 前端每秒重繪整張表 50 次

[App.tsx:94](frontend/src/App.tsx:94)

```javascript
setRows(current => [...current.filter(row => row.symbol !== update.symbol), update]);
```

每則 scanner 訊息（1 秒 50 則）都：重建整個陣列 → 觸發 `filtered` → `ordered` 重排 →
50 列 × 15 欄 = 750 個 cell 重新 render。也就是每秒約 **37,500 次 cell render**。

建議用 `useRef<Map<string, ScannerRow>>` 累積更新，配合
`requestAnimationFrame` 或 250ms 節流批次 `setRows` 一次。人眼也看不出 1s 與 250ms 的差別。

### 13. `MetricChart` 每次 parent render 都整個重建

[MetricChart.tsx:49](frontend/src/MetricChart.tsx:49)

```javascript
}, [data, markers]);
```

`markers` 在 [App.tsx:205](frontend/src/App.tsx:205) 是行內 array literal，每次 render 都是新參考
→ effect 每次都跑 → `chart.remove()` + `createChart()`。使用者會看到閃爍，且失去縮放/平移狀態。

修法：把 chart 建立與資料更新拆成兩個 effect（建立只依賴 mount，資料用
`series.setData()` 更新），並在 App 用 `useMemo` 包住 markers。

---

## P1：交易所 API 用量

### 14. 衍生品是 N+1 輪詢，可以降到 1/40 的請求量

| 目前 | 請求數 |
|---|---|
| Binance：每檔 `openInterest` + `premiumIndex`，12 秒一輪 | 2 × 44 / 12s ≈ **7.3 req/s** |
| OKX：每檔 `open-interest` + `funding-rate` + `mark-price`，30 秒一輪 | 3 × 44 / 30s ≈ **4.4 req/s** |

這兩家都有批次端點：

- Binance `GET /fapi/v1/premiumIndex`（不帶 symbol）→ 一次拿回全市場 mark price + funding。
  更好的是 `!markPrice@arr` WebSocket 串流，完全免輪詢。OI 沒有批次 REST，但有
  `openInterestHist`；或改用 `!ticker@arr` 搭配單獨的 OI 輪詢降頻。
- OKX `GET /api/v5/public/open-interest?instType=SWAP`（不帶 instId）→ 全部永續一次回。
  `mark-price?instType=SWAP` 同理。`funding-rate` 需要 instId，但可用
  `funding-rate-history` 或降低頻率（funding 每 8 小時才變一次，30 秒輪詢是浪費）。

**funding rate 用 30 秒 cadence 輪詢是明顯的過度輪詢** —— 它 8 小時才結算一次，
每 5 分鐘拉一次都綽綽有餘。單這一項就能砍掉 OKX 三分之一的請求。

### 15. OKX order book 的 REST bootstrap 是多餘的

[orderbooks.py:373](backend/app/collectors/orderbooks.py:373)

```python
await websocket.send_json({"op": "subscribe", "args": [{"channel": "books", ...}]})
await self._bootstrap_all()          # 每檔一次 REST /market/books?sz=400
```

OKX `books` channel 訂閱後**第一則訊息就是 `action: "snapshot"`**，
而程式碼在 [orderbooks.py:435](backend/app/collectors/orderbooks.py:435) 也確實會拿它重新 bootstrap。
所以那 25 次 REST 呼叫（含 `Semaphore(5)` 與四次重試退避）純屬浪費，
還是每次重連都會重打一次 —— 正好是最容易撞到 rate limit 的時機。

直接刪掉 OKX 的 `_bootstrap_all`，靠 channel snapshot 即可。
（Binance 的 REST bootstrap 是必要的，那邊要保留。）

---

## P1：指標語意的不一致

### 16. `classify_move` 對「OI 下降」視而不見

[scoring.py:128](backend/app/scoring.py:128)

```python
perp_active = perp_direction is not _Direction.NONE and _above(signal.oi_change, thresholds.oi_change)

def _above(value, threshold):
    return value is not None and value >= threshold      # 有號比較
```

OI **上升** 5% → `LEVERAGE_DRIVEN`。OI **下降** 5%（大規模強平／軋空平倉）→ 永遠不算。
但這明明是槓桿驅動行情最典型的形態之一。

而且系統其他地方都保留了符號的絕對值語意：
- `activity_score` 用 `_bounded_magnitude(oi_change)`，也就是 `abs()` —— 漲跌一視同仁。
- `_largest_absolute` 用 `max(..., key=abs)` —— 明確取絕對值最大者。
- `test_oi_negative_aggregation` 明確斷言 `-0.2` 會被保留下來。

也就是說：**採集層與評分層都認為 OI 下降是重要訊號，只有分類層把它丟掉了。**
若這是刻意的，需要在 `classify_move` 加註解說明為什麼；若不是（我判斷不是），
應改成 `_above(abs(signal.oi_change), thresholds.oi_change)`，
並考慮在 `move_type` 之外另外曝露 OI 的方向（增倉 vs 減倉），因為兩者的交易涵義相反。

### 17. TP 優先於 SL —— paper 統計會系統性高估勝率

[paper_trading.py:319](backend/app/paper_trading.py:319)

```python
reason = ("TAKE_PROFIT" if signed_return >= take_profit_pct
          else "STOP_LOSS" if signed_return <= -stop_loss_pct
          else "TIME_STOP" if ... else None)
```

判斷是在 1 秒一次的快照上做的。一秒之內若價格同時觸及 TP（+2%）與 SL（-1%）
（低流動性幣種的插針很常見），程式只看得到快照當下的值 —— 而快照值不可能同時滿足兩個條件，
所以嚴格說不是「同時觸發」的問題，真正的問題是：

**1 秒取樣會完全錯過期間內觸及 SL 但已回彈的情況。** 這是單向的偏誤（只會漏掉虧損、
不會漏掉獲利，因為若回彈到 TP 就記為 TP），會讓 `win_rate`、`profit_factor`、
`average_return` 全部偏樂觀。

修法（擇一）：
- 保守：既然已經在追蹤 `max_adverse_excursion_pct`，在判斷 reason 時一併檢查
  「本次區間內 MAE 是否已跌破 stop_loss」，若是則以 STOP_LOSS 結算。
- 徹底：用逐筆成交（`RollingTradeFlow` 已經有了）而非快照來偵測 TP/SL 觸發。

無論選哪個，`/api/paper/stats` 的輸出都應該在 README 或 UI 上標註
「1 秒取樣、樂觀偏誤」，避免拿它做決策。

### 18. 出場價用的是「聚合價」而非該倉位所在交易所的價格

[paper_trading.py:305](backend/app/paper_trading.py:305)

`_reference_price(detail)` 取的是 `detail["price"]`，也就是
`_preferred_price()` 挑出的 Binance-perp-優先中價（[runtime.py:713](backend/app/runtime.py:713)）。
但倉位可能開在 OKX。兩家永續在急速行情下的價差可以到數十個 bp，
足以誤觸 1% 的停損。實際成交模擬（`simulate_market_fill`）有正確用倉位自己的簿子，
但**觸發判斷**用錯了價格。

改成從 `detail["orderbooks"][position["exchange"]][position["market"]]` 取中價。

### 19. 穩定幣佔用了 universe 名額並污染 fragility 百分位

`config/universe.json` 含 `USDT`、`USDC`、`DAI`。

- `USDT` → 正規化成 `USDTUSDT`，兩家都不存在，這個名額**完全浪費**。
- `USDC` / `DAI` → `USDCUSDT` / `DAIUSDT` 確實存在，但它們的深度極深、價差極小、
  衝擊成本趨近 0。而 `liquidity_fragility_scores` 是**跨 symbol 的百分位排名**
  （[scoring.py:232](backend/app/scoring.py:232)），
  等於在樣本裡塞入兩個永遠佔據「最不脆弱」極值的離群點，把其餘 47 檔的分數整體往上推。

建議在 `universe.json` 加 `"enabled": false`（schema 已支援），
或在 `UniverseAsset` 加一個 `stablecoin: bool` 欄位並在 fragility 取樣時排除。
另外 `App.tsx` 硬寫的「Monitoring 44 crypto markets」與 universe 的 50 也對不上，順手一起修。

### 20. Binance 的 `1000X` 合約會被靜默丟棄

`normalized_symbol("1000PEPE")` → `1000PEPEUSDT`，與 universe 的 `PEPEUSDT` 不匹配。
Binance 永續對 PEPE、SHIB 等用 `1000PEPEUSDT` / `1000SHIBUSDT` 這種千倍合約命名
（現貨則是 `PEPEUSDT`）。結果是：

- 這些幣的 **Binance 永續完全不在監控範圍**（現貨在）。
- 沒有 Binance 側的永續資料 → `cross_exchange_state` 永遠是 `SINGLE_EXCHANGE`
  → 永遠拿不到 `activity_score` 的 +5 分 → 這兩檔的分數系統性偏低。
- OI / funding 只有 OKX 單邊。

建議在 `BinanceAdapter._parse_perp` 加一層 alias：辨識 `1000`/`1000000` 前綴，
把 symbol 正規化回 `PEPEUSDT` 並把 `base_quantity_multiplier` 設為 `1000`
（models 已經有這個欄位，OKX 的 `ctVal` 就是這樣用的），這樣數量單位也會自動對齊。

**這一項請先用 `python -m app discover-live-markets` 對線上實際驗證**，
我沒有連線確認 Binance 目前的合約命名。

### 21. `/history` 把不同交易所與市場混成同一條序列

[repository.py:64](backend/app/repository.py:64)

```python
select(MarketMetricRow).where(MarketMetricRow.symbol == symbol)   # 沒有 exchange / market 條件
```

同一秒會有 binance-spot、binance-perp、okx-spot、okx-perp 四筆。前端
`marketChartPoints` 用「每秒取第一筆」去重（[App.tsx:54](frontend/src/App.tsx:54)），
但「第一筆」取決於資料庫回傳順序 —— 於是 K 線會在四個場所的價格之間隨機跳動。

`history_range`（replay 用）有正確帶 exchange/market 過濾，`history` 沒有。
應該讓 `/api/symbol/{symbol}/history` 也接受 `exchange` / `market` query 參數，
預設 `binance` + `perp`（與 `_preferred_price` 一致）。

### 22. `history_range` 沒有上限

同檔 [repository.py:92](backend/app/repository.py:92)：無 `limit`。
一筆開了 3 天的倉位 → 每秒 1 列 × 3 表 × 4 個 exchange/market 組合 ≈ **80 萬列**
一次載進記憶體再序列化成 JSON。加上 `LIMIT` 與時間降採樣（例如 `date_trunc` 到分鐘）。

---

## P2：儲存與維運

### 23. 事件快照塞進了完整的 200 檔位 order book

[paper_trading.py:284](backend/app/paper_trading.py:284)、[paper_trading.py:408](backend/app/paper_trading.py:408)

```python
"signal_snapshot": _json_safe(dict(detail) | {...})
```

`detail["orderbooks"]` 包含 4 個 book × 400 檔位 × `[price, qty]`。
每個 `paper_trade_events` 列大約 100–200 KB JSON，而 SIGNAL_TRIGGERED / TRADE_SKIPPED
也全部照存。

建議：入庫前把 order book 裁剪到前 10 檔（或只存最佳買賣價 + 深度摘要）。
真正需要完整簿子的只有 entry/exit 的成交模擬，而那個結果已經以
`entry_fill` / `exit_fill` 的形式存下來了。

### 24. 用 `close_trade()` 來做每秒的 MFE/MAE 更新

[paper_trading.py:328](backend/app/paper_trading.py:328)

```python
if reason is None:
    await self.repository.close_trade(position["id"], {"max_favorable_...": ...})
    return None
```

方法名叫 `close_trade` 卻用來做「沒有平倉」的更新，語意誤導。
而且這是每個未平倉部位、每秒一次 `SELECT` + `UPDATE` + `COMMIT`。

改成 `update_trade(trade_id, values)`（`close_trade` 可以是它的薄包裝），
並且只在 MFE/MAE 實際變化時才寫；或者在記憶體累積、平倉時一次寫回
（重啟遺失 MFE/MAE 對 paper trading 是可接受的損失）。

### 25. `stats()` 把十萬筆交易拉進 Python 做聚合

[repository.py:245](backend/app/repository.py:245)

```python
trades = await self.list_trades(limit=100_000)
```

而前端在 PAPER 分頁**每收到一則 ws 事件就重新呼叫一次**
（[App.tsx:146](frontend/src/App.tsx:146) `socket.onmessage = refresh`，無 debounce）。
勝率、平均值、profit factor、分桶統計全都可以在 SQL 用
`GROUP BY` + `FILTER (WHERE ...)` 一次算完。至少先給前端加個 debounce。

### 26. 保留期修剪的頻率綁在 cadence 的圈數上

[runtime.py:242](backend/app/runtime.py:242)

```python
cycles += 1
if cycles % 3600 == 0:
```

只有在 `cadence_seconds == 1.0` 時這才等於「每小時」。
`cadence_seconds` 是可設定的建構參數，改成 0.5 就變成每 30 分鐘。
改用時間判斷：記 `_last_prune_at`，`if now - _last_prune_at >= 3600`。

另外 `prune_metrics` 是單一交易內對三張表各做一次無上限 `DELETE`
（[repository.py:53](backend/app/repository.py:53)）。30 天 × 每秒 × 200 市場的量級下，
這會產生一個持有大量列鎖的長交易。改成分批刪（`DELETE ... WHERE id IN (SELECT id ... LIMIT 10000)`
迴圈），或改用 PostgreSQL 的宣告式分區 + `DROP PARTITION`。

### 27. 沒有 migration 機制

[database.py:154](backend/app/database.py:154) 用 `Base.metadata.create_all`。
schema 一旦要改（上面好幾個建議都會改），既有部署只能手動處理。
現在導入 Alembic 的成本遠低於之後。

### 28. `.env.example` 誘導使用者填入交易所 API 金鑰

```
BINANCE_API_KEY=
BINANCE_API_SECRET=
OKX_API_KEY=
OKX_API_SECRET=
OKX_PASSPHRASE=
```

我 grep 過整個 repo：**沒有任何一行程式碼讀這五個變數。**
README 也明確寫「Exchange credentials are never required」。

這是純粹的風險：它在暗示使用者把真實的交易所金鑰寫進本機檔案，
而這個專案的定位正是「絕不下單、絕不碰帳戶端點」。**請直接刪掉這五行**，
只留 `DATABASE_URL`。

---

## P2：一致性與維護性

### 29. CI 沒有把 `ruff format` 與型別檢查納入

`ruff format --check backend` → **11 個檔案未格式化**。
這直接解釋了為什麼有些方法之間缺少空行（例如
[runtime.py:160](backend/app/runtime.py:160)、[api.py:79](backend/app/api.py:79)、
[repository.py:35](backend/app/repository.py:35)、[orderbook.py:43](backend/app/orderbook.py:43)），
讀起來像是被壓在一起的。

CI 加兩步：`ruff format --check .` 與 `mypy app`。
這個 codebase 型別註記寫得很完整，不跑 mypy 等於白寫 —— 而且 mypy 應該就會抓到
§1 的 `_timestamp` 回傳型別謊報（宣告 `-> datetime` 但呼叫端當它可能是 falsy）。

前端也建議加 `tsc --noEmit` 之外的 eslint。

### 30. Python 版本三處不一致

| 位置 | 版本 |
|---|---|
| `pyproject.toml` `requires-python` | `>=3.12` |
| CI (`ci.yml`) | `3.12` |
| `backend/Dockerfile` | `3.13-slim` |
| 本機 `.venv` | `3.13.5` |

CI 測 3.12、線上跑 3.13。建議 CI 用 matrix 同時測兩個，或統一到 3.13。

### 31. Vite dev proxy 預設埠與實際不符

[vite.config.ts](frontend/vite.config.ts)：`?? "http://127.0.0.1:8000"`
但 README 與 `docker-compose.yml` 對外都是 **8001**。
不透過 Docker 直接 `npm run dev` 的人會拿到連不上的 proxy。
預設值改成 8001，或在 README 註明要設 `VITE_API_PROXY`。

### 32. 死程式碼

- **`app/derivatives.py` 整個檔案**（`OpenInterestHistory` / `DerivativeMetrics` / `funding_state`）
  只被 `tests/test_derivatives.py` 使用，生產程式碼完全沒引用。
  它與 `LiveRuntime.oi_change` 是**同一件事的兩套實作，而且語意不同** ——
  runtime 版有 `_OI_MAX_TARGET_DISTANCE_MS`、`_OI_MIN_WINDOW_FRACTION` 這些
  「樣本必須夠接近目標時點」的保護，`OpenInterestHistory._change_at` 沒有。
  留著它，遲早有人挑錯的那個用。刪掉，或把 runtime 的邏輯搬進來、讓 runtime 呼叫它。
  `config/scoring.yaml` 裡的 `funding:` 區塊同樣沒有任何程式碼讀取。
- **`_ResyncingCollector`**（[orderbooks.py:98](backend/app/collectors/orderbooks.py:98)）
  註解自承是「legacy 測試契約的相容基底」，只有 `test_reconnect.py` 用。
  測試專用的類別不該住在生產模組裡 —— 搬到 `tests/`，或把 `test_reconnect` 改測真正的 manager。
- **`activity_score` 的 6 個位置參數**（[scoring.py:282](backend/app/scoring.py:282)）
  是為了「保留原本單一市場的呼叫形狀」，但生產程式碼只用關鍵字參數呼叫，
  只有 4 個測試檔還在用位置參數。把測試改掉，函式簽章就能砍掉一半。
- `_market_impact(is_buy=...)` 未使用（見 §7）。

### 33. `_execution_book` 是通用遞迴走訪，但資料形狀是已知的

[paper_trading.py:418](backend/app/paper_trading.py:418)

48 行的遞迴 walker、處理 `"perpetual"` / `"swap"` 等 `runtime` 從不產生的別名、
支援任意巢狀。而實際輸入永遠是 `_public_books()` 產出的
`{exchange: {market: {...} | None}}`（[runtime.py:721](backend/app/runtime.py:721)）。

它在每次 `process_update` 被呼叫最多 3 次（`_reference_price` 一次、
`_consider_entry` 兩次 —— 第 201 行與第 230 行是重複呼叫，可以合併）。

改成直接查表約 10 行，而且會**在 runtime 改變輸出格式時直接壞掉並被測試抓到**，
而不是像現在這樣默默回傳 `None`（表現為「永遠不進場」，是最難查的那種 bug）。

### 34. 設定載入有四套不同的驗證嚴格度

| 載入函式 | 缺 key | 型別錯誤 | 值域檢查 |
|---|---|---|---|
| `load_classification_thresholds` | 明確報錯 | 包成 ValueError | 有 |
| `load_trade_signal_thresholds` | 明確報錯 | 包成 ValueError | 有 |
| `load_data_retention_settings` | 明確報錯 | 包成 ValueError | 有 |
| `load_paper_trading_settings` | 全靠預設值 | **裸 TypeError** | **完全沒有** |

[paper_trading.py:41](backend/app/paper_trading.py:41) 是唯一的漏網之魚。
`notional_usdt: -1000`、`fee_bps: 500`、`stop_loss_pct: 2.0`（200% 停損）
都會被照單全收。而且 `rearm_activity_score > entry_activity_score` 這種
邏輯上互相矛盾的組合也沒人檢查（會導致永遠不進場）。

建議：這四個 loader 抽成一個共用的 `_load_section(path, name, cls, required)`，
並給 `PaperTradingSettings` 補 `__post_init__` 驗證（含跨欄位的
`rearm <= entry`、`0 < stop_loss_pct < 1` 等）。

### 35. `runtime.py` 859 行，`LiveRuntime` 承擔了太多職責

它同時是：collector 生命週期管理器、指標聚合引擎、persistence 批次器、
freshness 判定器、OI 視窗計算器。`_build_detail` 一個方法就 176 行。

不急著大改，但下一次要動它之前，建議至少把
「OI 視窗 + freshness 判定」抽成一個 `DerivativeState` 類別
（正好可以把 §32 的 `app/derivatives.py` 復活成這個角色），
以及把 `_build_detail` 裡組 dict 的部分拆成純函式 —— 那部分完全不需要 `self`，
拆出來就能單獨測試，不用像現在的測試那樣去戳 `runtime._derivatives` 私有屬性。

### 36. 有副作用的判斷式

`_available_book`（[runtime.py:744](backend/app/runtime.py:744)）與
`_derivative_is_fresh`（[runtime.py:762](backend/app/runtime.py:762)）
名字讀起來是純查詢，實際上會寫 `self._book_states` / `self._derivative_states` 並印 log。

`_derivative_is_fresh` 在一個 cadence 內會被同一個 snapshot 呼叫很多次
（`_build_detail` 一次、`oi_change` 四次、`_funding` 一次），
狀態轉換的 log 靠 `!= "stale"` 去重才沒有洗版，但這是脆弱的巧合。
建議把「判定」與「狀態轉換記錄」拆開，每個 cadence 對每個 key 只做一次轉換判定。

---

## 建議的處理順序

**先做（半天內，風險低、收益明確）**
1. §1 replay endpoint 的 500（含補測試）
2. §28 刪掉 `.env.example` 的 API 金鑰
3. §2 的第 1、2 點：`_integer` 型別處理 + `run()` 改捕捉 `Exception`
4. §9 刪掉 `_dirty_symbols`（或啟用它）
5. §29 CI 加 `ruff format --check` 與 `mypy`
6. §31 Vite 埠號

**接著（效能，這是目前最大的系統性風險）**

7. §6 `RollingTradeFlow.windows` 只算需要的視窗 + 反向提早跳出
8. §7 `calculate_liquidity` 只算用得到的 notional/percent
9. §8 `_books` 改成以 symbol 為第一層 key
10. §12 §13 前端節流與 chart 重建

做完 7–10 之後重跑一次 soak test，對照
`live-soak-test-report-2026-09-10.md` 看 cadence 延遲與 stale 比例的變化。

**再來（正確性與語意）**

11. §16 OI 方向、§17 TP/SL 取樣偏誤、§18 出場價來源
12. §3 OKX 節流、§14 批次端點、§15 移除 OKX REST bootstrap
13. §19 §20 universe 的穩定幣與 `1000X` 合約
14. §21 §22 history API 的過濾與上限

**有餘裕再處理**：§23–§27 儲存層、§32–§36 結構整理。

---

## 附註：關於 `httpx2`

`pyproject.toml` 的 dev 依賴寫 `httpx2>=2.12` 而非 `httpx`，一開始看起來很像
typosquat。查證後確認它是 Pydantic 團隊維護的 httpx 後繼版本（作者仍是 Tom Christie），
且 starlette 的 TestClient 已支援它 —— 104 個測試全過。**這一項沒問題**，
但值得在 `pyproject.toml` 加一行註解說明，避免下一個人（或下一次安全稽核）重新查一遍。
