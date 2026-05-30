"""
Betfair paper-trading entry point.

Modes:
  --scan-once : log in, scan markets once, print what was found, exit (smoke test).
  --assess-once : scan + assess one cycle's markets, print edges, place no bets.
  --paper     : run the paper-trading loop continuously (default).

Reads config/config.yaml plus .env overrides for secrets. No real bets are ever
placed; this is the validation instrument.

Run analysis separately:  python -m core.paper_analysis
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import yaml

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from core.betfair_client import BetfairClient
from core.betfair_scanner import BetfairScanner
from core.betfair_assessor import BetfairAssessor
from core.betfair_assessor2 import TwoStageAssessor
from core.assessment_cache import AssessmentGovernor
from core.betfair_paper import BetfairPaperTrader

logger = logging.getLogger("betfair.main")


def setup_logging(config: dict):
    level = getattr(logging, config.get("logging", {}).get("level", "INFO").upper())
    Path("logs").mkdir(exist_ok=True)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler("logs/betfair.log", encoding="utf-8")],
    )


def _env_overrides(config: dict):
    bf = config.setdefault("betfair", {})
    ac = config.setdefault("anthropic", {})
    tg = config.setdefault("telegram", {})
    mappings = [
        ("BETFAIR_APP_KEY", bf, "app_key"),
        ("BETFAIR_USERNAME", bf, "username"),
        ("BETFAIR_PASSWORD", bf, "password"),
        ("ANTHROPIC_API_KEY", ac, "api_key"),
        ("TELEGRAM_BOT_TOKEN", tg, "bot_token"),
        ("TELEGRAM_CHAT_ID", tg, "chat_id"),
    ]
    for env_name, section, key in mappings:
        v = os.getenv(env_name)
        if v:
            section[key] = v
    # Map the betfair_scanner block into the scanner key the scanner reads.
    if "betfair_scanner" in config:
        config["scanner"] = {**config.get("scanner", {}), **config["betfair_scanner"]}


def load_config(path: str = "config/config.yaml") -> dict:
    if load_dotenv:
        load_dotenv()
    with open(path) as f:
        config = yaml.safe_load(f)
    _env_overrides(config)
    return config


def build(config):
    client = BetfairClient(config)
    scanner = BetfairScanner(config, client=client)
    assessor = BetfairAssessor(config)  # single-stage fallback
    two_stage = TwoStageAssessor(config)
    governor = AssessmentGovernor(config)
    trader = BetfairPaperTrader(config, scanner, assessor,
                                two_stage=two_stage, governor=governor)
    return client, scanner, assessor, trader, two_stage, governor


def scan_once(config):
    client, scanner, assessor, trader, two_stage, governor = build(config)
    if not client.login():
        logger.error("Login failed — check credentials and account status")
        return
    markets = scanner.scan()
    logger.info(f"Found {len(markets)} markets")
    for m in markets[:20]:
        rs = ", ".join(f"{r.name}@{r.best_back}" for r in m.runners[:4] if r.best_back)
        logger.info(f"  [{m.sport}] {m.event_name} — {m.market_name} "
                    f"(matched £{m.total_matched:,.0f}, overround {m.overround:.3f}) | {rs}")


def assess_once(config):
    client, scanner, assessor, trader, two_stage, governor = build(config)
    if not client.login():
        logger.error("Login failed")
        return
    markets = scanner.scan()
    logger.info(f"Assessing {len(markets)} markets...")
    for m in markets[:10]:
        for a in assessor.assess_market(m):
            logger.info(f"  {a.runner_name} ({m.market_name}): AI {a.estimated_probability:.1%} "
                        f"vs fair {a.market_fair_prob:.1%} | edge {a.edge:+.1%} -> "
                        f"{a.recommended_side.value if a.recommended_side else '-'}")


def run_paper(config):
    client, scanner, assessor, trader, two_stage, governor = build(config)
    if not client.login():
        logger.error("Login failed")
        return
    interval = config.get("scanner", {}).get("scan_interval", 300)
    logger.info(f"Starting Betfair PAPER trading loop (interval {interval}s). No real bets.")
    cycle = 0
    try:
        while True:
            cycle += 1
            logger.info(f"=== Paper cycle #{cycle} ===")
            try:
                trader.run_cycle()
            except Exception as e:
                logger.error(f"Cycle error: {e}", exc_info=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        logger.info("Shutdown requested")
    finally:
        client.close()


def deep_once(config):
    client, scanner, assessor, trader, two_stage, governor = build(config)
    if not client.login():
        logger.error("Login failed")
        return
    markets = scanner.scan()
    logger.info(f"Two-stage assessing up to 3 markets (triage -> web-search deep)...")
    done = 0
    for m in markets:
        if done >= 3:
            break
        best_edge, _ = two_stage.triage(m)
        logger.info(f"  TRIAGE {m.event_name} — {m.market_name}: best rough edge {best_edge:.1%}")
        if best_edge < two_stage.triage_edge:
            logger.info("    below triage threshold; skipping deep assess")
            continue
        if not governor.can_deep_assess():
            logger.info("    daily deep budget exhausted")
            break
        logger.info("    -> deep web-search assessment:")
        for a in two_stage.deep_assess(m):
            logger.info(f"       {a.runner_name}: AI {a.estimated_probability:.1%} "
                        f"vs fair {a.market_fair_prob:.1%} | edge {a.edge:+.1%} -> "
                        f"{a.recommended_side.value if a.recommended_side else '-'}")
        governor.record_deep_assessment()
        done += 1
    logger.info(f"Deep budget remaining today: {governor.deep_budget_remaining()}")


def main():
    ap = argparse.ArgumentParser(description="Betfair paper trader")
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--scan-once", action="store_true")
    ap.add_argument("--assess-once", action="store_true")
    ap.add_argument("--deep-once", action="store_true")
    ap.add_argument("--paper", action="store_true")
    args = ap.parse_args()

    config = load_config(args.config)
    setup_logging(config)

    if args.scan_once:
        scan_once(config)
    elif args.assess_once:
        assess_once(config)
    elif args.deep_once:
        deep_once(config)
    else:
        run_paper(config)


if __name__ == "__main__":
    main()
