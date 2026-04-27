#!/usr/bin/env python3
"""
apply_production.py — one-shot patcher to wire dashboard.html → template.html.

Run from the services-dashboard/ directory:

    cd ~/path/to/services-dashboard
    python3 apply_production.py

What it does:
  1. Backs up scan.py and server.py to .bak files (refuses to overwrite).
  2. Replaces the inline HTML_TEMPLATE = r\"\"\"...\"\"\" block in scan.py with a
     loader that reads ./template.html at runtime.
  3. Adds a POST /api/explain endpoint to server.py that calls Anthropic
     server-side and persists the result into explanations.json.
  4. Verifies template.html exists.

Idempotent: re-running detects already-patched files and exits cleanly.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCAN_PY = HERE / "scan.py"
SERVER_PY = HERE / "server.py"
TEMPLATE_HTML = HERE / "template.html"

SCAN_LOADER_BLOCK = '''# ── HTML rendering ──────────────────────────────────────────────────────────

TEMPLATE_FILE = SCRIPT_DIR / "template.html"


def _load_template() -> str:
    """Read template.html fresh on each render so edits show up without restarting."""
    if not TEMPLATE_FILE.exists():
        raise FileNotFoundError(
            f"template.html not found at {TEMPLATE_FILE}. "
            "It must live next to scan.py."
        )
    return TEMPLATE_FILE.read_text(encoding="utf-8")
'''


SERVER_EXPLAIN_BLOCK = '''        if self.path == "/api/explain":
            # Force-regenerate (or generate) the markdown explanation for one
            # service and persist it to explanations.json. Called by the
            # "Regenerate" button in the dashboard.
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8") if length else "{}"
                payload = json.loads(body or "{}")
                label = (payload.get("label") or "").strip()
                if not label:
                    self._json(400, {"ok": False, "error": "missing label"})
                    return
            except Exception as e:  # noqa: BLE001
                self._json(400, {"ok": False, "error": f"bad request: {e}"})
                return

            with _latest_lock:
                snapshot = _latest_data
            if snapshot is None:
                self._json(503, {"ok": False, "error": "scan not ready"})
                return

            service = next(
                (s for s in snapshot["services"] if s.get("label") == label),
                None,
            )
            if service is None:
                self._json(404, {"ok": False, "error": f"unknown label: {label}"})
                return
            source = service.get("source") or {}
            if not source.get("content"):
                self._json(
                    422,
                    {"ok": False, "error": "no source captured for this service"},
                )
                return

            try:
                # Bust the cache so generate_explanation hits the API.
                cache = scan.load_cache()
                src_hash = scan._hash_source(
                    source.get("path", ""), source["content"]
                )
                cache_key = f"{label}::{src_hash}"
                if cache_key in cache:
                    cache.pop(cache_key, None)
                    scan.save_cache(cache)

                explanation = scan.generate_explanation(
                    source=source,
                    label=label,
                    schedule_summary=(service.get("schedule") or {}).get(
                        "summary", ""
                    ),
                    description=service.get("description") or "",
                )
            except Exception as e:  # noqa: BLE001
                self._json(500, {"ok": False, "error": str(e)})
                return

            if not explanation:
                self._json(
                    500,
                    {"ok": False, "error": "explanation generation returned empty"},
                )
                return

            # Update the in-memory snapshot so subsequent /api/data calls see
            # the new explanation immediately.
            with _latest_lock:
                if _latest_data is not None:
                    for s in _latest_data["services"]:
                        if s.get("label") == label:
                            s["explanation"] = explanation
                            break

            self._json(200, {"ok": True, "explanation": explanation})
            return

'''


def _backup(path: Path) -> Path:
    bak = path.with_suffix(path.suffix + ".bak")
    if bak.exists():
        print(f"  ✗ {bak.name} already exists — refusing to overwrite. "
              f"Move it aside if you really want to re-patch.")
        sys.exit(1)
    shutil.copy2(path, bak)
    print(f"  ✓ backed up {path.name} → {bak.name}")
    return bak


def patch_scan_py() -> bool:
    src = SCAN_PY.read_text(encoding="utf-8")
    if "TEMPLATE_FILE = SCRIPT_DIR" in src and 'HTML_TEMPLATE = r"""' not in src:
        print("  · scan.py already patched — skipping.")
        return False

    # Replace from the "# ── HTML rendering ──" banner through the end of the
    # giant HTML_TEMPLATE block (which terminates with: r\"\"\"\\n\\n).
    # Then the original render_html() follows; we keep it but it will now read
    # the fresh template via _load_template().
    pattern = re.compile(
        r"# ── HTML rendering ─+\n+HTML_TEMPLATE = r\"\"\".*?\"\"\"\n",
        re.DOTALL,
    )
    if not pattern.search(src):
        print("  ✗ couldn't find HTML_TEMPLATE block in scan.py — aborting.")
        sys.exit(2)

    new_src = pattern.sub(SCAN_LOADER_BLOCK, src, count=1)

    # Rewrite render_html so it pulls a fresh template.
    render_pat = re.compile(
        r"def render_html\(data: dict\[str, Any\]\) -> str:\n"
        r"    payload = json\.dumps\(data, default=str\)\n"
        r"    .*?html = HTML_TEMPLATE\.replace\(\"__HOST__\", data\.get\(\"host\", \"\"\)\)\n"
        r"    html = html\.replace\(\"__DATA__\", payload\)\n"
        r"    return html\n",
        re.DOTALL,
    )
    new_render = '''def render_html(data: dict[str, Any]) -> str:
    payload = json.dumps(data, default=str)
    # Defang sequences that would break the embedding <script>...</script> tag.
    payload = payload.replace("</", "<\\\\/")
    payload = payload.replace("\\u2028", "\\\\u2028").replace("\\u2029", "\\\\u2029")
    html = _load_template()
    html = html.replace("__HOST__", data.get("host", ""))
    html = html.replace("__DATA__", payload)
    return html
'''
    if not render_pat.search(new_src):
        print("  ✗ couldn't find render_html() in scan.py — aborting.")
        sys.exit(2)
    new_src = render_pat.sub(lambda _m: new_render, new_src, count=1)

    SCAN_PY.write_text(new_src, encoding="utf-8")
    print(f"  ✓ patched scan.py "
          f"({len(src):,} → {len(new_src):,} bytes; saved "
          f"{len(src) - len(new_src):,})")
    return True


def patch_server_py() -> bool:
    src = SERVER_PY.read_text(encoding="utf-8")
    if "/api/explain" in src:
        print("  · server.py already has /api/explain — skipping.")
        return False

    # Inject the new endpoint right before the existing "/api/fix" block.
    marker = '        if self.path == "/api/fix":'
    if marker not in src:
        print("  ✗ couldn't find /api/fix marker in server.py — aborting.")
        sys.exit(2)
    new_src = src.replace(marker, SERVER_EXPLAIN_BLOCK + marker, 1)
    SERVER_PY.write_text(new_src, encoding="utf-8")
    print(f"  ✓ patched server.py — added POST /api/explain")
    return True


def main() -> int:
    print("services-dashboard production patcher\n")

    for f in (SCAN_PY, SERVER_PY, TEMPLATE_HTML):
        if not f.exists():
            print(f"  ✗ missing required file: {f}")
            return 1
    print(f"  ✓ template.html present ({TEMPLATE_HTML.stat().st_size:,} bytes)\n")

    print("Backing up originals…")
    for f in (SCAN_PY, SERVER_PY):
        if f.with_suffix(f.suffix + ".bak").exists():
            print(f"  · {f.name}.bak already exists — leaving it; "
                  f"will overwrite {f.name} in place.")
        else:
            _backup(f)

    print("\nPatching scan.py…")
    patch_scan_py()
    print("\nPatching server.py…")
    patch_server_py()

    print("\nAll done. Restart the dashboard server:")
    print("    pkill -f 'python3 server.py' ; python3 server.py &")
    print("\nIf anything looks wrong, restore with:")
    print("    cp scan.py.bak scan.py && cp server.py.bak server.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
