from app.services.vix_apmem_service import build_vix_apmem_signal


if __name__ == "__main__":
    sig = build_vix_apmem_signal()
    print("\n==============================")
    print(" VIX + APMEM ENGINE")
    print("==============================")
    print(f"Date: {sig.as_of}")
    print(f"VIX: {sig.vix:.2f}")
    print(f"VIX Percentile: {sig.vix_percentile:.2f}")
    print(f"APMEM Vol: {sig.apmem_vol:.4f}")
    print(f"APMEM Trend: {sig.apmem_trend:.4f}")
    print(f"Score: {sig.score}/100")
    print(f"Regime: {sig.regime}")
    print(f"Trade Bias: {sig.bias}")
    print("==============================\n")