"""
HIP-3 trade copier.
"""
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from config import CopyBotConfig
from hip3_client import Hip3Client
from tracker import PositionChange


@dataclass
class TradeResult:
    success: bool
    coin: str
    side: str
    requested_size: float
    filled_size: float = 0.0
    avg_price: float = 0.0
    order_id: Optional[int] = None
    error: str = ""


class TradeCopier:
    def __init__(self, config: CopyBotConfig):
        self.config = config
        self.client = Hip3Client(
            config.wallet_address,
            config.private_key,
            account_address=config.account_address,
            max_orders_per_minute=config.max_daily_trades,
        )
        self._trade_timestamps: deque = deque(maxlen=config.max_daily_trades)

    def setup(self) -> None:
        self.client.setup()
        for coin in self.config.coins_to_copy:
            meta = self.client.get_meta(coin)
            if self.config.leverage > meta.max_leverage:
                logger.warning(
                    f"{coin}: configured leverage {self.config.leverage} exceeds max "
                    f"{meta.max_leverage}; using max"
                )
            self.client.set_leverage(coin, self.config.leverage)

    def get_our_equity(self, force: bool = False) -> float:
        return self.client.get_account_equity(force=force)

    def get_our_positions(self, force: bool = False) -> dict[str, float]:
        return self.client.get_positions(force=force)

    def get_mid_price(self, coin: str) -> float:
        return self.client.get_mid_price(coin)

    def target_position_to_desired_size(
        self,
        coin: str,
        target_size: float,
        target_equity: float,
    ) -> float:
        if abs(target_size) < 1e-10:
            return 0.0
        if self.config.scaling_mode == "proportional":
            our_eq = self.get_our_equity()
            ratio = (our_eq / target_equity) if target_equity > 0 else 0.0
            return target_size * ratio
        if self.config.scaling_mode == "fixed_ratio":
            return target_size * self.config.fixed_ratio
        if self.config.scaling_mode == "fixed_size":
            return self.config.fixed_size * (1.0 if target_size > 0 else -1.0)
        if self.config.scaling_mode == "fixed_notional":
            mid = self.get_mid_price(coin)
            if mid <= 0:
                logger.error(f"No price data for {coin}, cannot compute desired size")
                return 0.0
            return (self.config.fixed_notional_usd / mid) * (
                1.0 if target_size > 0 else -1.0
            )
        logger.error(f"Unknown scaling mode: {self.config.scaling_mode}")
        return 0.0

    def scale_delta(
        self,
        change: PositionChange,
        target_equity: float,
    ) -> float:
        raw = change.delta
        if self.config.scaling_mode == "proportional":
            our_eq = self.get_our_equity()
            ratio = (our_eq / target_equity) if target_equity > 0 else 0.0
            scaled = raw * ratio
        elif self.config.scaling_mode == "fixed_ratio":
            scaled = raw * self.config.fixed_ratio
        elif self.config.scaling_mode == "fixed_size":
            scaled = self.config.fixed_size * (1.0 if raw > 0 else -1.0)
        elif self.config.scaling_mode == "fixed_notional":
            mid = self.get_mid_price(change.coin)
            if mid <= 0:
                logger.error(f"No price data for {change.coin}, cannot scale")
                return 0.0
            scaled = (self.config.fixed_notional_usd / mid) * (1.0 if raw > 0 else -1.0)
        else:
            logger.error(f"Unknown scaling mode: {self.config.scaling_mode}")
            return 0.0

        mid = self.get_mid_price(change.coin)
        if (
            mid > 0
            and self.config.max_trade_usd > 0
            and abs(scaled) * mid > self.config.max_trade_usd
        ):
            capped = self.config.max_trade_usd / mid
            logger.warning(
                f"Per-trade cap hit: {abs(scaled):.6f} {change.coin} "
                f"(${abs(scaled) * mid:,.0f}) capped to {capped:.6f} "
                f"(${self.config.max_trade_usd:,.0f})"
            )
            scaled = capped * (1.0 if scaled > 0 else -1.0)
        return scaled

    def execute(
        self,
        coin: str,
        size_delta: float,
        dry_run: bool = True,
    ) -> Optional[TradeResult]:
        if abs(size_delta) < 1e-10:
            return None

        is_buy = size_delta > 0
        side = "BUY" if is_buy else "SELL"
        abs_size = abs(self.client.format_size(coin, abs(size_delta)))
        if abs_size == 0:
            logger.debug(f"Formatted size rounded to zero for {coin}, skipping")
            return None

        mid = self.get_mid_price(coin)
        if mid <= 0:
            logger.error(f"No HIP-3 price data for {coin}, cannot execute")
            return TradeResult(False, coin, side, abs_size, error="no price data")

        signed_delta = abs_size if is_buy else -abs_size
        current_size = self.get_our_positions().get(coin, 0.0)
        max_abs_pos = (
            self.config.max_position_usd / mid
            if self.config.max_position_usd > 0
            else float("inf")
        )
        proposed_size = current_size + signed_delta
        clipped_size = max(-max_abs_pos, min(max_abs_pos, proposed_size))
        signed_delta = clipped_size - current_size
        if abs(signed_delta) < 1e-10:
            logger.warning(
                f"Position cap blocks trade: {coin} current={current_size:+.6f}, "
                f"requested_delta={proposed_size - current_size:+.6f}, max_abs={max_abs_pos:.6f}"
            )
            return None
        if abs((proposed_size - current_size) - signed_delta) > 1e-10:
            logger.warning(
                f"Position cap clipped trade: {coin} "
                f"{proposed_size - current_size:+.6f} -> {signed_delta:+.6f}"
            )

        is_buy = signed_delta > 0
        side = "BUY" if is_buy else "SELL"
        abs_size = abs(self.client.format_size(coin, abs(signed_delta)))
        if abs_size == 0:
            return None

        notional = abs_size * mid
        if notional < self.config.min_trade_size_usd:
            logger.debug(
                f"HIP-3 trade too small: {abs_size} {coin} = ${notional:.2f} "
                f"(min ${self.config.min_trade_size_usd})"
            )
            return None

        now = time.time()
        day_ago = now - 86400
        while self._trade_timestamps and self._trade_timestamps[0] < day_ago:
            self._trade_timestamps.popleft()
        if len(self._trade_timestamps) >= self.config.max_daily_trades:
            logger.critical("Daily trade limit reached - refusing to execute")
            return TradeResult(False, coin, side, abs_size, error="daily limit")

        slip = self.config.slippage_bps / 10_000
        limit_px = mid * (1 + slip) if is_buy else mid * (1 - slip)
        formatted_limit = self.client.format_price(coin, limit_px)

        if dry_run:
            logger.info(
                f"[DRY RUN HIP-3] {side} {abs_size} {coin} @ ~${formatted_limit} "
                f"(mid=${mid:.4f}, notional=${notional:,.0f})"
            )
            return TradeResult(True, coin, side, abs_size, abs_size, mid)

        logger.warning(
            f"EXECUTING HIP-3: {side} {abs_size} {coin} @ ${formatted_limit} "
            f"(mid=${mid:.4f}, slippage={self.config.slippage_bps}bps)"
        )
        try:
            result = self.client.place_order(
                coin=coin,
                is_buy=is_buy,
                size=abs_size,
                limit_px=formatted_limit,
                reduce_only=False,
                tif="Ioc",
            )
            if result and result.get("status") == "ok":
                statuses = (
                    result.get("response", {})
                    .get("data", {})
                    .get("statuses", [])
                )
                if statuses:
                    status = statuses[0]
                    if "filled" in status:
                        fill = status["filled"]
                        avg = float(fill.get("avgPx", 0) or 0)
                        tsz = float(fill.get("totalSz", 0) or 0)
                        oid = fill.get("oid", 0)
                        self._trade_timestamps.append(now)
                        signed_fill = tsz if is_buy else -tsz
                        self.client.apply_local_fill(coin, signed_fill)
                        logger.success(
                            f"FILLED HIP-3: {side} {tsz} {coin} @ ${avg:.4f} (oid={oid})"
                        )
                        return TradeResult(True, coin, side, abs_size, tsz, avg, oid)
                    if "resting" in status:
                        oid = status["resting"].get("oid", 0)
                        self._trade_timestamps.append(now)
                        self.client.invalidate_positions_cache()
                        logger.warning(
                            f"HIP-3 order resting (unexpected for IOC): oid={oid}"
                        )
                        return TradeResult(True, coin, side, abs_size, 0, 0, oid)
                    if "error" in status:
                        err = status["error"]
                        logger.error(f"HIP-3 order rejected: {err}")
                        return TradeResult(False, coin, side, abs_size, error=err)
            logger.error(f"Unexpected HIP-3 order response: {result}")
            return TradeResult(False, coin, side, abs_size, error=str(result))
        except Exception as e:
            logger.error(f"HIP-3 execution exception: {e}")
            return TradeResult(False, coin, side, abs_size, error=str(e))
