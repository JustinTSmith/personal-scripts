#!/usr/bin/env python3
"""Obsidian Voice Note Processor

Monitors Life OS inbox for audio files, transcribes with local Whisper,
uses OpenAI to categorize, appends to capture.md, and creates a
per-note markdown file moved to a category-specific folder in the vault.
"""

import glob
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from openai import OpenAI

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Base paths (no backslashes; use proper spaces)
VAULT_ROOT = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Main Vault"
LIFE_OS_DIR = VAULT_ROOT / "Life OS"
INBOX_DIR = LIFE_OS_DIR / "Inbox" / "Voice"
CAPTURE_FILE = INBOX_DIR / "capture.md"

CHECK_INTERVAL = 300  # 5 minutes
AUDIO_EXTENSIONS = [".m4a", ".mp3", ".wav", ".m4v", ".mp4"]
WHISPER_MODEL = "whisper-1"  # OpenAI API model

ICLOUD_DOWNLOAD_TIMEOUT = 60   # seconds to wait for a file to download
ICLOUD_DOWNLOAD_POLL    = 2    # seconds between download checks

# Persistent scratch dir outside iCloud — survives crashes and copy retries
SCRATCH_DIR = Path.home() / "Workspace" / "scripts" / ".voice_scratch"

# Category → target subfolder under Life OS
CATEGORY_FOLDERS = {
    "📝 Note": LIFE_OS_DIR / "Inbox",          # general goes to Inbox
    "✅ Todo": LIFE_OS_DIR / "Tasks",
    "💭 Journal": LIFE_OS_DIR / "Journal",
    "💡 Idea": LIFE_OS_DIR / "Ideas",
    "📚 Learning": LIFE_OS_DIR / "Learning",
    "🎯 Goal": LIFE_OS_DIR / "Goals",
    "❓ Question": LIFE_OS_DIR / "Questions",
    "👥 Relations": LIFE_OS_DIR / "Personal" / "Relations",  # per-person subfolders
}

# Dedicated subfolder for notes about Mira under Journal
MIRA_DIR = LIFE_OS_DIR / "Journal" / "Mira"
MIRA_SLEEP_LOG = MIRA_DIR / "sleep-log.md"

# Food diary lives under Journal
FOOD_DIARY_DIR = LIFE_OS_DIR / "Journal" / "Food Diary"

RELATIONS_DIR = LIFE_OS_DIR / "Personal" / "Relations"

# Keywords that trigger Mira sleep-log appending
SLEEP_KEYWORDS = {"sleep", "slept", "sleeping", "sleeps", "nap", "napped", "napping",
                  "woke", "woken", "woke up", "wake", "bedtime", "overnight", "night"}

# ---------------------------------------------------------------------------
# Model initialization
# ---------------------------------------------------------------------------

client = OpenAI()  # uses OPENAI_API_KEY from env


# ---------------------------------------------------------------------------
# iCloud helpers
# ---------------------------------------------------------------------------

def is_icloud_placeholder(path: Path) -> bool:
    """Return True if the file is an iCloud evicted placeholder.
    
    iCloud placeholders are hidden dotfiles named .<filename>.icloud
    e.g. .MyRecording.m4a.icloud — the real file doesn't exist locally yet.
    """
    placeholder = path.parent / f".{path.name}.icloud"
    return placeholder.exists() and not path.exists()


def force_icloud_download(path: Path) -> bool:
    """Trigger iCloud download for a file and wait until it's local.
    
    Uses `brctl download` which is the supported macOS API for this.
    Returns True if the file is available locally within the timeout.
    """
    placeholder = path.parent / f".{path.name}.icloud"

    if not placeholder.exists():
        # Already local or doesn't exist at all
        return path.exists()

    print(f"  ☁️  iCloud placeholder detected for {path.name} — triggering download…")
    try:
        subprocess.run(["brctl", "download", str(path)], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        print(f"  ✗ brctl download failed: {e.stderr.decode().strip()}")
        return False

    # Poll until the real file appears and placeholder is gone
    elapsed = 0
    while elapsed < ICLOUD_DOWNLOAD_TIMEOUT:
        if path.exists() and not placeholder.exists():
            print(f"  ✓ Downloaded in {elapsed}s")
            return True
        time.sleep(ICLOUD_DOWNLOAD_POLL)
        elapsed += ICLOUD_DOWNLOAD_POLL
        print(f"  ⏳ Waiting for download… ({elapsed}s/{ICLOUD_DOWNLOAD_TIMEOUT}s)")

    print(f"  ✗ Timed out waiting for {path.name} to download from iCloud")
    return False


def get_audio_files(directory: Path) -> list[Path]:
    """Return all audio files in directory, including iCloud placeholders.
    
    Scans both real files and .icloud placeholder files so evicted audio
    files are not silently skipped.
    """
    files: list[Path] = []

    for ext in AUDIO_EXTENSIONS:
        # Real local files
        files.extend(directory.glob(f"*{ext}"))
        # iCloud placeholders: hidden files like .MyRecording.m4a.icloud
        for placeholder in directory.glob(f".*{ext}.icloud"):
            # Reconstruct the real (intended) path
            real_name = placeholder.name[1:].removesuffix(".icloud")  # strip leading dot + .icloud suffix
            real_path = directory / real_name
            if real_path not in files:
                files.append(real_path)

    # Process in mtime order (use placeholder mtime if real file not local yet)
    def sort_key(p: Path) -> float:
        if p.exists():
            return p.stat().st_mtime
        placeholder = p.parent / f".{p.name}.icloud"
        return placeholder.stat().st_mtime if placeholder.exists() else 0.0

    return sorted(files, key=sort_key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

from typing import Optional


def ensure_dirs():
    """Ensure base Life OS subfolders exist."""
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    for path in CATEGORY_FOLDERS.values():
        path.mkdir(parents=True, exist_ok=True)
    MIRA_DIR.mkdir(parents=True, exist_ok=True)
    FOOD_DIARY_DIR.mkdir(parents=True, exist_ok=True)
    RELATIONS_DIR.mkdir(parents=True, exist_ok=True)
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)


def _copy_via_finder(audio_path: Path, dest: Path) -> bool:
    """Copy via Finder using AppleScript — participates in NSFileCoordinator.

    Finder acquires a coordinated read, which is the only reliable way to
    read iCloud-managed files that have an active file-provider lock (EDEADLK).
    The script copies to SCRATCH_DIR and renames to dest if needed.
    """
    scratch_dir = dest.parent
    # AppleScript expects the destination to be a folder, not a file path.
    # Finder names the copy identically to the source, so we rename afterward.
    script = (
        f'tell application "Finder"\n'
        f'    set srcFile to POSIX file "{audio_path}" as alias\n'
        f'    set dstFolder to POSIX file "{scratch_dir}" as alias\n'
        f'    duplicate srcFile to dstFolder with replacing\n'
        f'end tell\n'
    )
    result = subprocess.run(["osascript", "-e", script], capture_output=True)
    if result.returncode != 0:
        err = result.stderr.decode().strip()
        raise RuntimeError(f"osascript: {err}")

    # Finder names the copy the same as the source; rename to dest if different.
    finder_copy = scratch_dir / audio_path.name
    if finder_copy != dest and finder_copy.exists():
        finder_copy.rename(dest)

    return dest.exists() and dest.stat().st_size > 0


def _evict_and_redownload(audio_path: Path) -> bool:
    """Nuclear option: evict the file from iCloud then force a fresh download.

    Resets stuck file-provider locks that survive all other methods.
    After re-download the file is a fresh local copy with no active lock.
    """
    print(f"  ☢️  Evicting and re-downloading {audio_path.name}…")
    try:
        subprocess.run(["brctl", "evict", str(audio_path)], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        print(f"  ✗ brctl evict failed: {e.stderr.decode().strip()}")
        return False

    # Wait for the placeholder to appear
    placeholder = audio_path.parent / f".{audio_path.name}.icloud"
    for _ in range(10):
        if placeholder.exists():
            break
        time.sleep(1)

    return force_icloud_download(audio_path)


def _copy_out_of_icloud(audio_path: Path, dest: Path) -> bool:
    """Copy an iCloud-managed audio file to dest outside the iCloud tree.

    Method order (each retries 3× with 5 s backoff):
      1. Finder/AppleScript — participates in NSFileCoordinator; the only
         reliable path when the iCloud file-provider holds an active lock.
      2. ditto              — Apple copy tool; uses a different copyfile path
         than cp; works when the lock has been released between retries.
      3. Evict + re-download + ditto — nuclear reset; clears stuck locks by
         forcing iCloud to evict and serve a fresh download.

    dest lives in SCRATCH_DIR (outside iCloud) so a crash mid-transcription
    leaves the copy intact and reusable on the next poll cycle.
    """
    # --- Method 1: Finder (NSFileCoordinator) ---
    print(f"  📋 Trying Finder copy (NSFileCoordinator)…")
    for attempt in range(3):
        try:
            if _copy_via_finder(audio_path, dest):
                print(f"  ✓ Copied via Finder (attempt {attempt + 1})")
                return True
        except Exception as e:
            print(f"  ⏳ Finder attempt {attempt + 1}/3 failed ({e}), retrying in 5s…")
        time.sleep(5)
    print(f"  ✗ Finder exhausted — trying ditto…")

    # --- Method 2: ditto ---
    print(f"  📋 Trying ditto…")
    for attempt in range(3):
        try:
            result = subprocess.run(["ditto", str(audio_path), str(dest)], capture_output=True)
            if result.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
                print(f"  ✓ Copied via ditto (attempt {attempt + 1})")
                return True
            err = result.stderr.decode().strip()
            print(f"  ⏳ ditto attempt {attempt + 1}/3 failed ({err!r}), retrying in 5s…")
        except Exception as e:
            print(f"  ⏳ ditto attempt {attempt + 1}/3 raised {e}, retrying in 5s…")
        time.sleep(5)
    print(f"  ✗ ditto exhausted — trying evict+redownload…")

    # --- Method 3: Evict + re-download + Finder ---
    if _evict_and_redownload(audio_path):
        print(f"  📋 Re-download succeeded — retrying Finder copy…")
        try:
            if _copy_via_finder(audio_path, dest):
                print(f"  ✓ Copied via Finder after evict+redownload")
                return True
        except Exception as e:
            print(f"  ✗ Finder copy after re-download failed: {e}")
    else:
        print(f"  ✗ Evict+redownload failed")

    return False


def transcribe_audio(audio_path: Path) -> Optional[str]:
    """Transcribe audio file using local Whisper model.

    Copy pipeline (mission-critical — three methods with retries):
      1. ditto   — primary; avoids EDEADLK from iCloud file-provider locks
      2. rsync   — secondary; independent block-transfer path
      3. Python read_bytes — final fallback; pure userspace byte copy

    Copies land in SCRATCH_DIR (outside iCloud) so a crash mid-transcription
    leaves the copy intact for the next poll cycle.
    """
    # --- iCloud gate ---
    if is_icloud_placeholder(audio_path):
        if not force_icloud_download(audio_path):
            print(f"  ✗ Skipping {audio_path.name} — could not download from iCloud")
            return None

    if not audio_path.exists():
        print(f"  ✗ Skipping {audio_path.name} — file not found locally")
        return None

    print(f"Transcribing: {audio_path.name}")

    # Use a stable scratch path named after the source file so a leftover copy
    # from a previous crashed run can be detected and reused.
    scratch_path = SCRATCH_DIR / audio_path.name

    # Reuse an existing scratch copy if it looks complete (same size).
    reused = False
    if scratch_path.exists():
        try:
            if scratch_path.stat().st_size == audio_path.stat().st_size:
                print(f"  ♻️  Reusing existing scratch copy: {scratch_path.name}")
                reused = True
            else:
                scratch_path.unlink()
        except Exception:
            scratch_path.unlink(missing_ok=True)

    if not reused:
        if not _copy_out_of_icloud(audio_path, scratch_path):
            print(f"  ✗ All copy methods failed for {audio_path.name} — will retry next cycle")
            return None

    try:
        with open(scratch_path, "rb") as f:
            result = client.audio.transcriptions.create(
                model=WHISPER_MODEL,
                file=f,
                language="en",
            )
        text = (result.text or "").strip()
        return text or None
    except Exception as e:
        print(f"Error transcribing {audio_path.name}: {e}")
        return None
    finally:
        scratch_path.unlink(missing_ok=True)


def categorize_note(transcription: str) -> str:
    """Use OpenAI to categorize the note type.

    Returns one of the configured labels, e.g. "✅ Todo".
    """
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Categorize this voice note into ONE of these categories:\n"
                        "- 📝 Note (general observation, idea, or thought)\n"
                        "- ✅ Todo (action item, task, reminder)\n"
                        "- 💭 Journal (personal reflection, feeling, experience)\n"
                        "- 💡 Idea (creative concept, business idea, inspiration)\n"
                        "- 📚 Learning (insight, lesson, knowledge to remember)\n"
                        "- 🎯 Goal (aspiration, target, objective)\n"
                        "- ❓ Question (something to research or follow up on)\n"
                        "- 👥 Relations (mentions a specific person by name AND describes a situation, "
                        "interaction, feeling, or observation about them — e.g. a conversation, conflict, "
                        "appreciation, concern, or dynamic with that person)\n\n"
                        "Respond with ONLY the emoji and category name, nothing else."
                    ),
                },
                {"role": "user", "content": transcription},
            ],
            temperature=0,
            max_tokens=20,
        )
        category = (resp.choices[0].message.content or "").strip()
        if category not in CATEGORY_FOLDERS:
            return "📝 Note"
        return category
    except Exception as e:
        print(f"Error categorizing: {e}")
        return "📝 Note"


def extract_person_name(transcription: str) -> str:
    """Extract the primary person's name from a Relations note.

    Returns a title-cased name suitable for use as a folder name.
    Falls back to 'Unknown' if extraction fails.
    """
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "This voice note is about a specific person. "
                        "Extract the full name (or first name if only first name is used) of the PRIMARY person "
                        "the speaker is talking about. "
                        "Respond with ONLY the name, nothing else. Title case. No punctuation."
                    ),
                },
                {"role": "user", "content": transcription},
            ],
            temperature=0,
            max_tokens=10,
        )
        name = (resp.choices[0].message.content or "").strip().strip(".")
        # Sanitize for use as a folder name
        name = "".join(c for c in name if c.isalnum() or c in (" ", "-", "_")).strip()
        return name.title() if name else "Unknown"
    except Exception as e:
        print(f"Error extracting person name: {e}")
        return "Unknown"


def generate_two_word_title(transcription: str) -> str:
    """Generate a short two-word title summarizing the note."""
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "Give me a concise two-word title that summarizes this note. Respond with ONLY the two words.",
                },
                {"role": "user", "content": transcription},
            ],
            temperature=0,
            max_tokens=8,
        )
        title = (resp.choices[0].message.content or "").strip()
        parts = [p for p in title.replace("\n", " ").split(" ") if p]
        if len(parts) >= 2:
            return " ".join(parts[:2])
        if parts:
            return parts[0]
    except Exception as e:
        print(f"Error generating title: {e}")

    first_words = [p for p in transcription.strip().split(" ") if p]
    return " ".join(first_words[:2]) if first_words else "note"


def create_markdown_note(
    transcription: str,
    category: str,
    recorded_time: datetime,
    audio_path: Path,
) -> Path:
    """Create formatted markdown note in the appropriate category folder.

    Routing priority (highest to lowest):
      1. Mira mention → Journal/Mira/  (overrides Relations even if categorized there)
         + sleep keywords → also append to Mira sleep log
      2. Food diary keywords → Journal/Food Diary/
      3. Relations (non-Mira) → Personal/Relations/<person>/
      4. Default category folder
    """
    sleep_log_needed = False

    if is_mira_note(transcription):
        target_dir = MIRA_DIR
        print(f"Mira note → Journal/Mira/")
        if is_mira_sleep(transcription):
            sleep_log_needed = True
            print(f"  + sleep keywords detected → will append to sleep log")
    elif is_food_diary(transcription):
        target_dir = FOOD_DIARY_DIR
        print(f"Food diary → Journal/Food Diary/")
    elif category == "👥 Relations":
        person = extract_person_name(transcription)
        target_dir = RELATIONS_DIR / person
        target_dir.mkdir(parents=True, exist_ok=True)
        print(f"Relations note → {person}/")
    else:
        target_dir = CATEGORY_FOLDERS.get(category, INBOX_DIR)

    title = generate_two_word_title(transcription)
    slug_title = "-".join(title.lower().split())
    ts = recorded_time.strftime("%Y-%m-%d-%H%M")
    filename = f"{ts}-{slug_title}.md"
    md_path = target_dir / filename

    person_line = ""
    if category == "👥 Relations" and target_dir.parent == RELATIONS_DIR:
        person_line = f"person: {target_dir.name}\n"

    note = (
        f"---\n"
        f"created: {recorded_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"category: {category}\n"
        f"source: voice_capture\n"
        f"title: {title}\n"
        f"{person_line}"
        f"---\n\n"
        f"# {category}\n\n"
        f"{transcription}\n\n"
        f"---\n"
        f"*Captured: {recorded_time.strftime('%A, %B %d, %Y at %I:%M %p')}*\n"
    )
    md_path.write_text(note, encoding="utf-8")
    print(f"✓ Created note: {md_path.relative_to(VAULT_ROOT)}")

    if sleep_log_needed:
        append_to_mira_sleep_log(transcription, recorded_time)

    return md_path


def is_mira_note(transcription: str) -> bool:
    return "mira" in transcription.lower()


def is_mira_sleep(transcription: str) -> bool:
    text = transcription.lower()
    return "mira" in text and bool(SLEEP_KEYWORDS & set(text.split()))


_FOOD_DIARY_PHRASES = (
    "food journal",
    "food diary",
    "food log",
    "i ate",
    "i had",
    "i drank",
    "i just ate",
    "just ate",
    "for breakfast i",
    "for lunch i",
    "for dinner i",
    "today i ate",
    "today i had",
)


def is_food_diary(transcription: str) -> bool:
    text = transcription.lower().strip()
    return any(text.startswith(p) or f" {p}" in text for p in _FOOD_DIARY_PHRASES)


def append_to_mira_sleep_log(transcription: str, recorded_time: datetime):
    """Append a timestamped entry to Mira's sleep log for the sleep skill to read."""
    try:
        if not MIRA_SLEEP_LOG.exists():
            MIRA_SLEEP_LOG.write_text(
                "# Mira Sleep Log\n\n"
                "Auto-generated by voice processor. One entry per voice note.\n\n",
                encoding="utf-8",
            )
        entry = (
            f"\n## {recorded_time.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"{transcription}\n"
        )
        with MIRA_SLEEP_LOG.open("a", encoding="utf-8") as f:
            f.write(entry)
        print(f"✓ Appended to Mira sleep log")
    except Exception as e:
        print(f"✗ Error writing Mira sleep log: {e}")


def append_to_capture(note_content: str):
    """Append the note content to capture.md in the Inbox."""
    try:
        if not CAPTURE_FILE.exists():
            CAPTURE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CAPTURE_FILE.write_text("# Capture Inbox\n\n", encoding="utf-8")

        with CAPTURE_FILE.open("a", encoding="utf-8") as f:
            f.write("\n\n" + note_content + "\n")

        print("✓ Appended to capture.md")
        return True
    except Exception as e:
        print(f"Error appending to capture.md: {e}")
        return False


def process_audio_file(audio_path: Path) -> bool:
    """Full pipeline for a single audio file."""
    print("=" * 60)
    print(f"Processing: {audio_path.name}")

    # Use placeholder mtime if file isn't local yet (will be downloaded in transcribe step)
    if audio_path.exists():
        recorded_time = datetime.fromtimestamp(audio_path.stat().st_mtime)
    else:
        placeholder = audio_path.parent / f".{audio_path.name}.icloud"
        recorded_time = datetime.fromtimestamp(placeholder.stat().st_mtime) if placeholder.exists() else datetime.now()

    # 1) Transcribe (handles iCloud download internally)
    transcription = transcribe_audio(audio_path)
    if not transcription:
        print(f"✗ Skipping {audio_path.name} – transcription failed or empty")
        return False

    print(f"Transcription preview: {transcription[:100]!r}")

    # 2) Categorize
    category = categorize_note(transcription)
    print(f"Category: {category}")

    # 3) Create markdown note in category folder
    md_path = create_markdown_note(transcription, category, recorded_time, audio_path)

    # 4) Append same content into capture.md
    if append_to_capture(md_path.read_text(encoding="utf-8")):
        # 5) Delete audio (and any lingering placeholder)
        try:
            if audio_path.exists():
                audio_path.unlink()
                print(f"✓ Deleted audio: {audio_path.name}")
            placeholder = audio_path.parent / f".{audio_path.name}.icloud"
            if placeholder.exists():
                placeholder.unlink()
                print(f"✓ Deleted iCloud placeholder: {placeholder.name}")
            return True
        except Exception as e:
            print(f"✗ Error deleting {audio_path.name}: {e}")

    return False


def monitor_inbox():
    """Main loop – check inbox every CHECK_INTERVAL seconds."""
    ensure_dirs()

    print("🎙️  Obsidian Voice Processor Started")
    print(f"📂 Monitoring: {INBOX_DIR}")
    print(f"📝 Capture file: {CAPTURE_FILE}")
    print(f"⏱️ Check interval: {CHECK_INTERVAL // 60} minutes\n")

    if not INBOX_DIR.exists():
        print(f"✗ Error: Inbox dir not found: {INBOX_DIR}")
        return

    processed_total = 0

    while True:
        try:
            audio_files = get_audio_files(INBOX_DIR)
            if audio_files:
                print(f"\n🔍 Found {len(audio_files)} audio file(s) (including iCloud placeholders)")
                for audio_path in audio_files:
                    if process_audio_file(audio_path):
                        processed_total += 1
                print(f"\n✓ Batch complete. Total processed so far: {processed_total}")
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] No audio files found. Waiting…")

            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            print(f"\n👋 Shutting down. Total files processed: {processed_total}")
            break
        except Exception as e:
            print(f"✗ Unexpected error: {e}")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    monitor_inbox()