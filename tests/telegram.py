import argparse
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.services.telegram_service import send_telegram_message

send_telegram_message("Test message 🚀")