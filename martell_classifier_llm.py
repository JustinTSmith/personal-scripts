from openai import OpenAI
import os

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM_PROMPT = """Classify this message using the Dan Martell leverage framework. Pick the single best category:

- eliminate: low-value, wasteful, should not be done at all — stop doing this
- automate: repetitive, rule-based, could be scripted or handled by a system without human judgment
- delegate: requires a human but not you — someone else or an agent should own this
- optimize: worth doing yourself but could be done faster, cheaper, or better — improve the approach
- normal: regular conversation, question, or task that doesn't fit a leverage category

Reply with exactly one word: eliminate, automate, delegate, optimize, or normal."""


def classify_martell(message: str) -> str:
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": message}
            ],
            max_tokens=10,
            temperature=0
        )
        label = response.choices[0].message.content.strip().lower()
        if label not in ("eliminate", "automate", "delegate", "optimize", "normal"):
            return "normal"
        return label
    except Exception as e:
        print(f"[martell_classifier] error: {e}")
        return "normal"
