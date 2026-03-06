"""
Copy Bot Configuration

Loads settings from environment variables and provides defaults.
"""
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv

# Load .env file (local only - Railway provides env vars directly)
env_file = Path(__file__).parent / ".env"
if env_file.exists():
    load_dotenv(env_file)


@dataclass
class CopyBotConfig:
    """All configuration for the copy trading bot."""

    # -- Target trader ----------------------------------------------
    target_address: str = ""

    # -- Your wallet credentials ------------------------------------
    wallet_address: str = ""
    private_key: str = ""
    account_address: str = ""  # agent-wallet; defaults to wallet_address

    # -- Scaling ----------------------------------------------------
    #   "proportional" - scale by (your equity / their equity)
    #   "fixed_ratio"  - multiply target delta by fixed_ratio
    #   "fixed_size"   - always trade fixed_size per signal (direction matched)
    #   "fixed_notional" - always trade fixed_notional_usd per signal
    scaling_mode: str = "fixed_ratio"
    fixed_ratio: float = 1.0
    fixed_size: float = 0.001        # used only in fixed_size mode
    fixed_notional_usd: float = 25.0  # used only in fixed_notional mode
    max_trade_usd: float = 0.0        # 0 disables per-trade notional cap

    # -- Position limits --------------------------------------------
    max_position_usd: float = 5000.0  # hard cap on notional exposure

    # -- Leverage ---------------------------------------------------
    leverage: int = 20
    is_cross: bool = True

    # -- Polling ----------------------------------------------------
    poll_interval_seconds: float = 3.0
    reconcile_mode: str = "state"  # "state" (recommended) or "delta"

    # -- Execution --------------------------------------------------
    slippage_bps: float = 10.0        # max slippage for IOC limit orders
    min_trade_size_usd: float = 11.0  # HL minimum is ~$10

    # -- Coin filter ------------------------------------------------
    coins_to_copy: List[str] = field(default_factory=lambda: ["BTC"])

    # -- Startup behaviour ------------------------------------------
    sync_on_startup: bool = True  # open target's current position immediately

    # -- Safety -----------------------------------------------------
    max_daily_trades: int = 200
    dry_run: bool = True  # SAFE DEFAULT - no real orders until you flip this

    # -- Logging ----------------------------------------------------
    log_level: str = "INFO"


def load_config() -> CopyBotConfig:
    """Build config from environment variables with sensible defaults."""
    cfg = CopyBotConfig(
        target_address=os.getenv(
            "COPY_TARGET_ADDRESS",
            "0xe339f3a21ac5cb468f0949a1da2ceb029eb036cf",
        ),
        wallet_address=os.getenv("HL_WALLET_ADDRESS", ""),
        private_key=os.getenv("HL_PRIVATE_KEY", ""),
        account_address=os.getenv("HL_ACCOUNT_ADDRESS", ""),
        scaling_mode=os.getenv("COPY_SCALING_MODE", "fixed_ratio"),
        fixed_ratio=float(os.getenv("COPY_FIXED_RATIO", "1.0")),
        fixed_size=float(os.getenv("COPY_FIXED_SIZE", "0.001")),
        fixed_notional_usd=float(os.getenv("COPY_FIXED_NOTIONAL_USD", "25.0")),
        max_trade_usd=float(os.getenv("COPY_MAX_TRADE_USD", "0.0")),
        max_position_usd=float(os.getenv("COPY_MAX_POSITION_USD", "5000")),
        leverage=int(os.getenv("COPY_LEVERAGE", "40")),
        is_cross=os.getenv("COPY_IS_CROSS", "false").lower() == "true",
        poll_interval_seconds=float(os.getenv("COPY_POLL_INTERVAL", "3.0")),
        reconcile_mode=os.getenv("COPY_RECONCILE_MODE", "state").lower(),
        slippage_bps=float(os.getenv("COPY_SLIPPAGE_BPS", "10.0")),
        min_trade_size_usd=float(os.getenv("COPY_MIN_TRADE_USD", "11.0")),
        coins_to_copy=os.getenv("COPY_COINS", "BTC").split(","),
        sync_on_startup=os.getenv("COPY_SYNC_STARTUP", "true").lower() == "true",
        max_daily_trades=int(os.getenv("COPY_MAX_DAILY_TRADES", "200")),
        dry_run=os.getenv("COPY_DRY_RUN", "true").lower() == "true",
        log_level=os.getenv("COPY_LOG_LEVEL", "INFO"),
    )

    # Default account_address to wallet_address
    if not cfg.account_address:
        cfg.account_address = cfg.wallet_address

    return cfg


def validate_config(cfg: CopyBotConfig) -> None:
    """Raise if the config is unusable."""
    if not cfg.target_address:
        raise ValueError("COPY_TARGET_ADDRESS is required")
    if not cfg.wallet_address or not cfg.private_key:
        raise ValueError("HL_WALLET_ADDRESS and HL_PRIVATE_KEY are required")
    if not cfg.wallet_address.startswith("0x") or len(cfg.wallet_address) != 42:
        raise ValueError(f"Invalid wallet address: {cfg.wallet_address}")
    if not cfg.private_key.startswith("0x") or len(cfg.private_key) != 66:
        raise ValueError("Invalid private key format")
    if cfg.reconcile_mode not in {"state", "delta"}:
        raise ValueError("COPY_RECONCILE_MODE must be 'state' or 'delta'")
