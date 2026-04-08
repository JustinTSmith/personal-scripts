from openai import OpenAI
import os

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM_PROMPT = """Classify this message into exactly one mode:

- operator: task execution, planning, priorities, commitments, follow-ups, getting things done, scheduling, project work
- coach: behavior change, habits, patterns, identity, accountability failures, shame, avoidance, guilt, emotional processing, "why do I keep doing this"
- strategist: direction, ROI, leverage decisions, what to stop or start, long-term focus, time allocation, what's worth doing at all

Reply with exactly one word: operator, coach, or strategist."""

def classify_mode_llm(message: str) -> str:
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
        mode = response.choices[0].message.content.strip().lower()
        if mode not in ("operator", "coach", "strategist"):
            return "operator"
        return mode
    except Exception as e:
        print(f"[mode_classifier] error: {e}")
        return "operator"
