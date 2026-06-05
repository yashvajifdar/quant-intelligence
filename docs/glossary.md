# Quant Intelligence — Finance Glossary

**Audience:** Software engineers with no prior finance background.
**Purpose:** Define every domain term used in this codebase so you can read signal logic, engine output, and risk rules without stopping to search.

Each entry answers: what it is, why it matters for this platform, and any formula you will see in the code.

---

## Section 1: Factor Signals

### Factor Investing

A strategy that selects or weights stocks based on measurable characteristics — called factors — that have historically produced above-market returns. Common factors include momentum, value, quality, and low volatility. This platform computes a score for each factor independently, then combines them into a composite signal that ranks every stock in the S&P 500 daily.

### Momentum Factor

The observation that stocks that have outperformed over the past 12 months tend to keep outperforming over the next 1-3 months. The standard construction uses **12-1 month momentum**: look back 12 months, but exclude the most recent month.

```
momentum_score = (price_today / price_12_months_ago) - 1
               excluding the last 30 days of that window
```

The last month is excluded because short-term price moves reverse — stocks that spiked last week tend to mean-revert next week (a separate phenomenon called short-term reversal). Including it would add noise that cancels the signal.

**Cross-sectional ranking** means you rank all 500 stocks against each other on a given day rather than comparing a single stock to its own history. A score in the 90th percentile is "strong momentum" because 90% of the universe scored lower that day. This normalization makes the factor comparable across different market regimes.

### Value Factor

Value investing buys stocks that trade cheaply relative to their fundamental worth, betting that the market has mispriced them temporarily. This platform computes four value metrics and combines them into a composite value score.

- **P/E (Price-to-Earnings):** `market_cap / net_income`. Lower means cheaper per dollar of profit. A P/E of 10 means you pay $10 for every $1 of annual earnings.
- **P/B (Price-to-Book):** `market_cap / book_value_of_equity`. Book value is assets minus liabilities. A P/B below 1 means the stock trades for less than its accounting net worth.
- **P/S (Price-to-Sales):** `market_cap / annual_revenue`. Useful for companies with no earnings yet; lower is cheaper per dollar of revenue.
- **EV/EBITDA (Enterprise Value / Earnings Before Interest, Taxes, Depreciation, Amortization):** `(market_cap + debt - cash) / EBITDA`. EV/EBITDA is preferred over P/E for cross-company comparison because it strips out differences in debt levels and accounting choices. A ratio of 8x is cheap; 25x is expensive.

### Quality Factor

Quality selects stocks with durable businesses — high profitability, low debt, and strong cash generation. Four metrics drive the composite:

- **ROE (Return on Equity):** `net_income / shareholders_equity`. Measures how efficiently a company converts equity capital into profit. ROE above 15% is considered high quality.
- **Gross Margin:** `(revenue - cost_of_goods_sold) / revenue`. Higher margin means more pricing power and competitive moat. A software company at 70% gross margin has far more durable profits than a retailer at 25%.
- **Debt/Equity:** `total_debt / shareholders_equity`. Lower is better. A ratio above 2.0 signals the business depends heavily on borrowed money and is vulnerable to interest rate increases.
- **Free Cash Flow:** `operating_cash_flow - capital_expenditures`. The cash a business actually generates after paying to maintain/grow itself. Earnings can be manipulated; free cash flow is harder to fake.

### Low Volatility Factor / Low Volatility Anomaly

The counterintuitive finding that low-volatility stocks deliver better risk-adjusted returns than high-volatility stocks over long periods. Finance theory predicts the opposite — more risk should mean more return. The anomaly likely persists because institutional managers are incentivized to chase high-volatility winners, leaving low-vol stocks underowned.

In this platform, low volatility is measured as the 252-day (one trading year) standard deviation of daily returns, then inverted so that a high score means low volatility:

```
low_vol_score = 1 / std_dev(daily_returns, window=252)
```

### Alpha

Return above what would be explained by the stock's exposure to the market. If the S&P 500 returns 10% and a stock returns 15% with the same market sensitivity, the extra 5% is alpha. This platform's AI engine is attempting to generate alpha — returns attributable to the signal quality, not just riding a bull market.

### Alpha Decay

The rate at which a signal loses its predictive power over time. A momentum signal computed today will be weaker as a predictor at 90 days than at 5 days because other market participants find and trade the same patterns, eliminating the edge. This platform refreshes signals daily to capture the period before alpha decays materially.

### Cross-Sectional Ranking

Ranking stocks against each other on a single date rather than comparing each stock to its own history. For example, if AAPL has a momentum score of 0.18 and MSFT has 0.22, a cross-sectional rank puts MSFT in a higher percentile on that date. Rankings are then normalized to a 0-1 score. This makes composite factor scores directly comparable regardless of the raw metric's scale.

---

## Section 2: Technical Indicators

### Moving Average (Simple vs Exponential)

A moving average smooths price data by averaging prices over a trailing window, removing short-term noise to reveal trend direction.

- **Simple Moving Average (SMA):** equal weight to every day in the window.
  ```
  SMA_50 = average(close_price, last_50_days)
  ```
- **Exponential Moving Average (EMA):** more weight to recent prices, reacting faster to new information.
  ```
  EMA_today = price_today * k + EMA_yesterday * (1 - k)
  where k = 2 / (window + 1)
  ```

**50-day SMA** tracks intermediate trend (roughly one quarter). **200-day SMA** tracks long-term trend (roughly one year). Both appear throughout this platform's regime detection and signal filtering logic.

### Golden Cross and Death Cross

A **golden cross** occurs when the 50-day SMA crosses above the 200-day SMA. It signals that intermediate-term momentum has turned positive relative to the long-term trend and is widely used as a bull signal. A **death cross** is the inverse — the 50-day falls below the 200-day — signaling deteriorating momentum and used as a bear signal. This platform uses these crossovers as macro regime filters: golden cross unlocks long recommendations, death cross suppresses them.

### RSI — Relative Strength Index

RSI measures whether a stock has moved too far, too fast — in either direction. It produces a value between 0 and 100.

```
RS  = avg_gain_over_14_periods / avg_loss_over_14_periods
RSI = 100 - (100 / (1 + RS))
```

The standard window is 14 trading days. **RSI below 30** is oversold — the stock has fallen sharply and may bounce. **RSI above 70** is overbought — the stock has risen sharply and may pull back. This platform uses RSI as an entry filter: long entries require RSI between 50 and 70 (uptrend with room to run, not yet extended).

### MACD — Moving Average Convergence Divergence

MACD measures trend momentum by tracking the gap between two EMAs of price. The standard parameters are **(12, 26, 9)**:

```
MACD_line    = EMA(close, 12) - EMA(close, 26)
Signal_line  = EMA(MACD_line, 9)
Histogram    = MACD_line - Signal_line
```

When the MACD line crosses above the signal line, short-term momentum is accelerating — a bullish crossover. When it crosses below, momentum is decelerating — bearish. The histogram visualizes the distance between the two lines. This platform uses MACD crossovers as an entry timing filter within positions the factor model has already ranked highly.

### ATR — Average True Range

ATR measures volatility as the average size of a stock's daily price swings over the past 14 periods. "True range" extends the simple high-minus-low by accounting for overnight gaps:

```
True Range = max(high - low,
                 abs(high - prev_close),
                 abs(low  - prev_close))
ATR_14 = average(True_Range, last_14_days)
```

ATR does not indicate direction — only magnitude. This platform uses ATR for two purposes: setting stop-loss levels (exit if price moves 1.25x ATR against you) and position sizing (allocate less capital to high-ATR stocks to keep dollar risk constant).

### Volume Confirmation

Price moves on high volume carry more conviction than moves on thin volume. A breakout above resistance on 150% or more of the 20-day average volume signals that institutional buyers (funds, not retail) are involved and the move is more likely to sustain. This platform flags volume confirmation as a required condition for breakout entries — a breakout on normal volume is treated as unconfirmed.

### Support and Resistance

**Support** is a price level where a stock has historically stopped falling and reversed upward — buyers consistently step in at that price. **Resistance** is a level where the stock has historically stopped rising and reversed downward — sellers consistently appear. These levels are not precise lines; they are zones. This platform identifies support and resistance by finding recent pivot lows and highs in the prior 52-week price series.

### Breakout

A breakout occurs when price moves decisively above a resistance level (bullish breakout) or below a support level (bearish breakdown). "Decisively" in this platform means: closing price more than 1% above resistance, on volume exceeding 150% of 20-day average volume. Breakouts signal that the supply/demand balance has shifted and the prior range is invalidated.

---

## Section 3: Macro and Market Regime

### Macro Regime

The broad economic and monetary environment that determines whether risk assets (stocks) are likely to appreciate or depreciate. This platform classifies the regime daily based on yield curve shape, VIX level, and S&P 500 trend. The regime gates what the AI engine is allowed to recommend — aggressive longs are suppressed in a risk-off, inverted-curve, high-VIX environment.

### Yield Curve

The yield curve plots interest rates for US Treasury bonds at different maturities (2-year, 5-year, 10-year, 30-year). The shape encodes the market's view of future growth and inflation.

- **Normal (upward-sloping):** long-term rates are higher than short-term. Signals expected growth.
- **Flat:** rates are similar at all maturities. Signals economic uncertainty.
- **Inverted:** short-term rates exceed long-term rates. Historically the most reliable recession predictor — every US recession in the past 50 years was preceded by inversion.

This platform tracks the **10Y-2Y spread** (10-year Treasury yield minus 2-year Treasury yield), sourced from FRED series `T10Y2Y`. A spread below 0 is an inverted curve; below -0.5 triggers a defensive regime flag.

### Fed Funds Rate

The interest rate at which US banks lend to each other overnight, set by the Federal Reserve. It is the baseline borrowing cost for the entire economy. When the Fed raises rates: borrowing becomes more expensive, corporate earnings expectations fall, and future cash flows are discounted more heavily — depressing stock prices. When the Fed cuts rates, the opposite occurs. This platform ingests the current rate from FRED series `FEDFUNDS` and uses the direction of rate changes as a regime modifier.

### CPI and Inflation Regime

CPI (Consumer Price Index) measures the average change in prices consumers pay for goods and services. High inflation (above 4% annualized) is negative for equity valuations because it forces the Fed to raise rates, compresses margins for businesses that cannot pass costs on, and erodes consumer purchasing power. This platform reads `CPIAUCSL` from FRED and classifies the inflation regime as low (<2%), moderate (2-4%), or high (>4%).

### VIX — CBOE Volatility Index

VIX measures the market's expected volatility for the S&P 500 over the next 30 days, derived from options prices. It is often called the "fear gauge." VIX rises when investors buy protective puts to hedge against a crash; it falls when markets are calm.

Key thresholds used in this platform:
- **VIX < 20:** calm market, risk-on conditions, normal position sizing
- **VIX 20-25:** elevated caution, reduce new entries
- **VIX > 25:** risk-off flag, reduce position sizes by 25%
- **VIX > 35:** crisis conditions, defensive posture only

VIX above 35 has historically corresponded to acute crises: the 2008 financial crisis, the March 2020 COVID crash.

### Risk-On vs Risk-Off

**Risk-on** describes a market environment where investors are comfortable taking risk — they buy equities, high-yield bonds, and growth assets. **Risk-off** describes the opposite: investors flee to safe-haven assets like US Treasuries, gold, and the US dollar. This platform computes a risk-on/risk-off composite from VIX level, yield curve shape, and the S&P 500's position relative to its 200-day SMA. The regime directly controls which recommendations the AI engine surfaces.

### Bull Market vs Bear Market

A **bull market** is a sustained rise of 20% or more from a recent low. A **bear market** is a sustained decline of 20% or more from a recent high. These are backward-looking labels — you do not know you are in one until after the move. This platform uses the 200-day SMA position as a forward-looking proxy: price above its 200-day SMA is in a structural uptrend; price below it is in a structural downtrend.

### 200-Day Moving Average as Regime Indicator

The 200-day SMA is the single most widely watched trend indicator in institutional markets. When the S&P 500 closes above its 200-day SMA, the majority of trend-following and systematic strategies are in buy mode. When it breaks below, they shift to sell or hedge. Because so many strategies use this level, it becomes self-fulfilling. This platform uses the S&P 500's position relative to its 200-day SMA as the primary regime gate for all long recommendations.

---

## Section 4: Options Concepts

### Options (Calls and Puts)

An option is a contract giving the buyer the right, but not the obligation, to buy or sell a stock at a set price before a set date. The seller of the option takes the obligation in exchange for a fee (the premium).

- **Call option:** the right to buy the stock. Profitable when the stock rises above the strike price.
- **Put option:** the right to sell the stock. Profitable when the stock falls below the strike price.

Options are frequently used by institutional players to express directional views with defined risk, making unusual options activity a proxy for informed money flow.

### Strike Price, Expiration, Premium

- **Strike price:** the price at which the option buyer can buy (call) or sell (put) the stock. An option with a strike price of $150 on a $140 stock is "out of the money."
- **Expiration:** the date on which the option contract expires. After expiration, the option is worthless if unexercised.
- **Premium:** the price paid by the buyer to the seller for the option contract. Quoted per share; standard contracts cover 100 shares. A $2.50 premium costs $250 per contract.

### Options Flow

Options flow refers to the aggregate stream of options transactions in real time — who is buying, what strikes, what expiration, at what size. Analyzing flow reveals whether the predominant activity is bullish (call buying), bearish (put buying), or hedging (both). This platform ingests options flow data from Unusual Whales to use as an independent confirmation signal.

### Unusual Options Activity

Options activity is flagged as unusual when a single trade or the day's aggregate volume significantly exceeds the stock's historical average. A stock that normally trades 500 options contracts per day suddenly seeing 10,000 contracts — especially in calls at out-of-the-money strikes — suggests a participant with non-public or high-conviction information is positioning for a large move. This platform treats unusual options activity as a +1 signal modifier for the AI engine.

### Sweep (Options Sweep)

A sweep is a large options order that is broken up and routed across multiple exchanges simultaneously to fill as quickly as possible, prioritizing speed over price. Sweeps indicate urgency — the buyer does not want to wait. A bullish sweep on a call option (above-ask price, multiple exchanges, large notional value) is interpreted as a strong directional bet by an institutional participant. This platform specifically filters for aggressive sweeps as the highest-conviction options signal.

### Block Trade (Options Block)

A block trade is a single large options transaction executed off-exchange or negotiated between two parties. The threshold for a block trade is typically 10,000+ contracts or $1M+ in notional premium. Blocks can represent institutional hedging or directional positioning. Unlike sweeps (which signal urgency), blocks signal size — a fund is making a large, deliberate position. This platform distinguishes sweeps from blocks and weights them differently in the signal model.

### Open Interest vs Volume

- **Volume:** the number of options contracts traded on a given day. Resets to zero each day.
- **Open interest:** the total number of active (open) contracts across all participants. Increases when new contracts are created; decreases when contracts are closed or expire.

A spike in volume with low open interest means traders are flipping existing contracts. A spike in volume with rising open interest means new directional positions are being opened — more significant. This platform tracks both, using open interest change to distinguish new positioning from noise.

### Put/Call Ratio

```
put_call_ratio = total_put_volume / total_call_volume
```

A ratio above 1.0 means more puts than calls are trading — bearish sentiment. Below 1.0 means more calls — bullish sentiment. The market-wide put/call ratio is also used as a contrarian indicator: extreme put buying (ratio > 1.5) often marks a short-term bottom because the fear has peaked. This platform computes per-ticker put/call ratios daily and uses them as sentiment inputs to the engine.

### Implied Volatility (IV) and IV Expansion Around Earnings

Implied volatility is the market's expectation of future price movement, extracted from current options premiums. Higher IV means the market expects bigger swings; lower IV means calmer expected movement.

```
option_price ≈ function of(IV, time_to_expiration, distance_from_strike)
```

IV typically expands in the days before an earnings announcement (options buyers pay more to position for the unknown outcome) and collapses sharply the day after earnings are released regardless of the result — this collapse is called **IV crush**. This platform flags stocks within 7 days of earnings as elevated-risk entries because IV pricing makes directional options bets expensive.

### Dark Pool

Dark pools are private, off-exchange trading venues where large institutional orders are executed without revealing the trade to the public market until after completion. They exist to prevent large orders from moving the market against the buyer/seller before the order fills. About 35-40% of all US equity volume trades in dark pools.

### Dark Pool Print

A dark pool print is the reported transaction after a large dark pool trade completes. The print shows price, size, and ticker — but not the direction (buy vs sell), which must be inferred from whether the transaction occurred closer to the bid or ask. Large dark pool prints above 100,000 shares are watched for accumulation patterns. This platform infers directional bias from repeat prints at consistent price levels.

---

## Section 5: Risk Management

### Position Sizing

The process of determining how many shares (or dollars) to allocate to a trade. Sizing is a function of account risk tolerance, the stock's volatility, and the distance to the stop loss — not of conviction level. This platform uses ATR-based sizing to keep every position's maximum dollar loss constant regardless of how volatile the underlying stock is.

### ATR-Based Stop Loss

The stop loss is placed at 1.25 times ATR below the entry price for long positions:

```
stop_price = entry_price - (1.25 * ATR_14)
```

The 1.25 multiplier provides a buffer beyond normal daily volatility so the position is not stopped out by routine noise — only by a genuine adverse move. ATR automatically adjusts the stop wider for volatile stocks and tighter for stable ones, making the rule self-adapting across the universe.

### Account Risk Per Trade

The standard rule is to risk no more than **1-2% of account value** on any single trade. If the account is $100,000 and the rule is 1%, the maximum loss on a single trade is $1,000. Combined with the ATR stop distance, this directly determines position size:

```
shares = max_dollar_risk / (1.25 * ATR_14)
       = ($100,000 * 0.01) / (1.25 * ATR_14)
```

This rule prevents any single bad trade from materially damaging the portfolio.

### Reward/Risk Ratio

The ratio of the profit target to the stop loss distance:

```
reward_risk = (target_price - entry_price) / (entry_price - stop_price)
```

A minimum of **2:1** is the standard. If you risk $1 per share, you need to target at least $2 gain per share. At a 50% win rate, a 2:1 reward/risk ratio produces positive expected value. Below 2:1, you need a win rate above 67% to break even — difficult to sustain. This platform rejects recommendations that do not achieve a 2:1 reward/risk ratio at the identified technical target.

### Trailing Stop

A stop loss that adjusts upward as the position gains. If a stock is entered at $100 with a stop at $97, and it rises to $110, the trailing stop might move to $107 — locking in $7 of profit while allowing further upside. This platform implements trailing stops that reset to 1.25x ATR below the highest closing price since entry.

### Max Drawdown

The largest peak-to-trough decline in portfolio value over a measured period:

```
max_drawdown = (trough_value - peak_value) / peak_value
```

A max drawdown of -15% means the portfolio fell 15% from its highest point before recovering. Max drawdown is the primary measure of downside risk for a portfolio — a high Sharpe ratio with a 40% drawdown is not acceptable because most investors would exit the strategy during the drawdown. Target max drawdown for this platform is below -20%.

### Portfolio Concentration

The degree to which a portfolio's returns are driven by a small number of positions. A portfolio with 5 equally-sized positions has very high concentration — one bad trade moves the portfolio 20%. This platform caps individual position sizes at 10% of portfolio value and requires a minimum of 10 active positions when the model is fully invested.

### Correlation Between Positions

Two positions are correlated when they tend to move together. A portfolio of 10 tech stocks does not provide meaningful diversification — when tech sells off, all 10 positions fall simultaneously. This platform computes the pairwise 60-day return correlation for all open positions. **Pairs with r > 0.7** are treated as a single position for sizing purposes — adding a second highly correlated stock doubles the sector bet, not the diversification.

### Value Trap

A stock that appears cheap on value metrics (low P/E, low P/B) but is cheap for a legitimate reason — the business is in permanent decline, the earnings are about to fall, or the accounting is misleading. A company trading at 5x earnings that earns nothing next year is not cheap. This platform defends against value traps by requiring quality factor scores above the 40th percentile before acting on value signals — a cheap, low-quality business is a trap.

### Momentum Crash

Momentum strategies periodically suffer rapid, severe reversals called momentum crashes. They occur during market recoveries after sharp declines: the prior losers (which the momentum model is short or underweight) recover violently while the prior winners (which the model owns) underperform. March 2020 and April 2009 are canonical examples. This platform mitigates momentum crash risk by capping momentum factor weight in high-VIX regimes and blending in quality and low-vol factors.

---

## Section 6: Portfolio Performance Metrics

### Total Return

The percentage change in portfolio value from start to end of a period, including dividends:

```
total_return = (end_value - start_value + dividends) / start_value
```

The simplest measure of performance. Does not account for how long the period was or the risk taken.

### Annualized Return

Total return scaled to an annual rate, enabling comparison across periods of different lengths:

```
annualized_return = (1 + total_return) ^ (365 / days_held) - 1
```

A strategy up 20% over 3 years has an annualized return of 6.3%, not 20%.

### Sharpe Ratio

The return generated per unit of total risk taken. The most widely used measure of risk-adjusted performance:

```
sharpe_ratio = (portfolio_return - risk_free_rate) / std_dev(portfolio_returns)
```

The risk-free rate is typically the 3-month Treasury yield. Both return and std deviation are annualized.

- **Sharpe > 1.0:** good. More than one unit of return per unit of risk.
- **Sharpe > 2.0:** excellent. Very few strategies sustain this over long periods.
- **Sharpe < 0.5:** the strategy barely compensates for its risk.

This platform targets a Sharpe above 1.2.

### Sortino Ratio

A variant of the Sharpe ratio that penalizes only downside volatility — upside volatility is not risk, it is the goal:

```
sortino_ratio = (portfolio_return - risk_free_rate) / std_dev(negative_returns_only)
```

Sortino is a better measure for strategies with skewed return distributions (more small gains, occasional large losses). This platform reports both; Sortino will always be higher than Sharpe for strategies with positive skew.

### Max Drawdown

See Section 5. Max drawdown appears in both risk management (live) and performance reporting (historical). In performance reports, it is the largest realized peak-to-trough decline over the entire backtest or live period.

### Win Rate

The percentage of closed trades that were profitable:

```
win_rate = profitable_trades / total_closed_trades
```

Win rate alone is meaningless without the average win/loss ratio. A 40% win rate with 3:1 average wins is superior to a 70% win rate with 0.5:1 average wins.

### Average Win/Loss Ratio

```
avg_win_loss = average_profit_on_winners / average_loss_on_losers
```

Combined with win rate, this determines expected value per trade:

```
expected_value = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
```

This platform targets a win rate above 45% with an average win/loss ratio above 2.0, producing positive expected value.

### Paper Trading / Paper Portfolio

Paper trading means simulating trades with hypothetical capital — recording entries, exits, and P&L without executing real orders. It is used to validate a strategy before risking actual money and to track live performance without brokerage integration. This platform is a paper portfolio — all positions and returns are simulated. Lookahead bias rules (below) are enforced strictly to make paper results meaningful.

### Lookahead Bias

Lookahead bias occurs when a model uses future information that would not have been available at the time of the decision. The most common form: using the day's closing price to both generate the signal and record the entry — in reality you would not know the close until after trading hours. This platform prevents lookahead bias by generating signals using the prior day's close and logging paper entries at the current day's close after the signal was generated.

---

## Section 7: Data Sources

### yfinance

A Python library that wraps Yahoo Finance's unofficial API to download historical price and fundamental data. Free with no API key required. This platform uses yfinance to ingest daily OHLCV data for all S&P 500 constituents and trailing twelve-month fundamental data (P/E, P/B, revenue, earnings) for the value and quality factor computation.

Data latency: approximately 15-30 minutes after market close on trading days. yfinance data has occasional gaps and adjustments — the ETL layer validates completeness before writing to the data store.

### OHLCV

The standard representation of a day's price data for a security:

- **Open:** price of the first trade of the day
- **High:** highest price traded during the day
- **Low:** lowest price traded during the day
- **Close:** price of the last trade before market close (4:00 PM ET for US equities)
- **Volume:** total number of shares traded during the day

Close price is the authoritative price for all signal computation. yfinance also provides **Adjusted Close**, which back-adjusts historical prices to account for splits and dividends — this platform uses Adjusted Close for return calculations.

### FRED — Federal Reserve Economic Data

A free, public API from the Federal Reserve Bank of St. Louis that provides over 800,000 economic time series. No account required for basic access. This platform reads three series:

- **T10Y2Y:** 10-year minus 2-year Treasury yield spread (daily). Regime indicator.
- **FEDFUNDS:** effective federal funds rate (monthly). Rate environment indicator.
- **CPIAUCSL:** seasonally adjusted Consumer Price Index, all urban consumers (monthly). Inflation regime indicator.

FRED data is accessed via the `fredapi` Python library. Series are ingested daily; FEDFUNDS and CPI update monthly so the ETL handles unchanged values correctly.

### Unusual Whales

A financial data provider specializing in options flow and Congressional trading disclosures. The free tier provides delayed (15-minute) options flow data, high-level unusual activity alerts, and basic flow statistics. This platform uses the free tier for options flow signals. The paid tier provides real-time sweeps, block trade data, and dark pool prints — relevant if the signal model is extended.

Unusual Whales does not have an official public API. This platform scrapes the public feed or uses the community-maintained Python client where available. This is the most fragile data dependency in the stack and is isolated to a single ETL module.

### S&P 500 Universe

The S&P 500 is an index of 500 large-cap US companies selected by S&P Global based on market capitalization, liquidity, profitability, and float. It represents approximately 80% of total US equity market capitalization. This platform uses the S&P 500 as its investable universe because: the data is free and clean, the constituents are liquid enough to trade without slippage, and it is the relevant benchmark for measuring performance.

Membership changes periodically (additions and deletions). This platform loads the current constituent list at startup using Wikipedia's S&P 500 page (which stays current) via a pandas read_html call. Survivorship bias — using today's list to backtest historical periods — is a known limitation of this approach and is documented in the backtest caveats.

### Ticker Symbol

A one-to-five character abbreviation that uniquely identifies a publicly traded security on a specific exchange. `AAPL` is Apple on NASDAQ; `BRK.B` is Berkshire Hathaway Class B on NYSE. Ticker symbols are the primary join key across all data sources in this platform. yfinance, FRED (for index ETFs), and Unusual Whales all use ticker symbols as identifiers.
