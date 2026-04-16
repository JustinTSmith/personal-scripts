#!/usr/bin/env python3
"""
AnteAGE MD Price Crawler
- Scrapes anteage.com for current prices
- Detects 20%+ drops vs compare_at_price or historical baseline
- Emails malleon@gmail.com when deals are found
- Persists prices.json to git between runs (historical tracking)
"""

import json
import os
import smtplib
import subprocess
import time
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PRICES_FILE = os.path.join(SCRIPT_DIR, "prices.json")
CREDS_FILE  = os.path.join(SCRIPT_DIR, "credentials.json")

THRESHOLD = 0.20  # 20% drop

CURL_CMD = [
    "curl", "-sL", "--max-time", "25", "--retry", "2", "--retry-delay", "3",
    "-A", (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "-H", "Accept: application/json",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg):
    print(msg, flush=True)


def fetch_json(url: str) -> dict | None:
    try:
        r = subprocess.run(CURL_CMD + [url], capture_output=True, timeout=35)
        if r.returncode == 0 and r.stdout:
            return json.loads(r.stdout.decode("utf-8", errors="replace"))
    except Exception as e:
        log(f"  fetch error: {e}")
    return None


def load_json(path: str, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path: str, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ── Scraper ───────────────────────────────────────────────────────────────────

def scrape_anteage() -> list[dict]:
    products = []
    data = fetch_json("https://anteage.com/products.json?limit=250")
    if not data:
        log("  anteage.com: no response")
        return products

    for product in data.get("products", []):
        title  = product.get("title", "").strip()
        handle = product.get("handle", "")
        url    = f"https://anteage.com/products/{handle}"

        for v in product.get("variants", []):
            price       = float(v.get("price") or 0)
            compare_raw = v.get("compare_at_price")
            compare_at  = float(compare_raw) if compare_raw else None
            vtitle      = (v.get("title") or "").strip()
            full_name   = f"{title} – {vtitle}" if vtitle and vtitle != "Default Title" else title

            if price > 0:
                products.append({
                    "name":             full_name,
                    "price":            price,
                    "compare_at_price": compare_at,
                    "url":              url,
                    "source":           "anteage.com",
                })

    log(f"  anteage.com: {len(products)} variants scraped")
    return products


# ── Price logic ───────────────────────────────────────────────────────────────

def product_key(p: dict) -> str:
    return f"{p['source']}::{p['name'][:80]}"


def check_deals(current: list[dict], stored: dict) -> list[dict]:
    deals = []
    for p in current:
        key = product_key(p)
        cur = p["price"]

        # Signal 1: store's listed compare_at_price
        cap = p.get("compare_at_price")
        if cap and cap > cur:
            drop = (cap - cur) / cap
            if drop >= THRESHOLD:
                deals.append({
                    **p,
                    "baseline_price": cap,
                    "drop_pct":       round(drop * 100, 1),
                    "savings":        round(cap - cur, 2),
                    "signal":         "compare_at_price",
                })
                continue

        # Signal 2: historical high baseline
        if key in stored:
            baseline = stored[key].get("baseline", 0)
            if baseline > 0 and cur < baseline:
                drop = (baseline - cur) / baseline
                if drop >= THRESHOLD:
                    deals.append({
                        **p,
                        "baseline_price": baseline,
                        "drop_pct":       round(drop * 100, 1),
                        "savings":        round(baseline - cur, 2),
                        "signal":         "historical_baseline",
                    })

    return deals


def update_baselines(current: list[dict], stored: dict) -> dict:
    now = datetime.now().isoformat()
    for p in current:
        key   = product_key(p)
        price = p["price"]
        if key not in stored:
            stored[key] = {"baseline": price, "first_seen": now}
        else:
            if price > stored[key].get("baseline", 0):
                stored[key]["baseline"] = price  # ratchet up to highest seen
        stored[key].update({
            "last_price":   price,
            "last_checked": now,
            "url":          p["url"],
            "source":       p["source"],
        })
    return stored


# ── Email ─────────────────────────────────────────────────────────────────────

def send_alert(deals: list[dict], creds: dict):
    sender    = creds["gmail_sender"]
    password  = creds["gmail_app_password"]
    recipient = creds["alert_recipient"]

    max_drop = max(d["drop_pct"] for d in deals)
    subject  = f"AnteAGE MD Sale Alert — Up to {max_drop}% Off"

    lines = ["Hi Marisa,", "",
             "A sale was detected on AnteAGE MD products with 20% or more off the regular price:",
             ""]
    for d in deals:
        lines += [
            f"• {d['name']}",
            f"  Sale Price:  ${d['price']:.2f}",
            f"  Was:         ${d['baseline_price']:.2f}",
            f"  Savings:     {d['drop_pct']}% off (${d['savings']:.2f})",
            f"  Link:        {d['url']}",
            "",
        ]
    lines += [
        "Act fast — these deals may not last long!",
        "",
        "---",
        "This alert was sent automatically by your AnteAGE MD price monitor.",
        "Monitored store: https://anteage.com",
    ]

    msg = MIMEMultipart()
    msg["From"]    = sender
    msg["To"]      = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText("\n".join(lines), "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.sendmail(sender, recipient, msg.as_string())
        log(f"  Email sent to {recipient}")
        return True
    except Exception as e:
        log(f"  Email failed: {e}")
        return False


# ── Git commit (persist prices.json between remote runs) ─────────────────────

def git_commit_prices():
    try:
        subprocess.run(["git", "config", "user.email", "bot@anteage-monitor"], cwd=SCRIPT_DIR, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name",  "AnteAGE Monitor"],     cwd=SCRIPT_DIR, check=True, capture_output=True)
        subprocess.run(["git", "add",    "prices.json"],                        cwd=SCRIPT_DIR, check=True, capture_output=True)
        result = subprocess.run(
            ["git", "commit", "-m", f"chore: update price history {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
            cwd=SCRIPT_DIR, capture_output=True, text=True
        )
        if "nothing to commit" in result.stdout or result.returncode == 1:
            log("  git: no price changes to commit")
        else:
            subprocess.run(["git", "push"], cwd=SCRIPT_DIR, check=True, capture_output=True)
            log("  git: prices.json pushed")
    except Exception as e:
        log(f"  git commit/push error: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("=== AnteAGE MD Price Crawler ===")
    log(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    creds  = load_json(CREDS_FILE, {})
    stored = load_json(PRICES_FILE, {})

    log("\nScraping sources...")
    products = scrape_anteage()
    log(f"Total variants: {len(products)}")

    deals   = check_deals(products, stored)
    updated = update_baselines(products, stored)
    save_json(PRICES_FILE, updated)
    log(f"Price history saved ({len(updated)} entries)")

    if deals:
        log(f"\n*** {len(deals)} DEAL(S) — 20%+ price drop detected ***")
        for d in deals:
            log(f"  {d['name']}")
            log(f"    Now: ${d['price']:.2f}  |  Was: ${d['baseline_price']:.2f}  |  -{d['drop_pct']}% (saves ${d['savings']:.2f})")
            log(f"    {d['url']}")

        if creds.get("gmail_app_password") and creds["gmail_app_password"] != "YOUR_16_CHAR_APP_PASSWORD":
            log("\nSending email alert...")
            send_alert(deals, creds)
        else:
            log("\nSkipping email — credentials.json not configured.")
    else:
        log("\nNo deals at or above 20% threshold.")

    log("\nPersisting price history...")
    git_commit_prices()

    # Machine-readable output
    print(f"DEALS_JSON:{json.dumps(deals)}")


if __name__ == "__main__":
    main()
