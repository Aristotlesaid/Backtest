from __future__ import annotations

import math


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bs_d1_d2(
    spot: float,
    strike: float,
    ttm_years: float,
    rate: float,
    dividend_yield: float,
    vol: float,
) -> tuple[float, float]:
    if spot <= 0 or strike <= 0 or ttm_years <= 0 or vol <= 0:
        raise ValueError("Invalid BS inputs.")
    sqrt_t = math.sqrt(ttm_years)
    d1 = (math.log(spot / strike) + (rate - dividend_yield + 0.5 * vol * vol) * ttm_years) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    return d1, d2


def bs_price(
    spot: float,
    strike: float,
    ttm_years: float,
    rate: float,
    dividend_yield: float,
    vol: float,
    option_type: str,
) -> float:
    d1, d2 = _bs_d1_d2(
        spot=spot,
        strike=strike,
        ttm_years=ttm_years,
        rate=rate,
        dividend_yield=dividend_yield,
        vol=vol,
    )

    disc_q = math.exp(-dividend_yield * ttm_years)
    disc_r = math.exp(-rate * ttm_years)

    kind = option_type.strip().lower()
    if kind == "call":
        return spot * disc_q * _norm_cdf(d1) - strike * disc_r * _norm_cdf(d2)
    if kind == "put":
        return strike * disc_r * _norm_cdf(-d2) - spot * disc_q * _norm_cdf(-d1)
    raise ValueError(f"Unsupported option_type: {option_type}")


def bs_vega(
    spot: float,
    strike: float,
    ttm_years: float,
    rate: float,
    dividend_yield: float,
    vol: float,
) -> float:
    d1, _ = _bs_d1_d2(
        spot=spot,
        strike=strike,
        ttm_years=ttm_years,
        rate=rate,
        dividend_yield=dividend_yield,
        vol=vol,
    )
    sqrt_t = math.sqrt(ttm_years)
    return spot * math.exp(-dividend_yield * ttm_years) * _norm_pdf(d1) * sqrt_t


def bs_greeks(
    spot: float,
    strike: float,
    ttm_years: float,
    rate: float,
    dividend_yield: float,
    vol: float,
    option_type: str,
) -> dict[str, float]:
    d1, d2 = _bs_d1_d2(
        spot=spot,
        strike=strike,
        ttm_years=ttm_years,
        rate=rate,
        dividend_yield=dividend_yield,
        vol=vol,
    )
    sqrt_t = math.sqrt(ttm_years)
    disc_q = math.exp(-dividend_yield * ttm_years)
    disc_r = math.exp(-rate * ttm_years)
    kind = option_type.strip().lower()

    if kind == "call":
        delta = disc_q * _norm_cdf(d1)
        theta_annual = (
            -spot * disc_q * _norm_pdf(d1) * vol / (2.0 * sqrt_t)
            - rate * strike * disc_r * _norm_cdf(d2)
            + dividend_yield * spot * disc_q * _norm_cdf(d1)
        )
    elif kind == "put":
        delta = disc_q * (_norm_cdf(d1) - 1.0)
        theta_annual = (
            -spot * disc_q * _norm_pdf(d1) * vol / (2.0 * sqrt_t)
            + rate * strike * disc_r * _norm_cdf(-d2)
            - dividend_yield * spot * disc_q * _norm_cdf(-d1)
        )
    else:
        raise ValueError(f"Unsupported option_type: {option_type}")

    gamma = disc_q * _norm_pdf(d1) / (spot * vol * sqrt_t)
    vega = spot * disc_q * _norm_pdf(d1) * sqrt_t
    theta = theta_annual / 365.0
    return {
        "Delta": float(delta),
        "Gamma": float(gamma),
        "Theta": float(theta),
        "Vega": float(vega),
    }


def implied_vol_newton(
    market_price: float,
    spot: float,
    strike: float,
    ttm_years: float,
    rate: float,
    dividend_yield: float,
    option_type: str,
    initial_vol: float = 0.3,
    tol: float = 1e-8,
    max_iter: int = 100,
) -> float:
    if market_price <= 0:
        raise ValueError("market_price must be > 0.")
    if spot <= 0 or strike <= 0:
        raise ValueError("spot/strike must be > 0.")
    if ttm_years <= 0:
        raise ValueError("ttm_years must be > 0.")

    sigma = float(initial_vol)
    if sigma <= 0:
        sigma = 0.3

    for _ in range(int(max_iter)):
        model = bs_price(
            spot=spot,
            strike=strike,
            ttm_years=ttm_years,
            rate=rate,
            dividend_yield=dividend_yield,
            vol=sigma,
            option_type=option_type,
        )
        diff = model - market_price
        if abs(diff) < tol:
            return sigma

        vega = bs_vega(
            spot=spot,
            strike=strike,
            ttm_years=ttm_years,
            rate=rate,
            dividend_yield=dividend_yield,
            vol=sigma,
        )
        if vega <= 1e-12:
            raise RuntimeError(
                f"Newton failed: tiny vega. option_type={option_type}, S={spot}, K={strike}, T={ttm_years}, price={market_price}"
            )

        sigma = sigma - diff / vega
        if sigma <= 0:
            sigma = 1e-6
        if sigma > 5.0:
            sigma = 5.0

    raise RuntimeError(
        f"Newton failed: no convergence. option_type={option_type}, S={spot}, K={strike}, T={ttm_years}, price={market_price}"
    )
