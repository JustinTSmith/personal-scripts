# services-dashboard production package

Drop these two files into your `services-dashboard/` directory (next to
`scan.py` and `server.py`), then run the patcher once.

## Files

- **`template.html`** — Console v2 dashboard. Replaces the inline
  `HTML_TEMPLATE` string in `scan.py`. Has `__HOST__` and `__DATA__`
  placeholders; `scan.py` substitutes them on each render.
- **`apply_production.py`** — One-shot patcher. Modifies `scan.py` and
  `server.py` in place (with `.bak` backups). Idempotent — safe to re-run.

## Install

```bash
cd ~/path/to/services-dashboard
cp /path/to/template.html .
cp /path/to/apply_production.py .
python3 apply_production.py
```

The patcher:
1. Backs up `scan.py` → `scan.py.bak` and `server.py` → `server.py.bak`.
2. Replaces the ~700-line `HTML_TEMPLATE = r"""..."""` block in `scan.py`
   with a 3-line loader that reads `template.html` fresh on each render
   (so you can iterate on the template without re-running scan).
3. Adds `POST /api/explain` to `server.py` — the dashboard's "Regenerate"
   button calls this; it runs `scan.generate_explanation()` server-side
   (uses your existing `ANTHROPIC_API_KEY` env), persists to
   `explanations.json`, and updates the in-memory snapshot.

## Restart

```bash
pkill -f 'python3 server.py' ; python3 server.py &
```

Open http://127.0.0.1:8765/ — you'll see Console v2 with:
- Personal / + System scope toggle in the header
- Category groups (Watchdogs, Gateways, Life OS, etc.)
- Friendly one-sentence descriptions per service
- Click any row → "How it works" panel with explanation/source toggle
- "Regenerate" button hits `/api/explain` and updates the cached
  explanation in `explanations.json`

## Rollback

```bash
cp scan.py.bak scan.py
cp server.py.bak server.py
# (template.html can stay; nothing else references it after rollback)
```
