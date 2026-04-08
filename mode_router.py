from mode_classifier_llm import classify_mode_llm
from martell_classifier_llm import classify_martell
from route_models import get_agent_for_mode

def route_context(message: str) -> dict:
    """
    Classifies a message and returns routing context.

    Returns:
        {
            "mode": "operator" | "coach" | "strategist",
            "agent": openclaw agent ID,
            "model": model ID to use,
            "martell": "eliminate" | "automate" | "delegate" | "optimize" | "normal",
        }
    """
    mode = classify_mode_llm(message)
    routing = get_agent_for_mode(mode)
    martell = classify_martell(message)

    return {
        "mode": mode,
        "agent": routing["agent"],
        "model": routing["model"],
        "martell": martell,
    }


if __name__ == "__main__":
    import sys
    msg = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else input("Message: ")
    result = route_context(msg)
    print(f"Mode:    {result['mode']}")
    print(f"Agent:   {result['agent']}")
    print(f"Model:   {result['model']}")
    print(f"Martell: {result['martell']}")
