"""
HIP-3 execution and account client.
"""
import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, Optional

import requests
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.signing import sign_l1_action
from loguru import logger


INFO_URL = "https://api.hyperliquid.xyz/info"
DEX_NAME = "xyz"


@dataclass
class Hip3Meta:
    coin: str
    index: int
    asset_id: int
    size_decimals: int
    price_decimals: int
    max_leverage: int

    @property
    def tick_size(self) -> float:
        return 10 ** (-self.price_decimals) if self.price_decimals > 0 else 1.0


class Hip3Client:
    def __init__(
        self,
        wallet_address: str,
        private_key: str,
        account_address: Optional[str] = None,
        max_orders_per_minute: int = 60,
    ):
        self.wallet_address = wallet_address
        self.private_key = private_key
        self.account_address = account_address or wallet_address

        self._account = Account.from_key(private_key)
        if self._account.address.lower() != wallet_address.lower():
            raise ValueError(
                "Private key does not match wallet address. "
                f"Expected {wallet_address}, got {self._account.address}"
            )

        base_url = constants.MAINNET_API_URL
        self.info = self._build_info_with_retry(base_url)
        self.exchange = Exchange(
            self._account,
            base_url,
            account_address=self.account_address,
        )
        self.query_address = self.account_address

        self._meta_by_coin: Dict[str, Hip3Meta] = {}
        self._mids_cache: Dict[str, float] = {}
        self._mids_ts: float = 0.0
        self._positions_cache: Dict[str, float] = {}
        self._positions_ts: float = 0.0
        self._equity_cache: float = 0.0
        self._equity_ts: float = 0.0
        self._order_timestamps: deque = deque(maxlen=max_orders_per_minute)

    def setup(self) -> None:
        self.refresh_meta(force=True)
        logger.info(
            f"HIP-3 client initialised | signer={self._account.address} | account={self.query_address}"
        )
        logger.info(f"Loaded metadata for {len(self._meta_by_coin)} HIP-3 pairs")

    def refresh_meta(self, force: bool = False) -> Dict[str, Hip3Meta]:
        if self._meta_by_coin and not force:
            return dict(self._meta_by_coin)
        resp = requests.post(
            INFO_URL,
            json={"type": "meta", "dex": DEX_NAME},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        universe = data.get("universe", [])
        meta: Dict[str, Hip3Meta] = {}
        for index, asset in enumerate(universe):
            coin = asset["name"]
            meta[coin] = Hip3Meta(
                coin=coin,
                index=index,
                asset_id=110000 + index,
                size_decimals=int(asset.get("szDecimals", 2)),
                price_decimals=int(asset.get("pxDecimals", 0) or 0),
                max_leverage=int(asset.get("maxLeverage", 10)),
            )
        self._meta_by_coin = meta
        return dict(meta)

    def has_coin(self, coin: str) -> bool:
        return coin in self._meta_by_coin

    def get_meta(self, coin: str) -> Hip3Meta:
        if coin not in self._meta_by_coin:
            self.refresh_meta(force=True)
        if coin not in self._meta_by_coin:
            raise ValueError(f"Unknown HIP-3 coin: {coin}")
        return self._meta_by_coin[coin]

    def get_account_equity(self, force: bool = False) -> float:
        if not force and (time.time() - self._equity_ts) < 60:
            return self._equity_cache
        state = self.info.user_state(self.query_address)
        margin = state.get("marginSummary", {})
        self._equity_cache = float(margin.get("accountValue", 0))
        if self._equity_cache <= 0:
            cross = state.get("crossMarginSummary", {})
            self._equity_cache = float(cross.get("accountValue", 0) or 0)
        if self._equity_cache <= 0:
            self._equity_cache = float(state.get("withdrawable", 0) or 0)
        self._equity_ts = time.time()
        return self._equity_cache

    def get_positions(self, force: bool = False) -> Dict[str, float]:
        if not force and (time.time() - self._positions_ts) < 2:
            return dict(self._positions_cache)
        state = self.info.user_state(self.query_address, dex=DEX_NAME)
        positions: Dict[str, float] = {}
        for entry in state.get("assetPositions", []):
            pos = entry.get("position", {})
            coin = pos.get("coin", "")
            size = float(pos.get("szi", 0))
            if abs(size) > 1e-10:
                positions[coin] = size
        self._positions_cache = positions
        self._positions_ts = time.time()
        return dict(positions)

    def invalidate_positions_cache(self) -> None:
        self._positions_ts = 0.0

    def apply_local_fill(self, coin: str, signed_fill: float) -> None:
        self._positions_cache[coin] = self._positions_cache.get(coin, 0.0) + signed_fill
        if abs(self._positions_cache[coin]) < 1e-10:
            self._positions_cache.pop(coin, None)
        self._positions_ts = 0.0

    def get_mid_price(self, coin: str) -> float:
        if (time.time() - self._mids_ts) >= 1:
            self._refresh_mids()
        if coin in self._mids_cache:
            return self._mids_cache[coin]
        self._refresh_mids()
        return self._mids_cache.get(coin, 0.0)

    def _refresh_mids(self) -> None:
        try:
            resp = requests.post(
                INFO_URL,
                json={"type": "metaAndAssetCtxs", "dex": DEX_NAME},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and len(data) >= 2:
                universe = data[0].get("universe", [])
                contexts = data[1]
                mids: Dict[str, float] = {}
                for idx, asset in enumerate(universe):
                    if idx >= len(contexts):
                        continue
                    mark_px = float(contexts[idx].get("markPx") or 0)
                    if mark_px > 0:
                        mids[asset["name"]] = mark_px
                self._mids_cache = mids
                self._mids_ts = time.time()
        except Exception as e:
            logger.error(f"Failed to refresh HIP-3 prices: {e}")

    def format_size(self, coin: str, size: float) -> float:
        meta = self.get_meta(coin)
        return round(size, meta.size_decimals)

    def format_price(self, coin: str, price: float) -> float:
        meta = self.get_meta(coin)
        return round(price / meta.tick_size) * meta.tick_size

    def price_to_wire(self, coin: str, price: float) -> str:
        meta = self.get_meta(coin)
        rounded = self.format_price(coin, price)
        return f"{rounded:.{meta.price_decimals}f}"

    def set_leverage(self, coin: str, leverage: int) -> bool:
        meta = self.get_meta(coin)
        leverage = min(leverage, meta.max_leverage)
        action = {
            "type": "updateLeverage",
            "asset": meta.asset_id,
            "isCross": False,
            "leverage": leverage,
        }
        try:
            result = self._sign_and_post(action)
            if result and result.get("status") == "ok":
                logger.info(f"Leverage set: {coin} {leverage}x (isolated)")
                return True
            logger.warning(f"HIP-3 leverage update response for {coin}: {result}")
            return True
        except Exception as e:
            logger.warning(f"Could not set HIP-3 leverage for {coin}: {e}")
            return True

    def place_order(
        self,
        coin: str,
        is_buy: bool,
        size: float,
        limit_px: float,
        reduce_only: bool = False,
        tif: str = "Ioc",
    ) -> dict:
        meta = self.get_meta(coin)
        formatted_size = self.format_size(coin, size)
        if formatted_size <= 0:
            raise ValueError(f"Formatted size rounded to zero for {coin}")
        action = {
            "type": "order",
            "orders": [
                {
                    "a": meta.asset_id,
                    "b": is_buy,
                    "p": self.price_to_wire(coin, limit_px),
                    "s": str(formatted_size),
                    "r": reduce_only,
                    "t": {"limit": {"tif": tif}},
                }
            ],
            "grouping": "na",
        }
        result = self._sign_and_post(action)
        self._order_timestamps.append(time.time())
        self.invalidate_positions_cache()
        return result

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return "429" in text or "too many requests" in text

    def _build_info_with_retry(self, base_url: str) -> Info:
        delay = 1.0
        last_exc: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                return Info(base_url, skip_ws=True, timeout=10)
            except Exception as e:
                last_exc = e
                if not self._is_rate_limit_error(e) or attempt == 3:
                    break
                logger.warning(
                    f"HIP-3 Info init rate-limited (attempt {attempt}/3). "
                    f"Retrying in {delay:.1f}s"
                )
                time.sleep(delay)
                delay = min(delay * 2, 4.0)
        if last_exc:
            raise last_exc
        raise RuntimeError("HIP-3 Info init failed without an exception")

    def _sign_and_post(self, action: dict) -> dict:
        timestamp = int(time.time() * 1000)
        expires_after = timestamp + 300000
        signature = sign_l1_action(
            wallet=self._account,
            action=action,
            active_pool=self.exchange.vault_address,
            nonce=timestamp,
            expires_after=expires_after,
            is_mainnet=True,
        )
        payload = {
            "action": action,
            "nonce": timestamp,
            "signature": signature,
            "vaultAddress": self.exchange.vault_address,
            "expiresAfter": expires_after,
        }
        logger.debug(f"HIP-3 payload: {json.dumps(payload)}")
        return self.exchange.post("/exchange", payload)
