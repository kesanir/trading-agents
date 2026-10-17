"""
core/config.py
Single source of truth for all runtime configuration.
Loaded once at process startup; all modules import `settings` from here.

Priority (highest → lowest):
  1. Environment variables
  2. .env file (auto-loaded by pydantic-settings)
  3. Defaults declared below

Never import this from data/ or db/ layers directly —
pass settings values as arguments to keep those layers testable.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _env_file() -> str:
    """
    Resolve which .env file to load.
    Order of precedence:
      1. ENV_FILE environment variable  (set by run.py --env before any import)
      2. Default ".env"
    """
    import os
    return os.environ.get("ENV_FILE", ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_file(),
        env_file_encoding="utf-8",
        case_sensitive=False,       # ALPACA_API_KEY == alpaca_api_key
        extra="ignore",             # silently drop unknown env vars
    )

    # ── Alpaca credentials ────────────────────────────────────────────────────
    ALPACA_API_KEY: str = Field(..., description="Alpaca API key (required)")
    ALPACA_SECRET_KEY: str = Field(..., description="Alpaca secret key (required)")
    ALPACA_PAPER: bool = Field(True, description="True = paper trading; False = live")

    # ── Risk thresholds — equity / single-leg option ──────────────────────────
    MAX_LOSS_PCT: float = Field(
        0.25, ge=0.01, le=1.0,
        description="Close position if unrealized loss >= this fraction of cost basis (default 25%)",
    )
    PROFIT_TARGET_PCT: float = Field(
        0.50, ge=0.01, le=10.0,
        description="Close position if unrealized profit >= this fraction of cost basis (default 50%)",
    )

    # ── Risk thresholds — credit spreads & iron condors ───────────────────────
    SPREAD_LOSS_PCT: float = Field(
        0.25, ge=0.01, le=1.0,
        description="Close spread if loss >= this fraction of net credit received (default 25%)",
    )
    SPREAD_PROFIT_PCT: float = Field(
        0.50, ge=0.01, le=1.0,
        description="Close spread if profit >= this fraction of net credit received (default 50%)",
    )

    # ── Trade placement defaults ────────────────────────────────────────────
    SPREAD_DTE_TARGET: int = Field(
        45, ge=7, le=180,
        description="Target days to expiration for new spread trades (default 45)",
    )
    SPREAD_CALL_DELTA: float = Field(
        0.30, ge=0.01, le=0.90,
        description="Target delta for short call leg in credit call spreads (default 0.30)",
    )
    SPREAD_PUT_DELTA: float = Field(
        0.20, ge=0.01, le=0.90,
        description="Target delta for short put leg in credit put spreads (default 0.20)",
    )
    SPREAD_WIDTH: float = Field(
        5.0, ge=0.5, le=50.0,
        description="Dollar width between strikes in a spread (default $5)",
    )
    SPREAD_DEFAULT_QTY: int = Field(
        1, ge=1, le=100,
        description="Default number of spread contracts to trade (default 1)",
    )

    # ── Option expiry safety ──────────────────────────────────────────────────
    EXPIRY_CUTOFF_MINUTES: int = Field(
        15, ge=0, le=60,
        description="Minutes before market close on expiry day to force-close short options (default 15 → 3:45 PM ET)",
    )

    # ── Scanner behaviour ─────────────────────────────────────────────────────
    SCAN_INTERVAL_SECONDS: int = Field(
        60, ge=10, le=3600,
        description="Seconds between scheduled scan cycles (default 60)",
    )
    SCAN_OUTSIDE_MARKET_HOURS: bool = Field(
        False,
        description="If True, run scans even when the equity market is closed (useful for futures/crypto)",
    )
    MAX_CONCURRENT_CLOSES: int = Field(
        5, ge=1, le=20,
        description="Maximum number of close operations submitted concurrently per scan cycle",
    )

    # ── Persistence ───────────────────────────────────────────────────────────
    DB_PATH: str = Field(
        "data/runs.db",
        description="Path to the SQLite run-history database (relative to CWD or absolute)",
    )
    DB_RECENT_RUNS_DEFAULT: int = Field(
        50, ge=1, le=1000,
        description="Default number of recent runs returned by load_recent_runs()",
    )

    # ── Logging ───────────────────────────────────────────────────────────────
    LOG_LEVEL: str = Field(
        "INFO",
        description="Python logging level: DEBUG | INFO | WARNING | ERROR | CRITICAL",
    )
    LOG_FORMAT: str = Field(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        description="Python logging format string",
    )

    # ── Safety / dry-run ─────────────────────────────────────────────────────
    DRY_RUN: bool = Field(
        True,
        description="If True, evaluate risk but do NOT submit any orders. Log intended actions only.",
    )
    ABORT_ON_SHORT_BUYBACK_FAILURE: bool = Field(
        True,
        description=(
            "If True (recommended), abort a spread close entirely when the short-leg buyback fails, "
            "leaving the long leg open as protection. "
            "If False, proceed to sell the long leg anyway (creates a naked short — use with caution)."
        ),
    )

    # ── API server ────────────────────────────────────────────────────────────
    API_HOST: str = Field("0.0.0.0", description="Uvicorn bind host")
    API_PORT: int = Field(8000, ge=1024, le=65535, description="Uvicorn bind port")
    API_RELOAD: bool = Field(False, description="Uvicorn auto-reload (dev only)")
    API_WORKERS: int = Field(1, ge=1, le=8, description="Uvicorn worker count (keep 1 for async)")

    # ── Validators ────────────────────────────────────────────────────────────
    @field_validator("LOG_LEVEL")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"LOG_LEVEL must be one of {valid}, got '{v}'")
        return upper

    @field_validator("ALPACA_API_KEY", "ALPACA_SECRET_KEY")
    @classmethod
    def validate_not_placeholder(cls, v: str, info) -> str:
        placeholders = {"your_key_here", "your_secret_here", "changeme", "xxx", ""}
        if v.lower() in placeholders:
            raise ValueError(
                f"{info.field_name} appears to be a placeholder value — set a real key"
            )
        return v

    # ── Derived helpers ───────────────────────────────────────────────────────
    @property
    def db_path(self) -> Path:
        return Path(self.DB_PATH)

    @property
    def is_live(self) -> bool:
        return not self.ALPACA_PAPER

    @property
    def expiry_cutoff_time(self):
        """Return datetime.time for the option expiry cutoff."""
        from datetime import time
        total_mins = 16 * 60 - self.EXPIRY_CUTOFF_MINUTES   # minutes from midnight
        return time(total_mins // 60, total_mins % 60)


settings = Settings()
