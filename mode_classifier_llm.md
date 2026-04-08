# mode_classifier_llm.py

## What it is

An LLM-powered classifier that routes messages to the correct operating mode within the OpenClaw agent system.

## What it does

Takes an input message and calls OpenAI (`gpt-4o-mini`) to classify it into one of three modes:

| Mode | Meaning |
|------|---------|
| `operator` | Tactical, execution-focused requests |
| `coach` | Mindset, accountability, or personal development |
| `strategist` | High-level thinking, planning, or business decisions |

Returns the mode label string. Used by `mode_router.py` as the first step in determining which agent and model should handle a message.

## How to run

Import and call:

```python
from mode_classifier_llm import classify_mode
mode = classify_mode("Help me think through my Q2 business strategy")
print(mode)  # → "strategist"
```

Or run standalone:

```bash
python3 mode_classifier_llm.py
```

## Dependencies

- Python 3
- `openai` Python package (`pip install openai`)
- `OPENAI_API_KEY` in environment or `.env`
