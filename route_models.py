# Maps mode → OpenClaw agent ID + model
# Update model IDs here when GPT-5 is available

AGENT_MAP = {
    "operator":   {"agent": "operator",   "model": "openai/gpt-5-mini"},
    "coach":      {"agent": "coach",      "model": "openai/gpt-5-mini"},
    "strategist": {"agent": "strategist", "model": "openai/gpt-5"},
    "therapist":  {"agent": "operator",   "model": "openai/gpt-5-mini"},
}

DEFAULT_AGENT = "operator"

def get_agent_for_mode(mode: str) -> dict:
    """Returns {"agent": agent_id, "model": model_id} for a given mode."""
    return AGENT_MAP.get(mode, AGENT_MAP[DEFAULT_AGENT])

def get_agent_id(mode: str) -> str:
    return get_agent_for_mode(mode)["agent"]

def get_model_id(mode: str) -> str:
    return get_agent_for_mode(mode)["model"]
