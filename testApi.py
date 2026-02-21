"""
Integration tests: HTTP layer via HTTPX + ASGI (no real server is needed).
Run:  pytest testApi.py -v
"""

import uuid
import pytest
import pytest_asyncio
from decimal import Decimal
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import StaticPool

# Import everything from the flat main.py 
from main import (
    app, Base, Asset, Portfolio, get_db,
    AsyncSessionLocal as _prod_session_factory,
)
import main as _main_module   # for patching idempotency session factory


# Test DB (isolated in-memory SQLite) 
_test_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    poolclass=StaticPool,
    connect_args={"check_same_thread": False},
)
_TestSession = async_sessionmaker(
    bind=_test_engine, class_=AsyncSession,
    expire_on_commit=False, autocommit=False, autoflush=False,
)

@pytest_asyncio.fixture(autouse=True)
async def _reset_db():
    # Fresh schema + seed data before every test
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async with _TestSession() as s:
        s.add_all([
            Asset(symbol="BTC", price=Decimal("50000"), version=0),
            Asset(symbol="ETH", price=Decimal("3000"),  version=0),
            Asset(symbol="USD", price=Decimal("1"),     version=0),
        ])
        s.add(Portfolio(user_id=1, asset_symbol="USD", quantity=Decimal("100000")))
        await s.commit()
    yield


@pytest_asyncio.fixture
async def client():
    """HTTPX client wired to the FastAPI app with the test DB injected."""
    async def _override_db():
        async with _TestSession() as s:
            yield s

    app.dependency_overrides[get_db] = _override_db
    _main_module.AsyncSessionLocal = _TestSession   # patch idempotency sessions

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
    _main_module.AsyncSessionLocal = _prod_session_factory


def _key() -> str:
    return str(uuid.uuid4())

# ORDER EXECUTION

@pytest.mark.asyncio
async def test_valid_buy_returns_200(client):
    r = await client.post("/api/v1/trading/execute", json={"user_id": 1, "symbol": "BTC", "pay_with": "USD", "quantity": "1.0"}, headers={"X-Idempotency-Key": _key()})
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "ORDER_FILLED"
    assert d["total_cost"] == "50000.00000000"
    assert d["remaining_balance"] == "50000.00000000"

@pytest.mark.asyncio
async def test_missing_idempotency_key_returns_400(client):
    r = await client.post("/api/v1/trading/execute", json={"user_id": 1, "symbol": "BTC", "pay_with": "USD", "quantity": "1.0"})
    assert r.status_code == 400
    assert r.json()["error_code"] == "ERR_IDEMPOTENCY_REQUIRED"


@pytest.mark.asyncio
async def test_insufficient_funds_returns_402(client):
    # 3 BTC × $50k = $150k > $100k balance
    r = await client.post("/api/v1/trading/execute", json={"user_id": 1, "symbol": "BTC", "pay_with": "USD", "quantity": "3.0"}, headers={"X-Idempotency-Key": _key()})
    assert r.status_code == 402
    assert r.json()["error_code"] == "ERR_LIQUIDITY"


@pytest.mark.asyncio
async def test_unknown_asset_returns_404(client):
    r = await client.post("/api/v1/trading/execute", json={"user_id": 1, "symbol": "DOGE", "pay_with": "USD", "quantity": "1.0"}, headers={"X-Idempotency-Key": _key()})
    assert r.status_code == 404
    assert r.json()["error_code"] == "ERR_ASSET_NOT_FOUND"


@pytest.mark.asyncio
async def test_negative_quantity_returns_422(client):
    r = await client.post("/api/v1/trading/execute", json={"user_id": 1, "symbol": "BTC", "pay_with": "USD", "quantity": "-1.0"}, headers={"X-Idempotency-Key": _key()})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_two_sequential_buys_drain_balance(client):
    r1 = await client.post("/api/v1/trading/execute", json={"user_id": 1, "symbol": "BTC", "pay_with": "USD", "quantity": "1.0"}, headers={"X-Idempotency-Key": _key()})
    assert r1.status_code == 200
    assert r1.json()["remaining_balance"] == "50000.00000000"

    r2 = await client.post("/api/v1/trading/execute", json={"user_id": 1, "symbol": "BTC", "pay_with": "USD", "quantity": "1.0"}, headers={"X-Idempotency-Key": _key()})
    assert r2.status_code == 200
    assert r2.json()["remaining_balance"] == "0.00000000"

    r3 = await client.post("/api/v1/trading/execute", json={"user_id": 1, "symbol": "BTC", "pay_with": "USD", "quantity": "1.0"}, headers={"X-Idempotency-Key": _key()})
    assert r3.status_code == 402

# IDEMPOTENCY

@pytest.mark.asyncio
async def test_same_key_three_times_identical_response(client):
    key = _key()
    body = {"user_id": 1, "symbol": "BTC", "pay_with": "USD", "quantity": "1.0"}

    r1 = await client.post("/api/v1/trading/execute", json=body, headers={"X-Idempotency-Key": key})
    r2 = await client.post("/api/v1/trading/execute", json=body, headers={"X-Idempotency-Key": key})
    r3 = await client.post("/api/v1/trading/execute", json=body, headers={"X-Idempotency-Key": key})

    assert r1.json() == r2.json() == r3.json()
    assert r1.status_code == r2.status_code == r3.status_code == 200


@pytest.mark.asyncio
async def test_idempotency_prevents_double_spend(client):
    """3 retries with the same key, only 1 BTC purchased, $50k debited"""
    key = _key()
    for _ in range(3):
        await client.post("/api/v1/trading/execute", json={"user_id": 1, "symbol": "BTC", "pay_with": "USD", "quantity": "1.0"}, headers={"X-Idempotency-Key": key})

    p = await client.get("/api/v1/trading/portfolio/1")
    h = p.json()["holdings"]
    assert Decimal(h["BTC"]) == Decimal("1.00000000")
    assert Decimal(h["USD"]) == Decimal("50000.00000000")

# PORTFOLIO

@pytest.mark.asyncio
async def test_portfolio_initial_state(client):
    r = await client.get("/api/v1/trading/portfolio/1")
    assert r.status_code == 200
    d = r.json()
    assert d["total_valuation_usd"] == "100000.00"
    assert d["holdings"]["USD"] == "100000.00000000"


@pytest.mark.asyncio
async def test_portfolio_updates_after_trade(client):
    await client.post("/api/v1/trading/execute", json={"user_id": 1, "symbol": "BTC", "pay_with": "USD", "quantity": "1.0"}, headers={"X-Idempotency-Key": _key()})

    r = await client.get("/api/v1/trading/portfolio/1")
    d = r.json()
    # $50k USD + 1 BTC×$50k = $100k total; value conserved
    assert d["total_valuation_usd"] == "100000.00"
    assert d["holdings"]["USD"] == "50000.00000000"
    assert d["holdings"]["BTC"] == "1.00000000"

@pytest.mark.asyncio
async def test_empty_portfolio_returns_zero(client):
    r = await client.get("/api/v1/trading/portfolio/9999")
    assert r.json()["total_valuation_usd"] == "0.00"

# DECIMAL PRECISION

def test_decimal_vs_float():
    # Python float is wrong for money. Decimal is exact
    assert 0.1 + 0.2 != 0.3                               # float breaks
    assert Decimal("0.1") + Decimal("0.2") == Decimal("0.3")  # Decimal correct

def test_no_scientific_notation():
   # $50,000 must never appear as 5E+4 
    v = Decimal("50000.00000000")
    assert "E" not in format(v, "f")

@pytest.mark.asyncio
async def test_satoshi_precision(client):
    # 0.00000001 BTC × $50,000 = $0.00050000 — micro-precision maintained
    r = await client.post("/api/v1/trading/execute", json={"user_id": 1, "symbol": "BTC", "pay_with": "USD", "quantity": "0.00000001"}, headers={"X-Idempotency-Key": _key()})
    assert r.status_code == 200
    assert r.json()["total_cost"] == "0.00050000"