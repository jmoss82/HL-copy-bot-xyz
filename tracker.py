"""
Position Tracker

Polls a target wallet via the public HyperLiquid info API (no auth needed)
and diffs positions between poll cycles to detect trade activity.

Supports both standard HyperLiquid perps and XYZ HIP-3 pairs.
"""
import time
import requests
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from loguru import logger


INFO_URL = "https://api.hyperliquid.xyz/info"


@dataclass
class PositionChange:
    """A single detected position change on the target wallet."""
    coin: str
    old_size: float
    new_size: float
    delta: float          # new_size - old_size (positive = bought, negative = sold)
    action: str           # OPEN, CLOSE, INCREASE, DECREASE, FLIP
    target_entry_px: float = 0.0
    target_leverage: int = 1
    timestamp: float = 0.0

    @property
    def is_buy(self) -> bool:
        return self.delta > 0

    def __repr__(self) -> str:
        side = "BUY" if self.is_buy else "SELL"
        return (
            f"{self.action} {self.coin}: {side} {abs(self.delta):.6f} "
            f"({self.old_size:+.6f} -> {self.new_size:+.6f})"
        )


class PositionTracker:
    """
    Watches a target wallet's perp positions via the public API.

    Polls both standard HL perps and XYZ HIP-3 pairs in each cycle.

    Usage:
        tracker = PositionTracker("0xABC...")
        positions = tracker.poll()          # fetch current state
        changes   = tracker.diff(positions) # compare to last snapshot
    """

    def __init__(self, target_address: str, timeout: int = 10):
        self.target_address = target_address
        self.timeout = timeout

        # Last-known positions: coin -> {"size": float, "entry_px": float, "leverage": int}
        self._last_positions: Dict[str, dict] = {}
        self._target_equity: float = 0.0
        self._last_poll_time: float = 0.0
        self._consecutive_errors: int = 0

    # -- Public API -------------------------------------------------

    def poll(self) -> Dict[str, dict]:
        """
        Fetch the target wallet's current positions (standard perps + XYZ HIP-3).

        Returns:
            Dict mapping coin -> {"size", "entry_px", "leverage"}
        """
        positions: Dict[str, dict] = {}

        # -- Standard perp positions --------------------------------
        try:
            resp = requests.post(
                INFO_URL,
                json={"type": "clearinghouseState", "user": self.target_address},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()

            self._consecutive_errors = 0
            self._last_poll_time = time.time()

            # Parse equity from standard account
            margin = data.get("marginSummary", {})
            self._target_equity = float(margin.get("accountValue", 0))

            for entry in data.get("assetPositions", []):
                pos = entry.get("position", {})
                coin = pos.get("coin", "")
                size = float(pos.get("szi", 0))
                if abs(size) < 1e-10:
                    continue
                lev_data = pos.get("leverage", {})
                leverage = int(lev_data.get("value", 1)) if isinstance(lev_data, dict) else 1
                positions[coin] = {
                    "size": size,
                    "entry_px": float(pos.get("entryPx", 0) or 0),
                    "leverage": leverage,
                }

        except Exception as e:
            self._consecutive_errors += 1
            logger.error(
                f"Failed to poll target wallet (attempt {self._consecutive_errors}): {e}"
            )
            if self._consecutive_errors >= 5:
                logger.critical("5 consecutive poll failures - check network / API")
            return self._last_positions  # return stale data so diff produces no changes

        # -- XYZ HIP-3 positions ------------------------------------
        try:
            xyz_resp = requests.post(
                INFO_URL,
                json={
                    "type": "clearinghouseState",
                    "user": self.target_address,
                    "dex": "xyz",
                },
                timeout=self.timeout,
            )
            xyz_resp.raise_for_status()
            xyz_data = xyz_resp.json()

            for entry in xyz_data.get("assetPositions", []):
                pos = entry.get("position", {})
                coin = pos.get("coin", "")
                size = float(pos.get("szi", 0))
                if abs(size) < 1e-10:
                    continue
                lev_data = pos.get("leverage", {})
                leverage = int(lev_data.get("value", 1)) if isinstance(lev_data, dict) else 1
                positions[coin] = {
                    "size": size,
                    "entry_px": float(pos.get("entryPx", 0) or 0),
                    "leverage": leverage,
                }

        except Exception as e:
            # Non-fatal: XYZ query failure just means we won't see XYZ positions this cycle
            logger.debug(f"XYZ position poll failed (non-fatal): {e}")

        return positions

    def diff(
        self,
        current: Dict[str, dict],
        coin_filter: Optional[List[str]] = None,
    ) -> List[PositionChange]:
        """
        Compare *current* positions to the last snapshot and return changes.

        Also updates the internal snapshot so the next diff is against *current*.

        Args:
            current:     Positions from poll().
            coin_filter: If set, only report changes for these coins.
                         Pass ["*"] or None to report all.
        """
        changes: List[PositionChange] = []
        all_coins = set(list(self._last_positions.keys()) + list(current.keys()))

        for coin in all_coins:
            # Apply filter
            if coin_filter and "*" not in coin_filter and coin not in coin_filter:
                continue

            old = self._last_positions.get(coin, {})
            new = current.get(coin, {})

            old_size = old.get("size", 0.0)
            new_size = new.get("size", 0.0)
            delta = new_size - old_size

            if abs(delta) < 1e-10:
                continue

            changes.append(PositionChange(
                coin=coin,
                old_size=old_size,
                new_size=new_size,
                delta=delta,
                action=self._classify(old_size, new_size),
                target_entry_px=new.get("entry_px", 0.0),
                target_leverage=new.get("leverage", 1),
                timestamp=time.time(),
            ))

        # Update snapshot
        self._last_positions = current
        return changes

    def seed(self, positions: Dict[str, dict]) -> None:
        """
        Set the internal snapshot without producing a diff.
        Useful for recording the target's state at startup when you don't
        want to treat existing positions as new trades.
        """
        self._last_positions = positions
        logger.info(f"Tracker seeded with {len(positions)} position(s)")

    # -- Properties -------------------------------------------------

    @property
    def target_equity(self) -> float:
        return self._target_equity

    @property
    def last_positions(self) -> Dict[str, dict]:
        return dict(self._last_positions)

    # -- Helpers ----------------------------------------------------

    @staticmethod
    def _classify(old_size: float, new_size: float) -> str:
        old_zero = abs(old_size) < 1e-10
        new_zero = abs(new_size) < 1e-10

        if old_zero and not new_zero:
            return "OPEN"
        if not old_zero and new_zero:
            return "CLOSE"
        if (old_size > 0 and new_size < 0) or (old_size < 0 and new_size > 0):
            return "FLIP"
        if abs(new_size) > abs(old_size):
            return "INCREASE"
        return "DECREASE"
