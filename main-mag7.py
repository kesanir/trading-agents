from app.agents.mag7_agent import build_mag7_summary
from app.services.formatting_service import format_mag7_summary

def main():
    summary = build_mag7_summary()
    print("=== MAG7 SUMMARY ===")
   

    formatted = format_mag7_summary(summary)
    print(formatted)

if __name__ == "__main__":
    main()