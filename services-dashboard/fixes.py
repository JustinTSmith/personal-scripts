"""
fixes.py — pure functions for auto-fix actions on launchd services.

Each function takes a service label and returns:
    {"ok": bool, "message": str, "stdout": str, "stderr": str}

Called from server.py via the /api/fix endpoint. Do not invoke arbitrary
commands; the action whitelist is enforced by the server.
"""

from __future__ import annotations

import plistlib
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"


def _find_plist(label: str) -> Path | None:
    """Locate the plist for a label, including .disabled / .superseded variants."""
    for stem_suffix in ("", ".disabled"):
        p = LAUNCH_AGENTS_DIR / f"{label}.plist{stem_suffix}"
        if p.exists():
            return p
    # Fall back to glob in case the file has additional suffixes
    matches = list(LAUNCH_AGENTS_DIR.glob(f"{label}.plist*"))
    return matches[0] if matches else None


def _run(args: list[str]) -> dict[str, Any]:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=15)
        return {"ok": r.returncode == 0, "stdout": r.stdout.strip(), "stderr": r.stderr.strip()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "stdout": "", "stderr": str(e)}


# ── Individual fix actions ─────────────────────────────────────────────────


def fix_unload(label: str) -> dict[str, Any]:
    p = _find_plist(label)
    if not p:
        return {"ok": False, "message": "plist not found", "stdout": "", "stderr": ""}
    r = _run(["launchctl", "unload", str(p)])
    r["message"] = f"Unloaded {label}" if r["ok"] else f"Failed to unload: {r['stderr']}"
    return r


def fix_load(label: str) -> dict[str, Any]:
    p = _find_plist(label)
    if not p:
        return {"ok": False, "message": "plist not found", "stdout": "", "stderr": ""}
    if p.suffix == ".disabled":
        return {"ok": False, "message": "Service is disabled — use 'enable' to re-enable.", "stdout": "", "stderr": ""}
    r = _run(["launchctl", "load", str(p)])
    r["message"] = f"Loaded {label}" if r["ok"] else f"Failed to load: {r['stderr']}"
    return r


def fix_restart(label: str) -> dict[str, Any]:
    p = _find_plist(label)
    if not p:
        return {"ok": False, "message": "plist not found", "stdout": "", "stderr": ""}
    if p.suffix == ".disabled":
        return {"ok": False, "message": "Service is disabled.", "stdout": "", "stderr": ""}
    _run(["launchctl", "unload", str(p)])
    r = _run(["launchctl", "load", str(p)])
    r["message"] = f"Restarted {label}" if r["ok"] else f"Restart failed: {r['stderr']}"
    return r


def fix_disable(label: str) -> dict[str, Any]:
    p = _find_plist(label)
    if not p:
        return {"ok": False, "message": "plist not found", "stdout": "", "stderr": ""}
    if p.suffix == ".disabled":
        return {"ok": True, "message": "Already disabled.", "stdout": "", "stderr": ""}
    _run(["launchctl", "unload", str(p)])
    new_path = p.with_name(p.name + ".disabled")
    try:
        p.rename(new_path)
        return {"ok": True, "message": f"Disabled {label} (renamed to {new_path.name})",
                "stdout": "", "stderr": ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"Rename failed: {e}", "stdout": "", "stderr": ""}


def fix_enable(label: str) -> dict[str, Any]:
    p = _find_plist(label)
    if not p:
        return {"ok": False, "message": "plist not found", "stdout": "", "stderr": ""}
    if p.suffix == ".disabled":
        new_path = p.with_suffix("")  # strips ".disabled"
        try:
            p.rename(new_path)
            p = new_path
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"Rename failed: {e}", "stdout": "", "stderr": ""}
    r = _run(["launchctl", "load", str(p)])
    r["message"] = f"Enabled {label}" if r["ok"] else f"Renamed but load failed: {r['stderr']}"
    return r


def fix_clear_stderr(label: str) -> dict[str, Any]:
    """Truncate the StandardErrorPath log file (no service interruption)."""
    p = _find_plist(label)
    if not p:
        return {"ok": False, "message": "plist not found", "stdout": "", "stderr": ""}
    try:
        plist = plistlib.loads(p.read_bytes())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"Could not parse plist: {e}", "stdout": "", "stderr": ""}
    err_path = plist.get("StandardErrorPath")
    if not err_path:
        return {"ok": False, "message": "No StandardErrorPath defined in plist.", "stdout": "", "stderr": ""}
    f = Path(err_path)
    if not f.exists():
        return {"ok": True, "message": f"stderr already empty ({err_path})", "stdout": "", "stderr": ""}
    try:
        size_before = f.stat().st_size
        f.write_text("")
        return {"ok": True, "message": f"Cleared {size_before} bytes from {err_path}",
                "stdout": "", "stderr": ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"Truncate failed: {e}", "stdout": "", "stderr": ""}


def fix_python_path(label: str) -> dict[str, Any]:
    """Replace versioned Cellar Python paths with /opt/homebrew/bin/python3."""
    p = _find_plist(label)
    if not p:
        return {"ok": False, "message": "plist not found", "stdout": "", "stderr": ""}
    try:
        plist = plistlib.loads(p.read_bytes())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"Could not parse plist: {e}", "stdout": "", "stderr": ""}

    args = plist.get("ProgramArguments") or []
    pat = re.compile(r"/opt/homebrew/Cellar/python@3\.\d+/[\d.]+/Frameworks/Python\.framework/Versions/3\.\d+/[^/]+/Python\.app/Contents/MacOS/Python")
    pat_simple = re.compile(r"/opt/homebrew/Cellar/python@3\.\d+/[\d.]+/bin/python3?")
    new_args = []
    changed = False
    for a in args:
        if isinstance(a, str):
            new = pat.sub("/opt/homebrew/bin/python3", a)
            new = pat_simple.sub("/opt/homebrew/bin/python3", new)
            if new != a:
                changed = True
            new_args.append(new)
        else:
            new_args.append(a)
    if not changed:
        return {"ok": False, "message": "No stale Python paths found in plist's ProgramArguments. (The runtime error may be coming from a wrapper or venv shebang — fix that file directly.)",
                "stdout": "", "stderr": ""}

    plist["ProgramArguments"] = new_args
    try:
        with open(p, "wb") as fh:
            plistlib.dump(plist, fh)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"Plist write failed: {e}", "stdout": "", "stderr": ""}

    _run(["launchctl", "unload", str(p)])
    r = _run(["launchctl", "load", str(p)])
    r["message"] = f"Python path updated and reloaded" if r["ok"] else f"Edited but reload failed: {r['stderr']}"
    return r


# ── Whitelist ─────────────────────────────────────────────────────────────


ACTIONS: dict[str, Callable[[str], dict[str, Any]]] = {
    "unload": fix_unload,
    "load": fix_load,
    "restart": fix_restart,
    "disable": fix_disable,
    "enable": fix_enable,
    "clear_stderr": fix_clear_stderr,
    "fix_python_path": fix_python_path,
}


def apply(action: str, label: str) -> dict[str, Any]:
    if action not in ACTIONS:
        return {"ok": False, "message": f"Unknown action: {action}", "stdout": "", "stderr": ""}
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", label):
        return {"ok": False, "message": "Invalid label", "stdout": "", "stderr": ""}
    return ACTIONS[action](label)
