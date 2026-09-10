# CEX Liquidity & Flow Tracker

CEX-only monitoring for Binance and OKX USDT spot and perpetual markets. It measures live order-book fragility, aggressive trade flow, open interest, funding, and exchange agreement. It includes local PostgreSQL-backed paper trading and replay using public normalized market data only; it never submits orders, accesses account data, or holds real positions.

## Run

```sh
docker compose up --build
```

- UI: `http://localhost:5174`
- API: `http://localhost:8001`
- PostgreSQL: internal Compose service

The backend loads `config/universe.json`, discovers currently live Binance/OKX markets, and uses only public market-data endpoints. Configure `DATABASE_URL` only when using a local non-Compose database. Exchange credentials are never required: no trading or account endpoints are implemented.

## Configuration

`config/scoring.yaml` controls metric scoring and retention. Set
`data_retention.metric_history_days` to retain aggregated market, flow, and derivative
metrics for that many days. Default: 30 days.

## Runtime smoke test

After `docker compose up --build` reports both services ready, wait for public market-data subscriptions to establish and run:

```sh
curl http://localhost:8001/api/scanner
curl http://localhost:8001/api/paper/positions
```

Expected result: the scanner response becomes a JSON array of monitored symbols with live `price`, `activity_score`, and `liquidity_fragility`; paper positions remains an empty JSON array until the configured local simulator opens a position. The runtime uses only public Binance and OKX market-data endpoints and refreshes dashboard state once per second. Flow history follows that cadence, while order-book and derivative history is written only for new, fresh source observations; stale exchange inputs remain unavailable and cannot trigger paper entries. The simulator never submits an order.

## Development checks

```sh
.venv/Scripts/python.exe -m pytest backend/tests
.venv/Scripts/ruff.exe check backend
cd frontend && npm run build
```
