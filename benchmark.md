# benchmark.py

## What it is

A performance benchmarking script for local Ollama models. Measures inference speed in tokens per second across a standardized prompt.

## What it does

Runs a fixed test prompt against a set of local Ollama models:

- `qwen2.5-coder`
- `qwen3`
- `deepseek-r1`

For each model it:
1. Sends the prompt to the Ollama API
2. Measures total elapsed time and token count
3. Calculates and prints tokens/second throughput

Output is a simple table comparing performance across models, useful for deciding which model to route different task types to.

## How to run

```bash
python3 benchmark.py
```

Ollama must be running with the target models already pulled:

```bash
ollama serve
ollama pull qwen2.5-coder
ollama pull qwen3
ollama pull deepseek-r1
```

## Dependencies

- Python 3
- Ollama running locally at `http://127.0.0.1:11434`
- Target models pulled via `ollama pull`
