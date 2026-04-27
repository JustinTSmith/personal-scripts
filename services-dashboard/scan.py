#!/usr/bin/env python3
"""
services-dashboard / scan.py

Scans ~/Library/LaunchAgents for all installed launchd services, cross-references
their runtime status from `launchctl list`, reads tail of their log files, and
generates a self-contained dashboard.html with embedded JSON.

Usage:
    python3 scan.py            # writes dashboard.html in this directory
    python3 scan.py --open     # also opens it in the default browser
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

HOME = Path.home()
LAUNCH_AGENTS_DIR = HOME / "Library" / "LaunchAgents"
SCRIPT_DIR = Path(__file__).resolve().parent
DASHBOARD_HTML = SCRIPT_DIR / "dashboard.html"

# Namespaces we consider "personal projects" — shown by default. Everything else
# (homebrew, google updaters, system stuff) is collapsed under "system" and
# hidden behind a filter toggle.
PERSONAL_NAMESPACES = {
    "com.justinsmith",
    "com.justinos",
    "com.openclaw",
    "ai.openclaw",
    "com.last30days",
    "com.paperclip",
    "com.mudrii",
    "com.user",
}

LOG_TAIL_LINES = 30


def run(cmd: list[str], **kw) -> str:
    """Run a command, return stdout. Never raises — returns '' on failure."""
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, **kw
        )
        return out.stdout
    except Exception:
        return ""


def get_launchctl_state() -> dict[str, dict[str, Any]]:
    """
    Parse `launchctl list` output. Returns {label: {pid, last_exit_code}}.
    Format: PID\tStatus\tLabel
    """
    state: dict[str, dict[str, Any]] = {}
    out = run(["launchctl", "list"])
    for line in out.splitlines()[1:]:  # skip header
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        pid_str, status_str, label = parts[0], parts[1], parts[2]
        pid = int(pid_str) if pid_str.isdigit() else None
        try:
            last_exit = int(status_str)
        except ValueError:
            last_exit = None
        state[label] = {"pid": pid, "last_exit_code": last_exit}
    return state


def parse_plist(path: Path) -> dict[str, Any] | None:
    """Parse a .plist file. Returns None if it can't be read."""
    try:
        with open(path, "rb") as f:
            return plistlib.load(f)
    except Exception:
        return None


def classify_schedule(plist: dict[str, Any]) -> dict[str, Any]:
    """
    Determine the schedule type and a human-readable description.
    Returns {type, summary, raw}.
    """
    if plist.get("KeepAlive"):
        ka = plist["KeepAlive"]
        if ka is True:
            return {"type": "keepalive", "summary": "Always-on (auto-restart)", "raw": True}
        return {"type": "keepalive", "summary": "Conditional keepalive", "raw": ka}

    if "StartCalendarInterval" in plist:
        sci = plist["StartCalendarInterval"]
        # Can be a dict or a list of dicts
        intervals = sci if isinstance(sci, list) else [sci]
        summaries = []
        for iv in intervals:
            parts = []
            if "Weekday" in iv:
                wd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][iv["Weekday"] % 7]
                parts.append(wd)
            if "Day" in iv:
                parts.append(f"day {iv['Day']}")
            h = iv.get("Hour")
            m = iv.get("Minute")
            if h is not None and m is not None:
                parts.append(f"{h:02d}:{m:02d}")
            elif h is not None:
                parts.append(f"{h:02d}:00")
            elif m is not None:
                parts.append(f":{m:02d} every hour")
            summaries.append(" ".join(parts) if parts else "scheduled")
        return {"type": "calendar", "summary": " · ".join(summaries), "raw": sci}

    if "StartInterval" in plist:
        secs = plist["StartInterval"]
        if secs >= 3600:
            summary = f"every {secs // 3600}h"
        elif secs >= 60:
            summary = f"every {secs // 60}m"
        else:
            summary = f"every {secs}s"
        return {"type": "interval", "summary": summary, "raw": secs}

    if plist.get("RunAtLoad"):
        return {"type": "ondemand", "summary": "Runs at load only", "raw": True}

    return {"type": "ondemand", "summary": "On-demand / triggered", "raw": None}


def status_for(state: dict[str, Any] | None, schedule: dict[str, Any], disabled: bool) -> str:
    """
    Compute a human-readable status:
      disabled   - .plist.disabled or Disabled key set
      running    - has an active PID
      error      - last exit code != 0 and not currently running
      scheduled  - registered, not running, scheduled to run
      loaded     - registered but not running and on-demand
      missing    - not in launchctl list at all
    """
    if disabled:
        return "disabled"
    if not state:
        return "missing"
    if state.get("pid"):
        return "running"
    exit_code = state.get("last_exit_code")
    if exit_code not in (0, None) and exit_code != 1:
        # Some scheduled jobs report code=1 when waiting; treat only clearly bad codes as error
        if exit_code > 1 or exit_code < 0:
            return "error"
    if schedule["type"] in {"calendar", "interval"}:
        return "scheduled"
    if schedule["type"] == "keepalive":
        # KeepAlive but not running = throttled or crashed
        return "error" if exit_code not in (0, None) else "loaded"
    return "loaded"


def tail_file(path: Path, n: int = LOG_TAIL_LINES) -> tuple[str, dict[str, Any]]:
    """Return (text, meta) for a log file. meta has size and mtime."""
    if not path or not path.exists():
        return "", {"size": 0, "mtime": None, "exists": False}
    try:
        size = path.stat().st_size
        mtime = dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
        # Read last N lines efficiently via tail
        out = run(["tail", "-n", str(n), str(path)])
        return out, {"size": size, "mtime": mtime, "exists": True}
    except Exception:
        return "", {"size": 0, "mtime": None, "exists": False}


def derive_namespace(label: str) -> str:
    """com.justinsmith.foo -> com.justinsmith"""
    parts = label.split(".")
    if len(parts) >= 3:
        return ".".join(parts[:2])
    return parts[0] if parts else "?"


def derive_name(label: str) -> str:
    """com.justinsmith.foo -> foo"""
    parts = label.split(".")
    return parts[-1] if parts else label


def project_dir_from_args(program_args: list[str], working_directory: str | None) -> str | None:
    """
    Best-effort: infer the project directory the service is running from.
    Look at the longest path-like argument that exists on disk.
    """
    if working_directory and Path(working_directory).is_dir():
        return working_directory
    candidates = []
    for arg in program_args or []:
        if isinstance(arg, str) and arg.startswith("/"):
            p = Path(arg)
            # Walk up parents to find an existing directory containing the file
            if p.exists():
                if p.is_dir():
                    candidates.append(str(p))
                else:
                    candidates.append(str(p.parent))
    if candidates:
        # Prefer paths under ~/Workspace
        ws = str(HOME / "Workspace")
        ws_paths = [c for c in candidates if c.startswith(ws)]
        if ws_paths:
            return max(ws_paths, key=len)
        return max(candidates, key=len)
    return None


def scan() -> dict[str, Any]:
    plists = sorted(LAUNCH_AGENTS_DIR.glob("*.plist*"))  # include .disabled
    state = get_launchctl_state()
    services = []

    for path in plists:
        # Skip backup files
        if any(suffix in path.name for suffix in (".bak.", ".superseded.")):
            continue
        disabled = path.suffix == ".disabled" or ".disabled" in path.name

        plist = parse_plist(path) if not disabled else parse_plist(path)
        if not plist:
            continue

        label = plist.get("Label") or path.stem.replace(".plist", "")
        namespace = derive_namespace(label)
        name = derive_name(label)

        program_args = plist.get("ProgramArguments") or (
            [plist["Program"]] if "Program" in plist else []
        )
        working_directory = plist.get("WorkingDirectory")
        project_dir = project_dir_from_args(program_args, working_directory)

        schedule = classify_schedule(plist)

        runtime = state.get(label)
        status = status_for(runtime, schedule, disabled)

        stdout_path = plist.get("StandardOutPath")
        stderr_path = plist.get("StandardErrorPath")
        stdout_tail, stdout_meta = tail_file(Path(stdout_path)) if stdout_path else ("", {"exists": False})
        stderr_tail, stderr_meta = tail_file(Path(stderr_path)) if stderr_path else ("", {"exists": False})

        # Activity heuristic: most recent log mtime
        activity = None
        for meta in (stdout_meta, stderr_meta):
            if meta.get("mtime"):
                if not activity or meta["mtime"] > activity:
                    activity = meta["mtime"]

        services.append({
            "label": label,
            "name": name,
            "namespace": namespace,
            "is_personal": namespace in PERSONAL_NAMESPACES,
            "plist_path": str(path),
            "disabled": disabled,
            "status": status,
            "pid": runtime.get("pid") if runtime else None,
            "last_exit_code": runtime.get("last_exit_code") if runtime else None,
            "schedule": schedule,
            "program_args": program_args,
            "working_directory": working_directory,
            "project_dir": project_dir,
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "stdout_meta": stdout_meta,
            "stderr_meta": stderr_meta,
            "last_activity": activity,
            "environment": plist.get("EnvironmentVariables") or {},
        })

    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "host": os.uname().nodename,
        "user": os.environ.get("USER", "?"),
        "launch_agents_dir": str(LAUNCH_AGENTS_DIR),
        "services": services,
    }


# ── HTML rendering ──────────────────────────────────────────────────────────


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Services Dashboard — __HOST__</title>
<style>
  :root {
    --bg: #0f1115;
    --panel: #161922;
    --panel-2: #1d212c;
    --border: #2a2f3d;
    --text: #e7eaf0;
    --muted: #8a93a6;
    --accent: #6ea8fe;
    --green: #4ade80;
    --amber: #f59e0b;
    --red: #ef4444;
    --gray: #6b7280;
    --blue: #3b82f6;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f6f7fb;
      --panel: #ffffff;
      --panel-2: #f0f2f8;
      --border: #dfe3ec;
      --text: #1a1d24;
      --muted: #6b7280;
    }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text); font: 14px/1.5 -apple-system, BlinkMacSystemFont, system-ui, sans-serif; }
  header { padding: 20px 28px; border-bottom: 1px solid var(--border); display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap; }
  header h1 { margin: 0; font-size: 20px; font-weight: 600; }
  header .meta { color: var(--muted); font-size: 12px; }
  header .stats { margin-left: auto; display: flex; gap: 14px; font-size: 12px; color: var(--muted); }
  header .stats b { color: var(--text); font-weight: 600; }
  .toolbar { display: flex; gap: 8px; padding: 14px 28px; border-bottom: 1px solid var(--border); flex-wrap: wrap; align-items: center; background: var(--panel); }
  .toolbar input[type=search] { flex: 1; min-width: 200px; padding: 8px 12px; background: var(--panel-2); border: 1px solid var(--border); border-radius: 6px; color: var(--text); font: inherit; }
  .toolbar button, .chip {
    padding: 6px 12px; background: var(--panel-2); border: 1px solid var(--border); border-radius: 999px;
    color: var(--text); font: inherit; font-size: 12px; cursor: pointer; user-select: none;
  }
  .toolbar button:hover, .chip:hover { border-color: var(--accent); }
  .chip.active { background: var(--accent); color: #000; border-color: var(--accent); }
  .chip .count { opacity: 0.6; margin-left: 6px; font-variant-numeric: tabular-nums; }
  main { padding: 18px 28px 80px; }
  .group { margin-bottom: 28px; }
  .group h2 { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin: 0 0 10px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 12px; }
  .card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px;
    cursor: pointer; transition: transform 0.08s ease, border-color 0.08s ease;
  }
  .card:hover { border-color: var(--accent); }
  .card .top { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }
  .card .name { font-weight: 600; font-size: 15px; word-break: break-all; }
  .card .label { color: var(--muted); font-size: 11px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all; }
  .badge {
    display: inline-flex; align-items: center; gap: 6px; padding: 3px 9px; border-radius: 999px;
    font-size: 11px; font-weight: 600; white-space: nowrap;
  }
  .badge::before { content: ''; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
  .badge.running { color: var(--green); background: rgba(74,222,128,0.12); }
  .badge.scheduled { color: var(--blue); background: rgba(59,130,246,0.12); }
  .badge.loaded { color: var(--gray); background: rgba(107,114,128,0.18); }
  .badge.error { color: var(--red); background: rgba(239,68,68,0.14); }
  .badge.disabled { color: var(--muted); background: rgba(139,147,166,0.14); }
  .badge.missing { color: var(--amber); background: rgba(245,158,11,0.14); }
  .card .meta { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; font-size: 12px; color: var(--muted); }
  .card .meta span { display: inline-flex; align-items: center; gap: 4px; }
  .card .meta b { color: var(--text); font-weight: 500; }
  .activity { font-size: 11px; color: var(--muted); margin-top: 8px; font-variant-numeric: tabular-nums; }
  /* Drawer */
  .drawer-bg { position: fixed; inset: 0; background: rgba(0,0,0,0.5); display: none; z-index: 50; }
  .drawer-bg.open { display: block; }
  .drawer {
    position: fixed; top: 0; right: 0; bottom: 0; width: min(720px, 96vw);
    background: var(--panel); border-left: 1px solid var(--border); z-index: 51;
    transform: translateX(100%); transition: transform 0.18s ease; overflow-y: auto;
  }
  .drawer.open { transform: translateX(0); }
  .drawer header { position: sticky; top: 0; background: var(--panel); }
  .drawer h3 { margin: 18px 28px 6px; font-size: 12px; text-transform: uppercase; color: var(--muted); letter-spacing: 0.06em; }
  .drawer .section { padding: 0 28px 14px; }
  .drawer pre {
    background: var(--panel-2); padding: 12px; border-radius: 6px; border: 1px solid var(--border);
    font: 11.5px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; overflow-x: auto;
    max-height: 360px; overflow-y: auto; white-space: pre-wrap; word-break: break-word;
  }
  .drawer .kv { display: grid; grid-template-columns: 140px 1fr; gap: 6px 14px; font-size: 13px; }
  .drawer .kv dt { color: var(--muted); }
  .drawer .kv dd { margin: 0; word-break: break-all; }
  .close { background: transparent; border: 0; color: var(--text); font-size: 24px; cursor: pointer; padding: 0 8px; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .empty { padding: 40px 0; text-align: center; color: var(--muted); }
  .ago { font-variant-numeric: tabular-nums; }
</style>
</head>
<body>
<header>
  <h1>Services Dashboard</h1>
  <span class="meta" id="generated"></span>
  <div class="stats" id="stats"></div>
</header>

<div class="toolbar">
  <input type="search" id="search" placeholder="Search by name, label, project path…" autofocus>
  <span class="chip active" data-status="all">All</span>
  <span class="chip" data-status="running">Running</span>
  <span class="chip" data-status="scheduled">Scheduled</span>
  <span class="chip" data-status="error">Error</span>
  <span class="chip" data-status="disabled">Disabled</span>
  <span style="width:1px;height:20px;background:var(--border);margin:0 4px"></span>
  <span class="chip active" data-scope="personal">Personal</span>
  <span class="chip" data-scope="all">+ System</span>
</div>

<main id="main"></main>

<div class="drawer-bg" id="drawer-bg"></div>
<aside class="drawer" id="drawer">
  <header>
    <h1 id="drawer-title">—</h1>
    <button class="close" id="drawer-close">×</button>
  </header>
  <div id="drawer-body"></div>
</aside>

<script>
const DATA = __DATA__;

const STATE = {
  search: '',
  status: 'all',
  scope: 'personal',
};

function timeAgo(iso) {
  if (!iso) return '—';
  const t = new Date(iso).getTime();
  const diff = Date.now() - t;
  const s = Math.floor(diff / 1000);
  if (s < 60) return s + 's ago';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm ago';
  const h = Math.floor(m / 60);
  if (h < 48) return h + 'h ago';
  const d = Math.floor(h / 24);
  return d + 'd ago';
}

function fmtBytes(n) {
  if (!n) return '0 B';
  if (n < 1024) return n + ' B';
  if (n < 1024*1024) return (n/1024).toFixed(1) + ' KB';
  return (n/1024/1024).toFixed(1) + ' MB';
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function renderHeader() {
  document.getElementById('generated').textContent =
    `Generated ${timeAgo(DATA.generated_at)} · ${DATA.host} · ${DATA.services.length} services`;

  const counts = { running: 0, scheduled: 0, error: 0, loaded: 0, disabled: 0, missing: 0 };
  for (const s of DATA.services) counts[s.status] = (counts[s.status] || 0) + 1;
  document.getElementById('stats').innerHTML = `
    <span><b>${counts.running}</b> running</span>
    <span><b>${counts.scheduled}</b> scheduled</span>
    <span><b>${counts.error || 0}</b> errors</span>
    <span><b>${counts.disabled}</b> disabled</span>
  `;

  // Update chip counts
  for (const chip of document.querySelectorAll('.chip[data-status]')) {
    const status = chip.dataset.status;
    const count = status === 'all' ? DATA.services.length : (counts[status] || 0);
    if (!chip.querySelector('.count')) {
      chip.insertAdjacentHTML('beforeend', `<span class="count">${count}</span>`);
    }
  }
}

function filtered() {
  const q = STATE.search.trim().toLowerCase();
  return DATA.services.filter(s => {
    if (STATE.scope === 'personal' && !s.is_personal) return false;
    if (STATE.status !== 'all' && s.status !== STATE.status) return false;
    if (q) {
      const hay = (s.label + ' ' + s.name + ' ' + (s.project_dir || '') + ' ' + (s.program_args || []).join(' ')).toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function render() {
  const main = document.getElementById('main');
  const items = filtered();
  if (!items.length) {
    main.innerHTML = `<div class="empty">No services match.</div>`;
    return;
  }

  // Group by namespace
  const byNs = {};
  for (const s of items) (byNs[s.namespace] ||= []).push(s);
  const order = Object.keys(byNs).sort((a, b) => byNs[b].length - byNs[a].length);

  main.innerHTML = order.map(ns => `
    <section class="group">
      <h2>${escapeHtml(ns)} <span style="opacity:0.5">· ${byNs[ns].length}</span></h2>
      <div class="grid">
        ${byNs[ns].map(card).join('')}
      </div>
    </section>
  `).join('');

  for (const el of main.querySelectorAll('.card')) {
    el.addEventListener('click', () => openDrawer(el.dataset.label));
  }
}

function card(s) {
  const sched = s.schedule || {};
  const stderrSize = (s.stderr_meta && s.stderr_meta.size) || 0;
  return `
    <div class="card" data-label="${escapeHtml(s.label)}">
      <div class="top">
        <div>
          <div class="name">${escapeHtml(s.name)}</div>
          <div class="label">${escapeHtml(s.label)}</div>
        </div>
        <span class="badge ${s.status}">${s.status}${s.pid ? ' · ' + s.pid : ''}</span>
      </div>
      <div class="meta">
        <span>📅 <b>${escapeHtml(sched.summary || '')}</b></span>
        ${stderrSize > 0 ? `<span style="color:var(--amber)">⚠️ stderr ${fmtBytes(stderrSize)}</span>` : ''}
      </div>
      <div class="activity">last activity: <span class="ago">${timeAgo(s.last_activity)}</span></div>
    </div>
  `;
}

function openDrawer(label) {
  const s = DATA.services.find(x => x.label === label);
  if (!s) return;
  document.getElementById('drawer-title').textContent = s.name;
  const sched = s.schedule || {};
  document.getElementById('drawer-body').innerHTML = `
    <div class="section">
      <span class="badge ${s.status}">${s.status}${s.pid ? ' · pid ' + s.pid : ''}</span>
    </div>

    <h3>Identity</h3>
    <div class="section">
      <dl class="kv">
        <dt>Label</dt><dd><code>${escapeHtml(s.label)}</code></dd>
        <dt>Namespace</dt><dd>${escapeHtml(s.namespace)}</dd>
        <dt>Plist</dt><dd><code>${escapeHtml(s.plist_path)}</code></dd>
        ${s.project_dir ? `<dt>Project dir</dt><dd><code>${escapeHtml(s.project_dir)}</code></dd>` : ''}
      </dl>
    </div>

    <h3>Schedule</h3>
    <div class="section">
      <dl class="kv">
        <dt>Type</dt><dd>${escapeHtml(sched.type || '—')}</dd>
        <dt>Summary</dt><dd>${escapeHtml(sched.summary || '—')}</dd>
        <dt>Last exit code</dt><dd>${s.last_exit_code == null ? '—' : s.last_exit_code}</dd>
      </dl>
    </div>

    <h3>Command</h3>
    <div class="section">
      <pre>${escapeHtml((s.program_args || []).join(' \\\n  '))}</pre>
    </div>

    ${s.stdout_path ? `
      <h3>stdout — <code>${escapeHtml(s.stdout_path)}</code> · ${fmtBytes(s.stdout_meta?.size || 0)} · ${timeAgo(s.stdout_meta?.mtime)}</h3>
      <div class="section">
        <pre>${escapeHtml(s.stdout_tail || '(empty)')}</pre>
      </div>
    ` : ''}

    ${s.stderr_path ? `
      <h3>stderr — <code>${escapeHtml(s.stderr_path)}</code> · ${fmtBytes(s.stderr_meta?.size || 0)} · ${timeAgo(s.stderr_meta?.mtime)}</h3>
      <div class="section">
        <pre>${escapeHtml(s.stderr_tail || '(empty)')}</pre>
      </div>
    ` : ''}

    ${Object.keys(s.environment || {}).length ? `
      <h3>Environment</h3>
      <div class="section">
        <pre>${escapeHtml(Object.entries(s.environment).map(([k,v]) => k + '=' + v).join('\n'))}</pre>
      </div>
    ` : ''}
  `;
  document.getElementById('drawer').classList.add('open');
  document.getElementById('drawer-bg').classList.add('open');
}

document.getElementById('drawer-close').addEventListener('click', closeDrawer);
document.getElementById('drawer-bg').addEventListener('click', closeDrawer);
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });
function closeDrawer() {
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('drawer-bg').classList.remove('open');
}

document.getElementById('search').addEventListener('input', e => {
  STATE.search = e.target.value; render();
});

for (const chip of document.querySelectorAll('.chip[data-status]')) {
  chip.addEventListener('click', () => {
    document.querySelectorAll('.chip[data-status]').forEach(c => c.classList.remove('active'));
    chip.classList.add('active');
    STATE.status = chip.dataset.status; render();
  });
}
for (const chip of document.querySelectorAll('.chip[data-scope]')) {
  chip.addEventListener('click', () => {
    document.querySelectorAll('.chip[data-scope]').forEach(c => c.classList.remove('active'));
    chip.classList.add('active');
    STATE.scope = chip.dataset.scope; render();
  });
}

renderHeader();
render();
</script>
</body>
</html>
"""


def render_html(data: dict[str, Any]) -> str:
    payload = json.dumps(data, default=str)
    html = HTML_TEMPLATE.replace("__HOST__", data.get("host", ""))
    html = html.replace("__DATA__", payload)
    return html


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--open", action="store_true", help="Open dashboard.html after generating")
    p.add_argument("--json-only", action="store_true", help="Print JSON to stdout, don't write HTML")
    args = p.parse_args()

    data = scan()

    if args.json_only:
        print(json.dumps(data, indent=2, default=str))
        return 0

    DASHBOARD_HTML.write_text(render_html(data))
    print(f"Wrote {DASHBOARD_HTML}")
    print(f"Services: {len(data['services'])} "
          f"({sum(1 for s in data['services'] if s['status']=='running')} running, "
          f"{sum(1 for s in data['services'] if s['status']=='scheduled')} scheduled, "
          f"{sum(1 for s in data['services'] if s['status']=='error')} errors)")

    if args.open:
        subprocess.run(["open", str(DASHBOARD_HTML)])

    return 0


if __name__ == "__main__":
    sys.exit(main())
