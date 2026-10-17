"""Fail closed for the reporting deployment, including paper accounts."""
def check_safety():
    from core.config import settings
    if settings.ALPACA_PAPER is not True:
        raise RuntimeError("Reporting deployment requires ALPACA_PAPER=True")
    if settings.DRY_RUN is not True:
        raise RuntimeError("Reporting deployment requires DRY_RUN=True; execution is not approved")
