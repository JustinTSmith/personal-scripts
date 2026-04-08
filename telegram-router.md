# telegram-router.js

## What it is

A Node.js Telegram webhook server that receives messages from your Telegram bot and routes them to the appropriate OpenClaw agent.

## What it does

1. **Receives Telegram updates** — listens for incoming webhook POST requests from Telegram
2. **Routes to OpenClaw** — forwards the message to the OpenClaw gateway; the gateway returns a JSON routing decision specifying which agent should handle it
3. **Fact-check shortcut** — if the message contains a URL, triggers a fact-checking flow
4. **Dispatches to agent** — sends the message to the determined agent endpoint
5. **Replies to Telegram** — sends the agent's response back to the originating Telegram chat

The router supports multi-agent routing: the gateway's response can redirect to `operator`, `coach`, or `strategist` agents depending on message content.

## How to run

```bash
node telegram-router.js
```

Requires a publicly accessible HTTPS URL for the Telegram webhook. For local development, use a tunnel:

```bash
cloudflared tunnel --url http://localhost:3000
```

Then register the webhook with Telegram:

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<tunnel-url>"
```

## Environment variables

- `TELEGRAM_BOT_TOKEN` — your bot token from @BotFather
- `OPENAI_API_KEY` — for OpenClaw gateway calls
- `OPENCLAW_GATEWAY_URL` — base URL of the OpenClaw gateway

## Dependencies

- Node.js
- `npm install` (installs express and any other deps)
- `TELEGRAM_BOT_TOKEN` in `.env`
