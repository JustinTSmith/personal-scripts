#!/usr/bin/env node
/**
 * Telegram → OpenClaw router middleware (Node)
 *
 * Receives Telegram webhook updates, sends the message to the main (PHI) router
 * agent, parses the JSON route decision, then calls the chosen specialist agent
 * and sends that reply back to Telegram.
 *
 * Requires:
 *   - OPENCLAW_GATEWAY_URL (e.g. http://127.0.0.1:18789)
 *   - OPENCLAW_GATEWAY_TOKEN (gateway.auth.token from openclaw.json)
 *   - TELEGRAM_BOT_TOKEN (your Telegram bot token)
 *
 * Run: node telegram-router.js
 * Set Telegram webhook to: https://your-host/telegram-webhook (or use ngrok for local)
 */

const http = require('http');
const https = require('https');
const url = require('url');
const { execFile } = require('child_process');
const path = require('path');

const FACT_CHECKER_SCRIPT = path.resolve(
  __dirname,
  '../skills/fact-checker/scripts/fact_checker.py'
);
const SOCIAL_URL_REGEX =
  /https?:\/\/(www\.)?(facebook\.com|instagram\.com|twitter\.com|x\.com)[^\s]*/i;

const GATEWAY_URL = process.env.OPENCLAW_GATEWAY_URL || 'http://127.0.0.1:18789';
const GATEWAY_TOKEN = process.env.OPENCLAW_GATEWAY_TOKEN || '';
const TELEGRAM_TOKEN = process.env.TELEGRAM_BOT_TOKEN || '';
const PORT = parseInt(process.env.TELEGRAM_ROUTER_PORT || '8765', 10);

const ALLOWED_ROUTES = new Set(['coding', 'reasoning', 'complex', 'writing', 'brainstorming', 'main']);

function parseRouterJson(text) {
  const trimmed = (text || '').trim();
  const raw = trimmed.replace(/^```(?:json)?\s*\n?/, '').replace(/\n?```\s*$/, '');
  try {
    const o = JSON.parse(raw);
    if (o && typeof o.route === 'string' && ALLOWED_ROUTES.has(o.route)) {
      return o.route;
    }
  } catch (_) {}
  return null;
}

function gatewayRequest(body) {
  const u = new URL('/hooks/agent', GATEWAY_URL);
  const isHttps = u.protocol === 'https:';
  const opts = {
    hostname: u.hostname,
    port: u.port || (isHttps ? 443 : 80),
    path: u.pathname,
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${GATEWAY_TOKEN}`,
      'x-openclaw-token': GATEWAY_TOKEN,
    },
  };
  return new Promise((resolve, reject) => {
    const req = (isHttps ? https : http).request(opts, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        const raw = Buffer.concat(chunks).toString('utf8');
        let data;
        try {
          data = JSON.parse(raw);
        } catch (_) {
          data = { raw };
        }
        resolve({ status: res.statusCode, data, raw });
      });
    });
    req.on('error', reject);
    req.setHeader('Content-Length', Buffer.byteLength(body));
    req.write(body);
    req.end();
  });
}

function extractReply(hookResponse) {
  const d = hookResponse.data;
  if (!d) return hookResponse.raw || '';
  if (typeof d.reply === 'string') return d.reply;
  if (typeof d.text === 'string') return d.text;
  if (typeof d.content === 'string') return d.content;
  if (Array.isArray(d.messages) && d.messages.length) {
    const last = d.messages[d.messages.length - 1];
    if (last && typeof last.content === 'string') return last.content;
  }
  return hookResponse.raw || '';
}

async function runAgent(agentId, message) {
  const body = JSON.stringify({
    message,
    agentId,
    deliver: false,
  });
  const res = await gatewayRequest(body);
  if (res.status !== 200) {
    throw new Error(`Gateway hooks/agent returned ${res.status}: ${res.raw}`);
  }
  return extractReply(res);
}

async function sendTelegram(method, payload) {
  const u = new URL(`/bot${TELEGRAM_TOKEN}/${method}`, 'https://api.telegram.org');
  const body = JSON.stringify(payload);
  const opts = {
    hostname: u.hostname,
    path: u.pathname + u.search,
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  };
  return new Promise((resolve, reject) => {
    const req = https.request(opts, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    });
    req.on('error', reject);
    req.end(body);
  });
}

async function handleUpdate(update) {
  const msg = update?.message;
  if (!msg?.text) return;
  const chatId = msg.chat?.id;
  const userText = msg.text.trim();
  if (!chatId || !userText) return;

  let replyText;
  try {
    // Fact-checker: message contains "fact check" + a social media URL
    const FACT_CHECK_INTENT = /\bfact[\s-]?check\b/i;
    const socialUrl = userText.match(SOCIAL_URL_REGEX)?.[0];
    if (socialUrl && FACT_CHECK_INTENT.test(userText)) {
      await sendTelegram('sendMessage', {
        chat_id: chatId,
        text: '🔬 Fact-checking that post… this takes ~30 seconds.',
        reply_to_message_id: msg.message_id,
      });
      replyText = await new Promise((resolve) => {
        execFile(
          'python3',
          [FACT_CHECKER_SCRIPT, socialUrl],
          { timeout: 120000 },
          (err, stdout, stderr) => {
            if (err && !stdout) {
              resolve(`❌ Fact-check failed: ${(stderr || err.message || '').slice(0, 200)}`);
            } else {
              resolve((stdout || '').trim() || '⚠️ No output from fact-checker.');
            }
          }
        );
      });
      await sendTelegram('sendMessage', {
        chat_id: chatId,
        text: replyText.slice(0, 4096),
        reply_to_message_id: msg.message_id,
      });
      return;
    }

    // Side channel: "use llama3" → route directly to Llama (complex agent), skip router
    const useLlama3 = /\buse llama3\b/i.test(userText);
    const messageForAgent = useLlama3
      ? userText.replace(/\buse llama3\b/gi, '').trim() || userText
      : userText;

    if (useLlama3) {
      replyText = await runAgent('complex', messageForAgent);
    } else {
      const routerReply = await runAgent('main', userText);
      const route = parseRouterJson(routerReply);

      if (route && route !== 'main') {
        replyText = await runAgent(route, userText);
      } else {
        replyText = routerReply || 'Router did not return a valid route or answer.';
      }
    }
  } catch (e) {
    replyText = `Error: ${e.message}`;
  }

  await sendTelegram('sendMessage', {
    chat_id: chatId,
    text: replyText.slice(0, 4096),
    reply_to_message_id: msg.message_id,
  });
}

const server = http.createServer((req, res) => {
  if (req.method !== 'POST' || req.url !== '/telegram-webhook') {
    res.writeHead(404);
    res.end();
    return;
  }
  const chunks = [];
  req.on('data', (c) => chunks.push(c));
  req.on('end', () => {
    try {
      const update = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      handleUpdate(update).catch((e) => console.error('handleUpdate:', e));
    } catch (_) {}
    res.writeHead(200);
    res.end();
  });
});

if (!GATEWAY_TOKEN || !TELEGRAM_TOKEN) {
  console.error('Set OPENCLAW_GATEWAY_TOKEN and TELEGRAM_BOT_TOKEN.');
  process.exit(1);
}

server.listen(PORT, () => {
  console.log(`Telegram router listening on http://0.0.0.0:${PORT}/telegram-webhook`);
  console.log('Set Telegram webhook to this URL (e.g. with ngrok for local).');
});
