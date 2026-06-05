"""DuckDB table definitions for the quant-intelligence warehouse.

Grain statements:
  universe     — one row = one S&P 500 constituent ticker
  prices       — one row = one ticker × one trading day
  fundamentals — one row = one ticker × one weekly fetch date
  macro        — one row = one calendar date
"""

import duckdb

CREATE_UNIVERSE = """
CREATE TABLE IF NOT EXISTS universe (
    ticker              VARCHAR PRIMARY KEY,
    company_name        VARCHAR NOT NULL,
    sector              VARCHAR,
    industry            VARCHAR,
    gics_sub_industry   VARCHAR,
    headquarters        VARCHAR,
    date_added          DATE,
    cik                 VARCHAR,
    loaded_at           TIMESTAMP DEFAULT current_timestamp
)
"""

CREATE_PRICES = """
CREATE TABLE IF NOT EXISTS prices (
    ticker      VARCHAR NOT NULL,
    date        DATE    NOT NULL,
    open        DOUBLE,
    high        DOUBLE,
    low         DOUBLE,
    close       DOUBLE,
    adj_close   DOUBLE,
    volume      BIGINT,
    loaded_at   TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (ticker, date)
)
"""

CREATE_FUNDAMENTALS = """
CREATE TABLE IF NOT EXISTS fundamentals (
    ticker              VARCHAR NOT NULL,
    fetched_date        DATE    NOT NULL,
    market_cap          DOUBLE,
    pe_ratio            DOUBLE,
    forward_pe          DOUBLE,
    pb_ratio            DOUBLE,
    ev_ebitda           DOUBLE,
    revenue_growth      DOUBLE,
    gross_margin        DOUBLE,
    operating_margin    DOUBLE,
    debt_equity         DOUBLE,
    roe                 DOUBLE,
    free_cashflow       DOUBLE,
    earnings_date       DATE,
    PRIMARY KEY (ticker, fetched_date)
)
"""

CREATE_MACRO = """
CREATE TABLE IF NOT EXISTS macro (
    date            DATE PRIMARY KEY,
    t10y2y          DOUBLE,
    fed_funds_rate  DOUBLE,
    cpi             DOUBLE,
    vix             DOUBLE,
    loaded_at       TIMESTAMP DEFAULT current_timestamp
)
"""

_ALL_TABLES = [CREATE_UNIVERSE, CREATE_PRICES, CREATE_FUNDAMENTALS, CREATE_MACRO]


def initialize_schema(db_path: str) -> None:
    """Create all tables if they do not already exist."""
    conn = duckdb.connect(db_path)
    for ddl in _ALL_TABLES:
        conn.execute(ddl)
    conn.close()
