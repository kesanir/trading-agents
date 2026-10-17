import argparse
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.trade_intent_service import load_latest_premarket_intent

intent = load_latest_premarket_intent()
print(intent)