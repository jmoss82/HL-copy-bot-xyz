"""
Trade Copier (with XYZ HIP-3 support)

Executes mirrored trades on your HyperLiquid account.
Standard perps use the official SDK (Exchange.order).
XYZ HIP-3 pairs (coins with 'xyz:' prefix) use raw sign_l1_action.
"""
import time
import requests
from typing import Dict, Optional
from collections import deque
from dataclasses import dataclass
from loguru import logger

from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from hyperliquid.utils.signing import sign_l1_action

from config import CopyBotConfig
from tracker import PositionChange


# XYZ HIP-3 asset IDs: coin name -> numeric asset ID
# These are fixed by the XYZ DEX and do not change.
XYZ_ASSET_IDS: Dict[str, int] = {
    "xyz:XYZ100":    110000,
    "xyz:TSLA":      110001,
    "xyz:NVDA":      110002,
    "xyz:GOLD":      110003,
    "xyz:HOOD":      110004,
    "xyz:INTC":      110005,
    "xyz:PLTR":      110006,
    "xyz:COIN":      110007,
    "xyz:META":      110008,
    "xyz:AAPL":      110009,
    "xyz:MSFT":      110010,
    "xyz:ORCL":      110011,
    "xyz:GOOGL":     110012,
    "xyz:AMZN":      110013,
    "xyz:AMD":       110014,
    "xyz:MU":        110015,
    "xyz:SNDK":      110016,
    "xyz:MSTR":      110017,
    "xyz:CRCL":      110018,
    "xyz:NFLX":      110019,
    "xyz:COST":      110020,
    "xyz:LLY":       110021,
    "xyz:SKHX":      110022,
    "xyz:TSM":       110023,
    "xyz:JPY":       110024,
    "xyz:EUR":       110025,
    "xyz:SILVER":    110026,
    "xyz:RIVN":      110027,
    "xyz:BABA":      110028,
    "xyz:CL":        110029,
    "xyz:COPPER":    110030,
    "xyz:NATGAS":    110031,
    "xyz:URANIUM":   110032,
    "xyz:ALUMINIUM": 110033,
}

INFO_URL = "https://api.hyperliquid.xyz/info"


@dataclass
class TradeResult:
    """Outcome of a single copy-trade execution."""
    success: bool
    coin: str
    side: str           # "BUY" or "SELL"
    requested_size: float
    filled_size: float = 0.0
    avg_price: float = 0.0
    order_id: Optional[int] = None
    error: str = ""


class TradeCopier:
    """
    Mirrors detected position changes onto your HyperLiquid account.

    Standard perps: IOC limit orders via the official HL SDK.
    XYZ HIP-3 pairs: IOC limit orders via raw sign_l1_action (SDK doesn't support xyz: coins).
    """

    def __init__(self, config: CopyBotConfig):
        self.config = config

        # SDK clients - initialised in setup()
        self._account: Optional[Account] = None
        self.info: Optional[Info] = None
        self.exchange: Optional[Exchange] = None

        # Resolved address used for info queries
        self.query_address: str = ""

        # Metadata: standard perp sz_decimals
        self._sz_decimals: Dict[str, int] = {}

        # Metadata: XYZ sz_decimals  {coin -> int}
        self._xyz_sz_decimals: Dict[str, int] = {}

        # Rate-limiting / daily trade counter
        self._trade_timestamps: deque = deque(maxlen=config.max_daily_trades)

        # Cached equity
        self._our_equity: float = 0.0
        self._equity_ts: float = 0.0

        # Cached positions (standard + xyz merged)
        self._positions_cache: Dict[str, float] = {}
        self._positions_ts: float = 0.0

        # Cached mid prices: standard perps
        self._mids_cache: Dict[str, float] = {}
        self._mids_ts: float = 0.0

        # Cached mid prices: XYZ perps (refreshed via metaAndAssetCtxs)
        self._xyz_mids_cache: Dict[str, float] = {}
        self._xyz_mids_ts: float = 0.0

    # -- Lifecycle --------------------------------------------------

    def setup(self) -> None:
        """Initialise SDK clients, load metadata, set leverage."""
        self._account = Account.from_key(self.config.private_key)

        if self._account.address.lower() != self.config.wallet_address.lower():
            raise ValueError(
                f"Private key does not match wallet address. "
                f"Expected {self.config.wallet_address}, got {self._account.address}"
            )

        base_url = constants.MAINNET_API_URL
        self.info = self._build_info_with_retry(base_url)

        acct = self.config.account_address
        if acct and acct.lower() != self._account.address.lower():
            self.exchange = Exchange(self._account, base_url, account_address=acct)
            self.query_address = acct
        else:
            self.exchange = Exchange(self._account, base_url)
            self.query_address = self._account.address

        logger.info(f"SDK initialised  | signer={self._account.address}")
        logger.info(f"Trading account  | address={self.query_address}")

        # Load standard perp metadata
        meta = self.info.meta()
        for asset in meta.get("universe", []):
            name = asset["name"]
            self._sz_decimals[name] = asset.get("szDecimals", 5)
        logger.info(f"Loaded metadata for {len(self._sz_decimals)} standard perps")

        # Load XYZ metadata if we have any xyz coins
        xyz_coins = [c for c in self.config.coins_to_copy if c.startswith("xyz:")]
        if xyz_coins:
            try:
                resp = requests.post(
                    INFO_URL,
                    json={"type": "meta", "dex": "xyz"},
                    timeout=10,
                )
                xyz_meta = resp.json()
                for asset in xyz_meta.get("universe", []):
                    name = f"xyz:{asset['name']}"
                    self._xyz_sz_decimals[name] = asset.get("szDecimals", 4)
                logger.info(f"Loaded metadata for {len(self._xyz_sz_decimals)} XYZ pairs")
            except Exception as e:
                logger.warning(f"Failed to load XYZ metadata: {e} (using defaults)")

        # Set leverage for each coin
        for coin in self.config.coins_to_copy:
            if coin == "*":
                continue
            if coin.startswith("xyz:"):
                self._set_xyz_leverage(coin)
            else:
                self._set_leverage(coin)

    # -- Account queries --------------------------------------------

    def get_our_equity(self, force: bool = False) -> float:
        """Account equity, cached for 60s unless force=True."""
        if not force and (time.time() - self._equity_ts) < 60:
            return self._our_equity
        try:
            state = self.info.user_state(self.query_address)
            self._our_equity = float(
                state.get("marginSummary", {}).get("accountValue", 0)
            )
            self._equity_ts = time.time()
        except Exception as e:
            logger.error(f"Failed to fetch our equity: {e}")
        return self._our_equity

    def get_our_positions(self, force: bool = False) -> Dict[str, float]:
        """
        Return our current positions as {coin: signed_size}.
        Includes both standard HL perps and XYZ HIP-3 positions.
        """
        if not force and (time.time() - self._positions_ts) < 2:
            return dict(self._positions_cache)
        try:
            positions: Dict[str, float] = {}

            # Standard perp positions
            state = self.info.user_state(self.query_address)
            for entry in state.get("assetPositions", []):
                pos = entry.get("position", {})
                coin = pos.get("coin", "")
                size = float(pos.get("szi", 0))
                if abs(size) > 1e-10:
                    positions[coin] = size

            # XYZ positions (separate dex query)
            xyz_coins = [c for c in self.config.coins_to_copy if c.startswith("xyz:")]
            if xyz_coins:
                xyz_state = self.info.user_state(self.query_address, dex="xyz")
                for entry in xyz_state.get("assetPositions", []):
                    pos = entry.get("position", {})
                    coin = pos.get("coin", "")
                    size = float(pos.get("szi", 0))
                    if abs(size) > 1e-10:
                        positions[coin] = size

            self._positions_cache = positions
            self._positions_ts = time.time()
            return dict(positions)
        except Exception as e:
            logger.error(f"Failed to fetch our positions: {e}")
            return dict(self._positions_cache)

    def get_mid_price(self, coin: str) -> float:
        """Current mid-market price. Routes xyz: coins via metaAndAssetCtxs."""
        if coin.startswith("xyz:"):
            return self._get_xyz_mid_price(coin)

        if (time.time() - self._mids_ts) >= 1:
            try:
                mids = self.info.all_mids()
                self._mids_cache = {k: float(v) for k, v in mids.items()}
                self._mids_ts = time.time()
            except Exception as e:
                logger.error(f"Failed to refresh mid prices: {e}")
        if coin in self._mids_cache:
            return self._mids_cache[coin]
        try:
            mids = self.info.all_mids()
            px = float(mids.get(coin, 0))
            if px > 0:
                self._mids_cache[coin] = px
                self._mids_ts = time.time()
            return px
        except Exception as e:
            logger.error(f"Failed to get mid price for {coin}: {e}")
            return 0.0

    def _get_xyz_mid_price(self, coin: str) -> float:
        """Fetch mid price for an XYZ coin. Batches all xyz prices once per second."""
        # Use cached value if fresh
        if coin in self._xyz_mids_cache and (time.time() - self._xyz_mids_ts) < 1:
            return self._xyz_mids_cache.get(coin, 0.0)

        # Batch-refresh all XYZ mark prices via metaAndAssetCtxs
        try:
            resp = requests.post(
                INFO_URL,
                json={"type": "metaAndAssetCtxs", "dex": "xyz"},
                timeout=5,
            )
            data = resp.json()
            if isinstance(data, list) and len(data) >= 2:
                universe = data[0].get("universe", [])
                contexts = data[1]
                for idx, asset in enumerate(universe):
                    if idx < len(contexts):
                        c = f"xyz:{asset['name']}"
                        mark_px = float(contexts[idx].get("markPx") or 0)
                        if mark_px > 0:
                            self._xyz_mids_cache[c] = mark_px
                self._xyz_mids_ts = time.time()
        except Exception as e:
            logger.error(f"Failed to refresh XYZ prices: {e}")
            # Fallback: single l2Book call for this specific coin
            try:
                resp = requests.post(
                    INFO_URL,
                    json={"type": "l2Book", "coin": coin},
                    timeout=5,
                )
                book = resp.json()
                levels = book.get("levels", [])
                if len(levels) >= 2 and levels[0] and levels[1]:
                    best_bid = float(levels[0][0]["px"])
                    best_ask = float(levels[1][0]["px"])
                    return (best_bid + best_ask) / 2
            except Exception as e2:
                logger.error(f"Fallback XYZ price fetch failed for {coin}: {e2}")

        return self._xyz_mids_cache.get(coin, 0.0)

    # -- Scaling ----------------------------------------------------

    def target_position_to_desired_size(
        self,
        coin: str,
        target_size: float,
        target_equity: float,
    ) -> float:
        """Convert target's absolute position into our desired absolute position."""
        if abs(target_size) < 1e-10:
            return 0.0

        if self.config.scaling_mode == "proportional":
            our_eq = self.get_our_equity()
            ratio = (our_eq / target_equity) if target_equity > 0 else 0
            desired = target_size * ratio
        elif self.config.scaling_mode == "fixed_ratio":
            desired = target_size * self.config.fixed_ratio
        elif self.config.scaling_mode == "fixed_size":
            desired = self.config.fixed_size * (1.0 if target_size > 0 else -1.0)
        elif self.config.scaling_mode == "fixed_notional":
            mid = self.get_mid_price(coin)
            if mid <= 0:
                logger.error(f"No price data for {coin}, cannot compute desired size")
                return 0.0
            desired = (self.config.fixed_notional_usd / mid) * (
                1.0 if target_size > 0 else -1.0
            )
        else:
            logger.error(f"Unknown scaling mode: {self.config.scaling_mode}")
            return 0.0

        return desired

    def scale_delta(
        self,
        change: PositionChange,
        target_equity: float,
    ) -> float:
        """Convert the target's raw delta into the size we should trade."""
        raw = change.delta

        if self.config.scaling_mode == "proportional":
            our_eq = self.get_our_equity()
            ratio = (our_eq / target_equity) if target_equity > 0 else 0
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

    # -- Execution --------------------------------------------------

    def execute(
        self,
        coin: str,
        size_delta: float,
        dry_run: bool = True,
    ) -> Optional[TradeResult]:
        """
        Place an IOC limit order to mirror a position change.

        Routes xyz: coins to _execute_xyz(), standard coins to _execute_standard().
        """
        if coin.startswith("xyz:"):
            return self._execute_xyz(coin, size_delta, dry_run)
        return self._execute_standard(coin, size_delta, dry_run)

    def _execute_standard(
        self,
        coin: str,
        size_delta: float,
        dry_run: bool,
    ) -> Optional[TradeResult]:
        """Standard HL perp: IOC limit order via the SDK."""
        if abs(size_delta) < 1e-10:
            return None

        is_buy = size_delta > 0
        abs_size = abs(size_delta)
        side = "BUY" if is_buy else "SELL"

        decimals = self._sz_decimals.get(coin, 5)
        abs_size = round(abs_size, decimals)
        if abs_size == 0:
            logger.debug(f"Size rounded to zero for {coin}, skipping")
            return None

        mid = self.get_mid_price(coin)
        if mid <= 0:
            logger.error(f"No price data for {coin}, cannot execute")
            return TradeResult(False, coin, side, abs_size, error="no price data")

        # Position cap
        signed_delta = abs_size if is_buy else -abs_size
        current_size = self.get_our_positions().get(coin, 0.0)
        max_abs_pos = self.config.max_position_usd / mid if self.config.max_position_usd > 0 else float("inf")
        proposed_size = current_size + signed_delta
        clipped_size = max(-max_abs_pos, min(max_abs_pos, proposed_size))
        signed_delta = clipped_size - current_size
        if abs(signed_delta) < 1e-10:
            logger.warning(
                f"Position cap blocks trade: {coin} current={current_size:+.6f}, "
                f"requested_delta={proposed_size - current_size:+.6f}, "
                f"max_abs={max_abs_pos:.6f}"
            )
            return None
        if abs((proposed_size - current_size) - signed_delta) > 1e-10:
            logger.warning(
                f"Position cap clipped trade: {coin} "
                f"{proposed_size - current_size:+.6f} -> {signed_delta:+.6f}"
            )

        is_buy = signed_delta > 0
        side = "BUY" if is_buy else "SELL"
        abs_size = abs(signed_delta)

        notional = abs_size * mid
        if notional < self.config.min_trade_size_usd:
            logger.debug(
                f"Trade too small: {abs_size} {coin} = ${notional:.2f} "
                f"(min ${self.config.min_trade_size_usd})"
            )
            return None

        # Daily limit
        now = time.time()
        day_ago = now - 86400
        while self._trade_timestamps and self._trade_timestamps[0] < day_ago:
            self._trade_timestamps.popleft()
        if len(self._trade_timestamps) >= self.config.max_daily_trades:
            logger.critical("Daily trade limit reached - refusing to execute")
            return TradeResult(False, coin, side, abs_size, error="daily limit")

        limit_px = self._slippage_ioc_price(coin, is_buy, mid)

        if dry_run:
            logger.info(
                f"[DRY RUN] {side} {abs_size} {coin} @ ~${self._fmt_price(limit_px)} "
                f"(mid=${self._fmt_price(mid)}, notional=${notional:,.0f})"
            )
            return TradeResult(True, coin, side, abs_size, abs_size, mid)

        logger.warning(
            f"EXECUTING: {side} {abs_size} {coin} @ ${self._fmt_price(limit_px)} "
            f"(mid=${self._fmt_price(mid)}, slippage={self.config.slippage_bps}bps)"
        )

        try:
            result = self.exchange.order(
                coin, is_buy, abs_size, limit_px,
                {"limit": {"tif": "Ioc"}},
                reduce_only=False,
            )
            self._trade_timestamps.append(now)

            if result and result.get("status") == "ok":
                statuses = (
                    result.get("response", {})
                    .get("data", {})
                    .get("statuses", [])
                )
                if statuses:
                    st = statuses[0]
                    if "filled" in st:
                        fill = st["filled"]
                        avg = float(fill.get("avgPx", 0))
                        tsz = float(fill.get("totalSz", 0))
                        oid = fill.get("oid", 0)
                        self._positions_ts = 0.0
                        logger.success(
                            f"FILLED: {side} {tsz} {coin} @ ${self._fmt_price(avg)} "
                            f"(oid={oid})"
                        )
                        return TradeResult(True, coin, side, abs_size, tsz, avg, oid)
                    if "resting" in st:
                        oid = st["resting"].get("oid", 0)
                        self._positions_ts = 0.0
                        logger.warning(f"Order resting (unexpected for IOC): oid={oid}")
                        return TradeResult(True, coin, side, abs_size, 0, 0, oid)
                    if "error" in st:
                        err = st["error"]
                        logger.error(f"Order rejected: {err}")
                        return TradeResult(False, coin, side, abs_size, error=err)

            logger.error(f"Unexpected order response: {result}")
            return TradeResult(False, coin, side, abs_size, error=str(result))

        except Exception as e:
            logger.error(f"Execution exception: {e}")
            return TradeResult(False, coin, side, abs_size, error=str(e))

    def _execute_xyz(
        self,
        coin: str,
        size_delta: float,
        dry_run: bool,
    ) -> Optional[TradeResult]:
        """XYZ HIP-3: IOC limit order via raw sign_l1_action."""
        if abs(size_delta) < 1e-10:
            return None

        if coin not in XYZ_ASSET_IDS:
            logger.error(f"No asset ID configured for XYZ coin: {coin}")
            return TradeResult(False, coin, "UNKNOWN", abs(size_delta), error="no asset ID")

        is_buy = size_delta > 0
        abs_size = abs(size_delta)
        side = "BUY" if is_buy else "SELL"

        # Round to XYZ sz_decimals
        decimals = self._xyz_sz_decimals.get(coin, 4)
        abs_size = round(abs_size, decimals)
        if abs_size == 0:
            logger.debug(f"XYZ size rounded to zero for {coin}, skipping")
            return None

        mid = self.get_mid_price(coin)
        if mid <= 0:
            logger.error(f"No XYZ price data for {coin}, cannot execute")
            return TradeResult(False, coin, side, abs_size, error="no price data")

        # Position cap
        signed_delta = abs_size if is_buy else -abs_size
        current_size = self.get_our_positions().get(coin, 0.0)
        max_abs_pos = self.config.max_position_usd / mid if self.config.max_position_usd > 0 else float("inf")
        proposed_size = current_size + signed_delta
        clipped_size = max(-max_abs_pos, min(max_abs_pos, proposed_size))
        signed_delta = clipped_size - current_size
        if abs(signed_delta) < 1e-10:
            logger.warning(f"Position cap blocks XYZ trade: {coin} current={current_size:+.6f}")
            return None

        is_buy = signed_delta > 0
        side = "BUY" if is_buy else "SELL"
        abs_size = abs(signed_delta)

        notional = abs_size * mid
        if notional < self.config.min_trade_size_usd:
            logger.debug(f"XYZ trade too small: {abs_size} {coin} = ${notional:.2f}")
            return None

        # Daily limit
        now = time.time()
        day_ago = now - 86400
        while self._trade_timestamps and self._trade_timestamps[0] < day_ago:
            self._trade_timestamps.popleft()
        if len(self._trade_timestamps) >= self.config.max_daily_trades:
            logger.critical("Daily trade limit reached - refusing to execute")
            return TradeResult(False, coin, side, abs_size, error="daily limit")

        # Price: aggressive IOC through the spread
        slip = self.config.slippage_bps / 10_000
        limit_px = mid * (1 + slip) if is_buy else mid * (1 - slip)

        # XYZ price formatting: 0 decimals for assets >= $10, 2 decimals otherwise
        px_decimals = 0 if mid >= 10 else 2
        price_str = f"{limit_px:.{px_decimals}f}"

        if dry_run:
            logger.info(
                f"[DRY RUN XYZ] {side} {abs_size} {coin} @ ~${price_str} "
                f"(mid=${self._fmt_price(mid)}, notional=${notional:,.0f})"
            )
            return TradeResult(True, coin, side, abs_size, abs_size, mid)

        logger.warning(
            f"EXECUTING XYZ: {side} {abs_size} {coin} @ ${price_str} "
            f"(mid=${self._fmt_price(mid)}, slippage={self.config.slippage_bps}bps)"
        )

        asset_id = XYZ_ASSET_IDS[coin]

        order = {
            "a": asset_id,
            "b": is_buy,
            "p": price_str,
            "s": str(abs_size),
            "r": False,
            "t": {"limit": {"tif": "Ioc"}},
        }
        action = {
            "type": "order",
            "orders": [order],
            "grouping": "na",
        }

        timestamp = int(time.time() * 1000)
        expires_after = timestamp + 300000  # 5 minutes

        try:
            signature = sign_l1_action(
                wallet=self._account,
                action=action,
                active_pool=None,
                nonce=timestamp,
                expires_after=expires_after,
                is_mainnet=True,
            )

            payload = {
                "action": action,
                "nonce": timestamp,
                "signature": signature,
                "vaultAddress": None,
                "expiresAfter": expires_after,
            }

            result = self.exchange.post("/exchange", payload)
            self._trade_timestamps.append(now)

            if result and result.get("status") == "ok":
                statuses = (
                    result.get("response", {})
                    .get("data", {})
                    .get("statuses", [])
                )
                if statuses:
                    st = statuses[0]
                    if "filled" in st:
                        fill = st["filled"]
                        avg = float(fill.get("avgPx", 0))
                        tsz = float(fill.get("totalSz", 0))
                        oid = fill.get("oid", 0)
                        self._positions_ts = 0.0
                        logger.success(
                            f"FILLED XYZ: {side} {tsz} {coin} @ ${self._fmt_price(avg)} "
                            f"(oid={oid})"
                        )
                        return TradeResult(True, coin, side, abs_size, tsz, avg, oid)
                    if "resting" in st:
                        oid = st["resting"].get("oid", 0)
                        self._positions_ts = 0.0
                        logger.warning(f"XYZ order resting (unexpected for IOC): oid={oid}")
                        return TradeResult(True, coin, side, abs_size, 0, 0, oid)
                    if "error" in st:
                        err = st["error"]
                        logger.error(f"XYZ order rejected: {err}")
                        return TradeResult(False, coin, side, abs_size, error=err)

            logger.error(f"Unexpected XYZ order response: {result}")
            return TradeResult(False, coin, side, abs_size, error=str(result))

        except Exception as e:
            logger.error(f"XYZ execution exception: {e}")
            return TradeResult(False, coin, side, abs_size, error=str(e))

    # -- Internal helpers -------------------------------------------

    def _slippage_ioc_price(self, coin: str, is_buy: bool, mid: float) -> float:
        """
        IOC price for standard perps: 5 significant figures,
        max (6 - szDecimals) decimal places.
        """
        slip = self.config.slippage_bps / 10_000
        px = mid * (1 + slip) if is_buy else mid * (1 - slip)
        px = float(f"{px:.5g}")
        max_decimals = max(0, 6 - int(self._sz_decimals.get(coin, 5)))
        return round(px, max_decimals)

    def _set_leverage(self, coin: str) -> None:
        """Set leverage for a standard perp coin via SDK."""
        try:
            self.exchange.update_leverage(
                self.config.leverage, coin, is_cross=self.config.is_cross,
            )
            mode = "cross" if self.config.is_cross else "isolated"
            logger.info(f"Leverage set: {coin} {self.config.leverage}x ({mode})")
        except Exception as e:
            logger.warning(f"Could not set leverage for {coin}: {e} (may already be set)")

    def _set_xyz_leverage(self, coin: str) -> None:
        """Set leverage for an XYZ HIP-3 coin via raw sign_l1_action."""
        if coin not in XYZ_ASSET_IDS:
            logger.warning(f"Cannot set leverage - unknown XYZ coin: {coin}")
            return

        asset_id = XYZ_ASSET_IDS[coin]
        action = {
            "type": "updateLeverage",
            "asset": asset_id,
            "isCross": False,  # XYZ always uses isolated margin
            "leverage": self.config.leverage,
        }

        timestamp = int(time.time() * 1000)
        expires_after = timestamp + 300000

        try:
            signature = sign_l1_action(
                wallet=self._account,
                action=action,
                active_pool=None,
                nonce=timestamp,
                expires_after=expires_after,
                is_mainnet=True,
            )
            payload = {
                "action": action,
                "nonce": timestamp,
                "signature": signature,
                "vaultAddress": None,
                "expiresAfter": expires_after,
            }
            result = self.exchange.post("/exchange", payload)
            if result and result.get("status") == "ok":
                logger.info(f"XYZ leverage set: {coin} {self.config.leverage}x (isolated)")
            else:
                logger.warning(f"XYZ leverage update for {coin}: {result}")
        except Exception as e:
            logger.warning(f"Could not set XYZ leverage for {coin}: {e} (may already be set)")

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return "429" in text or "too many requests" in text

    def _build_info_with_retry(self, base_url: str) -> Info:
        """Retry Info client init to survive transient 429 responses."""
        delay = 1.0
        last_exc: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                return Info(base_url, skip_ws=True)
            except Exception as e:
                last_exc = e
                if not self._is_rate_limit_error(e) or attempt == 3:
                    break
                logger.warning(
                    f"Info init rate-limited (attempt {attempt}/3). "
                    f"Retrying in {delay:.1f}s"
                )
                time.sleep(delay)
                delay = min(delay * 2, 4.0)
        if last_exc:
            raise last_exc
        raise RuntimeError("Info init failed without an exception")

    @staticmethod
    def _fmt_price(price: float) -> str:
        """Render prices with enough precision for sub-$1 perps and large-dollar assets."""
        if price >= 100:
            return f"{price:,.1f}"
        if price >= 1:
            return f"{price:,.3f}"
        if price >= 0.1:
            return f"{price:,.4f}"
        if price >= 0.01:
            return f"{price:,.5f}"
        return f"{price:,.6f}"
