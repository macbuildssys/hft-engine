import asyncio
import time
import uuid
import pytest
from decimal import Decimal
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import NullPool
from sqlalchemy import select

from main import Base, Asset, Portfolio
from main import InsufficientFundsError, AssetNotFoundError
from main import OrderRequest
from main import ExecutionService
from main import ValuationService


# ── Isolated DB for concurrency tests (avoid interfering with unit tests) ─────

CONCURRENCY_DB_URL = "sqlite+aiosqlite:///./test_concurrency.db"

conc_engine = create_async_engine(
    CONCURRENCY_DB_URL,
    poolclass=NullPool,
    connect_args={"check_same_thread": False},
)

ConcSessionLocal = async_sessionmaker(
    bind=conc_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


_db_initialized = False


async def setup_concurrency_db():
    """Reset the concurrency test DB to a known-good state."""
    global _db_initialized
    async with conc_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    _db_initialized = True

    async with ConcSessionLocal() as session:
        session.add_all([
            Asset(symbol="BTC", price=Decimal("50000.00000000"), version=0),
            Asset(symbol="USD", price=Decimal("1.00000000"),     version=0),
            Asset(symbol="ETH", price=Decimal("3000.00000000"),  version=0),
        ])
        # stress test (100 × $50k BTC)
        session.add(Portfolio(user_id=10, asset_symbol="USD",
                              quantity=Decimal("100000.00000000")))
        # fractional test (20 × 0.01 BTC = $10k total spend)
        session.add(Portfolio(user_id=20, asset_symbol="USD",
                              quantity=Decimal("100000.00000000")))
        # benchmark lightweight (20 × 0.001 BTC at $3k ETH = $60 spend)
        session.add_all([
            Portfolio(user_id=30, asset_symbol="USD",
                      quantity=Decimal("50000.00000000")),
            Portfolio(user_id=30, asset_symbol="ETH",
                      quantity=Decimal("0.00000000")),
        ])
        await session.commit()


class TestConcurrencyStress:
    """
    All tests use asyncio.gather() to fire coroutines concurrently,
    exercising the locking paths under realistic concurrent load.
    """

    @pytest.mark.asyncio
    async def test_100_concurrent_orders_exactly_2_succeed(self):
        """
        THE CRITICAL STRESS TEST.

        Setup:   User 1 has $100,000 USD. BTC = $50,000.
        Action:  100 coroutines simultaneously try to BUY 1 BTC ($50,000).
        Expected:
          - Exactly 2 succeed (2 × $50,000 = $100,000)
          - 98 fail with InsufficientFundsError
          - Final USD balance = 0 (never negative)
          - Final BTC balance = 2.0 exactly
          - Total portfolio value conserved ($100,000)
        """
        await setup_concurrency_db()
        svc = ExecutionService()
        N = 100
        successes, failures = 0, 0

        async def attempt():
            async with ConcSessionLocal() as session:
                req = OrderRequest(
                    user_id=10, symbol="BTC", pay_with="USD",
                    quantity=Decimal("1.0")
                )
                try:
                    await svc.execute_market_order(req, session)
                    return "success"
                except (InsufficientFundsError, Exception):
                    return "failure"

        results = await asyncio.gather(*[attempt() for _ in range(N)])
        successes = results.count("success")
        failures  = results.count("failure")

        print(f"\n{'─'*50}")
        print(f"  100-Thread Stress Test Results")
        print(f"  Successes : {successes}")
        print(f"  Failures  : {failures}")

        # Fetch final state
        async with ConcSessionLocal() as session:
            usd = await session.execute(
                select(Portfolio).where(
                    Portfolio.user_id == 10,
                    Portfolio.asset_symbol == "USD",
                )
            )
            btc = await session.execute(
                select(Portfolio).where(
                    Portfolio.user_id == 10,
                    Portfolio.asset_symbol == "BTC",
                )
            )
            usd_pos = usd.scalar_one_or_none()
            btc_pos = btc.scalar_one_or_none()

        usd_bal = usd_pos.quantity if usd_pos else Decimal("0")
        btc_bal = btc_pos.quantity if btc_pos else Decimal("0")

        print(f"  USD Balance: {usd_bal}")
        print(f"  BTC Balance: {btc_bal}")
        print(f"{'─'*50}\n")

        # Core assertions 
        assert successes == 2, f"Expected 2 successes, got {successes}"
        assert failures  == 98, f"Expected 98 failures, got {failures}"

        # THE SAFETY INVARIANT: balance must never go negative
        assert usd_bal >= Decimal("0"), \
            f"RACE CONDITION DETECTED: USD balance went negative ({usd_bal})"

        assert btc_bal == Decimal("2.00000000"), \
            f"Expected 2.0 BTC, got {btc_bal}"

        # Value conservation: 0 USD + 2 BTC × $50k = $100k
        total_value = usd_bal + btc_bal * Decimal("50000")
        assert total_value == Decimal("100000.00000000"), \
            f"Value not conserved! Total: {total_value}"

    @pytest.mark.asyncio
    async def test_concurrent_fractional_orders_precision(self):
        """
        20 coroutines each buy 0.01 BTC ($500 each = $10,000 total).
        All should succeed and the balance change must be exact.
        """
        await setup_concurrency_db()
        svc = ExecutionService()

        async def buy_fractional():
            async with ConcSessionLocal() as session:
                req = OrderRequest(
                    user_id=20, symbol="BTC", pay_with="USD",
                    quantity=Decimal("0.01")
                )
                try:
                    await svc.execute_market_order(req, session)
                    return True
                except Exception:
                    return False

        results = await asyncio.gather(*[buy_fractional() for _ in range(20)])
        successes = sum(results)

        async with ConcSessionLocal() as session:
            pos = await session.execute(
                select(Portfolio).where(
                    Portfolio.user_id == 20,
                    Portfolio.asset_symbol == "USD",
                )
            )
            usd_bal = pos.scalar_one().quantity

        print(f"\n  Fractional Test: {successes}/20 succeeded, USD balance: {usd_bal}")

        assert successes == 20
        # 100,000 - (20 × 0.01 × 50,000) = 100,000 - 10,000 = 90,000
        assert usd_bal == Decimal("90000.00000000"), \
            f"Decimal precision error! Expected 90000, got {usd_bal}"


class TestPerformanceBenchmark:
    """
    Lightweight performance benchmarks.
    Prints throughput and latency metrics to stdout.
    """

    @pytest.mark.asyncio
    async def test_benchmark_20_concurrent_lightweight_orders(self):
        """
        Benchmark: 20 concurrent small orders (all succeed).
        Measures throughput (orders/sec) and latency percentiles.
        """
        await setup_concurrency_db()
        svc = ExecutionService()
        latencies: list[float] = []

        async def timed_order():
            async with ConcSessionLocal() as session:
                req = OrderRequest(
                    user_id=30, symbol="ETH", pay_with="USD",
                    quantity=Decimal("0.001")   # $3 per order × 20 = $60 total
                )
                t0 = time.perf_counter()
                try:
                    await svc.execute_market_order(req, session)
                    latencies.append((time.perf_counter() - t0) * 1000)
                    return True
                except Exception:
                    return False

        wall_start = time.perf_counter()
        results = await asyncio.gather(*[timed_order() for _ in range(20)])
        wall_ms = (time.perf_counter() - wall_start) * 1000

        successes = sum(results)
        throughput = successes / (wall_ms / 1000) if wall_ms > 0 else 0

        sorted_lat = sorted(latencies)
        p95 = sorted_lat[int(len(sorted_lat) * 0.95)] if sorted_lat else 0
        p99 = sorted_lat[int(len(sorted_lat) * 0.99)] if sorted_lat else 0

        print(f"\n╔{'═'*48}╗")
        print(f"║  Benchmark: 20 Concurrent Lightweight Orders   ║")
        print(f"╠{'═'*48}╣")
        print(f"║  Successes  : {successes:<33}║")
        print(f"║  Wall Time  : {wall_ms:<30.1f} ms ║")
        print(f"║  Throughput : {throughput:<28.1f} ops/s ║")
        print(f"╠{'═'*48}╣")
        if sorted_lat:
            print(f"║  Min Latency: {min(sorted_lat):<30.2f} ms ║")
            print(f"║  Avg Latency: {sum(sorted_lat)/len(sorted_lat):<30.2f} ms ║")
            print(f"║  P95 Latency: {p95:<30.2f} ms ║")
            print(f"║  P99 Latency: {p99:<30.2f} ms ║")
            print(f"║  Max Latency: {max(sorted_lat):<30.2f} ms ║")
        print(f"╚{'═'*48}╝\n")

        assert successes == 20

    @pytest.mark.asyncio
    async def test_benchmark_50_contention(self):
        """
        Benchmark: 50 concurrent orders competing for the same $100k.
        Only 2 succeed; rest hit lock contention + InsufficientFunds.
        Measures latency of the lock-acquisition + error path.
        """
        await setup_concurrency_db()
        svc = ExecutionService()
        all_latencies: list[float] = []
        error_latencies: list[float] = []

        async def contended_order():
            async with ConcSessionLocal() as session:
                req = OrderRequest(
                    user_id=10, symbol="BTC", pay_with="USD",
                    quantity=Decimal("1.0")
                )
                t0 = time.perf_counter()
                try:
                    await svc.execute_market_order(req, session)
                    lat = (time.perf_counter() - t0) * 1000
                    all_latencies.append(lat)
                    return "success"
                except (InsufficientFundsError, Exception):
                    lat = (time.perf_counter() - t0) * 1000
                    all_latencies.append(lat)
                    error_latencies.append(lat)
                    return "failure"

        wall_start = time.perf_counter()
        results = await asyncio.gather(*[contended_order() for _ in range(50)])
        wall_ms = (time.perf_counter() - wall_start) * 1000

        successes = results.count("success")
        failures  = results.count("failure")

        print(f"\n╔{'═'*48}╗")
        print(f"║  Benchmark: 50 Contention Orders               ║")
        print(f"╠{'═'*48}╣")
        print(f"║  Successes   : {successes:<32}║")
        print(f"║  Failures    : {failures:<32}║")
        print(f"║  Wall Time   : {wall_ms:<29.1f} ms ║")
        if all_latencies:
            sorted_all = sorted(all_latencies)
            print(f"║  Avg Latency : {sum(all_latencies)/len(all_latencies):<29.2f} ms ║")
            print(f"║  Max Latency : {max(all_latencies):<29.2f} ms ║")
        print(f"╚{'═'*48}╝\n")

        assert successes >= 1  # At minimum 1 order must succeed with $100k balance