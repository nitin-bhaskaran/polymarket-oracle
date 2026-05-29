"""
Backtest harness — evaluate the edge/sizing premise on historical markets.

The whole thesis of this bot is "the AI's probability estimate is, on average,
closer to the truth than the market price, often enough and by enough to
overcome spread and fees." That is an empirical claim. This harness lets you
test it on resolved markets *before* risking capital, and re-test it whenever
you change the sizing or edge logic.

It deliberately does NOT call any live API. You feed it a dataset of resolved
markets, each with:
  - the market price at the decision time (yes_price),
  - the AI's estimated probability and confidence at that time,
  - the actual resolved outcome (1 for YES, 0 for NO),
  - optionally a spread.

It then runs each row through the SAME sizing logic the live bot uses
(core.sizing) and simulates the payoff: a winning binary token settles at 1.0,
a losing one at 0.0. The output is a calibration + P&L report.

Dataset formats accepted:
  - JSON: a list of objects with the fields above.
  - CSV: columns yes_price, ai_probability, confidence, outcome[, spread, question].

Example:
    python -m core.backtest --data data/backtest_sample.json --capital 130 \\
        --min-edge 0.05 --kelly-fraction 0.25

This is a measurement tool, not a trading component; it is imported by nothing
in the live path.
"""

import argparse
import csv
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.money import dec, usdc
from core.sizing import SizingConfig, SizingInputs, compute_position_size

logger = logging.getLogger("backtest")


@dataclass
class BacktestRow:
    yes_price: float
    ai_probability: float   # AI estimate of P(YES)
    confidence: float
    outcome: int            # 1 if YES resolved true, 0 if NO
    spread: float = 0.0
    question: str = ""


@dataclass
class BacktestResult:
    n_markets: int
    n_traded: int
    n_wins: int
    n_losses: int
    starting_capital: float
    ending_capital: float
    total_pnl: float
    roi_pct: float
    win_rate_pct: float
    avg_edge_traded: float
    brier_score: float      # calibration of the AI probabilities (lower is better)

    def render(self) -> str:
        lines = [
            "=" * 56,
            "BACKTEST RESULT",
            "=" * 56,
            f"Markets in dataset      : {self.n_markets}",
            f"Markets traded          : {self.n_traded}",
            f"Wins / Losses           : {self.n_wins} / {self.n_losses}",
            f"Win rate                : {self.win_rate_pct:.1f}%",
            f"Avg edge on traded      : {self.avg_edge_traded:.1%}",
            f"Starting capital        : ${self.starting_capital:.2f}",
            f"Ending capital          : ${self.ending_capital:.2f}",
            f"Total P&L               : ${self.total_pnl:+.2f}",
            f"ROI                     : {self.roi_pct:+.1f}%",
            f"AI Brier score          : {self.brier_score:.4f}  (0=perfect, 0.25=coin flip)",
            "=" * 56,
        ]
        return "\n".join(lines)


def load_dataset(path: str) -> list[BacktestRow]:
    """Load a backtest dataset from JSON or CSV."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Dataset not found: {path}")

    rows: list[BacktestRow] = []
    if p.suffix.lower() == ".json":
        raw = json.loads(p.read_text())
        for r in raw:
            rows.append(BacktestRow(
                yes_price=float(r["yes_price"]),
                ai_probability=float(r["ai_probability"]),
                confidence=float(r.get("confidence", 0.7)),
                outcome=int(r["outcome"]),
                spread=float(r.get("spread", 0.0)),
                question=r.get("question", ""),
            ))
    elif p.suffix.lower() in (".csv", ".tsv"):
        delim = "\t" if p.suffix.lower() == ".tsv" else ","
        with open(p, newline="") as f:
            reader = csv.DictReader(f, delimiter=delim)
            for r in reader:
                rows.append(BacktestRow(
                    yes_price=float(r["yes_price"]),
                    ai_probability=float(r["ai_probability"]),
                    confidence=float(r.get("confidence", 0.7)),
                    outcome=int(r["outcome"]),
                    spread=float(r.get("spread", 0.0) or 0.0),
                    question=r.get("question", ""),
                ))
    else:
        raise ValueError(f"Unsupported dataset format: {p.suffix}")
    return rows


def run_backtest(
    rows: list[BacktestRow],
    starting_capital: float,
    min_edge: float,
    sizing_cfg: SizingConfig,
    fee_pct: float = 0.0,
) -> BacktestResult:
    """
    Replay rows through the sizing logic and simulate binary payoffs.

    Capital compounds across the dataset in the order given (so order matters
    only for capital availability, not for correctness of the premise). Each
    traded position spends the sized amount; the outcome token settles at 1.0
    if our side won and 0.0 if it lost.
    """
    capital = starting_capital
    n_traded = n_wins = n_losses = 0
    edge_sum = 0.0
    brier_sum = 0.0

    for row in rows:
        # Brier over ALL rows (calibration of the AI, independent of trading).
        brier_sum += (row.ai_probability - row.outcome) ** 2

        edge = row.ai_probability - row.yes_price  # signed, for YES
        if abs(edge) < min_edge:
            continue

        # Decide side and the fair prob / price / win condition for that side.
        if edge > 0:
            entry_price = row.yes_price
            fair_prob = row.ai_probability
            side_won = row.outcome == 1
        else:
            entry_price = 1.0 - row.yes_price  # NO token price
            fair_prob = 1.0 - row.ai_probability
            side_won = row.outcome == 0

        spend = compute_position_size(
            SizingInputs(
                available_capital=capital,
                entry_price=entry_price,
                fair_probability=fair_prob,
                confidence=row.confidence,
                spread=row.spread,
            ),
            sizing_cfg,
        )
        if spend <= 0:
            continue

        n_traded += 1
        edge_sum += abs(edge)

        shares_bought = spend / entry_price if entry_price > 0 else 0.0
        # Binary settlement: each share pays 1.0 if our side won, else 0.0.
        payoff = shares_bought * (1.0 if side_won else 0.0)
        fee = spend * fee_pct
        pnl = payoff - spend - fee

        capital = usdc(dec(capital) + dec(pnl))
        if pnl >= 0:
            n_wins += 1
        else:
            n_losses += 1

    total_pnl = usdc(dec(capital) - dec(starting_capital))
    roi = (total_pnl / starting_capital * 100) if starting_capital > 0 else 0.0
    win_rate = (n_wins / n_traded * 100) if n_traded else 0.0
    avg_edge = (edge_sum / n_traded) if n_traded else 0.0
    brier = (brier_sum / len(rows)) if rows else 0.0

    return BacktestResult(
        n_markets=len(rows),
        n_traded=n_traded,
        n_wins=n_wins,
        n_losses=n_losses,
        starting_capital=starting_capital,
        ending_capital=capital,
        total_pnl=total_pnl,
        roi_pct=roi,
        win_rate_pct=win_rate,
        avg_edge_traded=avg_edge,
        brier_score=brier,
    )


def main():
    parser = argparse.ArgumentParser(description="Backtest the edge/sizing premise")
    parser.add_argument("--data", required=True, help="Path to JSON or CSV dataset")
    parser.add_argument("--capital", type=float, default=130.0)
    parser.add_argument("--min-edge", type=float, default=0.05)
    parser.add_argument("--max-position-pct", type=float, default=0.10)
    parser.add_argument("--kelly-fraction", type=float, default=0.25)
    parser.add_argument("--no-kelly", action="store_true", help="Use flat confidence sizing")
    parser.add_argument("--fee-pct", type=float, default=0.0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    rows = load_dataset(args.data)
    sizing_cfg = SizingConfig(
        max_position_pct=args.max_position_pct,
        kelly_fraction=args.kelly_fraction,
        min_trade_usd=1.0,
        use_kelly=not args.no_kelly,
    )
    result = run_backtest(
        rows,
        starting_capital=args.capital,
        min_edge=args.min_edge,
        sizing_cfg=sizing_cfg,
        fee_pct=args.fee_pct,
    )
    print(result.render())


if __name__ == "__main__":
    main()
