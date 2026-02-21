# ⚡ HFT Trading Engine

A high-frequency trading execution engine built with Python and FastAPI. Features ACID-compliant order execution, pessimistic row-level locking to prevent race conditions, idempotency guarantees, and exact Decimal arithmetic for all financial calculations.

## Table of Contents

- [Features](#features)
- [Quick Start](#quick-start)
- [Using the Web GUI (Swagger UI)](#using-the-web-gui-swagger-ui)
- [Using the CLI (curl)](#using-the-cli-curl)
- [API Reference](#api-reference)
- [Running Tests](#running-tests)
- [Production Deployment](#production-deployment)


## Features

- **Pessimistic locking**: `SELECT ... FOR UPDATE` + `asyncio.Lock` prevents double-spend under concurrent load
- **Exact arithmetic**:Python `Decimal` throughout, never `float` (no `0.1 + 0.2 = 0.30000000000000004`)
- **Idempotency**: `X-Idempotency-Key` header ensures exactly-once execution on retries
- **ACID transactions**: Full rollback on any failure, portfolio always consistent
- **Async I/O**: FastAPI + SQLAlchemy async engine, non-blocking throughout
- **Swagger UI**: interactive browser interface at `/docs`


## Quick Start

### 1. Clone and install

```
git clone https://github.com/macbuildssys/hft-engine.git
cd hft-engine
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Start the server

```
python main.py
```

You should see:

```
INFO: HFT Engine ready — http://localhost:8000/docs
INFO: Uvicorn running on http://0.0.0.0:8000
```

The server seeds the database automatically on first run:

- **BTC** = $50,000 | **ETH** = $3,000 | **SOL** = $100 | **USD** = $1
- **User 1** starts with $100,000 USD

**VM users:** access the UI from your host machine at `http://<vm-ip>:8000/docs`  

 Find your VM IP with: `hostname -I`



## Using the Web GUI (Swagger UI)

Open your browser and navigate to:

```
http://localhost:8000/docs
```

If running in a VM with no display, open this URL on your **host machine's browser** using your VM's IP address instead of `localhost`.

### Execute a Trade

1. Click **POST /api/v1/trading/execute**
2. Click **Try it out**
3. Fill in the **X-Idempotency-Key** field — use any unique string (e.g. `trade-001`)
4. In the **Request body**, enter:

```
{
  "user_id": 1,
  "symbol": "BTC",
  "pay_with": "USD",
  "quantity": "1.0"
}
```

5. Click **Execute**

**Expected response:**

```
{
  "status": "ORDER_FILLED",
  "symbol": "BTC",
  "quantity_filled": "1.0",
  "execution_price": "50000.00000000",
  "total_cost": "50000.00000000",
  "paid_with": "USD",
  "remaining_balance": "50000.00000000",
  "executed_at": "2026-02-21T14:01:48.011301",
  "executed_by_thread": "MainThread"
}
```

### Check Your Portfolio

1. Click **GET /api/v1/trading/portfolio/{user_id}**
2. Click **Try it out**
3. Enter `1` in the `user_id` field
4. Click **Execute**

### Test Idempotency

Send the same trade twice using the **same** `X-Idempotency-Key`. The second request returns the cached result — no second debit occurs. Change the key to execute a new trade.

### Trigger an Error

Try buying 3 BTC with only $100,000 balance:

```
{
  "user_id": 1,
  "symbol": "BTC",
  "pay_with": "USD",
  "quantity": "3.0"
}
```

Returns `402 Payment Required`  `ERR_LIQUIDITY`.


## Using the CLI (curl)

Open a second terminal while `python main.py` is running in the first.

### Health check

```
curl http://localhost:8000/health
```

### Execute a trade

```
curl -X POST http://localhost:8000/api/v1/trading/execute \
  -H "Content-Type: application/json" \
  -H "X-Idempotency-Key: $(python3 -c 'import uuid; print(uuid.uuid4())')" \
  -d '{"user_id": 1, "symbol": "BTC", "pay_with": "USD", "quantity": "1.0"}'
```

### Buy ETH

```
curl -X POST http://localhost:8000/api/v1/trading/execute \
  -H "Content-Type: application/json" \
  -H "X-Idempotency-Key: $(python3 -c 'import uuid; print(uuid.uuid4())')" \
  -d '{"user_id": 1, "symbol": "ETH", "pay_with": "USD", "quantity": "5.0"}'
```

### Check portfolio

```
curl http://localhost:8000/api/v1/trading/portfolio/1
```

### List all assets

```
curl http://localhost:8000/api/v1/trading/assets
```

### Test idempotency (same key = same result, no double debit)

```
KEY="my-unique-key-abc123"

curl -X POST http://localhost:8000/api/v1/trading/execute \
  -H "Content-Type: application/json" \
  -H "X-Idempotency-Key: $KEY" \
  -d '{"user_id": 1, "symbol": "BTC", "pay_with": "USD", "quantity": "0.5"}'

# Retry with same key — returns cached response, no second trade
curl -X POST http://localhost:8000/api/v1/trading/execute \
  -H "Content-Type: application/json" \
  -H "X-Idempotency-Key: $KEY" \
  -d '{"user_id": 1, "symbol": "BTC", "pay_with": "USD", "quantity": "0.5"}'
```

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/trading/execute` | Execute a market BUY order |
| `GET` | `/api/v1/trading/portfolio/{user_id}` | Get portfolio valuation |
| `GET` | `/api/v1/trading/assets` | List all market assets and prices |
| `GET` | `/health` | Health check |

### POST /api/v1/trading/execute

**Required header:** `X-Idempotency-Key: <unique-string>`

**Request body:**

| Field | Type | Description |
|-------|------|-------------|
| `user_id` | integer | Trading account ID |
| `symbol` | string | Asset to buy (e.g. `BTC`, `ETH`, `SOL`) |
| `pay_with` | string | Payment asset (e.g. `USD`) |
| `quantity` | string | Amount to buy — use string to preserve Decimal precision |

**Response codes:**

| Code | Meaning |
|------|---------|
| `200` | Order filled successfully |
| `400` | Missing `X-Idempotency-Key` header |
| `402` | Insufficient funds |
| `404` | Unknown asset symbol |
| `422` | Invalid request body (e.g. negative quantity) |



## Running Tests

```
# All tests
pytest testApi.py testStress.py -v

# With benchmark output printed
pytest testApi.py testStress.py -v -s

# API tests only
pytest testApi.py -v

# Concurrency stress tests only
pytest testStress.py -v -s
```

### Test Results

```
testApi.py::test_valid_buy_returns_200                        PASSED
testApi.py::test_missing_idempotency_key_returns_400          PASSED
testApi.py::test_insufficient_funds_returns_402               PASSED
testApi.py::test_unknown_asset_returns_404                    PASSED
testApi.py::test_negative_quantity_returns_422                PASSED
testApi.py::test_two_sequential_buys_drain_balance            PASSED
testApi.py::test_same_key_three_times_identical_response      PASSED
testApi.py::test_idempotency_prevents_double_spend            PASSED
testApi.py::test_portfolio_initial_state                      PASSED
testApi.py::test_portfolio_updates_after_trade                PASSED
testApi.py::test_empty_portfolio_returns_zero                 PASSED
testApi.py::test_decimal_vs_float                             PASSED
testApi.py::test_no_scientific_notation                       PASSED
testApi.py::test_satoshi_precision                            PASSED
testStress.py::TestConcurrencyStress::test_100_concurrent     PASSED
testStress.py::TestConcurrencyStress::test_20_fractional      PASSED
testStress.py::TestBenchmarks::test_benchmark_20_concurrent   PASSED
testStress.py::TestBenchmarks::test_benchmark_50_contention   PASSED

18 passed in 1.01s
```

### Critical Stress Test

The 100-thread test is the most important: it fires 100 simultaneous BTC orders against a $100,000 balance. The locking guarantees:

- Exactly **2 succeed** (2 × $50,000 = $100,000)
- **98 fail** with `InsufficientFundsError`
- Final USD balance = **$0** — never negative
- Value is **conserved** — 0 USD + 2 BTC × $50k = $100,000


### Why Two Lock Layers?

| Layer | Scope | Works on |
|-------|-------|----------|
| `asyncio.Lock` | Within one process | SQLite + PostgreSQL |
| `SELECT FOR UPDATE` | Across multiple processes | PostgreSQL (production) |

SQLite ignores `FOR UPDATE`, the `asyncio.Lock` handles correctness in development. In production with PostgreSQL and multiple workers, both layers work together.

### Why Decimal, Not Float?

```
# float 
0.1 + 0.2 == 0.30000000000000004   # True — broken

# Decimal
Decimal("0.1") + Decimal("0.2") == Decimal("0.3")  # True — exact
```

All quantities use `Decimal` with 8 decimal places (1 satoshi = 0.00000001 BTC).

## Production Deployment

Switch from SQLite to PostgreSQL by changing one line in `main.py`:

```
# Development (SQLite)
DATABASE_URL = "sqlite+aiosqlite:///./hft_trade.db"

# Production (PostgreSQL)
DATABASE_URL = "postgresql+asyncpg://user:password@localhost/hft"
```

Also install the PostgreSQL async driver:

```
pip install asyncpg
```

For multiple workers, replace `asyncio.Lock` with a Redis distributed lock:

```
pip install redis aioredis
```

**Recommended production stack:**

```
Load Balancer (nginx)
    ↓
2–4 Uvicorn workers
    ↓
PostgreSQL (primary + read replica)
    ↓
Redis (distributed locks + idempotency cache)
```