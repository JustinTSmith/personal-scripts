# martell_classifier_llm.py

## What it is

An LLM-powered classifier that categorizes messages into Dan Martell's leverage framework categories.

## What it does

Takes an input message and calls OpenAI (`gpt-4o-mini`) to classify it into one of five categories:

| Category | Meaning |
|----------|---------|
| `eliminate` | Task or distraction that should be cut entirely |
| `automate` | Can be handled by a system or script |
| `delegate` | Should be handed off to someone else |
| `optimize` | Keep doing, but do it more efficiently |
| `normal` | Regular task, no leverage action needed |

Returns the category label. Used by the message routing pipeline to help decide how to handle incoming requests.

## How to run

Import and call directly:

```python
from martell_classifier_llm import classify
label = classify("Schedule a 30-minute call with the team")
print(label)  # → "delegate"
```

Or run as a standalone test:

```bash
python3 martell_classifier_llm.py
```

## Dependencies

- Python 3
- `openai` Python package (`pip install openai`)
- `OPENAI_API_KEY` set in environment or `.env`
