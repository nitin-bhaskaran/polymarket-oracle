"""
Paper-bet analysis — turns weeks of tagged paper bets into a keep/kill/revise
decision.

Reads data/paper_bets.jsonl and reports, overall and sliced by each attribution
dimension (phase, sport, side, style, edge band, confidence band):
  - settled count, fill rate, win rate
  - total and average net P&L, ROI on staked/risked capital
  - Brier score and a simple calibration table (predicted vs actual)

Usage:
    python -m core.paper_analysis [--path data/paper_bets.jsonl] [--min-n 10]

The --min-n guard suppresses slices with too few settled bets to be meaningful
(small samples are noise, not signal). Run this after a few weeks; the slices
with enough volume and positive risk-adjusted return are the features to keep.
"""

import argparse
from collections import defaultdict

from core.paper_store import PaperBetStore
from core.betfair_models import PaperBet, PaperBetStatus


def _stats(bets: list[PaperBet]) -> dict:
    settled = [b for b in bets if b.status == PaperBetStatus.SETTLED]
    filled = [b for b in bets if b.status in (PaperBetStatus.FILLED, PaperBetStatus.SETTLED)]
    placed = len(bets)
    n = len(settled)
    if n == 0:
        return {"placed": placed, "filled": len(filled), "settled": 0}

    wins = sum(1 for b in settled if b.won)
    net = sum((b.net_pnl or 0.0) for b in settled)
    risked = sum(b.liability for b in settled) or 1.0
    brier = sum((b.ai_probability - (1.0 if b.won else 0.0)) ** 2 for b in settled) / n

    return {
        "placed": placed,
        "filled": len(filled),
        "settled": n,
        "fill_rate": len(filled) / placed if placed else 0.0,
        "win_rate": wins / n,
        "net_pnl": net,
        "avg_pnl": net / n,
        "roi_on_risk": net / risked,
        "brier": brier,
    }


def _fmt(s: dict) -> str:
    if s.get("settled", 0) == 0:
        return f"placed={s['placed']} filled={s.get('filled',0)} settled=0 (no settled bets yet)"
    return (f"settled={s['settled']:>4} "
            f"win={s['win_rate']*100:5.1f}% "
            f"net={s['net_pnl']:+8.2f} "
            f"roi/risk={s['roi_on_risk']*100:+6.1f}% "
            f"brier={s['brier']:.3f}")


def _calibration(bets: list[PaperBet], buckets=10) -> str:
    settled = [b for b in bets if b.status == PaperBetStatus.SETTLED]
    if not settled:
        return "  (no settled bets)"
    rows = defaultdict(lambda: [0, 0])  # bucket -> [count, wins]
    for b in settled:
        idx = min(buckets - 1, int(b.ai_probability * buckets))
        rows[idx][0] += 1
        rows[idx][1] += 1 if b.won else 0
    lines = ["  predicted -> actual   (n)"]
    for i in range(buckets):
        if i not in rows:
            continue
        cnt, wins = rows[i]
        lo, hi = i / buckets, (i + 1) / buckets
        actual = wins / cnt if cnt else 0.0
        lines.append(f"  {lo:.1f}-{hi:.1f}: predicted~{(lo+hi)/2:.2f} actual {actual:.2f} (n={cnt})")
    return "\n".join(lines)


def analyse(path: str, min_n: int = 10):
    store = PaperBetStore(path)
    bets = store.all()
    if not bets:
        print("No paper bets recorded yet.")
        return

    print("=" * 64)
    print("PAPER-BET ANALYSIS")
    print("=" * 64)
    print(f"Total bets recorded: {len(bets)}")
    print(f"Status: {store.count_by_status()}")
    print()
    print("OVERALL:")
    print("  " + _fmt(_stats(bets)))
    print()
    print("CALIBRATION (overall):")
    print(_calibration(bets))
    print()

    dimensions = {
        "phase": lambda b: b.phase.value,
        "sport": lambda b: b.sport or "unknown",
        "side": lambda b: b.side.value,
        "style": lambda b: b.style.value,
        "edge_band": lambda b: b.edge_band,
        "confidence_band": lambda b: b.confidence_band,
        "strategy": lambda b: b.strategy,
    }
    for dim, keyfn in dimensions.items():
        groups = defaultdict(list)
        for b in bets:
            groups[keyfn(b)].append(b)
        print(f"BY {dim.upper()}:")
        for key, gb in sorted(groups.items()):
            s = _stats(gb)
            flag = ""
            if s.get("settled", 0) < min_n:
                flag = "  [low n — not yet conclusive]"
            print(f"  {key:>12}: {_fmt(s)}{flag}")
        print()


def main():
    ap = argparse.ArgumentParser(description="Analyse paper-bet results")
    ap.add_argument("--path", default="data/paper_bets.jsonl")
    ap.add_argument("--min-n", type=int, default=10)
    args = ap.parse_args()
    analyse(args.path, args.min_n)


if __name__ == "__main__":
    main()
