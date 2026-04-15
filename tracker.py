"""
HIP-3 target position tracker.
"""
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests
from loguru import logger


INFO_URL = "https://api.hyperliquid.xyz/info"


@dataclass
class PositionChange:
    coin: str
    old_size: float
    new_size: float
    delta: float
    action: str
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
    def __init__(self, target_address: str, timeout: int = 10):
        self.target_address = target_address
        self.timeout = timeout
        self._last_positions: Dict[str, dict] = {}
        self._target_equity: float = 0.0
        self._consecutive_errors: int = 0

    def poll(self) -> Dict[str, dict]:
        positions: Dict[str, dict] = {}
        try:
            eq_resp = requests.post(
                INFO_URL,
                json={"type": "clearinghouseState", "user": self.target_address},
                timeout=self.timeout,
            )
            eq_resp.raise_for_status()
            eq_data = eq_resp.json()
            self._target_equity = float(
                eq_data.get("marginSummary", {}).get("accountValue", 0)
            )

            resp = requests.post(
                INFO_URL,
                json={
                    "type": "clearinghouseState",
                    "user": self.target_address,
                    "dex": "xyz",
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            self._consecutive_errors += 1
            logger.error(
                f"Failed to poll HIP-3 target wallet (attempt {self._consecutive_errors}): {e}"
            )
            if self._consecutive_errors >= 5:
                logger.critical("5 consecutive HIP-3 poll failures - check network / API")
            return self._last_positions

        self._consecutive_errors = 0
        for entry in data.get("assetPositions", []):
            pos = entry.get("position", {})
            coin = pos.get("coin", "")
            size = float(pos.get("szi", 0))
            if abs(size) < 1e-10:
                continue
            lev_data = pos.get("leverage", {})
            leverage = (
                int(float(lev_data.get("value", 1)))
                if isinstance(lev_data, dict)
                else 1
            )
            positions[coin] = {
                "size": size,
                "entry_px": float(pos.get("entryPx", 0) or 0),
                "leverage": leverage,
            }
        return positions

    def diff(
        self,
        current: Dict[str, dict],
        coin_filter: Optional[List[str]] = None,
    ) -> List[PositionChange]:
        changes: List[PositionChange] = []
        all_coins = set(self._last_positions.keys()) | set(current.keys())
        for coin in all_coins:
            if coin_filter and "*" not in coin_filter and coin not in coin_filter:
                continue
            old = self._last_positions.get(coin, {})
            new = current.get(coin, {})
            old_size = old.get("size", 0.0)
            new_size = new.get("size", 0.0)
            delta = new_size - old_size
            if abs(delta) < 1e-10:
                continue
            changes.append(
                PositionChange(
                    coin=coin,
                    old_size=old_size,
                    new_size=new_size,
                    delta=delta,
                    action=self._classify(old_size, new_size),
                    target_entry_px=new.get("entry_px", 0.0),
                    target_leverage=new.get("leverage", 1),
                    timestamp=time.time(),
                )
            )
        self._last_positions = current
        return changes

    def seed(self, positions: Dict[str, dict]) -> None:
        self._last_positions = positions
        logger.info(f"Tracker seeded with {len(positions)} HIP-3 position(s)")

    @property
    def target_equity(self) -> float:
        return self._target_equity

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
