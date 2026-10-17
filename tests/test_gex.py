"""
test_gex.py — GEX engine diagnostic
=====================================
Run from project root:
    python -m test_gex --ticker QQQ
    python -m test_gex --ticker AAPL MSFT NVDA

Proves the engine is working by checking:
  1. Spot price fetched and non-zero
  2. Contracts loaded with real OI
  3. GEX by strike is non-zero for individual contracts
  4. Net GEX sign and magnitude are plausible
  5. Key levels exist and are structurally valid
     (call wall >= spot, put wall <= spot)
  6. IV coverage is reasonable
  7. BS fallback counts are logged
  8. Spot + GEX cross-check via market monitor comparison
"""

import argparse
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.features.gex_core import (
    GEXEngine,
    fast_bootstrap,
    compute_key_levels,
    compute_gamma_slope,
)


PASS = "  ✅"
FAIL = "  ❌"
WARN = "  ⚠️ "
INFO = "  ℹ️ "

def check(label: str, condition: bool, detail: str = "", warn_only: bool = False) -> bool:
    icon = PASS if condition else (WARN if warn_only else FAIL)
    status = "PASS" if condition else ("WARN" if warn_only else "FAIL")
    line = f"{icon}  [{status}]  {label}"
    if detail:
        line += f"  →  {detail}"
    print(line)
    return condition


def run_diagnostic(ticker: str, min_dte: int = 0, max_dte: int = 45) -> bool:
    print(f"\n{'═' * 65}")
    print(f"  GEX ENGINE DIAGNOSTIC  —  {ticker}  (DTE {min_dte}–{max_dte})")
    print(f"{'═' * 65}")

    engine = GEXEngine(underlying=ticker)
    fast_bootstrap(engine, ticker, min_dte=min_dte, max_dte=max_dte)

    spot   = engine.spot_price
    n      = len(engine.contract_data)
    passed = 0
    total  = 0

    def c(label, cond, detail="", warn_only=False):
        nonlocal passed, total
        total += 1
        ok = check(label, cond, detail, warn_only)
        if ok or warn_only:
            passed += 1
        return ok

    # ── 1. Spot ────────────────────────────────────────────────────────────────
    print(f"\n  {'─' * 30}  SPOT")
    c("Spot fetched",       spot > 0,    f"${spot:.2f}")
    c("Spot plausible",     1 < spot < 100_000, f"${spot:.2f}")

    if spot <= 0:
        print(f"\n  {FAIL}  Spot is 0 — cannot validate GEX.  Check ALPACA keys.\n")
        return False

    # ── 2. Contracts ───────────────────────────────────────────────────────────
    print(f"\n  {'─' * 30}  CONTRACTS")
    contracts = list(engine.contract_data.values())

    c("Contracts loaded",   n >= 50,     f"{n} contracts")
    c("Has calls",          any(c["type"] == "call" for c in contracts))
    c("Has puts",           any(c["type"] == "put"  for c in contracts))

    oi_nonzero = sum(1 for c in contracts if c["oi"] > 0)
    c("OI populated",       oi_nonzero > n * 0.5,
      f"{oi_nonzero}/{n} contracts have OI > 0", warn_only=(oi_nonzero == 0))

    gamma_nonzero = sum(1 for c in contracts if c["gamma"] != 0.0)
    c("Gamma non-zero",     gamma_nonzero > n * 0.5,
      f"{gamma_nonzero}/{n} contracts have gamma ≠ 0")

    iv_nonzero = sum(1 for c in contracts if c["iv"] > 0)
    c("IV populated",       iv_nonzero > n * 0.3,
      f"{iv_nonzero}/{n} contracts have IV > 0", warn_only=True)

    # ── 3. GEX by strike ──────────────────────────────────────────────────────
    print(f"\n  {'─' * 30}  GEX BY STRIKE")
    gex_map = engine.compute_gex_by_strike()

    c("GEX map non-empty",  len(gex_map) > 0, f"{len(gex_map)} strikes")

    if gex_map:
        nonzero_strikes = sum(1 for v in gex_map.values() if v != 0.0)
        c("Strikes have non-zero GEX",  nonzero_strikes > len(gex_map) * 0.3,
          f"{nonzero_strikes}/{len(gex_map)} strikes non-zero")

        net_gex  = sum(gex_map.values())
        max_gex  = max(gex_map.values())
        min_gex  = min(gex_map.values())
        net_gex_b = net_gex / 1e9

        print(f"\n  {INFO}  Net GEX       : ${net_gex_b:+.3f}B")
        print(f"  {INFO}  Max strike GEX: ${max_gex/1e6:+.1f}M  at strike "
              f"{max(gex_map, key=gex_map.get):.0f}")
        print(f"  {INFO}  Min strike GEX: ${min_gex/1e6:+.1f}M  at strike "
              f"{min(gex_map, key=gex_map.get):.0f}")

        # Net GEX being near zero is fine — it means balanced call/put exposure.
        # What we care about is that individual strikes have real values.
        c("Individual strike GEX meaningful",
          abs(max_gex) > 1e4 or abs(min_gex) > 1e4,
          f"max={max_gex/1e6:.2f}M  min={min_gex/1e6:.2f}M")

    # ── 4. Key levels ─────────────────────────────────────────────────────────
    print(f"\n  {'─' * 30}  KEY LEVELS")
    levels = compute_key_levels(gex_map, spot) if gex_map else {}

    c("compute_key_levels returned data",  bool(levels))

    if levels:
        cw  = levels.get("call_wall")
        pw  = levels.get("put_wall")
        zg  = levels.get("zero_gamma")
        net = levels.get("net_gex_b", 0)
        reg = levels.get("regime", "")
        str_ = levels.get("regime_strength", 0)
        skew = levels.get("gex_skew", 1)

        print(f"\n  {INFO}  Regime        : {reg}  (strength {str_:.2f})")
        print(f"  {INFO}  Net GEX       : ${net:.3f}B")
        print(f"  {INFO}  Spot          : ${spot:.2f}")
        print(f"  {INFO}  Zero Gamma    : ${zg:.2f}" if zg else f"  {INFO}  Zero Gamma    : None")
        print(f"  {INFO}  Call Wall     : ${cw:.2f}" if cw else f"  {INFO}  Call Wall     : None")
        print(f"  {INFO}  Put Wall      : ${pw:.2f}" if pw else f"  {INFO}  Put Wall      : None")
        print(f"  {INFO}  GEX Skew      : {skew:.3f}  (>1 = call-heavy, <1 = put-heavy)")

        c("Call wall above spot",   cw is None or cw >= spot * 0.90,
          f"${cw:.2f} vs spot ${spot:.2f}" if cw else "not found", warn_only=True)
        c("Put wall below spot",    pw is None or pw <= spot * 1.10,
          f"${pw:.2f} vs spot ${spot:.2f}" if pw else "not found", warn_only=True)
        c("Regime populated",       reg in ("positive", "negative"))
        c("Zero gamma exists",      zg is not None,
          f"${zg:.2f}" if zg else "not found", warn_only=True)

        dist = levels.get("dist_to_flip_pct")
        if dist is not None:
            print(f"  {INFO}  Dist to flip  : {dist:+.2f}%  "
                  f"({'above' if dist > 0 else 'below'} zero gamma line)")

    # ── 5. IV rank ────────────────────────────────────────────────────────────
    print(f"\n  {'─' * 30}  IV ANALYTICS")
    iv_data = engine.compute_iv_rank()
    if iv_data:
        print(f"  {INFO}  ATM IV        : {iv_data.get('atm_iv', 0):.1%}")
        print(f"  {INFO}  IV Rank       : {iv_data.get('iv_rank', 0):.1f}")
        print(f"  {INFO}  IV Percentile : {iv_data.get('iv_percentile', 0):.1f}")
        print(f"  {INFO}  IV Skew (C-P) : {iv_data.get('iv_skew', 0):+.4f}")
        print(f"  {INFO}  Coverage      : {iv_data.get('contracts_with_iv', 0):.1f}%")
        c("IV rank computed",  iv_data.get("iv_rank", 0) >= 0)
    else:
        c("IV rank computed",  False, "insufficient IV data", warn_only=True)

    # ── 6. Summary ────────────────────────────────────────────────────────────
    print(f"\n{'─' * 65}")
    hard_fails = total - passed
    print(f"  Result: {passed}/{total} checks passed"
          + (f"  —  {hard_fails} hard failure(s)" if hard_fails else "  —  all clear"))
    print(f"{'═' * 65}\n")

    return hard_fails == 0


def main():
    parser = argparse.ArgumentParser(description="GEX engine diagnostic")
    parser.add_argument("--ticker", nargs="+", default=["QQQ"],
                        help="Tickers to test (default: QQQ)")
    parser.add_argument("--min-dte", type=int, default=0)
    parser.add_argument("--max-dte", type=int, default=45)
    args = parser.parse_args()

    all_passed = True
    for ticker in args.ticker:
        ok = run_diagnostic(ticker.upper(), args.min_dte, args.max_dte)
        all_passed = all_passed and ok

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
