import requests
import time

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"

MODELS = [
    "qwen2.5-coder:latest",
    "qwen3:14b",
    "deepseek-r1:latest",
]

PROMPT = "Explain in detail how a neural network learns."

def benchmark(model):
    print(f"\n--- Testing {model} ---")

    start = time.time()

    response = requests.post(OLLAMA_URL, json={
        "model": model,
        "prompt": PROMPT,
        "stream": False
    })

    end = time.time()

    data = response.json()

    total_time = end - start
    tokens = data.get("eval_count", 0)

    tps = tokens / total_time if total_time > 0 else 0

    print(f"Time: {total_time:.2f}s")
    print(f"Tokens: {tokens}")
    print(f"Tokens/sec: {tps:.2f}")

for m in MODELS:
    benchmark(m)