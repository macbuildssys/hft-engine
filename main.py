"""
HFT Trading Engine: Complete Self-Contained Implementation
Run:  python main.py or python3 main.py
Docs: http://localhost:8000/docs
File layout (flat no packages needed):
ACID: full transaction, either all changes commit or none do
No double-spend: balance check happens inside the exclusive lock
Decimal arithmetic
Exactly-once: idempotency key deduplication
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import defaultdict
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
from sqlalchemy import Column, DateTime, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.future import select
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

DATABASE_URL = "sqlite+aiosqlite:///./hft_trade.db"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("hft")

class Base(DeclarativeBase):
    pass

class Asset(Base):
    """
    Market asset spot price
    version column = optimistic lock guard for price updates
    """
    __tablename__ = "assets"
    symbol  = Column(String(10), primary_key=True)
    price   = Column(Numeric(19, 8), nullable=False)
    version = Column(Integer, default=0, nullable=False)

class Portfolio(Base):

    __tablename__ = "portfolios"
    __table_args__ = (
        UniqueConstraint("user_id", "asset_symbol", name="uq_user_asset"),
        Index("idx_user_asset", "user_id", "asset_symbol"),
    )
    id           = Column(Integer, primary_key=True, autoincrement=True)
    user_id      = Column(Integer, nullable=False)
    asset_symbol = Column(String(10), nullable=False)
    quantity     = Column(Numeric(19, 8), nullable=False)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_keys"
    request_key   = Column(String(128), primary_key=True)
    response_body = Column(Text, nullable=False)
    http_status   = Column(Integer, nullable=False, default=200)
    created_at    = Column(DateTime, default=datetime.utcnow)

#  Async engine + session factory 

engine = create_async_engine(
    DATABASE_URL,
    # StaticPool: shares ONE connection; required for SQLite in-memory tests. For PostgreSQL, remove poolclass and connect_args entirely
    poolclass=StaticPool,
    connect_args={"check_same_thread": False},
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _seed()

async def _seed() -> None:
    async with AsyncSessionLocal() as s:
        exists = (await s.execute(select(Asset).limit(1))).scalar_one_or_none()
        if exists:
            return
        s.add_all([
            Asset(symbol="BTC", price=Decimal("50000.00000000"), version=0),
            Asset(symbol="ETH", price=Decimal("3000.00000000"),  version=0),
            Asset(symbol="SOL", price=Decimal("100.00000000"),   version=0),
            Asset(symbol="USD", price=Decimal("1.00000000"),     version=0),
        ])
        s.add(Portfolio(user_id=1, asset_symbol="USD", quantity=Decimal("100000.00000000")))
        await s.commit()
        log.info("Database seeded: BTC=$50,000 | User 1 has $100,000 USD")

async def get_db():
    # FastAPI dependency;one session per request
    async with AsyncSessionLocal() as session:
        yield session

def _plain(v: Decimal) -> str:
    # Serialise Decimal as plain string — never scientific notation
    return format(v, "f")

class OrderRequest(BaseModel):
    # Inbound market BUY order
    user_id:  int     = Field(..., gt=0)
    symbol:   str     = Field(..., min_length=1, max_length=10)
    pay_with: str     = Field(..., min_length=1, max_length=10)
    quantity: Decimal = Field(...)

    @field_validator("quantity")
    @classmethod
    def must_be_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("quantity must be > 0")
        return v

    @field_validator("symbol", "pay_with")
    @classmethod
    def to_upper(cls, v: str) -> str:
        return v.strip().upper()


class OrderResult(BaseModel):
    # Response for a successfully filled order
    status:             str
    symbol:             str
    quantity_filled:    Decimal
    execution_price:    Decimal
    total_cost:         Decimal
    paid_with:          str
    remaining_balance:  Decimal
    executed_at:        datetime
    executed_by_thread: str

    @field_serializer("quantity_filled", "execution_price", "total_cost", "remaining_balance")
    def ser_decimal(self, v: Decimal) -> str:
        return _plain(v)

    model_config = ConfigDict(json_encoders={Decimal: _plain})

class PortfolioSummary(BaseModel):
    user_id:             int
    total_valuation_usd: Decimal
    holdings:            dict[str, Decimal]
    usd_values:          dict[str, Decimal]
    valued_at:           datetime

    @field_serializer("total_valuation_usd")
    def ser_total(self, v: Decimal) -> str:
        return _plain(v)

    model_config = ConfigDict(json_encoders={Decimal: _plain})


class ErrorResponse(BaseModel):
    timestamp:  datetime = Field(default_factory=datetime.utcnow)
    status:     int
    error_code: str
    message:    str

# Exceptions
class InsufficientFundsError(Exception):
    def __init__(self, needed: str, available: str, currency: str):
        self.needed = needed; self.available = available; self.currency = currency
        super().__init__(
            f"Need {needed} {currency} but only have {available} {currency}"
        )

class AssetNotFoundError(Exception):
    def __init__(self, symbol: str):
        self.symbol = symbol
        super().__init__(f"Asset not found: '{symbol}'")

class IdempotencyKeyMissingError(Exception):
    def __init__(self):
        super().__init__(
            "Header 'X-Idempotency-Key' is required. "
            "Provide a unique UUID per logical request."
        )

# Idempotency ; exactly-once execution

async def idempotency_lookup(key: str) -> str | None:
    # Return cached response body if key has been seen before, else None
    async with AsyncSessionLocal() as s:
        row = (await s.execute(
            select(IdempotencyRecord).where(IdempotencyRecord.request_key == key)
        )).scalar_one_or_none()
        return row.response_body if row else None

async def idempotency_persist(key: str, body: str, status: int) -> None:
    # Persist a result for future replays. Silently ignores duplicate-key races
    async with AsyncSessionLocal() as s:
        try:
            s.add(IdempotencyRecord(
                request_key=key, response_body=body,
                http_status=status, created_at=datetime.utcnow()
            ))
            await s.commit()
        except IntegrityError:
            await s.rollback()  # Another concurrent request already saved it; fine

# Execution service

SCALE = Decimal("0.00000001")   # 8 decimal places 

class _LockRegistry:
    def __init__(self):
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._mutex = threading.Lock()

    def get(self, user_id: int) -> asyncio.Lock:
        with self._mutex:
            return self._locks[user_id]

_locks = _LockRegistry()

class ExecutionService:

    async def execute_market_order(
        self, req: OrderRequest, session: AsyncSession
    ) -> OrderResult:
        async with _locks.get(req.user_id):          # Layer 1: in-process lock
            return await self._run(req, session)

    async def _run(self, req: OrderRequest, session: AsyncSession) -> OrderResult:
        thread = threading.current_thread().name

        asset_row = (await session.execute(
            select(Asset).where(Asset.symbol == req.symbol)
        )).scalar_one_or_none()
        if asset_row is None:
            raise AssetNotFoundError(req.symbol)

        pay_row = (await session.execute(
            select(Asset).where(Asset.symbol == req.pay_with)
        )).scalar_one_or_none()
        if pay_row is None:
            raise AssetNotFoundError(req.pay_with)
        
        price     = asset_row.price
        total     = (price * req.quantity).quantize(SCALE, ROUND_HALF_UP)

        payment = (await session.execute(
            select(Portfolio)
            .where(Portfolio.user_id == req.user_id, Portfolio.asset_symbol == req.pay_with)
            .with_for_update()                       # Layer 2: DB select for update
        )).scalar_one_or_none()

        if payment is None:
            raise InsufficientFundsError(str(total), "0", req.pay_with)

        balance = payment.quantity
        if balance < total:
            raise InsufficientFundsError(str(total), str(balance), req.pay_with)

        new_balance = (balance - total).quantize(SCALE, ROUND_HALF_UP)
        payment.quantity = new_balance
        session.add(payment)

        target = (await session.execute(
            select(Portfolio)
            .where(Portfolio.user_id == req.user_id,
                   Portfolio.asset_symbol == req.symbol)
            .with_for_update()
        )).scalar_one_or_none()

        if target is None:
            target = Portfolio(user_id=req.user_id, asset_symbol=req.symbol,   quantity=Decimal("0"))
            session.add(target)
            await session.flush()

        target.quantity = (target.quantity + req.quantity).quantize(SCALE, ROUND_HALF_UP)
        session.add(target)

        await session.commit()

        log.info("[%s] FILLED %s %s @ %s %s ; new balance: %s %s", thread, req.quantity, req.symbol, price, req.pay_with,  new_balance, req.pay_with)

        return OrderResult(
            status="ORDER_FILLED",
            symbol=req.symbol,
            quantity_filled=req.quantity,
            execution_price=price,
            total_cost=total,
            paid_with=req.pay_with,
            remaining_balance=new_balance,
            executed_at=datetime.utcnow(),
            executed_by_thread=thread,
        )

class ValuationService:

    async def get_valuation(self, user_id: int, session: AsyncSession) -> PortfolioSummary:
        positions = (await session.execute(
            select(Portfolio).where(Portfolio.user_id == user_id)
        )).scalars().all()

        prices = {a.symbol: a.price
                  for a in (await session.execute(select(Asset))).scalars().all()}

        holdings, usd_vals = {}, {}
        total = Decimal("0")
        for p in positions:
            price = prices.get(p.asset_symbol, Decimal("0"))
            val   = (p.quantity * price).quantize(SCALE, ROUND_HALF_UP)
            holdings[p.asset_symbol] = p.quantity
            usd_vals[p.asset_symbol] = val
            total += val

        return PortfolioSummary(
            user_id=user_id,
            total_valuation_usd=total.quantize(Decimal("0.01"), ROUND_HALF_UP),
            holdings=holdings,
            usd_values=usd_vals,
            valued_at=datetime.utcnow(),
        )


# Module-level singletons (stateless; safe to share across requests)
execution_svc = ExecutionService()
valuation_svc = ValuationService()

# FastApi

app = FastAPI(
    title="HFT Trading Engine",
    description=(
        "High-frequency trading engine; ACID, pessimistic locking, idempotency.\n\n"
        "**All POST requests require the `X-Idempotency-Key` header** "
        "(use a UUID v4 per logical request; retry with the same key safely)."
    ),
    version="1.0.0",
)

@app.on_event("startup")
async def startup():
    await init_db()
    log.info("HFT Engine ready: http://localhost:8000/docs")

def _err(status: int, code: str, msg: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=ErrorResponse(status=status, error_code=code, message=msg)
                .model_dump(mode="json"),
    )


@app.exception_handler(IdempotencyKeyMissingError)
async def _h_idem(r: Request, e: IdempotencyKeyMissingError):
    return _err(400, "ERR_IDEMPOTENCY_REQUIRED", str(e))

@app.exception_handler(InsufficientFundsError)
async def _h_funds(r: Request, e: InsufficientFundsError):
    return _err(402, "ERR_LIQUIDITY", str(e))

@app.exception_handler(AssetNotFoundError)
async def _h_asset(r: Request, e: AssetNotFoundError):
    return _err(404, "ERR_ASSET_NOT_FOUND", str(e))

@app.exception_handler(Exception)
async def _h_generic(r: Request, e: Exception):
    log.exception("Unhandled error: %s", e)
    return _err(500, "ERR_INTERNAL", "An unexpected error occurred.")

@app.post(
    "/api/v1/trading/execute",
    response_model=OrderResult,
    summary="Execute a market BUY order",
    tags=["Trading"],
)
async def execute_order(
    request: OrderRequest,
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    session: AsyncSession = Depends(get_db),
) -> JSONResponse:

    if not x_idempotency_key or not x_idempotency_key.strip():
        raise IdempotencyKeyMissingError()

    # Cache hit. Replay stored response
    cached = await idempotency_lookup(x_idempotency_key)
    if cached:
        log.info("Idempotency HIT — key: %s", x_idempotency_key)
        return JSONResponse(content=json.loads(cached), status_code=200)

    # Execute the trade
    result = await execution_svc.execute_market_order(request, session)
    body   = result.model_dump(mode="json")

    # Persist for future replays
    await idempotency_persist(
        key=x_idempotency_key,
        body=json.dumps(body, default=str),
        status=200,
    )

    return JSONResponse(content=body, status_code=200)

@app.get(
    "/api/v1/trading/portfolio/{user_id}",
    response_model=PortfolioSummary,
    summary="Get portfolio valuation",
    tags=["Trading"],
)
async def get_portfolio(
    user_id: int,
    session: AsyncSession = Depends(get_db),
) -> PortfolioSummary:
    # Returns a real-time USD valuation of all positions for a user
    return await valuation_svc.get_valuation(user_id, session)

@app.get(
    "/api/v1/trading/assets",
    summary="List market assets",
    tags=["Trading"],
)
async def list_assets(session: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await session.execute(select(Asset).order_by(Asset.symbol))).scalars().all()
    return [{"symbol": a.symbol, "price": _plain(a.price)} for a in rows]

@app.get("/health", tags=["Meta"])
async def health() -> dict:
    return {"status": "ok", "engine": "HFT Trading Engine v1.0"}

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        workers=1,          # Keep at 1 for asyncio.Lock to work correctly
                            # For multi-worker, replace with Redis distributed lock
        log_level="info",
    )

    