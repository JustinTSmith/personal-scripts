# mode_router.py

## What it is

The central message routing logic for the OpenClaw agent system. Classifies an incoming message and maps it to the appropriate agent and model.

## What it does

Two-stage routing pipeline:

1. **Mode classification** — calls `mode_classifier_llm.classify_mode()` to determine if the message is `operator`, `coach`, or `strategist`
2. **Model/agent lookup** — uses `route_models.py` to retrieve the OpenClaw agent ID and model ID (e.g. `gpt-5-mini` or `gpt-5`) for that mode

Returns a routing decision dict with the target agent and model, which the caller (typically `telegram-router.js` or another entry point) uses to dispatch the message.

## How to run

```python
from mode_router import route
decision = route("What's the highest leverage thing I should be doing right now?")
print(decision)
# → {"mode": "strategist", "agent_id": "...", "model_id": "gpt-5"}
```

## Dependencies

- `mode_classifier_llm.py`
- `route_models.py`
- `OPENAI_API_KEY` in environment or `.env`
