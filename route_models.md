# route_models.py

## What it is

A configuration file that maps operating modes to specific OpenClaw agent IDs and OpenAI model IDs.

## What it does

Defines a lookup table used by `mode_router.py`. Each mode maps to:

- **agent_id** — the OpenClaw agent endpoint to route the message to
- **model_id** — the OpenAI model to use (`gpt-5-mini` for lighter modes, `gpt-5` for strategic)

Current mappings:

| Mode | Model |
|------|-------|
| `operator` | gpt-5-mini |
| `coach` | gpt-5-mini |
| `strategist` | gpt-5 |

## How to use

Not run directly — imported by `mode_router.py`:

```python
from route_models import get_route
route = get_route("operator")
# → {"agent_id": "...", "model_id": "gpt-5-mini"}
```

To update routing (e.g. change which model a mode uses), edit the mapping dict in this file.

## Dependencies

- Python 3 (no external packages)
