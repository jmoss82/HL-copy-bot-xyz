#!/usr/bin/env python3
"""
HIP-3 copy bot.
"""
import asyncio
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from config import CopyBotConfig, load_config, validate_config
from copier import TradeCopier
from tracker import PositionTracker


@dataclass
class LifecycleSession:
    coin: str
    direction: int
    target_anchor_size: float
    our_anchor_size: float
    copy_ratio: float
    last_target_size: float
    opened_at: float


class CopyBot:
    def __init__(self, config: CopyBotConfig):
        self.config = config
        self.tracker = PositionTracker(config.target_address)
        self.copier = TradeCopier(config)
        self._sim_positions: dict[str, float] = {}
        self._lifecycle_sessions: dict[str, LifecycleSession] = {}
        self._startup_locked_coins: set[str] = set()

        self.running = False
        self.start_time: float = 0.0
        self.trades_executed: int = 0

        # Exponential backoff on API rate limits / repeated poll failures.
        self._current_interval: float = config.poll_interval_seconds
        self._max_backoff: float = 30.0

    def setup(self) -> None:
        logger.info("Initialising HIP-3 copy bot...")
        validate_config(self.config)
        self.copier.setup()
        self._sim_positions = self.copier.get_our_positions()
        our_equity = self.copier.get_our_equity(force=True)
        logger.info(f"Your account equity: ${our_equity:,.2f}")

    def startup_sync(self) -> None:
        logger.info(f"Polling target HIP-3 wallet: {self.config.target_address}")
        target_positions = self.tracker.poll()
        filtered = self._filter_coins(target_positions)

        if not filtered:
            logger.info("Target has no matching HIP-3 positions - starting clean")
            self.tracker.seed({})
            return

        for coin, data in filtered.items():
            size = data["size"]
            side = "LONG" if size > 0 else "SHORT"
            logger.info(
                f"  Target: {side} {abs(size):.6f} {coin} "
                f"(entry ${self._fmt_price(data['entry_px'])}, {data['leverage']}x)"
            )

        if self.config.reconcile_mode == "lifecycle":
            self._startup_sync_lifecycle(filtered)
        elif self.config.sync_on_startup:
            logger.info("sync_on_startup=True - matching target positions now")
            our_positions = self.copier.get_our_positions()
            for coin, data in filtered.items():
                target_size = data["size"]
                our_size = our_positions.get(coin, 0.0)
                needed = self.copier.target_position_to_desired_size(
                    coin, target_size, self.tracker.target_equity
                )
                gap = needed - our_size
                if abs(gap) < 1e-10:
                    logger.info(f"  {coin}: already in sync (size={our_size:.6f})")
                    continue
                logger.info(
                    f"  {coin}: need {needed:+.6f}, have {our_size:+.6f}, gap {gap:+.6f}"
                )
                result = self.copier.execute(coin, gap, dry_run=self.config.dry_run)
                if result and result.success:
                    self._record_position_change(coin, gap)
                    self.trades_executed += 1
        else:
            self._startup_locked_coins = set(filtered.keys())
            for coin in self._startup_locked_coins:
                logger.info(f"  {coin}: target already in position - locked until they close")

        self.tracker.seed(target_positions)

    async def run(self) -> None:
        self.running = True
        self.start_time = time.time()
        heartbeat_interval = 60
        last_heartbeat = time.time()

        logger.info(
            f"Entering main loop | poll={self.config.poll_interval_seconds}s "
            f"coins={self.config.coins_to_copy} "
            f"mode={'DRY RUN' if self.config.dry_run else 'LIVE'}"
        )

        while self.running:
            try:
                target_positions = self.tracker.poll()

                if self.tracker._consecutive_errors > 0:
                    new_interval = min(self._current_interval * 2, self._max_backoff)
                    if new_interval != self._current_interval:
                        self._current_interval = new_interval
                        logger.warning(
                            f"Rate-limited / polling failed - backing off to "
                            f"{self._current_interval:.0f}s polling"
                        )
                elif self._current_interval > self.config.poll_interval_seconds:
                    logger.info(
                        f"API recovered - resuming "
                        f"{self.config.poll_interval_seconds}s polling"
                    )
                    self._current_interval = self.config.poll_interval_seconds

                filtered = self._filter_coins(target_positions)

                if self.config.reconcile_mode == "delta":
                    changes = self.tracker.diff(filtered, self.config.coins_to_copy)
                    for change in changes:
                        logger.warning(f"TARGET MOVED: {change}")
                        scaled = self.copier.scale_delta(change, self.tracker.target_equity)
                        if abs(scaled) < 1e-10:
                            continue
                        result = self.copier.execute(
                            change.coin,
                            scaled,
                            dry_run=self.config.dry_run,
                        )
                        if result and result.success:
                            self._record_position_change(change.coin, scaled)
                            self.trades_executed += 1
                else:
                    self.tracker.seed(filtered)
                    self._release_startup_locks(filtered)
                    our_positions = self._effective_positions()
                    for coin in self._coins_to_reconcile(filtered, our_positions):
                        if coin in self._startup_locked_coins:
                            continue
                        if self.config.reconcile_mode == "lifecycle":
                            self._reconcile_lifecycle_coin(
                                coin,
                                filtered.get(coin, {}).get("size", 0.0),
                                our_positions.get(coin, 0.0),
                            )
                            continue
                        target_size = filtered.get(coin, {}).get("size", 0.0)
                        desired_size = self.copier.target_position_to_desired_size(
                            coin, target_size, self.tracker.target_equity
                        )
                        current_size = our_positions.get(coin, 0.0)
                        delta = desired_size - current_size
                        if abs(delta) < 1e-10:
                            continue
                        mid = self.copier.get_mid_price(coin)
                        if mid > 0 and abs(delta) * mid < self.config.min_trade_size_usd:
                            continue
                        logger.warning(
                            f"REBALANCE {coin}: target={target_size:+.6f}, "
                            f"desired={desired_size:+.6f}, ours={current_size:+.6f}"
                        )
                        result = self.copier.execute(
                            coin,
                            delta,
                            dry_run=self.config.dry_run,
                        )
                        if result and result.success:
                            self._record_position_change(coin, delta)
                            self.trades_executed += 1

                now = time.time()
                if now - last_heartbeat >= heartbeat_interval:
                    self._heartbeat(filtered)
                    last_heartbeat = now

                await asyncio.sleep(self._current_interval)
            except Exception as e:
                logger.error(f"Main-loop error: {e}")
                logger.exception(e)
                await asyncio.sleep(5)

    def stop(self) -> None:
        logger.info("Stopping HIP-3 copy bot...")
        self.running = False
        self._print_summary()
        logger.info("HIP-3 copy bot stopped.")

    def _filter_coins(self, positions: dict) -> dict:
        if "*" in self.config.coins_to_copy:
            return positions
        return {k: v for k, v in positions.items() if k in self.config.coins_to_copy}

    def _heartbeat(self, target_positions: dict) -> None:
        runtime = time.time() - self.start_time
        hours = int(runtime // 3600)
        mins = int((runtime % 3600) // 60)
        parts = [f"Runtime: {hours}h{mins:02d}m", f"Trades: {self.trades_executed}"]
        if self.config.reconcile_mode == "lifecycle":
            parts.append(f"Sessions: {len(self._lifecycle_sessions)}")
        for coin, data in target_positions.items():
            size = data["size"]
            side = "L" if size > 0 else "S"
            parts.append(f"Target {coin}: {side}{abs(size):.4f}")
        our = self._effective_positions()
        for coin in self.config.coins_to_copy:
            our_size = our.get(coin, 0.0)
            if abs(our_size) < 1e-10:
                parts.append(f"Ours {coin}: flat")
            else:
                side = "L" if our_size > 0 else "S"
                parts.append(f"Ours {coin}: {side}{abs(our_size):.4f}")
        logger.info("HEARTBEAT | " + " | ".join(parts))

    def _coins_to_reconcile(self, target_positions: dict, our_positions: dict) -> list[str]:
        if "*" in self.config.coins_to_copy:
            return sorted(set(target_positions.keys()) | set(our_positions.keys()))
        return [coin for coin in self.config.coins_to_copy if coin != "*"]

    def _effective_positions(self) -> dict[str, float]:
        if self.config.dry_run:
            return dict(self._sim_positions)
        return self.copier.get_our_positions()

    def _startup_sync_lifecycle(self, filtered: dict) -> None:
        if self.config.sync_on_startup:
            logger.info("sync_on_startup=True - joining target lifecycle now")
            our_positions = self._effective_positions()
            for coin, data in filtered.items():
                session = self._build_lifecycle_session(coin, data["size"])
                if session is None:
                    continue
                self._lifecycle_sessions[coin] = session
                current_size = our_positions.get(coin, 0.0)
                delta = session.our_anchor_size - current_size
                if abs(delta) < 1e-10:
                    continue
                result = self.copier.execute(coin, delta, dry_run=self.config.dry_run)
                if result and result.success:
                    self._record_position_change(coin, delta)
                    self.trades_executed += 1
        else:
            self._startup_locked_coins = set(filtered.keys())
            for coin in self._startup_locked_coins:
                logger.info(f"  {coin}: target already in position - locked until they close")

    def _release_startup_locks(self, target_positions: dict) -> None:
        for coin in list(self._startup_locked_coins):
            if abs(target_positions.get(coin, {}).get("size", 0.0)) < 1e-10:
                self._startup_locked_coins.discard(coin)
                logger.info(f"{coin}: startup lock released - will follow next entry")

    def _reconcile_lifecycle_coin(
        self,
        coin: str,
        target_size: float,
        current_size: float,
    ) -> None:
        session = self._lifecycle_sessions.get(coin)

        if abs(target_size) < 1e-10:
            if session is None:
                return
            result = self.copier.execute(coin, -current_size, dry_run=self.config.dry_run)
            if result and result.success:
                self._record_position_change(coin, -current_size)
                self.trades_executed += 1
                self._lifecycle_sessions.pop(coin, None)
            elif abs(current_size) < 1e-10:
                self._lifecycle_sessions.pop(coin, None)
            return

        if session is None:
            session = self._build_lifecycle_session(coin, target_size)
            if session is None:
                return
            self._lifecycle_sessions[coin] = session
            delta = session.our_anchor_size - current_size
            if abs(delta) < 1e-10:
                return
            result = self.copier.execute(coin, delta, dry_run=self.config.dry_run)
            if result and result.success:
                self._record_position_change(coin, delta)
                self.trades_executed += 1
            return

        target_direction = 1 if target_size > 0 else -1
        if target_direction != session.direction:
            self._handle_lifecycle_flip(coin, target_size, current_size, session)
            return

        desired_size = target_size * session.copy_ratio
        delta = desired_size - current_size
        if abs(delta) < 1e-10:
            session.last_target_size = target_size
            return
        result = self.copier.execute(coin, delta, dry_run=self.config.dry_run)
        if result and result.success:
            self._record_position_change(coin, delta)
            self.trades_executed += 1
        session.last_target_size = target_size

    def _handle_lifecycle_flip(
        self,
        coin: str,
        target_size: float,
        current_size: float,
        session: LifecycleSession,
    ) -> None:
        if abs(current_size) > 1e-10:
            close_result = self.copier.execute(
                coin, -current_size, dry_run=self.config.dry_run
            )
            if close_result and close_result.success:
                self._record_position_change(coin, -current_size)
                self.trades_executed += 1
                current_size = 0.0
            else:
                return
        self._lifecycle_sessions.pop(coin, None)
        new_session = self._build_lifecycle_session(coin, target_size)
        if new_session is None:
            return
        self._lifecycle_sessions[coin] = new_session
        open_delta = new_session.our_anchor_size - current_size
        if abs(open_delta) < 1e-10:
            return
        open_result = self.copier.execute(coin, open_delta, dry_run=self.config.dry_run)
        if open_result and open_result.success:
            self._record_position_change(coin, open_delta)
            self.trades_executed += 1

    def _build_lifecycle_session(
        self,
        coin: str,
        target_size: float,
    ) -> LifecycleSession | None:
        desired_size = self.copier.target_position_to_desired_size(
            coin,
            target_size,
            self.tracker.target_equity,
        )
        if abs(target_size) < 1e-10 or abs(desired_size) < 1e-10:
            logger.warning(
                f"SESSION OPEN {coin}: could not build lifecycle anchor "
                f"(target={target_size:+.6f}, desired={desired_size:+.6f})"
            )
            return None
        return LifecycleSession(
            coin=coin,
            direction=1 if target_size > 0 else -1,
            target_anchor_size=target_size,
            our_anchor_size=desired_size,
            copy_ratio=desired_size / target_size,
            last_target_size=target_size,
            opened_at=time.time(),
        )

    def _record_position_change(self, coin: str, delta: float) -> None:
        if not self.config.dry_run:
            return
        self._sim_positions[coin] = self._sim_positions.get(coin, 0.0) + delta
        if abs(self._sim_positions[coin]) < 1e-10:
            self._sim_positions.pop(coin, None)

    def _print_summary(self) -> None:
        runtime = time.time() - self.start_time if self.start_time else 0
        hours = int(runtime // 3600)
        mins = int((runtime % 3600) // 60)
        print(f"\n{'=' * 60}")
        print("  HIP-3 Copy Bot Summary")
        print(f"{'=' * 60}")
        print(f"  Mode:          {'DRY RUN' if self.config.dry_run else 'LIVE'}")
        print(
            f"  Target:        {self.config.target_address[:10]}...{self.config.target_address[-6:]}"
        )
        print(f"  Runtime:       {hours}h {mins}m")
        print(f"  Trades:        {self.trades_executed}")
        print(f"  Coins tracked: {', '.join(self.config.coins_to_copy)}")
        print(f"{'=' * 60}\n")

    @staticmethod
    def _fmt_price(price: float) -> str:
        if price >= 100:
            return f"{price:,.1f}"
        if price >= 1:
            return f"{price:,.3f}"
        if price >= 0.1:
            return f"{price:,.4f}"
        if price >= 0.01:
            return f"{price:,.5f}"
        return f"{price:,.6f}"


async def main() -> None:
    cfg = load_config()

    bot_dir = Path(__file__).parent
    log_dir = bot_dir / "logs"
    log_dir.mkdir(exist_ok=True)

    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
        level=cfg.log_level,
    )
    logger.add(
        str(log_dir / "copy_bot_hip3_{time}.log"),
        rotation="1 day",
        retention="14 days",
        level="DEBUG",
    )

    mode_label = "DRY RUN" if cfg.dry_run else "LIVE TRADING"
    print(
        f"""
    +======================================================+
    |         HYPERLIQUID HIP-3 COPY BOT                  |
    |         Mode: {mode_label: <39}|
    +======================================================+
    """
    )

    logger.info(f"Target:    {cfg.target_address}")
    logger.info(f"Coins:     {cfg.coins_to_copy}")
    logger.info(f"Scaling:   {cfg.scaling_mode}")
    logger.info(f"Copy mode: {cfg.reconcile_mode}")
    logger.info(f"Leverage:  {cfg.leverage}x isolated")
    logger.info(f"Polling:   every {cfg.poll_interval_seconds}s")
    logger.info(f"Slippage:  {cfg.slippage_bps} bps")
    logger.info(f"Dry run:   {cfg.dry_run}")

    bot = CopyBot(cfg)

    def handle_signal(sig, _frame):
        logger.info(f"Signal {sig} received - shutting down")
        bot.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        bot.setup()
        bot.startup_sync()
        await bot.run()
    except KeyboardInterrupt:
        bot.stop()
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        bot.stop()
        raise


if __name__ == "__main__":
    asyncio.run(main())
