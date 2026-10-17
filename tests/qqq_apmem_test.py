import argparse
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.services.qqq_apmem_signal import build_qqq_apmem_signal

sig = build_qqq_apmem_signal()
print(sig)
print(sig.to_dict())