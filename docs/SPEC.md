# Build an Autonomous AI Day-Trading Paper-Trading Application

You are a senior software architect, quantitative trading engineer, ML/AI engineer, DevOps engineer, security engineer, and full-stack developer.

Build a production-quality **autonomous AI-powered stock day-trading application**.

The application must initially operate **EXCLUSIVELY in paper-trading mode**. It must never place a real-money order.

The system should continuously analyze the stock market, identify potential intraday trading opportunities, decide whether to enter or exit positions, execute paper trades automatically, manage risk, and provide a web/mobile-friendly dashboard showing exactly what the system did and why.

The objective is NOT to guarantee profits. The system must prioritize capital preservation, configurable risk limits, and controlled experimentation.

---

# 1. CORE REQUIREMENT

Build an autonomous trading system that:

1. Collects real-time or near-real-time U.S. stock market data.
2. Filters the universe to stocks priced above a configurable minimum price.
3. Does NOT trade penny stocks.
4. Analyzes market conditions and individual stocks.
5. Generates potential BUY/SELL/HOLD decisions.
6. Uses AI/ML intelligence to evaluate trading opportunities.
7. Uses deterministic risk-management rules before allowing any trade.
8. Automatically places paper orders.
9. Automatically manages open positions.
10. Automatically exits positions according to predefined rules.
11. Prevents excessive trading.
12. Tracks every decision and order.
13. Calculates realized and unrealized P&L.
14. Provides daily/weekly/monthly performance.
15. Provides a calendar showing daily profit/loss.
16. Allows the user to click a day and see all trades taken that day.
17. Allows the user to click a trade and see:

    * Symbol
    * Buy price
    * Sell price
    * Quantity
    * Entry time
    * Exit time
    * Profit/loss
    * Percentage return
    * Stop-loss
    * Take-profit
    * AI confidence
    * Signals that triggered the trade
    * Reason for entry
    * Reason for exit
18. Runs automatically without requiring the user to approve individual trades.
19. Provides a master PAUSE/STOP button that immediately prevents new trades.
20. Has a configurable maximum amount of capital that can be exposed at one time.

---

# 2. IMPORTANT SAFETY ARCHITECTURE

Do NOT allow the AI model itself to directly control unrestricted brokerage APIs.

Use this architecture:

Market Data
|
v
Market Data Service
|
v
Feature Engineering
|
v
AI/ML Analysis
|
v
Trading Signal
|
v
Risk Management Engine
|
+---- REJECT
|
v
Order Manager
|
v
Paper Broker API
|
v
Execution/Fills
|
v
Portfolio & P&L
|
v
Database
|
v
Dashboard

The AI proposes trades.

The deterministic Risk Engine has final authority.

The AI must NEVER be able to bypass:

* Maximum position size
* Maximum total exposure
* Maximum daily loss
* Maximum loss per trade
* Maximum number of trades
* Maximum number of open positions
* Minimum stock price
* Trading hours
* Stop-loss requirements
* Take-profit requirements
* Kill switch
* Data-quality checks
* Account buying-power checks

---

# 3. INITIAL BROKER

Implement the first version using:

Alpaca Paper Trading API.

Use the PAPER endpoint only.

The application must have an explicit environment variable:

TRADING_MODE=PAPER

The application must refuse to start automated trading if:

TRADING_MODE != PAPER

Do NOT implement live trading in Version 1.

Create the broker abstraction so that a future broker can be plugged in:

Broker
├── AlpacaPaperBroker
└── FutureLiveBroker (NOT IMPLEMENTED)

Never place a real order in this project.

Use separate paper credentials stored in environment variables/secrets.

Never commit API keys to Git.

---

# 4. CONFIGURATION

Create a configuration screen and configuration file.

The following must be configurable without changing source code.

Example:

MIN_STOCK_PRICE = $10

MAX_CAPITAL_PER_TRADE = $10,000

MAX_TOTAL_EXPOSURE = $50,000

MAX_OPEN_POSITIONS = 5

MAX_DAILY_LOSS = $500

MAX_LOSS_PER_TRADE = $100

MAX_DAILY_TRADES = 20

MAX_TRADES_PER_SYMBOL_PER_DAY = 3

DEFAULT_STOP_LOSS_PERCENT = 1.0%

DEFAULT_TAKE_PROFIT_PERCENT = 2.0%

MIN_AI_CONFIDENCE = 0.70

MIN_AVERAGE_DAILY_VOLUME = configurable

MAX_SPREAD_PERCENT = configurable

NO_NEW_TRADES_AFTER = 3:30 PM ET

FORCE_CLOSE_TIME = 3:55 PM ET

TRADING_START_TIME = 9:35 AM ET

TRADING_END_TIME = 4:00 PM ET

ALLOW_SHORT_SELLING = false

ALLOW_FRACTIONAL_SHARES = configurable

PAPER_INITIAL_BALANCE = $100,000.00

MAX_CONCURRENT_POSITIONS = configurable

AI_MODEL = configurable

The user should be able to change these settings through the UI.

---

# 5. CAPITAL/RISK MODEL

Implement strict capital protection.

For every proposed trade calculate:

position_size

maximum_allowed_loss

stop_loss_price

take_profit_price

risk_reward_ratio

portfolio_exposure_after_trade

daily_loss_after_trade

The system must reject a trade if any risk constraint is violated.

Example:

Account = $100,000

Maximum trade allocation = $2,000

Maximum loss per trade = $100

Stock = $50

Stop loss = 1%

Risk per share = $0.50

Maximum shares based on risk = 200

Maximum shares based on capital = 40

Therefore:

Allowed quantity = MIN(200, 40) = 40 shares

Position value = $2,000

Maximum theoretical stop loss = $20

---

# 6. DAILY LOSS PROTECTION

Implement a hard daily loss limit.

Example:

MAX_DAILY_LOSS = $500

If realized + unrealized loss reaches:

-$500

then:

1. Cancel all pending entry orders.
2. Prevent new trades.
3. Continue monitoring existing positions.
4. Optionally close existing positions according to configurable emergency policy.
5. Mark system status:

DAILY_LOSS_LIMIT_REACHED

6. Require the user to manually reset trading.

Never allow the AI to override this.

---

# 7. STOCK UNIVERSE

Do not blindly scan every listed stock.

Create a configurable stock universe.

Initial filtering:

price >= $10

average daily volume >= configurable threshold

market capitalization >= configurable threshold

exclude penny stocks

exclude ETFs unless explicitly enabled

exclude leveraged ETFs unless explicitly enabled

exclude OTC securities

exclude securities with insufficient market data

exclude securities with excessive bid/ask spreads

exclude halted securities

exclude securities with suspicious/poor-quality data

The user should be able to configure these filters.

---

# 8. MARKET ANALYSIS

Build a multi-factor analysis engine.

Use multiple signals rather than relying on a single indicator.

Possible features:

PRICE:

* Current price
* Previous close
* Gap %
* Intraday return
* VWAP distance
* ATR
* Daily range
* 5-minute range
* 15-minute range

MOMENTUM:

* RSI
* MACD
* Rate of Change
* Moving averages
* EMA 9
* EMA 20
* EMA 50

VOLUME:

* Relative volume
* Volume spike
* Volume trend
* Volume vs historical average

VOLATILITY:

* ATR
* Historical volatility
* Intraday volatility

MARKET CONTEXT:

* SPY direction
* QQQ direction
* Sector ETF direction
* Market breadth if available
* VIX or volatility proxy

PRICE ACTION:

* Breakouts
* Support/resistance
* VWAP reclaim
* VWAP rejection
* Higher highs
* Lower lows
* Consolidation breakouts

---

# 9. AI DECISION ENGINE

The AI should NOT simply ask:

"Will this stock go up?"

Instead create a structured decision model.

For every candidate stock generate:

{
symbol,
action,
confidence,
expected_return,
estimated_risk,
stop_loss,
take_profit,
holding_period,
signals,
reasoning,
market_context,
risk_reward_ratio
}

Possible actions:

BUY
SELL
HOLD
NO_TRADE

Example:

{
"symbol": "AAPL",
"action": "BUY",
"confidence": 0.81,
"expected_return": 0.018,
"estimated_risk": 0.007,
"stop_loss": 0.010,
"take_profit": 0.020,
"holding_period": "intraday",
"risk_reward_ratio": 2.0,
"signals": [
"VWAP reclaim",
"high relative volume",
"EMA momentum",
"market trend positive"
]
}

The AI must output structured JSON.

Do NOT allow free-form AI output to directly trigger trades.

---

# 10. AI + QUANTITATIVE MODEL

Do not make the entire trading system dependent on an LLM.

Use a hybrid approach:

Technical/quantitative signals
+
Statistical/ML model
+
Market regime detection
+
Optional LLM reasoning layer

The deterministic/ML layer should produce the primary trading score.

The LLM can be used to:

* interpret market context
* summarize signals
* classify news/sentiment if reliable data is available
* explain decisions
* detect unusual conditions

The LLM must NOT override the deterministic risk engine.

---

# 11. NEWS/SENTIMENT

Create an optional news-analysis component.

For candidate stocks retrieve recent relevant news.

Classify:

POSITIVE
NEGATIVE
NEUTRAL
UNKNOWN

Also detect:

earnings
guidance
FDA events
mergers
acquisitions
analyst upgrades/downgrades
major lawsuits
regulatory events
significant company announcements

Do not trade solely because an LLM says news is positive.

If news data is unavailable or stale:

NEWS_SIGNAL = UNKNOWN

Do not fabricate news.

---

# 12. TRADE ENTRY STRATEGIES

Implement several configurable strategies.

At minimum:

## Strategy A — Momentum Breakout

Conditions can include:

* price above VWAP
* EMA 9 > EMA 20
* increasing volume
* relative volume above threshold
* breakout of recent high
* market regime supportive
* acceptable spread
* AI confidence above threshold

## Strategy B — VWAP Reclaim

Conditions:

* stock previously below VWAP
* price reclaims VWAP
* increasing volume
* momentum confirmation

## Strategy C — Pullback

Conditions:


* established intraday trend
* controlled pullback
* support near EMA/VWAP
* volume confirmation
* market trend confirmation

Each strategy must have its own configuration.

---

# 13. ENTRY SCORING

Create a scoring system.

Example:

Momentum: 20%
Volume: 20%
Price action: 20%
Market regime: 15%
Volatility: 10%
Technical indicators: 10%
News/sentiment: 5%

Calculate:

TRADE_SCORE = weighted score

Only pass candidates above:

MIN_TRADE_SCORE

The exact weights must be configurable.

Do not claim that a particular score guarantees profitability.

---

# 14. EXIT ENGINE

Every position must have an exit plan.

Support:

1. Stop loss
2. Take profit
3. Trailing stop
4. Time-based exit
5. Momentum reversal
6. VWAP loss
7. Market regime reversal
8. End-of-day liquidation

Example:

BUY AAPL @ $200

Stop = $198

Take profit = $204

If price reaches $204:

SELL

If price falls to $198:

SELL

If configured trailing stop activates:

adjust stop dynamically.

---

# 15. NO OVERNIGHT POSITIONS

Version 1 must be intraday only.

Before the market closes:

close all open positions.

Do not carry positions overnight.

Implement a safety mechanism so that the system attempts to close all positions before the configured force-close time.

If a position cannot be closed:

raise a HIGH PRIORITY ALERT.

---

# 16. ORDER MANAGEMENT

Implement an Order Manager.

It must handle:

* submit order
* order status
* partial fills
* rejected orders
* canceled orders
* retries
* duplicate order prevention
* network errors
* broker errors
* stale orders

Every order needs:

client_order_id

Use idempotency.

The system must never accidentally submit duplicate orders because of a retry or application restart.

---

# 17. POSITION MANAGEMENT

Maintain a local position state.

For each position:

symbol
quantity
average_entry_price
current_price
market_value
unrealized_pnl
realized_pnl
stop_price
take_profit_price
entry_time
strategy
AI_score
risk_amount

Synchronize periodically with the broker.

If local state and broker state disagree:

STOP NEW TRADES

and raise an alert.

---

# 18. DATABASE

Use PostgreSQL.

Create tables such as:

users
settings
symbols
market_data
trading_signals
ai_decisions
orders
fills
positions
closed_positions
daily_pnl
risk_events
system_events
strategy_performance

Store every important decision.

Example AI decision record:

timestamp
symbol
strategy
market_price
action
confidence
score
signals
reasoning
stop_loss
take_profit
risk_amount
decision_result

---

# 19. DAILY P&L CALENDAR

Create a major dashboard component:

## Trading Calendar

Example:

September 2026

Mon Tue Wed Thu Fri
1   2   3   4   5
+$82 -$31 +$124 +$45 ...

Use:

GREEN = profitable day

RED = losing day

GRAY = no trading

For each day show:

Realized P&L
Number of trades
Winning trades
Losing trades
Win rate
Largest winner
Largest loser
Total volume

When clicking a day:

show every trade from that date.

Example:

September 21

Daily P&L: +$184.25

Trades: 7

AAPL
BUY 10:02 AM
SELL 10:31 AM
+$72.50

NVDA
BUY 11:14 AM
SELL 11:47 AM
+$91.20

MSFT
BUY 1:22 PM
SELL 1:39 PM
-$28.45

etc.

---

# 20. DASHBOARD

Create a modern responsive web dashboard.

Main dashboard:

Account Value
Cash
Buying Power
Today's P&L
Total P&L
Open Positions
Daily Loss Limit
Daily Loss Used
Number of Trades
Win Rate
System Status

System status:

RUNNING
PAUSED
DAILY LOSS LIMIT
MARKET CLOSED
DATA ERROR
BROKER ERROR
EMERGENCY STOP

---

# 21. LIVE POSITIONS

Show:

Symbol
Price
Quantity
Entry
Current
Unrealized P&L
P&L %
Stop
Target
AI Confidence
Strategy
Holding Time

Allow:

Close Position

from the UI.

But the user must not be able to disable risk controls accidentally.

---

# 22. TRADE HISTORY

Provide filtering:

Date
Symbol
Strategy
Profit/Loss
Winning/Losing
AI confidence
Entry time
Exit time

Allow CSV export.

---

# 23. AI DECISION EXPLANATION

For every trade display:

WHY DID THE AI ENTER?

Example:

AAPL entered because:

* Price reclaimed VWAP
* Relative volume = 2.3x
* EMA 9 crossed above EMA 20
* SPY trend positive
* Breakout above previous 15-minute high
* Risk/reward = 2.1
* AI confidence = 82%

WHY DID THE AI EXIT?

Example:

* Take-profit reached

or:

* VWAP breakdown
* Momentum reversal
* Stop-loss triggered

Do not generate explanations that contradict the actual numerical signals.

The explanation should be generated from the recorded decision data.

---

# 24. BACKTESTING

Build a backtesting module before relying on paper trading.

Allow:

Symbol universe
Date range
Starting capital
Strategies
Risk parameters
Transaction costs
Slippage assumptions

Calculate:

Total return
Annualized return
Maximum drawdown
Sharpe ratio
Sortino ratio
Win rate
Profit factor
Average win
Average loss
Largest loss
Largest winning streak
Largest losing streak
Number of trades
Average holding time

IMPORTANT:

Do not optimize parameters on the entire historical dataset.

Use:

TRAINING PERIOD
VALIDATION PERIOD
OUT-OF-SAMPLE TEST PERIOD

Avoid look-ahead bias.

Avoid survivorship bias where practical.

Include realistic transaction costs and slippage assumptions.

---

# 25. PAPER-TRADING VALIDATION

After backtesting:

Phase 1:
Historical backtest

Phase 2:
Paper trading

Phase 3:
Paper trading for an extended period

Phase 4:
Review performance and risk

Do NOT automatically transition to live trading.

Live trading must require a deliberate manual architecture/configuration change and a separate safety review.

---

# 26. MONITORING

Implement:

structured logging

metrics

health checks

broker connectivity monitoring

market data connectivity monitoring

database health

order latency

order rejection rate

data freshness

AI response latency

system CPU/memory

Alerts for:

daily loss limit

broker disconnected

market data stale

order rejected

position mismatch

unexpected position

failed liquidation

application crash

AI service unavailable

---

# 27. FAIL-SAFE BEHAVIOR

If market data becomes stale:

STOP NEW TRADES

If broker API fails:

STOP NEW TRADES

If database fails:

STOP NEW TRADES

If AI service fails:

STOP NEW TRADES

If account state cannot be reconciled:

STOP NEW TRADES

If risk engine fails:

STOP NEW TRADES

If application restarts:

reconcile positions/orders with broker before allowing new trades.

Never assume there are no positions after restart.

---

# 28. SECURITY

Implement:

environment variables/secrets

encrypted credentials

JWT/session authentication

role-based access

CSRF protection where appropriate

API rate limiting

audit logs

secure password handling

No secrets in source code.

No API keys in Git.

No broker credentials in frontend code.

The browser must never receive broker API secrets.

---

# 29. TECHNOLOGY STACK

Use a maintainable production architecture.

Recommended:

Frontend:
React + TypeScript

Backend:
Python + FastAPI

Trading/AI:
Python

Database:
PostgreSQL

Cache:
Redis

Background jobs:
Celery or equivalent

WebSocket:
FastAPI WebSockets

Containerization:
Docker

Deployment:
AWS

Possible AWS architecture:

CloudFront
|
Application Load Balancer
|
FastAPI
|
-

|             |               |
PostgreSQL   Redis        Trading Engine
|
Alpaca Paper API

Use Docker Compose for local development.

Create Dockerfiles for every service.

Create:

docker-compose.yml

.env.example

README.md

database migrations

unit tests

integration tests

end-to-end tests

---

# 30. PROJECT STRUCTURE

Create a clean repository:

/frontend
/backend
/trading-engine
/ai-engine
/risk-engine
/market-data
/backtesting
/database
/infrastructure
/tests
/docs

Example:

/backend
/api
/services
/models
/repositories
/config

/trading-engine
broker.py
order_manager.py
position_manager.py
execution.py

/risk-engine
risk_manager.py
position_sizer.py
limits.py

/ai-engine
signal_generator.py
feature_engineering.py
model.py
decision.py

/market-data
market_feed.py
indicators.py
universe.py

---

# 31. AI MODEL DESIGN

Make the AI provider pluggable.

Create:

AIProvider

implementations can include:

OpenAIProvider
AnthropicProvider
LocalModelProvider

The application must continue operating safely if an AI provider is unavailable.

Do not send unnecessary sensitive account information to an external AI provider.

The AI receives structured market information, not brokerage credentials.

---

# 32. IMPORTANT AI PROMPT

Create a strict internal AI decision prompt similar to:

"You are a market-analysis component.

Analyze the supplied structured market data.

You must return valid JSON only.

You must not invent market data.

You must not invent news.

You must not claim certainty.

You must return NO_TRADE when evidence is insufficient.

You must identify:

* direction
* confidence
* supporting signals
* contradictory signals
* estimated risk
* potential reward
* stop loss
* take profit
* suggested holding period

The output is only a trading proposal.

A separate deterministic risk engine has final authority and may reject your proposal."

---

# 33. MARKET REGIME

Implement a market regime detector.

Possible regimes:

BULLISH
BEARISH
SIDEWAYS
HIGH_VOLATILITY
LOW_VOLATILITY
UNKNOWN

Use market-level indicators such as:

SPY
QQQ
VIX or equivalent
market breadth
intraday trend

If regime is UNKNOWN:

reduce or disable trading according to configuration.

---

# 34. TRADE QUALITY FILTER

Before executing any trade calculate:

Expected reward
Expected risk
Risk/reward ratio
Spread
Liquidity
Volatility
Position size
Portfolio correlation
Current daily loss
Number of open positions
Existing exposure to sector
Market regime

Reject poor-quality trades.

---

# 35. CORRELATION CONTROL

Avoid opening multiple highly correlated positions.

Example:

If already holding:

NVDA
AMD
AVGO

and all are strongly correlated with the same semiconductor movement,

the system should consider the aggregate exposure rather than treating them as independent trades.

Create:

MAX_SECTOR_EXPOSURE

MAX_CORRELATED_EXPOSURE

---

# 36. TRADING HOURS

Use U.S. Eastern Time.

Default:

Start:
9:35 AM ET

Stop entering new positions:
3:30 PM ET

Close positions:
3:55 PM ET

No overnight positions.

Make all times configurable.

---

# 37. PAPER MODE SAFETY

Display a highly visible banner:

PAPER TRADING — NO REAL MONEY

Every page should clearly show:

TRADING MODE: PAPER

The backend should also enforce paper mode.

Do not rely only on the UI.

---

# 38. TESTING

Write tests for:

Position sizing

Risk calculations

Daily loss calculation

Stop-loss calculation

Take-profit calculation

Maximum exposure

Maximum trades

Duplicate orders

Order retry

Partial fills

Broker failures

Market-data failures

AI malformed response

AI unavailable

Database unavailable

Application restart

Position reconciliation

End-of-day liquidation

Daily loss lockout

---

# 39. SIMULATION TEST

Create a simulation mode where market data is replayed.

Example:

Replay one trading day at accelerated speed.

The system should behave as if it were receiving live market data.

This will allow us to test the complete pipeline before connecting to paper trading.

---

# 40. UI PAGES

Create these pages:

1. Dashboard
2. Trading Calendar
3. Open Positions
4. Trade History
5. Trade Details
6. AI Decisions
7. Strategies
8. Risk Settings
9. Market Scanner
10. Backtesting
11. Performance
12. System Health
13. Logs
14. Settings

---

# 41. USER CONTROLS

Provide:

START TRADING

PAUSE TRADING

EMERGENCY STOP

CLOSE ALL POSITIONS

RESET DAILY LOCK

These actions must be authenticated.

EMERGENCY STOP must:

* prevent new trades
* cancel open entry orders
* optionally close positions according to configuration

---

# 42. PERFORMANCE ANALYTICS

Show:

Daily P&L

Weekly P&L

Monthly P&L

Cumulative P&L

Win rate

Profit factor

Average trade

Average winning trade

Average losing trade

Maximum drawdown

Sharpe ratio

Number of trades

Average holding time

Strategy performance

Symbol performance

Time-of-day performance

AI-confidence vs outcome

---

# 43. CALENDAR REQUIREMENT

This is particularly important.

The calendar must allow:

September 2026

Click September 21.

Show:

Daily P&L: +$123.45

Trades: 8

Winning: 5

Losing: 3

Then list:

AAPL +$42.10
NVDA +$51.22
MSFT -$12.45
AMD +$31.50
...

Click AAPL.

Show:

Entry:
10:14:32 AM

Exit:
10:42:11 AM

Entry price:
$xxx

Exit price:
$xxx

Quantity:
xx

Gross P&L:
$xx

Fees:
$xx

Net P&L:
$xx

Stop:
$xx

Target:
$xx

Strategy:
VWAP Reclaim

AI confidence:
xx%

Decision explanation:
...

---

# 44. AUDITABILITY

Every automated action must be explainable from stored data.

For every trade store:

What the market data showed.

What the quantitative strategy calculated.

What the AI recommended.

What the risk engine allowed/rejected.

What order was submitted.

What the broker returned.

What fill occurred.

Why the position exited.

This is mandatory.

---

# 45. DEVELOPMENT PROCESS

Do NOT generate the entire project blindly in one step.

Build incrementally.

PHASE 1:
Architecture + repository + database + configuration

PHASE 2:
Market-data service

PHASE 3:
Stock scanner

PHASE 4:
Technical indicators

PHASE 5:
Backtesting engine

PHASE 6:
Risk engine

PHASE 7:
Paper broker integration

PHASE 8:
Order/position management

PHASE 9:
AI decision engine

PHASE 10:
Dashboard

PHASE 11:
Calendar/P&L

PHASE 12:
Monitoring/alerts

PHASE 13:
Docker

PHASE 14:
AWS deployment

PHASE 15:
End-to-end testing

After each phase:

1. Build the code.
2. Run tests.
3. Fix errors.
4. Explain what was implemented.
5. Show the files changed.
6. Provide commands to run it.
7. Do not proceed to the next phase until the current phase works.

---

# 46. DEVELOPMENT RULE

Do not give me pseudocode when actual implementation is requested.

Create actual working code.

Do not leave TODO placeholders for core functionality.

Do not say:

"implement this later"

for core trading functionality.

If an external API requires credentials, create the integration and provide an .env.example file with placeholders.

---

# 47. README

Create a comprehensive README containing:

Architecture

Prerequisites

Installation

Environment variables

Alpaca paper account setup

Database setup

Docker setup

Running locally

Running tests

Running backtests

Starting paper trading

Stopping trading

Emergency stop

Configuration

AWS deployment

Troubleshooting

Security

Risk limitations

---

# 48. IMPORTANT FINANCIAL REQUIREMENT

Never claim:

"minimal losses guaranteed"

"profits guaranteed"

"AI will make money"

"this strategy cannot lose"

Instead the application should explicitly state:

"Trading involves substantial risk. This system does not guarantee profits or prevent losses. Paper-trading results may differ materially from live trading."

---

# 49. FUTURE LIVE TRADING

Do NOT implement live trading in Version 1.

However, design the architecture so that eventually:

Paper Broker
and
Live Broker

can implement the same interface.

Before any future live implementation, require:

LIVE_TRADING_ENABLED=true

AND

explicit confirmation

AND

separate live API credentials

AND

separate risk limits

AND

additional safety checks.

Never automatically switch from paper to live.

---

# 50. FIRST TASK

Start by creating:

1. Complete architecture diagram.
2. Technology decisions.
3. Repository structure.
4. Database schema.
5. API specification.
6. Configuration model.
7. Risk-engine design.
8. Trading-engine design.
9. AI decision-engine design.
10. UI wireframe.
11. Docker architecture.
12. Development roadmap.

Then implement PHASE 1.

Do not implement live trading.

At every stage prioritize:

CAPITAL PRESERVATION
SAFETY
AUDITABILITY
DETERMINISTIC RISK CONTROLS
RELIABILITY
TESTABILITY
SECURITY

The system should be designed as a serious software engineering project, not as a simple trading script.
