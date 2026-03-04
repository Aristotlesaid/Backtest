from __future__ import annotations

import importlib


def build_term_atm_greeks_hv_snapshot(**kwargs):
    snapshot_mod = importlib.import_module(".term_atm_ivhv", __name__)
    importlib.reload(snapshot_mod)
    return snapshot_mod.build_term_atm_greeks_hv_snapshot(**kwargs)


def build_term_atm_iv_hv_snapshot(**kwargs):
    return build_term_atm_greeks_hv_snapshot(**kwargs)


def notebook_show_term_atm_greeks(**kwargs):
    snapshot_mod = importlib.import_module(".term_atm_ivhv", __name__)
    importlib.reload(snapshot_mod)
    return snapshot_mod.notebook_show_term_atm_greeks(**kwargs)


__all__ = [
    "build_term_atm_greeks_hv_snapshot",
    "build_term_atm_iv_hv_snapshot",
    "notebook_show_term_atm_greeks",
]
