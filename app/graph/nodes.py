from app.graph.state import TradingState
from app.agents.regime_agent import classify_regime
from app.agents.mag7_agent import get_mag7_signal
from app.agents.futures_scalp_agent import scalp_decision
from app.agents.risk_agent import risk_check

def regime_node(state: TradingState):
    regime, chop_score = classify_regime()
    state["regime"] = regime
    state["chop_score"] = chop_score
    return state

def mag7_node(state: TradingState):
    state["mag7_signal"] = get_mag7_signal()
    return state

def futures_node(state: TradingState):
    state["futures_signal"] = scalp_decision(
        regime=state["regime"],
        mag7_signal=state["mag7_signal"]
    )
    return state

def risk_node(state: TradingState):
    state["risk_ok"] = risk_check(state["futures_signal"])
    return state

def execution_node(state: TradingState):
    if state["risk_ok"] and state["futures_signal"]:
        signal = state["futures_signal"]
        state["alert_message"] = (
            f"{signal['instrument']} "
            f"{signal['direction']} "
            f"{signal['entry']}"
        )
    return state