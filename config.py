"""
HIP-3 copy-bot configuration.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv


env_file = Path(__file__).parent / ".env"
if env_file.exists():
    load_dotenv(env_file)


@dataclass
class CopyBotConfig:
    target_address: str = ""

    wallet_address: str = ""
    private_key: str = ""
    account_address: str = ""

    scaling_mode: str = "fixed_notional"
    fixed_ratio: float = 1.0
    fixed_size: float = 1.0
    fixed_notional_usd: float = 20.0
    max_trade_usd: float = 0.0

    max_position_usd: float = 200.0
    leverage: int = 5

    poll_interval_seconds: float = 3.0
    reconcile_mode: str = "lifecycle"

    slippage_bps: float = 10.0
    min_trade_size_usd: float = 11.0

    coins_to_copy: List[str] = field(default_factory=lambda: ["xyz:SILVER"])
    sync_on_startup: bool = False

    max_daily_trades: int = 200
    dry_run: bool = True
    log_level: str = "INFO"


def _parse_coins(raw: str) -> List[str]:
    return [coin.strip() for coin in raw.split(",") if coin.strip()]


def load_config() -> CopyBotConfig:
    cfg = CopyBotConfig(
        target_address=os.getenv("COPY_TARGET_ADDRESS", ""),
        wallet_address=os.getenv("HL_WALLET_ADDRESS", ""),
        private_key=os.getenv("HL_PRIVATE_KEY", ""),
        account_address=os.getenv("HL_ACCOUNT_ADDRESS", ""),
        scaling_mode=os.getenv("COPY_SCALING_MODE", "fixed_notional").lower(),
        fixed_ratio=float(os.getenv("COPY_FIXED_RATIO", "1.0")),
        fixed_size=float(os.getenv("COPY_FIXED_SIZE", "1.0")),
        fixed_notional_usd=float(os.getenv("COPY_FIXED_NOTIONAL_USD", "20.0")),
        max_trade_usd=float(os.getenv("COPY_MAX_TRADE_USD", "0.0")),
        max_position_usd=float(os.getenv("COPY_MAX_POSITION_USD", "200.0")),
        leverage=int(os.getenv("COPY_LEVERAGE", "5")),
        poll_interval_seconds=float(os.getenv("COPY_POLL_INTERVAL", "3.0")),
        reconcile_mode=os.getenv("COPY_RECONCILE_MODE", "lifecycle").lower(),
        slippage_bps=float(os.getenv("COPY_SLIPPAGE_BPS", "10.0")),
        min_trade_size_usd=float(os.getenv("COPY_MIN_TRADE_USD", "11.0")),
        coins_to_copy=_parse_coins(os.getenv("COPY_COINS", "xyz:SILVER")),
        sync_on_startup=os.getenv("COPY_SYNC_STARTUP", "false").lower() == "true",
        max_daily_trades=int(os.getenv("COPY_MAX_DAILY_TRADES", "200")),
        dry_run=os.getenv("COPY_DRY_RUN", "true").lower() == "true",
        log_level=os.getenv("COPY_LOG_LEVEL", "INFO"),
    )
    if not cfg.account_address:
        cfg.account_address = cfg.wallet_address
    return cfg


def validate_config(cfg: CopyBotConfig) -> None:
    if not cfg.target_address:
        raise ValueError("COPY_TARGET_ADDRESS is required")
    if not cfg.wallet_address or not cfg.private_key:
        raise ValueError("HL_WALLET_ADDRESS and HL_PRIVATE_KEY are required")
    if not cfg.wallet_address.startswith("0x") or len(cfg.wallet_address) != 42:
        raise ValueError(f"Invalid wallet address: {cfg.wallet_address}")
    if not cfg.private_key.startswith("0x") or len(cfg.private_key) != 66:
        raise ValueError("Invalid private key format")
    if cfg.reconcile_mode not in {"state", "delta", "lifecycle"}:
        raise ValueError(
            "COPY_RECONCILE_MODE must be 'state', 'delta', or 'lifecycle'"
        )
    if cfg.scaling_mode not in {
        "proportional",
        "fixed_ratio",
        "fixed_size",
        "fixed_notional",
    }:
        raise ValueError(
            "COPY_SCALING_MODE must be proportional, fixed_ratio, fixed_size, or fixed_notional"
        )
    if not cfg.coins_to_copy:
        raise ValueError("COPY_COINS must contain at least one HIP-3 symbol")
    non_xyz = [coin for coin in cfg.coins_to_copy if not coin.startswith("xyz:")]
    if non_xyz:
        raise ValueError(
            f"HIP-3 bot only supports xyz:* symbols. Invalid coins: {', '.join(non_xyz)}"
        )
