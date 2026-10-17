from app.agents.regime_agent import classify_regime, scan_named_universe

def main():
    single = classify_regime("QQQ")
    print("=== SINGLE ===")
    print({
        "ticker": single["ticker"],
        "spot": single["spot"],
        "regime": single["regime"],
        "net_gex_b": single["net_gex_b"],
        "call_wall": single["call_wall"],
        "put_wall": single["put_wall"],
        "score": single["score"],
    })

    multi = scan_named_universe("mag7", top_n=10)

    print("=== MAG7 RAW ===")
    print("total:", multi["total"])
    print("loaded:", multi["loaded"])
    print("candidate_count:", len(multi["candidates"]))
    print("error_count:", len(multi["errors"]))

    if multi["errors"]:
        print("\n=== ERRORS ===")
        for row in multi["errors"]:
            print(row["ticker"], "->", row["error"])

    if multi["candidates"]:
        print("\n=== CANDIDATES ===")
        for row in multi["candidates"]:
            print({
                "ticker": row["ticker"],
                "regime": row["regime"],
                "score": row["score"],
                "strength": row["strength"],
                "contracts": row["contracts"],
                "call_wall": row["call_wall"],
                "put_wall": row["put_wall"],
            })
    else:
        print("\nNo MAG7 candidates passed filters.")

if __name__ == "__main__":
    main()