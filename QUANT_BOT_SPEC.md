# MASTER SYSTEM SPECIFICATION: AUTONOMOUS QUANTITATIVE TRADING ENGINE (LIVE PRODUCTION)
**Version:** 3.0 (Final Consolidated Release)  
**Target OS:** Local Ubuntu Server (Dockerized or Native Python 3.11+ with `chrony` NTP)  
**Exchange:** Binance Spot (Non-IP-Whitelisted Dynamic IP Compatible)  
**AI Layer:** Multi-Key OpenRouter Free-Tier Rotation + 100% Deterministic Math Fallback  
**Monitoring:** Local LAN Web Dashboard (`FastAPI` + TradingView `Lightweight Charts` on Port `8000`)

---

## 0. AGENT DIRECTIVE (FOR ANTIGRAVITY)

> **CRITICAL MISSION:** You are building a 24/7 live-production quantitative cryptocurrency trading system that executes with real capital on Binance Spot. **There is zero margin for execution error.**  
> 1. **Never rely on external SaaS harnesses** (ignore `agenkit.xyz` or `typesafe.ai`). Everything runs locally on the operator's Ubuntu server.  
> 2. **The operator will ONLY provide:** Binance API Key/Secret and 2–3 free OpenRouter API Keys in `.env`. Everything else (market scanning, pair selection, regime detection, sizing, execution, stop-loss placement, overnight calibration, and the LAN web dashboard) must operate autonomously out-of-the-box.  
> 3. **Zero Unprotected Exposure:** Every filled entry order MUST immediately place a Server-Side Stop-Loss (`STOP_LOSS_LIMIT`) on Binance. If Stop-Loss placement fails after 3 retries, immediately market-sell the position.  
> 4. **Zero-Cost Resilience:** Real-time candle evaluation must be 100% deterministic (pure Python/NumPy/TA-Lib) taking `<50ms`. OpenRouter free APIs are used strictly for asynchronous 4-hour macro commentary and overnight audits, with automatic key rotation and instant fallback to deterministic math on `HTTP 429` or timeouts.

---

## 1. DUAL-LAYER SYSTEM ARCHITECTURE (THE BRAIN & THE REFLEX)

Split the system strictly into two operational layers, a deterministic state engine, a hard risk gate, and a local LAN dashboard. **Never blur them, and never delegate math or risk limits to an LLM.**

```text
┌────────────────────────────────────────────────────────────────────────────┐
│ LAYER 1: THE BRAIN (System 2 - Slow, Deep Research & Overnight Review)     │
│ - Runs every 1-4 hours & overnight (00:05 UTC).                            │
│ - Evaluates BTC/ETH Market Regime ('trending', 'mean_reverting',           │
│   'high_vol', 'crisis') via Deterministic Math + OpenRouter Multi-Key Pool.│
│ - Runs Autonomous Market Scanner (Top 15-20 Liquid USDT Pairs on Binance). │
│ - Performs Overnight Post-Mortem (Brier Score calibration, parameter tune).│
└─────────────────────────────────────┬──────────────────────────────────────┘
                                      │ Updates Approved Watchlist & Regime
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ DETERMINISTIC STATE ENGINE (Pure Python / TA-Lib - Zero Future Leakage)    │
│ - Converts live orderbook & CLOSED candles (iloc[-2]) into a compact       │
│   numeric snapshot (<400 tokens) with strict causal timestamps.            │
│ - Computes Mid-Price, Spread, Orderbook Imbalance, Realized Volatility,    │
│   RSI(14), EMA(9/21/50/200), ADX(14), ATR(14), Inventory, and Drawdown.    │
└─────────────────────────────────────┬──────────────────────────────────────┘
                                      │ Feeds Compact Numeric State
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ LAYER 2: THE REFLEX (System 1 - Fast, Calibrated Decision Scorer)          │
│ - Evaluates every closed candle in <50ms across the 15-20 scanned pairs.   │
│ - Outputs strictly typed ReflexDecisionSchema (Direction, Setup Quality    │
│   0-3, Calibrated Confidence 0.0-1.0, Toxic Flow check, Risk State).       │
└─────────────────────────────────────┬──────────────────────────────────────┘
                                      │ Passes Typed Proposal
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ HARD DETERMINISTIC RISK & EXECUTION GATE (Code Owns Every Threshold)       │
│ - Gate Conditions: setup_quality >= 2, confidence > 0.80, risk_state ==    │
│   'safe', toxic_flow == 'none', regime != 'crisis'.                        │
│ - Sizing: Fractional Kelly Criterion (Capped strictly at Quarter-Kelly)    │
│   bounded by MAX_POSITION_USD and Binance minNotional limits.              │
│ - Idempotent CCXT Execution + Mandatory Server-Side Stop-Loss.             │
└─────────────────────────────────────┬──────────────────────────────────────┘
                                      │ Persists State & Telemetry
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ DUCKDB STORAGE & LOCAL LAN DASHBOARD (FastAPI + Lightweight Charts :8000)  │
│ - Real-time Equity, PnL, Active Watchlist, Candlestick Charts with Buy/SL  │
│   markers, Brier Score calibration logs, and 1-Click Emergency Kill-Switch.│
└────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. PROJECT DIRECTORY STRUCTURE

Antigravity must initialize and build this exact directory structure:

```text
quant-bot-live/
├── .env.example                 # Template requiring ONLY Binance & 2-3 OpenRouter keys
├── .gitignore                   # Excludes .env, data/*.db*, logs/*, __pycache__/
├── Dockerfile                   # Ubuntu/Python 3.11 image with compiled TA-Lib C library
├── docker-compose.yml           # Orchestrates engine + LAN dashboard (Port 8000)
├── requirements.txt             # Pinned production dependencies
├── main.py                      # Async entrypoint: runs Engine + FastAPI Dashboard + Scheduler
├── config/
│   ├── __init__.py
│   └── settings.py              # Pydantic BaseSettings with strict defaults & validation
├── database/
│   ├── __init__.py
│   └── db_manager.py            # Thread-safe DuckDB manager (trades, orders, states, brier)
├── engine/
│   ├── __init__.py
│   ├── exchange.py              # Async CCXT Binance wrapper (Precision, Clock Sync, Backoff)
│   ├── scanner.py               # Autonomous Dynamic Whitelist & BTC/ETH Regime Classifier
│   ├── state_engine.py          # Causal numeric snapshot generator (Strictly iloc[:-1])
│   ├── schemas.py               # Pydantic Typed Schemas (StateSnapshot, ReflexDecisionSchema)
│   ├── decision_reflex.py       # Deterministic quantitative setup scorer (<50ms)
│   ├── openrouter_pool.py       # Multi-key OpenRouter rotator (3 keys) + Instant Fallback
│   ├── risk_engine.py           # Hard gates, Quarter-Kelly calculator, Armed Kill-Switch
│   ├── execution.py             # Idempotent order placement + Mandatory Server-Side SL
│   └── reconciler.py            # 30-second state sync between DuckDB and Binance Spot
├── review/
│   ├── __init__.py
│   └── overnight_review.py      # 00:05 UTC Brier Score calibration & self-tuning loop
├── dashboard/
│   ├── __init__.py
│   ├── server.py                # FastAPI LAN backend bound to 0.0.0.0:8000
│   └── static/
│       └── index.html           # Single-page UI (Tailwind + TradingView Lightweight Charts)
└── tests/
    ├── test_causality.py        # Verifies zero future leakage (iloc[-1] exclusion)
    ├── test_kelly_and_risk.py   # Verifies Quarter-Kelly caps & Kill-Switch triggers
    ├── test_idempotency.py      # Verifies clientOrderId duplicate prevention & SL fallback
    └── test_openrouter_pool.py  # Verifies 3-key rotation and HTTP 429 graceful fallback
```

---

## 3. ENVIRONMENT CONFIGURATION (`.env.example`)

The operator only needs to fill in the first 5 lines. All risk and quantitative parameters have battle-tested defaults in `config/settings.py`:

```ini
# === REQUIRED OPERATOR CREDENTIALS ===
BINANCE_API_KEY=your_binance_spot_api_key_here
BINANCE_API_SECRET=your_binance_spot_api_secret_here

# Provide 1 to 3 free OpenRouter API keys for automatic round-robin rotation
OPENROUTER_API_KEY_1=sk-or-v1-first_free_key
OPENROUTER_API_KEY_2=sk-or-v1-second_free_key
OPENROUTER_API_KEY_3=sk-or-v1-third_free_key

# === FREE OPENROUTER MODELS (COMMA-SEPARATED FALLBACK CHAIN) ===
OPENROUTER_MODELS=deepseek/deepseek-r1:free,qwen/qwen3-235b-a22b:free,meta-llama/llama-3.3-70b-instruct:free

# === RISK & CAPITAL GUARDRAILS (PRE-CONFIGURED SAFE DEFAULTS) ===
TIMEFRAME=15m
MAX_OPEN_POSITIONS=3
MAX_POSITION_USD=25.0
DAILY_MAX_LOSS_USD=5.0
MAX_DRAWDOWN_PCT=15.0
MIN_24H_VOLUME_USD=50000000.0
MAX_SPREAD_PCT=0.15
DASHBOARD_PORT=8000
```

---

## 4. CORE MODULE SPECIFICATIONS

### 4.1. Autonomous Market Scanner & Regime Engine (`engine/scanner.py`)
The operator never manually selects coins. Every 1 hour (and on startup), the scanner:
1. **Macro Regime Detection:**
   - Fetches closed `1h` and `15m` candles for `BTC/USDT` and `ETH/USDT`.
   - Computes `EMA(50)`, `EMA(200)`, `ADX(14)`, and `ATR(14) / Close` (realized volatility).
   - Classifies `regime` into:
     - `'crisis'`: BTC `1h` drop `> 4%` in 4 candles OR volatility spike `> 3x` rolling average. **(Blocks all new Longs immediately).**
     - `'trending'`: Price `> EMA(200)` and `ADX(14) >= 22`.
     - `'high_vol'`: ATR volatility in top 85th percentile without crash breakdown.
     - `'mean_reverting'`: `ADX(14) < 22` and price oscillating within Bollinger Bands.
2. **Dynamic Whitelist Filtering:**
   - Calls `exchange.fetch_tickers()` for all `/USDT` spot pairs.
   - **Hard Exclusions:** Leveraged tokens (`UP/`, `DOWN/`, `BULL/`, `BEAR/`), stablecoins (`USDC/`, `FDUSD/`, `DAI/`, `TUSD/`, `USDP/`), and fiat pairs (`EUR/`, `TRY/`).
   - **Volume Filter:** `quoteVolume >= 50,000,000` USDT ($50M 24h volume).
   - **Spread / Slippage Filter:** `((ask - bid) / mid_price) * 100 <= 0.15%`.
   - Selects top **20 liquid pairs** ranked by 24h volume momentum and stores them in DuckDB `active_watchlist`.

### 4.2. Multi-Key OpenRouter Rotator (`engine/openrouter_pool.py`)
- Loads `OPENROUTER_API_KEY_1`, `OPENROUTER_API_KEY_2`, and `OPENROUTER_API_KEY_3` into a round-robin async pool.
- **Usage Scope:** Called ONLY during the 1-hour/4-hour Brain cycle (to refine setup quality explanations) and the `00:05 UTC` Overnight Review. **Never called in the critical millisecond order path.**
- **Fault Tolerance:**
  - Strict timeout of `8.0 seconds` per request.
  - On `HTTP 429 (Rate Limit)`, `HTTP 5xx`, or timeout, immediately rotate to Key #2 -> Key #3 -> Next Free Model in `OPENROUTER_MODELS`.
  - If all keys/models fail, log a warning and return the pure deterministic calculation without throwing an unhandled exception or halting trading.

### 4.3. Deterministic State Engine (`engine/state_engine.py`)
- **Strict Causality (Zero Repainting):** When fetching OHLCV via CCXT (`limit=250`), immediately drop the last forming candle (`df_closed = df.iloc[:-1]`). Compute all indicators on `df_closed` and attach `causal_timestamp_ms = int(df_closed.iloc[-1]['timestamp'])`.
- Fetch top 10 levels of the live orderbook (`fetch_order_book(symbol, limit=10)`) and compute:
  - $\text{mid\_price} = \frac{\text{best\_bid} + \text{best\_ask}}{2}$
  - $\text{spread\_bps} = \frac{\text{best\_ask} - \text{best\_bid}}{\text{mid\_price}} \times 10000$
  - $\text{orderbook\_imbalance} = \frac{\sum V_{\text{bid}} - \sum V_{\text{ask}}}{\sum V_{\text{bid}} + \sum V_{\text{ask}}} \in [-1.0, +1.0]$
- Detect `toxic_flow`:
  - `'spread_widening'` if $\text{spread\_bps} > 15.0$.
  - `'volume_exhaustion'` if current closed candle volume $< 0.3 \times \text{SMA(Volume, 20)}$ on a breakout.
  - `'spoofing_suspected'` if $\text{orderbook\_imbalance} < -0.65$ while price attempts an upward cross.
  - `'none'` otherwise.

### 4.4. Typed Decision Schema & Reflex Scorer (`engine/schemas.py` & `decision_reflex.py`)
Every evaluated pair on every closed candle produces an immutable `ReflexDecisionSchema`:

```python
from pydantic import BaseModel, Field
from typing import Literal

class ReflexDecisionSchema(BaseModel):
    symbol: str
    causal_timestamp_ms: int
    regime: Literal['trending', 'mean_reverting', 'high_vol', 'crisis']
    direction: Literal['long', 'short', 'neutral']
    toxic_flow: Literal['none', 'spoofing_suspected', 'spread_widening', 'volume_exhaustion']
    setup_quality: int = Field(ge=0, le=3)
    direction_confidence: float = Field(ge=0.0, le=1.0)
    risk_state: Literal['safe', 'near_limit', 'reduce']
    entry_price: float = Field(gt=0.0)
    invalidation_price: float = Field(gt=0.0, description="Hard Server-Side Stop-Loss Price")
    take_profit_price: float = Field(gt=0.0)
    expected_reward_risk_ratio: float = Field(gt=0.0)
    rationale: str
```

**Scoring Logic (`decision_reflex.py`):**
- Evaluates two complementary sub-strategies based on `regime`:
  1. **Trend Momentum (`regime == 'trending'`):** Price $> \text{EMA}(200)$, $\text{EMA}(9) > \text{EMA}(21)$, $48 \le \text{RSI}(14) \le 68$, Volume $> 1.2 \times \text{SMA(Vol, 20)}$, and $\text{orderbook\_imbalance} > -0.15$.
  2. **Mean Reversion Dip (`regime == 'mean_reverting'`):** Price $> \text{EMA}(200)$ (macro uptrend intact), Price touches lower Bollinger Band ($20, 2\sigma$), $\text{RSI}(14) < 34$ turning up on closed candle.
- Calculates `invalidation_price` (Stop-Loss) at $\text{entry\_price} - 2.0 \times \text{ATR}(14)$ (capped at max $1.8\%$ distance from entry) and `take_profit_price` at $\text{entry\_price} + 2.5 \times (\text{entry\_price} - \text{invalidation\_price})$.
- Assigns `setup_quality` ($0$ to $3$) and calibrated `direction_confidence` ($0.00$ to $0.95$).

---

## 5. HARD DETERMINISTIC RISK GATE & EXECUTION (`risk_engine.py` & `execution.py`)

### 5.1. Non-Negotiable Execution Gates
Code rejects any trade proposal unless **ALL** conditions below are `True`:
- `decision.direction == 'long'` (Spot Long only)
- `decision.setup_quality >= 2`
- `decision.direction_confidence >= 0.80` (dynamically adjustable upward by Brier calibration)
- `decision.risk_state == 'safe'` and `decision.toxic_flow == 'none'`
- `decision.regime != 'crisis'`
- `open_positions < settings.MAX_OPEN_POSITIONS`
- `system_halted == False`

### 5.2. Quarter-Kelly Position Sizing Formula
Never use arbitrary sizing. Calculate exact capital allocation:
1. Let $p = \text{direction\_confidence}$, $q = 1.0 - p$, and $b = \text{expected\_reward\_risk\_ratio}$ (net of $0.20\%$ round-trip Binance Spot fee buffer).
2. Full Kelly fraction:
   $$f^* = \frac{b \cdot p - q}{b}$$
3. Quarter-Kelly safe fraction:
   $$f_{\text{quarter}} = \max(0.0, \min(0.25, f^* \times 0.25))$$
4. Target USD Position:
   $$\text{Position}_{\text{USD}} = \min\left(\text{Free USDT} \times f_{\text{quarter}}, \text{MAX\_POSITION\_USD}\right)$$
5. Verify $\text{Position}_{\text{USD}} \ge \text{market['limits']['cost']['min']} \times 1.10$ (typically $\ge 11.0\text{ USDT}$ on Binance).

### 5.3. Idempotent Execution & Atomic Server-Side Stop-Loss
1. **Precision Formatting:** Always format quantities and prices via `exchange.amount_to_precision(symbol, qty)` and `exchange.price_to_precision(symbol, price)`.
2. **Deterministic `clientOrderId`:** Generate `newClientOrderId = f"QB_{symbol.replace('/','')}_{causal_timestamp_ms}"`.
3. **Timeout Recovery:** If `create_order()` raises `RequestTimeout` or `NetworkError`, query `fetch_order_by_client_order_id()` before retrying.
4. **Immediate Server-Side Stop-Loss (`STOP_LOSS_LIMIT`):**
   - Within `500ms` of entry fill, place a server-side `STOP_LOSS_LIMIT` order on Binance for the exact filled amount (net of fees, checking actual free token balance via `fetch_balance()`) with `stopPrice = invalidation_price` and limit `price = invalidation_price * 0.997` (to guarantee fill during fast drops).
   - **Emergency Flatten Rule:** If the server-side Stop-Loss order fails after 3 attempts, immediately execute a `create_market_sell_order()` to close the unprotected position, log a critical alert, and pause that symbol for 4 hours.

### 5.4. Dynamic IP & Clock Drift Resilience
- Because the operator runs on a Dynamic IP without IP-whitelisting on Binance:
  - Catch `ccxt.AuthenticationError` / expired key errors explicitly and trigger an instant dashboard banner alert.
  - Enable `options: {'adjustForTimeDifference': True, 'recvWindow': 10000}` in CCXT Binance initialization, and verify clock drift every 15 minutes.

---

## 6. OVERNIGHT BRIER SCORE CALIBRATION (`review/overnight_review.py`)

Every night at `00:05 UTC`:
1. Evaluate all resolved trades and logged high-confidence setups from the past 24 hours.
2. Compute the **Brier Score**:
   $$BS = \frac{1}{N} \sum_{i=1}^{N} (f_i - o_i)^2$$
   where $f_i$ is `direction_confidence` and $o_i \in \{0, 1\}$ is the binary outcome ($1$ if profitable net of fees, $0$ if stopped out).
3. **Self-Improvement Rule:**
   - If $BS > 0.25$ (overconfident / poorly calibrated), automatically raise `MIN_CONFIDENCE_THRESHOLD` by `+0.03` (up to `0.90`) and reduce `MAX_POSITION_USD` by `20%` for the next 24 hours.
   - Optionally call the OpenRouter pool to generate a concise human-readable Markdown summary saved to `logs/overnight_audit_YYYY_MM_DD.md` and displayed on the Local Dashboard.

---

## 7. LOCAL LAN WEB DASHBOARD (`dashboard/server.py` & `index.html`)

Build a fast, lightweight web interface accessible from any device on the operator's home Wi-Fi/LAN at `http://<ubuntu-lan-ip>:8000`:
- **Top KPI Bar:**
  - Live Total USDT Equity, Free USDT, Daily Realized PnL ($ and %), Current Drawdown %, Market Regime Badge (`TRENDING` / `MEAN_REVERTING` / `CRISIS`), and System Status (`ACTIVE` / `HALTED`).
- **Interactive Chart Panel (TradingView `Lightweight Charts` CDN):**
  - Dropdown to select any symbol from the active watchlist or open positions.
  - Displays 15m candlesticks, EMA(200), Entry markers, Take-Profit lines, and live **Server-Side Stop-Loss lines**.
- **Open Positions & Orders Table:**
  - Symbol, Entry Price, Current Price, Unrealized PnL, Server-Side SL Order ID, SL Price, and an individual **"Close Position (Market)"** button.
- **Autonomous Scanner Watchlist Table:**
  - Top 20 liquid pairs with 24h Volume ($M), Spread (bps), RSI(14), Setup Quality (0-3), and Confidence %.
- **OpenRouter Pool & Overnight Brier Telemetry:**
  - Displays active/healthy OpenRouter keys, latest Brier Score, and the latest Overnight Review notes.
- **Armed Emergency Kill-Switch Button:**
  - A prominent red **"PANIC STOP / FLATTEN ALL"** button (`POST /api/kill-switch`) that cancels all open orders on Binance, market-sells open positions, sets `system_halted = True` in DuckDB, and locks the engine.

---

## 8. 6-PHASE BUILD & VERIFICATION PLAN FOR ANTIGRAVITY

Execute these 6 phases sequentially. **Do not use `TODO` or `pass` placeholders.** Run `pytest` at the end of each phase before proceeding.

- [ ] **Phase 1: Core Config, DuckDB Layer & Multi-Key OpenRouter Pool**
  - Implement `config/settings.py`, `database/db_manager.py`, and `engine/openrouter_pool.py`.
  - Run `pytest tests/test_openrouter_pool.py` to verify 3-key rotation and offline fallback.
- [ ] **Phase 2: CCXT Binance Wrapper & Autonomous Market Scanner**
  - Implement `engine/exchange.py` and `engine/scanner.py` ($50M volume filter, 0.15% max spread, BTC/ETH regime detector).
- [ ] **Phase 3: Causal State Engine & Reflex Decision Scorer**
  - Implement `engine/schemas.py`, `engine/state_engine.py`, and `engine/decision_reflex.py`.
  - Run `pytest tests/test_causality.py` to prove `iloc[-1]` is never used in indicator calculations.
- [ ] **Phase 4: Hard Risk Engine, Quarter-Kelly & Idempotent Execution**
  - Implement `engine/risk_engine.py`, `engine/execution.py` (with mandatory `STOP_LOSS_LIMIT` + dust/fee handling + emergency market-sell fallback), and `engine/reconciler.py`.
  - Run `pytest tests/test_kelly_and_risk.py` and `pytest tests/test_idempotency.py`.
- [ ] **Phase 5: Overnight Brier Calibration & Local LAN Dashboard**
  - Implement `review/overnight_review.py`, `dashboard/server.py`, and `dashboard/static/index.html` (using Tailwind CSS + Lightweight Charts CDN).
- [ ] **Phase 6: Main Loop Integration, Dockerization & Pre-Flight Self-Test**
  - Implement `main.py` (running the async trading loop, 30s reconciler, APScheduler jobs, and Uvicorn dashboard concurrently with graceful `SIGINT/SIGTERM` shutdown).
  - Create `Dockerfile`, `docker-compose.yml`, and a `preflight_check.py` script that tests Binance connectivity, clock sync (`<500ms`), DuckDB write permissions, and OpenRouter keys before enabling live orders.

---

## 9. OPERATOR QUICK-START (UBUNTU SERVER)

Once Antigravity finishes building and testing all 6 phases:

1. **Copy `.env.example` to `.env` and paste your keys:**
   ```bash
   cp .env.example .env
   nano .env
   ```
2. **Run Pre-Flight Verification (No orders placed, checks API, Clock, and DB):**
   ```bash
   docker compose run --rm quant-bot python preflight_check.py
   ```
3. **Launch the Live System & LAN Dashboard:**
   ```bash
   docker compose up -d --build
   ```
4. **Open the Dashboard from any browser on your local Wi-Fi:**
   ```text
   http://<YOUR_UBUNTU_LOCAL_IP>:8000
   ```