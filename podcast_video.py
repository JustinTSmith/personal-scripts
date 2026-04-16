#!/usr/bin/env python3
"""podcast_video.py

Podcast-to-Illustrated-Video Pipeline
======================================
Chains: AssemblyAI → Claude API → Replicate (Flux) → Shotstack

Usage:
    python podcast_video.py --audio-url "https://drive.google.com/file/d/FILE_ID/"

    # Resume from cached steps (saves API money on re-runs):
    python podcast_video.py --audio-url "..." --skip-transcribe output/transcript.json
    python podcast_video.py --audio-url "..." --skip-scenes output/scenes.json

Required environment variables (put in .env):
    ASSEMBLYAI_API_KEY
    ANTHROPIC_API_KEY
    REPLICATE_API_TOKEN
    SHOTSTACK_API_KEY
"""

import os
import re
import json
import time
import logging
import argparse
import requests
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from pathlib import Path
from dotenv import load_dotenv

import anthropic

# load_dotenv has a bug on this Python 3.14 install — use dotenv_values instead
from dotenv import dotenv_values as _dotenv_values

def _load_env_file(path):
    """Load env file, setting vars that are absent or empty in the environment."""
    try:
        for k, v in _dotenv_values(path).items():
            if not os.environ.get(k) and v:
                os.environ[k] = v
    except Exception:
        pass

_load_env_file(Path.home() / ".config" / "ai" / ".env")  # canonical secrets
_load_env_file(Path(".env"))  # local overrides

# ---------------------------------------------------------------------------
# Configuration — edit these to change behaviour
# ---------------------------------------------------------------------------

ASSEMBLYAI_API_KEY   = os.getenv("ASSEMBLYAI_API_KEY")
ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY       = os.getenv("OPENAI_API_KEY")
SHOTSTACK_API_KEY    = os.getenv("SHOTSTACK_API_KEY")
SHOTSTACK_SANDBOX_KEY = os.getenv("SHOTSTACK_SANDBOX_KEY")

ANTHROPIC_MODEL    = "claude-opus-4-6"

# Use sandbox key + stage endpoint (watermarked, free).
# Switch to SHOTSTACK_API_KEY + "v1" for watermark-free production renders.
_SHOTSTACK_USE_SANDBOX = bool(SHOTSTACK_SANDBOX_KEY)
SHOTSTACK_BASE_URL = (
    "https://api.shotstack.io/stage/render"
    if _SHOTSTACK_USE_SANDBOX
    else "https://api.shotstack.io/v1/render"
)
_SHOTSTACK_KEY = SHOTSTACK_SANDBOX_KEY if _SHOTSTACK_USE_SANDBOX else SHOTSTACK_API_KEY

OUTPUT_DIR  = Path("output")
IMAGES_DIR  = OUTPUT_DIR / "images"

# Style prefix prepended to every DALL-E image prompt
STYLE_PREFIX = (
    "Studio Ghibli psychedelic cartoon illustration, "
    "vivid swirling colors, dreamlike surreal atmosphere, "
    "luminous glowing light, bold ink outlines, trippy kaleidoscopic patterns, "
    "lush vibrant saturated fantasy. "
)

# Named speaker colors (used after Claude identifies who is who)
SPEAKER_COLORS = {
    "ERICA": "#FFD700",   # yellow
    "ZOHAR": "#A855F7",   # vivid purple
    "GUEST": "#D4D4D4",   # grey-white for any other speaker
}
DEFAULT_SPEAKER_COLOR = "#D4D4D4"

# Ken Burns effects rotated across scene clips
KEN_BURNS_EFFECTS = ["zoomIn", "zoomOut", "slideLeft", "slideRight", "slideUp"]

MAX_IMAGE_WORKERS    = 1    # sequential to avoid DALL-E rate limits
SUBTITLE_CHUNK_WORDS = 6    # words per subtitle chunk
SHOTSTACK_POLL_SEC  = 10   # seconds between status polls
SHOTSTACK_TIMEOUT   = 600  # max render wait (10 min)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Word:
    text: str
    start: int   # milliseconds
    end: int     # milliseconds


@dataclass
class Utterance:
    speaker: str   # "A", "B", "C" …
    start_ms: int
    end_ms: int
    text: str
    words: list[Word]


@dataclass
class TranscriptResult:
    utterances: list[Utterance]
    audio_duration_ms: int


@dataclass
class Scene:
    index: int
    start_time: float        # seconds
    end_time: float          # seconds
    description: str
    image_prompt: str
    image_url: Optional[str] = None   # filled in Step 3


@dataclass
class SubtitleClip:
    text: str
    speaker: str
    start_sec: float
    end_sec: float
    color: str


# ---------------------------------------------------------------------------
# Step 0: Google Drive URL conversion
# ---------------------------------------------------------------------------

def convert_gdrive_url(url: str) -> str:
    """Convert a Google Drive share URL to a direct-download URL.

    Input:  https://drive.google.com/file/d/FILE_ID/view?usp=sharing
    Output: https://drive.google.com/uc?export=download&id=FILE_ID

    For files >100 MB that trigger Google's virus-scan warning page,
    install gdown (pip install gdown) and use gdown.download() instead.
    """
    match = re.search(r"/file/d/([^/?]+)", url)
    if not match:
        raise ValueError(f"Could not extract file ID from Google Drive URL: {url}")
    file_id = match.group(1)
    return f"https://drive.google.com/uc?export=download&id={file_id}"


# ---------------------------------------------------------------------------
# Step 1: Transcription — AssemblyAI
# ---------------------------------------------------------------------------

def _download_and_upload_audio(gdrive_url: str) -> str:
    """Download audio from Google Drive and upload it to AssemblyAI.

    Google Drive URLs can't be fetched directly by external APIs (auth + virus-scan
    confirmation pages). This downloads locally via gdown then uploads to AssemblyAI's
    CDN, returning an assemblyai.com URL that the transcription API can access.
    """
    import gdown

    local_path = OUTPUT_DIR / "podcast_audio.mp3"
    if not local_path.exists():
        log.info("  Downloading audio from Google Drive via gdown...")
        gdown.download(gdrive_url, str(local_path), quiet=False, fuzzy=True)
    else:
        log.info("  Using cached local audio: %s", local_path)

    log.info("  Uploading audio to AssemblyAI CDN...")
    headers = {"authorization": ASSEMBLYAI_API_KEY}
    with open(local_path, "rb") as f:
        upload_resp = requests.post(
            "https://api.assemblyai.com/v2/upload",
            headers=headers,
            data=f,
            timeout=300,
        )
    upload_resp.raise_for_status()
    upload_url = upload_resp.json()["upload_url"]
    log.info("  Uploaded: %s", upload_url)
    return upload_url


def transcribe_audio(audio_url: str) -> TranscriptResult:
    """Transcribe audio via AssemblyAI REST API (bypasses SDK versioning issues).

    Downloads from Google Drive, uploads to AssemblyAI, polls until complete.
    Returns utterances with word-level timestamps.
    """
    log.info("Step 1: Transcribing audio via AssemblyAI REST API...")

    # AssemblyAI can't fetch Google Drive URLs directly — upload via their CDN first
    assemblyai_url = _download_and_upload_audio(audio_url)

    headers = {
        "authorization": ASSEMBLYAI_API_KEY,
        "content-type": "application/json",
    }

    # Submit transcription job
    body = {"audio_url": assemblyai_url, "speaker_labels": True, "speech_models": ["universal-2"]}
    log.info("  Request body: %s", json.dumps(body))
    resp = requests.post(
        "https://api.assemblyai.com/v2/transcript",
        json=body,
        headers=headers,
        timeout=30,
    )
    log.info("  Response: %s %s", resp.status_code, resp.text)
    if not resp.ok:
        raise RuntimeError(f"AssemblyAI transcript submit failed {resp.status_code}: {resp.text}")
    resp.raise_for_status()
    transcript_id = resp.json()["id"]
    log.info("  Transcript ID: %s — polling...", transcript_id)

    # Poll until complete
    poll_url = f"https://api.assemblyai.com/v2/transcript/{transcript_id}"
    while True:
        poll = requests.get(poll_url, headers=headers, timeout=30)
        poll.raise_for_status()
        data = poll.json()
        status = data["status"]
        log.info("  Status: %s", status)
        if status == "completed":
            break
        elif status == "error":
            raise RuntimeError(f"AssemblyAI error: {data.get('error')}")
        time.sleep(5)

    # Parse utterances + words
    utterances = []
    for u in data.get("utterances") or []:
        words = [Word(text=w["text"], start=w["start"], end=w["end"]) for w in u.get("words", [])]
        utterances.append(Utterance(
            speaker=u["speaker"],
            start_ms=u["start"],
            end_ms=u["end"],
            text=u["text"],
            words=words,
        ))

    audio_duration_ms = max(u.end_ms for u in utterances) if utterances else 0
    log.info("  Done: %d utterances, %.1fs total", len(utterances), audio_duration_ms / 1000)
    return TranscriptResult(utterances=utterances, audio_duration_ms=audio_duration_ms)


# ---------------------------------------------------------------------------
# Step 2: Scene breakdown — Claude API
# ---------------------------------------------------------------------------

def _format_transcript_for_claude(result: TranscriptResult) -> str:
    """Render utterances as compact timestamped script lines for Claude.

    Format: [MM:SS Speaker A] The text of the utterance.
    """
    lines = []
    for u in result.utterances:
        minutes, seconds = divmod(u.start_ms // 1000, 60)
        lines.append(f"[{minutes:02d}:{seconds:02d} Speaker {u.speaker}] {u.text}")
    return "\n".join(lines)


def generate_scenes(transcript: TranscriptResult) -> list[Scene]:
    """Ask Claude to segment the transcript into 12–18 illustrated scenes.

    Each scene gets start/end times and a Flux-ready image prompt in the
    Studio Ghibli × comic-book style.
    """
    log.info("Step 2: Generating scenes via Claude...")
    formatted = _format_transcript_for_claude(transcript)
    total_seconds = transcript.audio_duration_ms / 1000

    system_prompt = (
        "You are a creative director turning a podcast transcript into an illustrated video. "
        "You will receive a timestamped, speaker-labeled transcript. "
        "Divide it into 12–18 scenes for illustration. "
        "Respond with ONLY a valid JSON array — no markdown fences, no commentary. "
        "Each element must have exactly these keys:\n"
        '  "start_time"   — float, seconds from the start of the audio\n'
        '  "end_time"     — float, seconds from the start of the audio\n'
        '  "description"  — string, 1–2 sentences summarizing the scene topic\n'
        '  "image_prompt" — string, detailed visual prompt for an AI image model\n'
        "Scenes must be contiguous and cover the full audio with no gaps."
    )

    user_prompt = (
        f"Total podcast duration: {total_seconds:.1f} seconds.\n\n"
        f"Transcript:\n{formatted}\n\n"
        "Divide this into 12–18 scenes. For each scene's image_prompt, describe a specific "
        "visual moment: setting, characters (silhouettes/archetypes — no real people), mood, "
        "key visual elements, lighting, and color palette. "
        "Match the 'Studio Ghibli × comic-book ink-wash illustration' aesthetic."
    )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw_json = response.content[0].text.strip()

    # Strip accidental markdown fences if Claude includes them despite instructions
    raw_json = re.sub(r"^```[a-z]*\s*", "", raw_json)
    raw_json = re.sub(r"\s*```$", "", raw_json)

    scenes_data = json.loads(raw_json)
    scenes = [
        Scene(
            index=i,
            start_time=float(s["start_time"]),
            end_time=float(s["end_time"]),
            description=s["description"],
            image_prompt=s["image_prompt"],
        )
        for i, s in enumerate(scenes_data)
    ]

    log.info("  Done: %d scenes generated", len(scenes))
    return scenes


# ---------------------------------------------------------------------------
# Step 3: Illustration generation — Replicate (Flux), parallelized
# ---------------------------------------------------------------------------

def _dalle_generate(prompt: str) -> str:
    """Generate one image via OpenAI DALL-E 3. Returns a temporary CDN URL.

    Uses 1792x1024 — the closest DALL-E 3 size to 16:9 widescreen.
    """
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": "dall-e-3",
        "prompt": prompt,
        "n": 1,
        "size": "1792x1024",
        "quality": "standard",   # use "hd" for higher detail (2x cost)
        "response_format": "url",
    }
    resp = requests.post(
        "https://api.openai.com/v1/images/generations",
        json=body,
        headers=headers,
        timeout=120,
    )
    if not resp.ok:
        raise RuntimeError(f"DALL-E 3 error {resp.status_code}: {resp.text}")
    return resp.json()["data"][0]["url"]


def _upload_to_catbox(local_path: Path) -> str:
    """Upload a local image to catbox.moe (free, no auth) and return the public URL.

    catbox.moe is a reliable free image host with permanent storage.
    This avoids the AssemblyAI CDN SSL cert mismatch that blocks Shotstack.
    """
    with open(local_path, "rb") as f:
        resp = requests.post(
            "https://catbox.moe/user/api.php",
            data={"reqtype": "fileupload"},
            files={"fileToUpload": (local_path.name, f)},
            timeout=120,
        )
    resp.raise_for_status()
    url = resp.text.strip()
    if not url.startswith("http"):
        raise RuntimeError(f"catbox.moe unexpected response: {url!r}")
    return url


def _ensure_hosted_image_urls(scenes: list["Scene"]) -> list["Scene"]:
    """Ensure every scene's image_url is accessible by Shotstack (no SSL cert issues).

    AssemblyAI CDN URLs have an SSL cert mismatch. This function:
    1. Checks the .url sidecar file first — if it already has a non-AssemblyAI URL,
       uses that directly without re-uploading (avoids repeated catbox.moe hits).
    2. Otherwise uploads the local PNG to catbox.moe and writes the new URL back
       to the sidecar file so future runs skip the upload.
    """
    for scene in scenes:
        if not scene.image_url or "cdn.assemblyai.com" not in scene.image_url:
            continue

        # Check sidecar cache — previous runs may have already re-hosted this image
        cdn_cache = IMAGES_DIR / f"scene_{scene.index:02d}.url"
        if cdn_cache.exists():
            cached = cdn_cache.read_text().strip()
            if cached.startswith("http") and "cdn.assemblyai.com" not in cached:
                scene.image_url = cached
                log.info("  Scene %02d: using cached hosted URL", scene.index)
                continue

        local_path = IMAGES_DIR / f"scene_{scene.index:02d}.png"
        if not local_path.exists():
            raise FileNotFoundError(
                f"Scene {scene.index}: local image missing at {local_path} "
                "and AssemblyAI CDN URL is not accessible by Shotstack."
            )
        log.info("  Re-hosting scene %02d on catbox.moe...", scene.index)
        new_url = _upload_to_catbox(local_path)
        scene.image_url = new_url
        cdn_cache.write_text(new_url)
        log.info("  Scene %02d → %s", scene.index, new_url)
    return scenes


def _generate_single_image(scene: Scene, output_dir: Path) -> tuple[int, str]:
    """Generate one illustration via DALL-E 3, download it, upload to catbox.moe.

    Returns (scene.index, public_cdn_url) suitable for Shotstack.
    DALL-E URLs expire in ~1h so we re-host on catbox.moe immediately.
    Local copy saved to output/images/scene_XX.png for caching.
    """
    full_prompt = STYLE_PREFIX + scene.image_prompt
    dest = output_dir / f"scene_{scene.index:02d}.png"
    cdn_cache = output_dir / f"scene_{scene.index:02d}.url"

    # Resume: if we already uploaded this image, reuse the CDN URL
    if cdn_cache.exists():
        url = cdn_cache.read_text().strip()
        log.info("  Scene %02d using cached CDN URL", scene.index)
        return scene.index, url

    prompts_to_try = [
        full_prompt,
        # Fallback 1: drop the style prefix, use just the scene description
        f"Studio Ghibli illustration, soft watercolor style: {scene.description}",
        # Fallback 2: very safe generic scene
        f"A peaceful illustrated scene, Studio Ghibli style, depicting: {scene.description[:100]}",
    ]

    for attempt, prompt in enumerate(prompts_to_try):
        try:
            dalle_url = _dalle_generate(prompt)

            r = requests.get(dalle_url, timeout=60)
            r.raise_for_status()
            dest.write_bytes(r.content)

            cdn_url = _upload_to_catbox(dest)
            cdn_cache.write_text(cdn_url)
            log.info("  Scene %02d → %s", scene.index, cdn_url[:60] + "...")
            return scene.index, cdn_url

        except RuntimeError as exc:
            if "content_policy_violation" in str(exc):
                log.warning("  Scene %02d content policy hit — trying simpler prompt (attempt %d)",
                            scene.index, attempt + 1)
                if attempt >= len(prompts_to_try) - 1:
                    raise
                continue
            raise
        except Exception as exc:
            wait = 5 * (2 ** attempt)
            log.warning("  Scene %02d attempt %d failed: %s — retrying in %ds",
                        scene.index, attempt + 1, exc, wait)
            if attempt >= 2:
                raise
            time.sleep(wait)


def generate_illustrations(scenes: list[Scene]) -> list[Scene]:
    """Generate all scene illustrations in parallel via Replicate Flux.

    Mutates each Scene.image_url with the Replicate CDN URL.
    Images also saved locally to output/images/scene_XX.webp.
    """
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Step 3: Generating %d illustrations (max %d parallel)...", len(scenes), MAX_IMAGE_WORKERS)

    results: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=MAX_IMAGE_WORKERS) as executor:
        futures = {
            executor.submit(_generate_single_image, scene, IMAGES_DIR): scene.index
            for scene in scenes
        }
        for future in as_completed(futures):
            idx, cdn_url = future.result()
            results[idx] = cdn_url

    for scene in scenes:
        scene.image_url = results[scene.index]

    log.info("  Done: all illustrations generated")
    return scenes


# ---------------------------------------------------------------------------
# Step 3b: Speaker identification
# ---------------------------------------------------------------------------

SPEAKER_MAP_CACHE = OUTPUT_DIR / "speaker_map.json"

def _identify_speakers(transcript: TranscriptResult) -> dict[str, str]:
    """Ask Claude to map speaker labels (A/B/C…) to ERICA / ZOHAR / GUEST.

    Caches the result to output/speaker_map.json so subsequent runs don't
    consume Anthropic API credits.
    """
    if SPEAKER_MAP_CACHE.exists():
        mapping = json.loads(SPEAKER_MAP_CACHE.read_text())
        log.info("  Speaker map (cached): %s", mapping)
        return mapping

    lines = []
    for u in transcript.utterances[:20]:
        mins, secs = divmod(u.start_ms // 1000, 60)
        lines.append(f"[Speaker {u.speaker} at {mins:02d}:{secs:02d}]: {u.text[:140]}")

    prompt = (
        "This is the beginning of a podcast transcript. "
        "The two regular hosts are named Erica and Zohar. "
        "Based on the transcript below, identify which speaker label (A, B, C, etc.) "
        "corresponds to Erica, Zohar, or a guest.\n\n"
        "Transcript:\n" + "\n".join(lines) + "\n\n"
        'Return ONLY a JSON object, e.g. {"A": "ERICA", "B": "ZOHAR", "C": "GUEST"}. '
        "Any speaker who is not clearly Erica or Zohar should be labeled GUEST."
    )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    m = re.search(r'\{[^}]+\}', raw, re.DOTALL)
    if not m:
        log.warning("  Speaker identification failed — treating all as GUEST")
        return {}
    mapping = json.loads(m.group())
    SPEAKER_MAP_CACHE.write_text(json.dumps(mapping))
    log.info("  Speaker map: %s", mapping)
    return mapping


# ---------------------------------------------------------------------------
# Step 4: Video assembly — Shotstack
# ---------------------------------------------------------------------------

def _make_waveform_parts(num_bars: int = 60) -> tuple[str, str]:
    """Return (html_body, css) for an animated CSS equalizer bar chart.

    Keeps the payload small: html is just div tags; css holds all keyframes.
    Fixed seed → deterministic layout across runs.
    """
    import random
    rng = random.Random(42)

    css_parts: list[str] = [
        "body{display:flex;align-items:flex-end;justify-content:center;"
        "gap:4px;padding:0 30px;overflow:hidden;background:transparent;}",
        ".b{width:6px;background:rgba(210,210,210,0.55);border-radius:2px 2px 0 0;}",
    ]
    bar_tags: list[str] = []
    for i in range(num_bars):
        min_h = rng.randint(4, 20)
        max_h = rng.randint(30, 90)
        dur   = round(rng.uniform(0.4, 1.5), 2)
        delay = round(rng.uniform(0.0, 1.5),  2)
        css_parts.append(
            f".b{i}{{height:{min_h}px;"
            f"animation:k{i} {dur}s {delay}s ease-in-out infinite alternate;}}"
            f"@keyframes k{i}{{to{{height:{max_h}px;}}}}"
        )
        bar_tags.append(f'<div class="b b{i}"></div>')

    return "".join(bar_tags), "".join(css_parts)


def _build_image_card_clips(scenes: list[Scene]) -> tuple[list[dict], list[dict]]:
    """Build two sets of clips for the right-side image panel:

    1. card_bg_clips  — white rounded-rectangle HtmlAsset (card shadow/border)
    2. image_clips    — native ImageAsset with Ken Burns effect on top

    Using a native ImageAsset (not <img> inside HtmlAsset) is required because
    Shotstack's headless renderer blocks external image URLs inside HtmlAsset.
    """
    card_bg_clips: list[dict] = []
    image_clips:   list[dict] = []

    # Card background (white rounded rect, no image)
    card_css = (
        "div{width:100%;height:100%;background:#ffffff;"
        "border-radius:22px;box-shadow:0 8px 32px rgba(0,0,0,0.45);}"
    )

    for i, scene in enumerate(scenes):
        if not scene.image_url:
            continue
        duration = max(scene.end_time - scene.start_time, 0.5)
        effect   = KEN_BURNS_EFFECTS[i % len(KEN_BURNS_EFFECTS)]

        # White card background (behind the image)
        card_bg_clips.append({
            "asset": {
                "type":       "html",
                "html":       "<div></div>",
                "css":        card_css,
                "width":      510,
                "height":     380,
                "background": "transparent",
            },
            "start":    round(scene.start_time, 3),
            "length":   round(duration, 3),
            "position": "right",
            "offset":   {"x": -0.03, "y": -0.09},
        })

        # Native ImageAsset on top — Ken Burns + fade transitions
        image_clips.append({
            "asset": {
                "type": "image",
                "src":  scene.image_url,
            },
            "start":    round(scene.start_time, 3),
            "length":   round(duration, 3),
            "scale":    0.42,          # ~537×302px within the 1280×720 frame
            "position": "right",
            "offset":   {"x": -0.03, "y": -0.09},
            "effect":   effect,
            "fit":      "cover",
            "transition": {"in": "fade", "out": "fade"},
        })

    return card_bg_clips, image_clips


def _build_subtitle_clips(
    transcript: TranscriptResult,
    speaker_map: dict[str, str],
    max_duration: float | None = None,
) -> list[dict]:
    """Build subtitle clips.

    Two modes:
    • Karaoke (when max_duration is set): one clip per word, shows a 7-word
      window with the active word highlighted in speaker colour + black outline
      and surrounding words dimmed.  Richer but ~7× more clips — only viable
      for short test renders.
    • Chunk (default / full video): groups words into SUBTITLE_CHUNK_WORDS
      clips in the speaker colour.  Keeps payload under Shotstack's 390 KB cap.
    """
    clips      = []
    karaoke    = max_duration is not None
    LINE_WORDS = 20   # words per line (both modes)

    # Build line groups: utterances split into LINE_WORDS chunks.
    line_groups: list[tuple] = []
    for utterance in transcript.utterances:
        name  = speaker_map.get(utterance.speaker, "GUEST")
        color = SPEAKER_COLORS.get(name, DEFAULT_SPEAKER_COLOR)
        words = [w for w in utterance.words if w.text.strip()]
        chunk_size = LINE_WORDS
        for i in range(0, len(words), chunk_size):
            line_groups.append((words[i : i + chunk_size], color))

    for chunk_words, color in line_groups:
        if not chunk_words:
            continue

        if karaoke:
            # ---- full-line top-karaoke: one clip per word ------------------
            # Shows the whole line; past words lit at 65%, current word in
            # speaker colour + black outline, future words dim at 20%.
            for word_idx, word in enumerate(chunk_words):
                start_s = word.start / 1000.0
                if start_s >= max_duration:
                    break

                if word_idx + 1 < len(chunk_words):
                    clip_end = chunk_words[word_idx + 1].start / 1000.0
                else:
                    clip_end = word.end / 1000.0
                duration = max(min(clip_end - start_s, 5.0), 0.08)

                html_parts = []
                for i, w in enumerate(chunk_words):
                    s = (w.text.replace("&", "&amp;")
                               .replace("<", "&lt;")
                               .replace(">", "&gt;"))
                    if i < word_idx:
                        html_parts.append(f'<span class="p">{s}</span>')
                    elif i == word_idx:
                        html_parts.append(f'<span class="c">{s}</span>')
                    else:
                        html_parts.append(f'<span class="f">{s}</span>')

                html = "<p>" + " ".join(html_parts) + "</p>"
                css = (
                    "body{display:flex;align-items:center;padding:0 20px;background:transparent;}"
                    "p{font:700 38px/1.4 Georgia,serif;width:100%;margin:0;}"
                    ".p{color:#aaa;}"
                    f".c{{color:{color};"
                    "text-shadow:-2px -2px 0 #000,2px -2px 0 #000,"
                    "-2px 2px 0 #000,2px 2px 0 #000;}}"
                    ".f{color:#333;}"
                )
                clips.append({
                    "asset": {
                        "type":       "html",
                        "html":       html,
                        "css":        css,
                        "width":      1200,
                        "height":     160,
                        "background": "transparent",
                    },
                    "start":    round(start_s, 3),
                    "length":   round(duration, 3),
                    "position": "top",
                    "offset":   {"x": 0, "y": 0},
                })

        else:
            # ---- chunk mode with CSS-animation karaoke ---------------------
            # One clip per 20-word group (keeps payload small), but each word
            # gets an animation-delay so it lights up exactly when spoken.
            # @keyframes "h": word → speaker-colour (0–70%) → dim (100%)
            # fill-mode is omitted (defaults to none) so the word returns to
            # its base dim colour after the animation, cleanly distinguishing
            # past-from-future is not needed — the bright flash is enough.
            start_s  = chunk_words[0].start / 1000.0
            end_s    = chunk_words[-1].end  / 1000.0
            duration = max(end_s - start_s, 0.1)

            spans = []
            for w in chunk_words:
                rel   = round(w.start / 1000.0 - start_s, 2)   # delay from clip start
                wdur  = round(max((w.end - w.start) / 1000.0, 0.15), 2)
                safe  = (w.text.replace("&", "&amp;")
                               .replace("<", "&lt;")
                               .replace(">", "&gt;"))
                # Use <b> (3 chars shorter than <span> open + close) to save payload
                spans.append(f'<b style="animation:h {wdur}s {rel}s">{safe}</b>')

            html = "<p>" + " ".join(spans) + "</p>"
            outline = "-2px -2px 0 #000,2px -2px 0 #000,-2px 2px 0 #000,2px 2px 0 #000"
            css = (
                "body{display:flex;align-items:center;padding:0 20px;background:transparent;}"
                "p{font:700 38px/1.4 Georgia,serif;width:100%;margin:0;}"
                "p b{font-weight:700;color:rgba(255,255,255,.18);}"
                f"@keyframes h{{0%,70%{{color:{color};"
                f"text-shadow:{outline}}}"
                "100%{color:rgba(255,255,255,.18)}}"
            )
            clips.append({
                "asset": {
                    "type":       "html",
                    "html":       html,
                    "css":        css,
                    "width":      1200,
                    "height":     160,
                    "background": "transparent",
                },
                "start":    round(start_s, 3),
                "length":   round(duration, 3),
                "position": "top",
                "offset":   {"x": 0, "y": 0},
            })
    return clips


def _build_shotstack_payload(
    audio_url: str,
    scenes: list[Scene],
    transcript: TranscriptResult,
    speaker_map: dict[str, str],
    test_duration: float | None = None,
    limit_duration: float | None = None,
) -> dict:
    """Construct the Shotstack timeline render payload.

    Layout (replicates Memory Weave style):
        • Black background fills the whole frame
        • RIGHT: Ghibli illustration in a rounded white card (Ken Burns animated)
        • LEFT:  Speaker-coloured large serif subtitle text (karaoke in test mode)
        • BOTTOM: Animated CSS equalizer waveform strip

    Track order in Shotstack (first = top z-layer):
        Track 0: subtitle text (top)
        Track 1: image cards
        Track 2: waveform strip
        Track 3: black background
    """
    cap = test_duration or limit_duration
    total_sec = cap or (transcript.audio_duration_ms / 1000.0)

    # ---- Subtitle clips ------------------------------------------------
    subtitle_clips = _build_subtitle_clips(transcript, speaker_map, max_duration=test_duration)
    # For limit_duration (chunk mode), trim clips that start after the cap
    if limit_duration:
        subtitle_clips = [c for c in subtitle_clips if c["start"] < limit_duration]
    log.info("  Built %d subtitle clips%s", len(subtitle_clips),
             f" (first {cap}s)" if cap else "")

    # ---- Image card clips (right side, rounded card) -----------------------
    card_bg_clips, image_clips = _build_image_card_clips(scenes)
    if cap:
        card_bg_clips = [c for c in card_bg_clips if c["start"] < cap]
        image_clips   = [c for c in image_clips   if c["start"] < cap]
    log.info("  Built %d image clips + %d card bg clips", len(image_clips), len(card_bg_clips))

    # ---- Waveform clip (bottom strip, full duration) -----------------------
    waveform_html, waveform_css = _make_waveform_parts(num_bars=60)
    waveform_clip = {
        "asset": {
            "type":       "html",
            "html":       waveform_html,
            "css":        waveform_css,
            "width":      1280,
            "height":     110,
            "background": "transparent",
        },
        "start":    0,
        "length":   round(total_sec, 3),
        "position": "bottom",
        "offset":   {"x": 0, "y": 0},
    }

    return {
        "timeline": {
            "soundtrack": {
                "src":    audio_url,
                "effect": "fadeInFadeOut",
                "volume": 1,
            },
            "background": "#0a0a0a",      # black canvas — avoids a full bg clip
            # Shotstack: Track 0 = highest z-index (rendered on top).
            "tracks": [
                {"clips": subtitle_clips},     # 0: subtitles ON TOP
                {"clips": image_clips},        # 1: images (Ken Burns)
                {"clips": card_bg_clips},      # 2: white card backgrounds
                {"clips": [waveform_clip]},    # 3: waveform at bottom
            ],
        },
        "output": {
            "format":     "mp4",
            "resolution": "hd",    # 1280×720
            "fps":        25,
            "quality":    "high",
        },
    }


def _shotstack_submit(payload: dict) -> str:
    """POST a render job to Shotstack. Returns the render ID."""
    headers = {
        "Content-Type": "application/json",
        "x-api-key": _SHOTSTACK_KEY,
    }
    resp = requests.post(SHOTSTACK_BASE_URL, json=payload, headers=headers, timeout=30)
    if not resp.ok:
        raise RuntimeError(f"Shotstack submit failed {resp.status_code}: {resp.text}")
    resp.raise_for_status()
    render_id = resp.json()["response"]["id"]
    log.info("  Render submitted: %s", render_id)
    return render_id


def _shotstack_poll(render_id: str) -> str:
    """Poll Shotstack until the render finishes. Returns the final MP4 URL."""
    headers = {"x-api-key": _SHOTSTACK_KEY}
    url = f"{SHOTSTACK_BASE_URL}/{render_id}"
    deadline = time.time() + SHOTSTACK_TIMEOUT

    while time.time() < deadline:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()["response"]
        status = data["status"]
        log.info("  Render status: %s", status)

        if status == "done":
            video_url = data["url"]
            log.info("  Render complete: %s", video_url)
            return video_url
        elif status == "failed":
            raise RuntimeError(f"Shotstack render failed: {data.get('error', 'unknown error')}")

        # Intermediate statuses: "queued", "fetching", "rendering", "saving"
        time.sleep(SHOTSTACK_POLL_SEC)

    raise TimeoutError(f"Shotstack render timed out after {SHOTSTACK_TIMEOUT}s")


def _build_batch_payload(
    audio_url: str,
    scenes: list[Scene],
    transcript: TranscriptResult,
    speaker_map: dict[str, str],
    batch_start_s: float,
    batch_end_s: float,
) -> dict:
    """Build a Shotstack payload for a time window [batch_start_s, batch_end_s).

    Times in subtitle/image clips are shifted to be relative to batch_start_s.
    Audio is played from batch_start_s using the asset-level `trim` parameter.
    """
    dur = round(batch_end_s - batch_start_s, 3)

    # ---- Karaoke subtitle clips for this window (times relative to batch) ----
    sub_clips = _build_subtitle_clips(
        transcript, speaker_map, max_duration=batch_end_s
    )
    # Keep only clips in [batch_start_s, batch_end_s) and shift times.
    adjusted_subs = []
    for c in sub_clips:
        abs_start = c["start"]
        if abs_start < batch_start_s or abs_start >= batch_end_s:
            continue
        c2 = dict(c)
        c2["start"] = round(abs_start - batch_start_s, 3)
        # Clamp length so clip doesn't overflow the batch
        c2["length"] = round(min(c["length"], batch_end_s - abs_start), 3)
        adjusted_subs.append(c2)

    # ---- Image/card clips for this window (times relative to batch) ----------
    card_bg_clips, image_clips = _build_image_card_clips(scenes)
    def adjust_clips(clips: list[dict]) -> list[dict]:
        result = []
        for c in clips:
            abs_s = c["start"]
            abs_e = abs_s + c["length"]
            # Include clip if it overlaps [batch_start_s, batch_end_s)
            if abs_e <= batch_start_s or abs_s >= batch_end_s:
                continue
            c2 = dict(c)
            c2["start"]  = round(max(abs_s - batch_start_s, 0), 3)
            c2["length"] = round(min(abs_e, batch_end_s) - max(abs_s, batch_start_s), 3)
            result.append(c2)
        return result

    adj_cards  = adjust_clips(card_bg_clips)
    adj_images = adjust_clips(image_clips)

    # ---- Waveform for batch duration ------------------------------------------
    waveform_html, waveform_css = _make_waveform_parts(num_bars=60)
    waveform_clip = {
        "asset": {
            "type": "html", "html": waveform_html, "css": waveform_css,
            "width": 1280, "height": 110, "background": "transparent",
        },
        "start": 0, "length": dur, "position": "bottom", "offset": {"x": 0, "y": 0},
    }

    # ---- Audio clip with trim (soundtrack level doesn't support trim) ---------
    audio_clip = {
        "asset": {
            "type":   "audio",
            "src":    audio_url,
            "trim":   round(batch_start_s, 3),
            "volume": 1,
        },
        "start":  0,
        "length": dur,
    }

    return {
        "timeline": {
            "background": "#0a0a0a",
            "tracks": [
                {"clips": adjusted_subs},   # 0: subtitles ON TOP
                {"clips": adj_images},       # 1: images
                {"clips": adj_cards},        # 2: card backgrounds
                {"clips": [waveform_clip]},  # 3: waveform
                {"clips": [audio_clip]},     # 4: audio (trimmed to batch)
            ],
        },
        "output": {
            "format": "mp4", "resolution": "hd", "fps": 25, "quality": "high",
        },
    }


KARAOKE_WORDS_PER_BATCH = 280   # keeps each batch payload safely under 390 KB


def _render_full_karaoke(
    audio_url: str,
    scenes: list[Scene],
    transcript: TranscriptResult,
    speaker_map: dict[str, str],
) -> Path:
    """Render the full video as karaoke by splitting into word-count batches.

    Returns the path of the final concatenated MP4.
    """
    import subprocess

    # Collect all words with their timestamps
    all_words: list[tuple[float, float, str]] = []   # (start_s, end_s, speaker)
    for utt in transcript.utterances:
        for w in utt.words:
            if w.text.strip():
                all_words.append((w.start / 1000.0, w.end / 1000.0, utt.speaker))

    total_sec = transcript.audio_duration_ms / 1000.0

    # Build batch time windows based on word count
    batches: list[tuple[float, float]] = []   # (start_s, end_s)
    i = 0
    while i < len(all_words):
        batch_start_s = all_words[i][0]
        j = min(i + KARAOKE_WORDS_PER_BATCH, len(all_words)) - 1
        batch_end_s   = all_words[j][1] if j + 1 >= len(all_words) else all_words[j + 1][0]
        # Last batch extends to end of audio
        if j + 1 >= len(all_words):
            batch_end_s = total_sec
        batches.append((batch_start_s, batch_end_s))
        i += KARAOKE_WORDS_PER_BATCH

    log.info("  Karaoke: %d words → %d batches", len(all_words), len(batches))

    # Submit all batches in parallel
    batch_ids: list[str] = []
    for idx, (bs, be) in enumerate(batches):
        payload = _build_batch_payload(audio_url, scenes, transcript, speaker_map, bs, be)
        psize = len(json.dumps(payload))
        log.info("  Batch %02d [%.1f–%.1f s]  payload %.1fKB",
                 idx, bs, be, psize / 1024)
        if psize > 390 * 1024:
            log.warning("  Batch %02d is %.1fKB — may be rejected by Shotstack", idx, psize/1024)
        rid = _shotstack_submit(payload)
        batch_ids.append(rid)

    # Poll all batches
    batch_urls: list[str] = [""] * len(batch_ids)
    pending = list(range(len(batch_ids)))
    headers = {"x-api-key": _SHOTSTACK_KEY}
    deadline = time.time() + SHOTSTACK_TIMEOUT

    while pending and time.time() < deadline:
        still_pending = []
        for idx in pending:
            url = f"{SHOTSTACK_BASE_URL}/{batch_ids[idx]}"
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()["response"]
            status = data["status"]
            if status == "done":
                batch_urls[idx] = data["url"]
                log.info("  Batch %02d done → %s", idx, data["url"])
            elif status == "failed":
                raise RuntimeError(f"Batch {idx} failed: {data.get('error', '?')}")
            else:
                still_pending.append(idx)
        pending = still_pending
        if pending:
            log.info("  Waiting for batches: %s", pending)
            time.sleep(SHOTSTACK_POLL_SEC)

    if pending:
        raise TimeoutError("Karaoke batch renders timed out")

    # Download all batch segments
    seg_dir = OUTPUT_DIR / "segments"
    seg_dir.mkdir(exist_ok=True)
    seg_paths: list[Path] = []
    for idx, url in enumerate(batch_urls):
        dest = seg_dir / f"seg_{idx:03d}.mp4"
        log.info("  Downloading segment %02d …", idx)
        with requests.get(url, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        seg_paths.append(dest)

    # Concatenate with ffmpeg
    concat_list = seg_dir / "concat.txt"
    concat_list.write_text(
        "\n".join(f"file '{p.resolve()}'" for p in seg_paths), encoding="utf-8"
    )
    raw_out = OUTPUT_DIR / "podcast_video_raw.mp4"
    log.info("  Concatenating %d segments …", len(seg_paths))
    result = subprocess.run([
        "ffmpeg", "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy", str(raw_out), "-y",
    ], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg concat failed:\n{result.stderr[-500:]}")

    log.info("  Concatenated → %s", raw_out)
    return raw_out


def render_video(
    audio_url: str,
    scenes: list[Scene],
    transcript: TranscriptResult,
    test_duration: float | None = None,
    limit_duration: float | None = None,
) -> str:
    """Build the Shotstack timeline, submit the render, and return the video URL."""
    cap = test_duration or limit_duration
    log.info("Step 4: Assembling video via Shotstack%s...",
             f" (first {cap}s)" if cap else "")

    scenes = _ensure_hosted_image_urls(scenes)

    log.info("  Identifying speakers via Claude...")
    speaker_map = _identify_speakers(transcript)

    payload = _build_shotstack_payload(
        audio_url=audio_url,
        scenes=scenes,
        transcript=transcript,
        speaker_map=speaker_map,
        test_duration=test_duration,
        limit_duration=limit_duration,
    )

    # Save payload for debugging / manual inspection
    payload_path = OUTPUT_DIR / "shotstack_payload.json"
    payload_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.info("  Payload saved to %s", payload_path)

    render_id = _shotstack_submit(payload)
    return _shotstack_poll(render_id)


# ---------------------------------------------------------------------------
# Caching helpers — skip expensive steps on re-runs
# ---------------------------------------------------------------------------

def _save_transcript(t: TranscriptResult, path: Path) -> None:
    data = {
        "audio_duration_ms": t.audio_duration_ms,
        "utterances": [
            {
                "speaker": u.speaker,
                "start_ms": u.start_ms,
                "end_ms": u.end_ms,
                "text": u.text,
                "words": [{"text": w.text, "start": w.start, "end": w.end} for w in u.words],
            }
            for u in t.utterances
        ],
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.info("Transcript cached to %s", path)


def _load_transcript(path: str | Path) -> TranscriptResult:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    utterances = [
        Utterance(
            speaker=u["speaker"],
            start_ms=u["start_ms"],
            end_ms=u["end_ms"],
            text=u["text"],
            words=[Word(text=w["text"], start=w["start"], end=w["end"]) for w in u["words"]],
        )
        for u in data["utterances"]
    ]
    return TranscriptResult(
        utterances=utterances,
        audio_duration_ms=data["audio_duration_ms"],
    )


def _save_scenes(scenes: list[Scene], path: Path) -> None:
    data = [
        {
            "index": s.index,
            "start_time": s.start_time,
            "end_time": s.end_time,
            "description": s.description,
            "image_prompt": s.image_prompt,
            "image_url": s.image_url,
        }
        for s in scenes
    ]
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.info("Scenes cached to %s", path)


def _load_scenes(path: str | Path) -> list[Scene]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        Scene(
            index=s["index"],
            start_time=s["start_time"],
            end_time=s["end_time"],
            description=s["description"],
            image_prompt=s["image_prompt"],
            image_url=s.get("image_url"),
        )
        for s in data
    ]


def _download_file(url: str, dest: Path) -> None:
    log.info("Downloading final video to %s ...", dest)
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    log.info("Saved: %s", dest)


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Podcast → Illustrated Video Pipeline")
    parser.add_argument(
        "--audio-url", required=True,
        help="Google Drive share URL (or any direct audio URL)",
    )
    parser.add_argument(
        "--skip-transcribe", metavar="FILE",
        help="Path to a cached transcript JSON — skip AssemblyAI",
    )
    parser.add_argument(
        "--skip-scenes", metavar="FILE",
        help="Path to a cached scenes JSON — skip AssemblyAI + Claude",
    )
    parser.add_argument(
        "--skip-images", metavar="FILE",
        help="Path to a cached scenes-with-images JSON — skip Steps 1–3",
    )
    parser.add_argument(
        "--test-duration", metavar="SECONDS", type=float, default=None,
        help="Render only the first N seconds (enables karaoke word-highlight mode)",
    )
    parser.add_argument(
        "--limit-duration", metavar="SECONDS", type=float, default=None,
        help="Render only the first N seconds in chunk mode (no karaoke) — for testing",
    )
    args = parser.parse_args()

    # ---- Validate env vars ------------------------------------------------
    missing = [name for name, val in [
        ("ASSEMBLYAI_API_KEY", ASSEMBLYAI_API_KEY),
        ("ANTHROPIC_API_KEY",  ANTHROPIC_API_KEY),
        ("OPENAI_API_KEY",     OPENAI_API_KEY),
        ("SHOTSTACK_API_KEY or SHOTSTACK_SANDBOX_KEY", _SHOTSTACK_KEY),
    ] if not val]
    if missing:
        raise EnvironmentError(f"Missing environment variables: {', '.join(missing)}")

    OUTPUT_DIR.mkdir(exist_ok=True)
    IMAGES_DIR.mkdir(exist_ok=True)

    # Convert Google Drive share URL → direct download URL
    audio_url = convert_gdrive_url(args.audio_url)
    log.info("Audio URL: %s", audio_url)

    # ---- Step 1: Transcribe -----------------------------------------------
    if args.skip_images:
        # Scenes (with images) cached — we still need transcript for subtitles
        if args.skip_transcribe:
            transcript = _load_transcript(args.skip_transcribe)
        else:
            transcript = transcribe_audio(audio_url)
            _save_transcript(transcript, OUTPUT_DIR / "transcript.json")
        scenes = _load_scenes(args.skip_images)
        log.info("Loaded %d scenes with images from cache", len(scenes))
    elif args.skip_scenes:
        # Scenes cached — still need transcript for subtitles
        if args.skip_transcribe:
            transcript = _load_transcript(args.skip_transcribe)
        else:
            transcript = transcribe_audio(audio_url)
            _save_transcript(transcript, OUTPUT_DIR / "transcript.json")
        scenes = _load_scenes(args.skip_scenes)
        log.info("Loaded %d scenes from cache", len(scenes))
        # Step 3: generate illustrations
        scenes = generate_illustrations(scenes)
        _save_scenes(scenes, OUTPUT_DIR / "scenes_with_images.json")
    else:
        if args.skip_transcribe:
            transcript = _load_transcript(args.skip_transcribe)
        else:
            transcript = transcribe_audio(audio_url)
            _save_transcript(transcript, OUTPUT_DIR / "transcript.json")

        # Step 2: scene breakdown
        scenes = generate_scenes(transcript)
        _save_scenes(scenes, OUTPUT_DIR / "scenes.json")

        # Step 3: illustrations
        scenes = generate_illustrations(scenes)
        _save_scenes(scenes, OUTPUT_DIR / "scenes_with_images.json")

    # ---- Step 4: Render ---------------------------------------------------
    import subprocess

    test_duration  = args.test_duration
    limit_duration = args.limit_duration
    cap = test_duration or limit_duration

    if cap:
        # Short test render via single Shotstack job (render_video handles setup)
        log.info("Step 4: Assembling video via Shotstack (first %ss)...", cap)
        video_url = render_video(
            audio_url=audio_url,
            scenes=scenes,
            transcript=transcript,
            test_duration=test_duration,
            limit_duration=limit_duration,
        )
        if test_duration:
            out_name = f"podcast_video_test_{int(test_duration)}s.mp4"
        else:
            out_name = f"podcast_video_limit_{int(limit_duration)}s.mp4"
        out_path = OUTPUT_DIR / out_name
        _download_file(video_url, out_path)

        # Quick frame check from S3 URL
        probe_sec = min(10, cap - 1)
        frame_path = OUTPUT_DIR / "frame_check.jpg"
        subprocess.run([
            "ffmpeg", "-ss", str(probe_sec), "-i", video_url,
            "-frames:v", "1", "-update", "1", "-q:v", "2", str(frame_path), "-y",
        ], capture_output=True)
        if frame_path.exists():
            log.info("Frame extracted → %s", frame_path)

        print("\n" + "=" * 60)
        print("Pipeline complete!")
        print(f"Video URL : {video_url}")
        print(f"Local copy: {out_path}")
        if frame_path.exists():
            print(f"Frame check: {frame_path}")
        print("=" * 60)

    else:
        # Full-video render: karaoke batches → segments → concat → compress
        log.info("Step 4: Assembling full karaoke video in batches...")
        scenes = _ensure_hosted_image_urls(scenes)
        log.info("  Identifying speakers via Claude...")
        speaker_map = _identify_speakers(transcript)
        raw_path = _render_full_karaoke(
            audio_url=audio_url,
            scenes=scenes,
            transcript=transcript,
            speaker_map=speaker_map,
        )

        # Compress
        compressed = OUTPUT_DIR / "podcast_video.mp4"
        log.info("Compressing video (this may take a few minutes)...")
        result = subprocess.run([
            "ffmpeg", "-i", str(raw_path),
            "-c:v", "libx264", "-crf", "20",
            "-minrate", "500k", "-maxrate", "4000k", "-bufsize", "8000k",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(compressed), "-y",
        ], capture_output=True, text=True)
        final_path = compressed if result.returncode == 0 else raw_path
        if result.returncode != 0:
            log.warning("Compression failed:\n%s", result.stderr[-300:])

        # Frame check from the local file
        frame_path = OUTPUT_DIR / "frame_check.jpg"
        subprocess.run([
            "ffmpeg", "-ss", "27", "-i", str(final_path),
            "-frames:v", "1", "-update", "1", "-q:v", "2", str(frame_path), "-y",
        ], capture_output=True)

        print("\n" + "=" * 60)
        print("Pipeline complete!")
        print(f"Local copy: {final_path}")
        if frame_path.exists():
            print(f"Frame check: {frame_path}")
        print("=" * 60)


if __name__ == "__main__":
    main()
