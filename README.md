# CEX Liquidity & Flow Tracker

CEX-only monitoring for Binance and OKX USDT spot and perpetual markets. It measures live order-book fragility, aggressive trade flow, open interest, funding, and exchange agreement. It includes local PostgreSQL-backed paper trading and replay using public normalized market data only; it never submits orders, accesses account data, or holds real positions.

## Run

```sh
docker compose up --build
```

- UI: `http://localhost:5174`
- API: `http://localhost:8001`
- PostgreSQL: internal Compose service

The backend loads `config/universe.json`, discovers currently live Binance/OKX markets, and uses only public market-data endpoints. Copy `.env.example` if a local non-Compose database URL or exchange credentials are required. Credentials are not needed for public data and no trading or account endpoints are implemented.

## Development checks

```sh
.venv/Scripts/python.exe -m pytest
cd frontend && npm run build
```
