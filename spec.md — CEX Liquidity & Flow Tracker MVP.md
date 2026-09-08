# CEX Liquidity & Flow Tracker — MVP Spec

## 1. Goal

建立一個 CEX-only crypto scanner。

交易所只支援：

- Binance
- OKX

監控範圍：

- **全球市值排名前 50 的加密貨幣**
- BTC / ETH 等大型幣照常包含
- 只分析 Binance / OKX 上存在的 USDT Spot / USDT Perpetual 市場
- 如果 Top 50 中某幣沒有可用 USDT 市場，就跳過該 exchange

系統目標不是預測漲跌，而是找出：

1. 市場深度薄
2. 少量資金即可造成較大 price impact
3. 短時間出現異常 aggressive buy / sell
4. OI 快速增加或下降
5. Spot / Perp flow 出現 divergence
6. Binance / OKX 出現一致或不一致的異常

不做：

- DEX
- On-chain
- Wallet tracking
- 自動交易
- 下單
- RSI / MACD
- AI prediction
- 使用者帳號系統

---

# 2. Tech Stack

Backend：

```text
Python 3.12+
FastAPI
asyncio
aiohttp
websockets
Pydantic
SQLAlchemy
PostgreSQL
```

Frontend：

```text
React
TypeScript
Vite
Lightweight Charts
```

第一版不要 Redis。

---

# 3. Market Universe

建立：

```text
config/universe.json
```

保存市值前 50：

```json
[
  {
    "rank": 1,
    "symbol": "BTC",
    "name": "Bitcoin",
    "enabled": true
  }
]
```

MVP 不要求第三方 Market Cap API。

先使用設定檔維護 Top 50。

架構上保留：

```python
MarketUniverseProvider
```

介面，未來可以換成 CoinGecko 等來源自動更新。

### Universe 規則

不要再使用：

```text
MIN_24H_VOLUME
```

作為 symbol exclusion。

BTC、ETH 等大型幣必須保留。

啟動時：

```text
Top 50 universe
        ↓
Binance supported symbols
        ↓
OKX supported symbols
        ↓
建立實際監控市場
```

例如：

```text
BTC

Binance Spot       YES
Binance Perp       YES
OKX Spot           YES
OKX Perp           YES
```

如果：

```text
ABC

Binance Spot       YES
Binance Perp       NO
OKX Spot           NO
OKX Perp           YES
```

仍然監控存在的市場。

Stablecoin 如果沒有有意義的 USDT market，可以自動 skip。

---

# 4. Symbol Normalization

所有 exchange-specific symbol 必須轉成統一格式。

例如：

```text
Binance:
BTCUSDT

OKX Spot:
BTC-USDT

OKX Perp:
BTC-USDT-SWAP
```

Internal symbol：

```text
BTCUSDT
```

Engine 不可以直接依賴 Binance / OKX symbol 格式。

---

# 5. Exchange Adapters

建立：

```text
backend/app/exchanges/

base.py
binance.py
okx.py
```

所有 exchange adapter output 必須轉成 normalized model。

### Trade

```python
class NormalizedTrade:
    exchange: str
    symbol: str
    market: str        # spot / perp
    timestamp: int
    price: float
    quantity: float
    quote_value: float
    side: str          # BUY / SELL taker
```

### OrderBook

```python
class PriceLevel:
    price: float
    quantity: float

class OrderBook:
    exchange: str
    symbol: str
    market: str
    timestamp: int
    bids: list[PriceLevel]
    asks: list[PriceLevel]
```

### Derivatives

```python
class DerivativeSnapshot:
    exchange: str
    symbol: str
    timestamp: int
    open_interest: float
    open_interest_usd: float
    funding_rate: float | None
    mark_price: float
```

---

# 6. Order Book Collector

Order Book 是此 project 最重要的資料。

流程：

```text
REST snapshot
      ↓
建立 local orderbook
      ↓
WebSocket incremental updates
      ↓
sequence validation
      ↓
持續更新 RAM 中的 orderbook
```

Requirements：

- 自動 reconnect
- reconnect 後重新 snapshot
- sequence 發生 gap 時重新 snapshot
- 不允許 silent corruption
- local book 存 RAM
- 不要把每個 raw depth event 寫進 PostgreSQL

每秒計算一次 derived metrics。

---

# 7. Liquidity Metrics

Mid price：

```text
mid = (best_bid + best_ask) / 2
```

每個市場計算：

```text
spread_percent

bid_depth_0_5
ask_depth_0_5

bid_depth_1
ask_depth_1

bid_depth_2
ask_depth_2

bid_depth_5
ask_depth_5
```

Depth 單位全部統一：

```text
USDT
```

例如：

```text
ask_depth_2
=
mid ~ mid * 1.02
範圍內所有 ask 的 price * quantity
```

---

# 8. Price Impact

根據目前 orderbook 模擬 market order。

測試：

```text
$1,000
$5,000
$10,000
$25,000
$50,000
$100,000
```

分別計算：

```text
buy_impact_1k
buy_impact_5k
buy_impact_10k
buy_impact_25k
buy_impact_50k
buy_impact_100k

sell_impact_1k
...
```

Price impact 使用實際逐層吃單後的 VWAP：

```text
impact =
(VWAP - mid) / mid
```

另外計算：

```text
capital_to_move_up_1pct
capital_to_move_up_2pct
capital_to_move_up_5pct

capital_to_move_down_1pct
capital_to_move_down_2pct
capital_to_move_down_5pct
```

這是系統核心指標。

---

# 9. Trade Flow / CVD

收集：

```text
Binance Spot trades
Binance Perp trades

OKX Spot trades
OKX Perp trades
```

每筆成交區分：

```text
Aggressive BUY
Aggressive SELL
```

維護 rolling window：

```text
10 sec
1 min
5 min
15 min
1 hour
```

每個 window 計算：

```text
buy_volume
sell_volume
delta
buy_sell_ratio
CVD
```

公式：

```text
delta = buy_volume - sell_volume
```

```text
CVD += delta
```

Spot 與 Perp 必須完全分開計算。

---

# 10. Flow Pressure

這是另一個核心指標。

### Buy Pressure

```text
5m aggressive buy volume
-------------------------
current ask depth 2%
```

### Sell Pressure

```text
5m aggressive sell volume
--------------------------
current bid depth 2%
```

需要：

```text
1m_buy_pressure
5m_buy_pressure
15m_buy_pressure

1m_sell_pressure
5m_sell_pressure
15m_sell_pressure
```

例如：

```text
5m aggressive buy = $300,000
Ask depth +2%      = $50,000

Buy Pressure = 6.0x
```

---

# 11. Order Book Imbalance

計算：

```text
OBI =
(bid_depth_2 - ask_depth_2)
/
(bid_depth_2 + ask_depth_2)
```

範圍：

```text
-1 ~ +1
```

OBI 只作為 context。

禁止單獨把：

```text
OBI > 0
```

解讀為 bullish。

---

# 12. Open Interest

只對 Perpetual market 執行。

至少：

```text
30 sec
```

更新一次。

保存：

```text
current OI
OI USD
```

計算：

```text
ΔOI 1m
ΔOI 5m
ΔOI 15m
ΔOI 1h
```

公式：

```text
(current - previous)
/
previous
```

---

# 13. Funding

保存：

```text
current_funding_rate
next_funding_time
```

第一版不用做複雜 Funding prediction。

只需要讓 Engine 可以判斷：

```text
normal
elevated positive
elevated negative
```

threshold 放 config，不寫死在 UI。

---

# 14. Spot vs Perp Classification

每個 symbol 判斷：

```text
SPOT_DRIVEN
LEVERAGE_DRIVEN
MIXED
NEUTRAL
```

### SPOT_DRIVEN

條件傾向：

```text
Spot buy pressure ↑
Spot CVD ↑
Perp pressure 相對低
```

### LEVERAGE_DRIVEN

條件傾向：

```text
Perp buy pressure ↑
Perp CVD ↑
OI ↑
```

### MIXED

```text
Spot + Perp 都明顯增加
```

### NEUTRAL

沒有顯著訊號。

Threshold 全部集中：

```text
config/scoring.yaml
```

不要散落 hardcode。

---

# 15. Cross Exchange Engine

同一個 symbol 如果 Binance + OKX 都存在，做 cross-exchange comparison。

計算：

```text
price spread

Binance Spot CVD
OKX Spot CVD

Binance Perp CVD
OKX Perp CVD

Binance ΔOI
OKX ΔOI

Binance Buy Pressure
OKX Buy Pressure
```

建立：

```text
CONFIRMED
DIVERGENT
SINGLE_EXCHANGE
```

### CONFIRMED

兩個 exchange 同方向出現異常。

### DIVERGENT

例如：

```text
Binance:
Buy Pressure 5.8x
OI +18%

OKX:
Buy Pressure 0.8x
OI +1%
```

標記：

```text
BINANCE_DRIVEN
```

反之：

```text
OKX_DRIVEN
```

---

# 16. Liquidity Fragility Score

Score：

```text
0 ~ 100
```

越高代表流動性越脆弱。

Input：

```text
Depth ±2%
Price Impact $10K
Price Impact $50K
Spread
Capital to Move 2%
```

不要直接因為 market cap 小就加分。

市值排名只負責決定監控 universe。

Scoring 使用目前 Top 50 監控市場之間的 percentile normalization。

這樣：

```text
BTC
ETH
SOL
...
```

都可以彼此比較。

---

# 17. Activity / Anomaly Score

另外建立：

```text
Activity Score 0 ~ 100
```

Input：

```text
1m / 5m Buy Pressure
1m / 5m Sell Pressure
CVD change
ΔOI
Funding
Volume acceleration
Cross-exchange confirmation
```

不要把 Liquidity Fragility 和 Activity 混成一個黑箱 score。

UI 至少分成：

```text
Liquidity Fragility
Activity
```

---

# 18. Scanner Ranking

首頁預設表格：

```text
Rank
Symbol
Market Cap Rank
Price
24h Change

Liquidity Fragility

1m Buy Pressure
5m Buy Pressure
1m Sell Pressure
5m Sell Pressure

Spot CVD 5m
Perp CVD 5m

OI Change 5m
OI Change 1h

Funding

Move Type
Cross Exchange State
Activity Score
```

支援依任何欄位排序。

預設：

```text
Activity Score DESC
```

並提供：

```text
Liquidity Fragility DESC
Buy Pressure DESC
Sell Pressure DESC
OI Change DESC
```

---

# 19. Coin Detail Page

點擊 BTC、ETH 或任何 symbol 後顯示：

## Header

```text
BTCUSDT

Market Cap Rank
Price
24H Change

Binance
OKX
```

## Liquidity

```text
Spread

Depth ±0.5%
Depth ±1%
Depth ±2%
Depth ±5%

$1K Impact
$5K Impact
$10K Impact
$50K Impact

Capital to Move 1%
Capital to Move 2%
Capital to Move 5%
```

## Flow

```text
Spot CVD
Perp CVD

Buy Pressure
Sell Pressure

1m
5m
15m
```

## Derivatives

```text
OI
ΔOI 5m
ΔOI 15m
ΔOI 1h

Funding
```

## Cross Exchange

Binance / OKX 並排。

---

# 20. Database

不要保存 raw orderbook。

保存每秒 aggregated snapshot。

### market_metrics

```text
timestamp
exchange
symbol
market

price
spread

bid_depth_0_5
ask_depth_0_5
bid_depth_1
ask_depth_1
bid_depth_2
ask_depth_2
bid_depth_5
ask_depth_5

buy_impact_10k
sell_impact_10k
buy_impact_50k
sell_impact_50k

obi
```

### flow_metrics

```text
timestamp
exchange
symbol
market

buy_volume_1m
sell_volume_1m

buy_volume_5m
sell_volume_5m

cvd_1m
cvd_5m

buy_pressure_1m
buy_pressure_5m

sell_pressure_1m
sell_pressure_5m
```

### derivative_metrics

```text
timestamp
exchange
symbol

open_interest
open_interest_usd

oi_change_5m
oi_change_15m
oi_change_1h

funding_rate
```

---

# 21. Backend API

至少提供：

```text
GET /api/universe
```

```text
GET /api/scanner
```

```text
GET /api/symbol/{symbol}
```

```text
GET /api/symbol/{symbol}/history
```

以及：

```text
WS /ws/scanner
WS /ws/symbol/{symbol}
```

Frontend 不要直接連 Binance / OKX。

所有 exchange data：

```text
Exchange
→ Backend
→ Normalization / Engine
→ Frontend
```

---

# 22. Reliability

必須處理：

```text
WebSocket disconnect
API timeout
rate limit
missing sequence
invalid message
exchange maintenance
symbol delisting
```

每個 collector 都要：

```text
auto reconnect
exponential backoff
structured logging
```

Dashboard 必須顯示：

```text
Binance: CONNECTED
OKX: CONNECTED

Last update:
xxx ms ago
```

避免資料其實停掉但 UI 還顯示舊值。

---

# 23. API Keys

Market data 優先使用 public endpoints。

不要把 API key 寫進 source。

支援：

```text
.env
```

例如：

```text
BINANCE_API_KEY=
BINANCE_API_SECRET=

OKX_API_KEY=
OKX_API_SECRET=
OKX_PASSPHRASE=
```

如果 public market data 不需要 authentication，就不要傳 API key。

此 MVP 禁止：

```text
create order
cancel order
account balance
position management
withdraw
transfer
```

即使使用者提供 API key，也不要實作交易功能。

---

# 24. UI Style

UI 做成專業 market terminal。

不要：

```text
大量卡片
巨大 Hero
漸層 Landing Page
花俏動畫
```

主要畫面應該是：

```text
Top toolbar

Scanner table
───────────────────────────────

右側 / 下方：
Selected Symbol overview
```

風格接近：

```text
TradingView
CoinGlass
Bloomberg terminal
```

重視：

```text
資訊密度
快速排序
數字 alignment
即時更新
Dark mode
```

---

# 25. MVP Completion Criteria

只有以下全部完成才算 MVP 完成：

- Top 50 universe 可以載入
- BTC 必須正常監控
- Binance Spot collector
- Binance Perp collector
- OKX Spot collector
- OKX Perp collector
- Local orderbook 正確維護
- Sequence gap 自動 resync
- Spot / Perp trades
- Depth ±0.5 / 1 / 2 / 5%
- Price impact
- Capital to Move
- CVD
- Buy / Sell Pressure
- OBI
- OI
- Funding
- ΔOI
- Spot vs Perp classification
- Binance vs OKX comparison
- Liquidity Fragility Score
- Activity Score
- Scanner UI
- Symbol Detail UI
- PostgreSQL history
- WebSocket reconnect
- Backend tests

---

# 26. Implementation Order

Terra 請照順序實作，不要一次全部混著做。

### Phase 1

```text
Project skeleton
Universe
Symbol normalization
Binance / OKX symbol discovery
```

### Phase 2

```text
Binance OrderBook
OKX OrderBook
Local book validation
Liquidity metrics
Price impact
```

### Phase 3

```text
Spot / Perp trades
CVD
Flow Pressure
```

### Phase 4

```text
OI
Funding
ΔOI
```

### Phase 5

```text
Cross Exchange Engine
Liquidity Fragility
Activity Score
```

### Phase 6

```text
PostgreSQL
FastAPI
WebSocket API
```

### Phase 7

```text
React Scanner
Symbol Detail
Charts
```

### Phase 8

```text
Reconnect testing
Sequence corruption testing
API failure testing
README
docker-compose
```

---

# 27. Important Design Rule

不要試圖預測：

```text
下一根 K 線漲還是跌
```

系統應該回答：

```text
這個市場現在有多薄？

需要多少資金可以推動價格？

目前 aggressive capital 是否異常？

異常主要來自 Spot 還是 Perp？

OI 是否同步增加？

Binance 和 OKX 是否互相確認？
```

這六個問題是 MVP 的核心。

任何不直接幫助回答這六個問題的功能，第一版先不要做。