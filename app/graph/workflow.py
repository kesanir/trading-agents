from langgraph.graph import StateGraph
from app.graph.state import TradingState
from app.graph.nodes import (
    regime_node,
    mag7_node,
    futures_node,
    risk_node,
    execution_node,
)
from app.graph.router import route_after_regime

def build_graph():
    graph = StateGraph(TradingState)

    graph.add_node("regime_node", regime_node)
    graph.add_node("mag7_node", mag7_node)
    graph.add_node("futures_node", futures_node)
    graph.add_node("risk_node", risk_node)
    graph.add_node("execution_node", execution_node)

    graph.set_entry_point("regime_node")

    graph.add_conditional_edges(
        "regime_node",
        route_after_regime,
        {
            "mag7_node": "mag7_node",
            "end": "__end__"
        }
    )

    graph.add_edge("mag7_node", "futures_node")
    graph.add_edge("futures_node", "risk_node")
    graph.add_edge("risk_node", "execution_node")
    graph.add_edge("execution_node", "__end__")

    return graph.compile()