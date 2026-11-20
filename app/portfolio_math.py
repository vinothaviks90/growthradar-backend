"""Portfolio, profit-taking and missed-opportunity calculations.

Average-cost (ACB-style) accounting. All functions are pure and unit-tested.
Currency conversion always carries the rate and its timestamp; a missing rate
raises instead of silently assuming 1.0.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Position:
    qty: float = 0.0
    avg_cost: float = 0.0        # per share, position currency
    realized_pnl: float = 0.0

    def buy(self, qty: float, price: float) -> None:
        if qty <= 0 or price < 0:
            raise ValueError("buy qty must be > 0 and price >= 0")
        total_cost = self.avg_cost * self.qty + price * qty
        self.qty += qty
        self.avg_cost = total_cost / self.qty if self.qty else 0.0

    def sell(self, qty: float, price: float) -> float:
        """Sell qty shares at price; returns realized P&L of this sale."""
        if qty <= 0 or qty > self.qty + 1e-9:
            raise ValueError("sell qty must be > 0 and <= held qty")
        pnl = (price - self.avg_cost) * qty
        self.realized_pnl += pnl
        self.qty -= qty
        if self.qty <= 1e-9:
            self.qty = 0.0
            self.avg_cost = 0.0
        return pnl


def pct_change(old: float, new: float) -> float:
    if old == 0:
        raise ValueError("pct_change base is zero")
    return (new - old) / old * 100.0


def unrealized_pnl(pos: Position, current_price: float) -> float:
    return (current_price - pos.avg_cost) * pos.qty


def convert(amount: float, rate: float | None) -> float:
    """Convert amount by rate (target per 1 unit of source). Rate required."""
    if rate is None:
        raise ValueError("exchange rate not available")
    return amount * rate


def milestones_hit(gain_pct: float, milestones: list[float]) -> list[float]:
    """Profit milestones are REVIEW triggers, not sell instructions."""
    return [m for m in sorted(milestones) if gain_pct >= m]


def post_sale_stats(sale_price: float, current_price: float, qty_sold: float) -> dict:
    """Missed-opportunity math.

    The hypothetical figure uses the actual quantity sold and is always
    labelled hypothetical: it is a comparison, never a realized return, and
    it does not assume the user would have held forever.
    """
    move = pct_change(sale_price, current_price)
    hypothetical = (current_price - sale_price) * qty_sold
    return {
        "move_since_sale_pct": round(move, 2),
        "hypothetical_difference": round(hypothetical, 2),
        "hypothetical_label": "Hypothetical comparison on the quantity actually sold — not a realized return",
    }


def recommendation_return(price_then: float, price_now: float) -> float:
    return pct_change(price_then, price_now)


def benchmark_relative(rec_return_pct: float, bench_return_pct: float) -> float:
    return round(rec_return_pct - bench_return_pct, 2)
