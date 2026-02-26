from __future__ import annotations


def apply_slippage_to_open(open_price: float, side: str, slippage_ticks: float, min_tick: float) -> float:
    side = side.upper()
    if slippage_ticks <= 0 or min_tick <= 0:
        return float(open_price)

    offset = float(slippage_ticks) * float(min_tick)
    if side == "SELL":
        return max(float(min_tick), float(open_price) - offset)
    if side == "BUY":
        return float(open_price) + offset
    raise ValueError(f"Unsupported side: {side}")


def option_cash_flow(price: float, qty_contracts: int, multiplier: int, side: str) -> float:
    gross = float(price) * int(qty_contracts) * int(multiplier)
    side = side.upper()
    if side == "SELL":
        return gross
    if side == "BUY":
        return -gross
    raise ValueError(f"Unsupported side: {side}")


def estimate_option_fee(qty_contracts: int, fee_per_contract: float) -> float:
    return abs(int(qty_contracts)) * float(fee_per_contract)


def estimate_etf_fee(notional: float, fee_rate: float) -> float:
    return abs(float(notional)) * float(fee_rate)
