def route_after_regime(state):
    if state["regime"] == "chop":
        return "end"
    return "mag7_node"