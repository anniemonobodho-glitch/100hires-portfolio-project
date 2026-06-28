#!/usr/bin/env python3
"""
Fetch a YouTube video transcript and save it as Markdown.

Primary method:
    Supadata API (when SUPADATA_API_KEY is set)

Fallback method:
    youtube-transcript-api (free Python package, no API key required)

Requirements:
    pip install -r requirements.txt

Example:
    export SUPADATA_API_KEY="your-api-key-here"   # optional
    python scripts/fetch_youtube_transcript.py \\
        "https://www.youtube.com/watch?v=-4cu882OJ8E" \\
        --expert "Aleyda Solis" \\
        --channel "Crawling Mondays by Aleyda" \\
        --output "aleyda-solis-ai-seo-video.md"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Supadata base URL (see https://docs.supadata.ai/)
SUPADATA_BASE_URL = "https://api.supadata.ai/v1"

# Where transcript Markdown files are saved (relative to repo root).
REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_BASE_DIR = REPO_ROOT / "research" / "youtube-transcripts"

# How long to wait between async job status checks (seconds).
JOB_POLL_INTERVAL_SECONDS = 2
JOB_POLL_MAX_ATTEMPTS = 60

def get_api_key() -> str | None:
    """
    Read the Supadata API key from the environment if it exists.

    Returns None when the key is missing so the script can skip Supadata and
    use the fallback method instead of exiting early.
    """
    api_key = os.environ.get("SUPADATA_API_KEY", "").strip()
    return api_key or None


def extract_video_id(youtube_url: str) -> str:
    """
    Pull the 11-character YouTube video ID out of common URL formats.

    Supports:
      - https://www.youtube.com/watch?v=VIDEO_ID
      - https://youtu.be/VIDEO_ID
      - https://www.youtube.com/embed/VIDEO_ID
    """
    patterns = [
        r"(?:youtube\.com/watch\?.*v=|youtu\.be/|youtube\.com/embed/)([A-Za-z0-9_-]{11})",
        r"^([A-Za-z0-9_-]{11})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, youtube_url)
        if match:
            return match.group(1)
    raise ValueError(f"Could not extract a YouTube video ID from URL: {youtube_url}")


def slugify(text: str) -> str:
    """Convert a name or title into a safe lowercase filename slug."""
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-") or "video"


def supadata_get(
    api_key: str,
    endpoint: str,
    params: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | list[Any] | str]:
    """
    Send a GET request to Supadata and return (status_code, parsed_json).

    Uses only Python's standard library for Supadata calls.
    """
    query = f"?{urlencode(params)}" if params else ""
    url = f"{SUPADATA_BASE_URL}{endpoint}{query}"

    request = Request(
        url,
        headers={
            "x-api-key": api_key,
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=60) as response:
            status_code = response.getcode()
            raw_body = response.read().decode("utf-8")
    except HTTPError as exc:
        status_code = exc.code
        raw_body = exc.read().decode("utf-8", errors="replace")
    except URLError as exc:
        raise RuntimeError(
            f"Network error while contacting Supadata: {exc.reason}"
        ) from exc

    if not raw_body.strip():
        return status_code, {}

    try:
        parsed = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Supadata returned non-JSON data (HTTP {status_code}): {raw_body[:500]}"
        ) from exc

    return status_code, parsed


def format_publish_date(raw_date: str | None) -> str:
    """Turn an ISO timestamp into a readable YYYY-MM-DD date, or 'Not available'."""
    if not raw_date:
        return "Not available"

    try:
        normalized = raw_date.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        return parsed.date().isoformat()
    except ValueError:
        return raw_date


def fetch_video_metadata(api_key: str, youtube_url: str) -> dict[str, str]:
    """
    Fetch video title, channel name, and publish date from Supadata metadata API.

    Docs: https://docs.supadata.ai/get-metadata
    """
    status_code, payload = supadata_get(api_key, "/metadata", {"url": youtube_url})

    if status_code >= 400 or not isinstance(payload, dict):
        return {
            "title": "Unknown video title",
            "channel": "Unknown channel",
            "publish_date": "Not available",
        }

    # Unified metadata schema uses author.displayName and createdAt.
    title = payload.get("title") or "Unknown video title"
    author = payload.get("author") or {}
    channel = (
        author.get("displayName")
        or author.get("username")
        or "Unknown channel"
    )
    publish_date = format_publish_date(payload.get("createdAt"))

    # Some older Supadata responses may nest channel info differently.
    if channel == "Unknown channel" and isinstance(payload.get("channel"), dict):
        channel = payload["channel"].get("name") or channel
    if publish_date == "Not available":
        publish_date = format_publish_date(payload.get("uploadDate"))

    return {
        "title": title,
        "channel": channel,
        "publish_date": publish_date,
    }


def poll_transcript_job(api_key: str, job_id: str) -> dict[str, Any]:
    """
    Poll Supadata until an async transcript job finishes.

    Long videos may return HTTP 202 with a jobId instead of the transcript immediately.
    Docs: https://docs.supadata.ai/get-transcript
    """
    for attempt in range(1, JOB_POLL_MAX_ATTEMPTS + 1):
        status_code, payload = supadata_get(api_key, f"/transcript/{job_id}")

        if status_code >= 400:
            raise RuntimeError(format_supadata_error(status_code, payload))

        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected job status response: {payload!r}")

        job_status = payload.get("status", "completed")
        if job_status in {"completed", "failed"}:
            if job_status == "failed":
                raise RuntimeError(
                    f"Supadata transcript job {job_id} failed: {payload!r}"
                )
            return payload

        print(
            f"Waiting for transcript job {job_id} "
            f"(attempt {attempt}/{JOB_POLL_MAX_ATTEMPTS})...",
            file=sys.stderr,
        )
        time.sleep(JOB_POLL_INTERVAL_SECONDS)

    raise RuntimeError(
        f"Timed out waiting for Supadata transcript job {job_id} to complete."
    )


def format_supadata_error(status_code: int, payload: Any) -> str:
    """Build a clear, human-readable error message from a Supadata error response."""
    if isinstance(payload, dict) and payload.get("error"):
        parts = [
            f"Supadata request failed (HTTP {status_code}).",
            f"Error code: {payload.get('error')}",
            f"Message: {payload.get('message', 'No message provided')}",
        ]
        if payload.get("details"):
            parts.append(f"Details: {payload['details']}")
        if payload.get("documentationUrl"):
            parts.append(f"Docs: {payload['documentationUrl']}")
        return "\n".join(parts)

    return f"Supadata request failed (HTTP {status_code}): {payload!r}"


def extract_transcript_text(payload: dict[str, Any]) -> str:
    """
    Normalize Supadata transcript responses into plain text.

    Supadata may return:
      - {"content": "plain text...", "lang": "en"} when text=true
      - {"content": [{"text": "...", "offset": 0, ...}, ...], "lang": "en"} when text=false
    """
    content = payload.get("content")

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        if not content:
            return ""

        # Timestamped chunks: join each segment's text field.
        if all(isinstance(chunk, dict) for chunk in content):
            return " ".join(
                chunk.get("text", "").strip()
                for chunk in content
                if chunk.get("text")
            ).strip()

        # Fallback if content is a list of plain strings.
        if all(isinstance(chunk, str) for chunk in content):
            return " ".join(chunk.strip() for chunk in content if chunk.strip())

    raise RuntimeError(
        "Could not parse transcript text from Supadata response. "
        f"Unexpected content format: {content!r}"
    )


def try_supadata_transcript(
    api_key: str,
    youtube_url: str,
    lang: str = "en",
) -> tuple[str | None, str | None]:
    """
    Try to fetch a transcript from Supadata.

    Returns:
        (transcript_text, error_message)

    On success: (text, None)
    On failure: (None, error_message)
    """
    try:
        params = {
            "url": youtube_url,
            "text": "true",  # Ask for plain text instead of timestamped chunks.
            "lang": lang,
        }
        status_code, payload = supadata_get(api_key, "/youtube/transcript", params)

        # Async processing: Supadata returns a job ID to poll.
        if status_code == 202 and isinstance(payload, dict) and payload.get("jobId"):
            payload = poll_transcript_job(api_key, payload["jobId"])

        if status_code >= 400 or not isinstance(payload, dict):
            return None, format_supadata_error(status_code, payload)

        transcript_text = extract_transcript_text(payload)
        if not transcript_text:
            return None, (
                "Supadata returned an empty transcript. The video may have no captions "
                "or no detectable speech."
            )

        return transcript_text, None

    except RuntimeError as exc:
        return None, str(exc)


def fetch_transcript_youtube_transcript_api(video_id: str, lang: str = "en") -> str:
    """
    Fallback: fetch a transcript directly from YouTube using youtube-transcript-api.

    This package reads publicly available captions. No API key is required.
    Docs: https://github.com/jdepoix/youtube-transcript-api
    """
    try:
        from youtube_transcript_api import (
            IpBlocked,
            NoTranscriptFound,
            RequestBlocked,
            TranscriptsDisabled,
            VideoUnavailable,
            YouTubeTranscriptApi,
        )
    except ImportError as exc:
        raise RuntimeError(
            "The fallback package 'youtube-transcript-api' is not installed.\n"
            "Install it with:\n"
            "  pip install -r requirements.txt"
        ) from exc

    api = YouTubeTranscriptApi()

    try:
        # Ask for the preferred language first, then fall back to any available caption.
        fetched_transcript = api.fetch(video_id, languages=[lang, "en"])
    except NoTranscriptFound as exc:
        raise RuntimeError(
            "No transcript was found for this video in the requested language."
        ) from exc
    except TranscriptsDisabled as exc:
        raise RuntimeError(
            "Transcripts are disabled for this video on YouTube."
        ) from exc
    except VideoUnavailable as exc:
        raise RuntimeError(
            "This YouTube video is unavailable (private, deleted, or restricted)."
        ) from exc
    except (RequestBlocked, IpBlocked) as exc:
        raise RuntimeError(
            "YouTube blocked the transcript request from this network/IP address."
        ) from exc

    # Join each caption snippet into one plain-text transcript.
    transcript_text = " ".join(
        snippet.text.strip()
        for snippet in fetched_transcript
        if snippet.text.strip()
    ).strip()

    if not transcript_text:
        raise RuntimeError(
            "youtube-transcript-api returned an empty transcript for this video."
        )

    return transcript_text


def build_markdown(
    *,
    video_title: str,
    expert_name: str,
    channel_name: str,
    youtube_url: str,
    publish_date: str,
    collection_date: str,
    collection_method: str,
    transcript_text: str,
) -> str:
    """Assemble the final Markdown document using the project template."""
    return f"""# Video Transcript: {video_title}

**Expert / Creator:** {expert_name}
**Channel:** {channel_name}
**YouTube URL:** {youtube_url}
**Publish date:** {publish_date}
**Transcript collection date:** {collection_date}
**Collection method:** {collection_method}
**Topic relevance:** AI-powered SEO content production

## Why this video was selected

This video was selected because it is relevant to AI search, SEO strategy, technical SEO, or AI-powered content production. Replace this placeholder with a short note on why this specific video matters for the research playbook.

## Key Takeaways

* Placeholder takeaway 1
* Placeholder takeaway 2
* Placeholder takeaway 3

## Transcript

{transcript_text}
"""


def resolve_output_path(
    *,
    output_filename: str | None,
    expert_name: str | None,
    video_id: str,
    video_title: str,
) -> Path:
    """Choose where to write the Markdown file."""
    if output_filename:
        filename = output_filename
        if not filename.endswith(".md"):
            filename = f"{filename}.md"
        return OUTPUT_BASE_DIR / filename

    expert_slug = slugify(expert_name) if expert_name else "unknown-expert"
    title_slug = slugify(video_title)[:60]
    filename = f"{expert_slug}-{title_slug}-{video_id}.md"
    return OUTPUT_BASE_DIR / expert_slug / filename


def print_both_methods_failed(
    *,
    supadata_attempted: bool,
    supadata_error: str | None,
    fallback_error: str,
) -> None:
    """Print a beginner-friendly message when neither method could fetch a transcript."""
    print("\nError: Could not fetch a transcript for this video.", file=sys.stderr)
    print(
        "\nThis usually means the video does not have captions/transcripts available, "
        "or access was blocked.",
        file=sys.stderr,
    )

    if supadata_attempted and supadata_error:
        print("\nSupadata attempt:", file=sys.stderr)
        print(supadata_error, file=sys.stderr)

    print("\nFallback attempt (youtube-transcript-api):", file=sys.stderr)
    print(fallback_error, file=sys.stderr)

    print(
        "\nWhat you can try next:",
        file=sys.stderr,
    )
    print("  1. Open the video on YouTube and check whether captions are available.", file=sys.stderr)
    print("  2. Confirm the URL is correct and the video is public.", file=sys.stderr)
    print("  3. Retry later if the request was blocked temporarily.", file=sys.stderr)
    print("  4. Install dependencies if needed: pip install -r requirements.txt", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    """Define and parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Fetch a YouTube transcript (Supadata first, youtube-transcript-api fallback) "
            "and save it as Markdown."
        ),
    )
    parser.add_argument(
        "youtube_url",
        help="Full YouTube video URL (for example: https://www.youtube.com/watch?v=VIDEO_ID)",
    )
    parser.add_argument(
        "--expert",
        help='Expert or creator name (for example: "Aleyda Solis")',
    )
    parser.add_argument(
        "--channel",
        help="YouTube channel name (optional; fetched automatically if omitted)",
    )
    parser.add_argument(
        "--output",
        help="Output filename or path relative to research/youtube-transcripts/",
    )
    parser.add_argument(
        "--lang",
        default="en",
        help="Preferred transcript language code (ISO 639-1). Default: en",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    api_key = get_api_key()

    try:
        video_id = extract_video_id(args.youtube_url)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Default metadata used when Supadata metadata is unavailable.
    metadata = {
        "title": f"YouTube video {video_id}",
        "channel": args.channel or "Unknown channel",
        "publish_date": "Not available",
    }

    if api_key:
        print(f"Fetching metadata for video ID: {video_id}", file=sys.stderr)
        try:
            metadata = fetch_video_metadata(api_key, args.youtube_url)
        except RuntimeError as exc:
            print(
                f"Warning: Could not fetch metadata from Supadata.\n{exc}",
                file=sys.stderr,
            )
    else:
        print(
            "Note: SUPADATA_API_KEY is not set. Skipping Supadata metadata lookup.",
            file=sys.stderr,
        )

    channel_name = args.channel or metadata["channel"]
    expert_name = args.expert or channel_name
    collection_date = date.today().isoformat()

    transcript_text: str | None = None
    collection_method = "Supadata API"
    supadata_attempted = False
    supadata_error: str | None = None

    if api_key:
        supadata_attempted = True
        print("Fetching transcript from Supadata...", file=sys.stderr)
        transcript_text, supadata_error = try_supadata_transcript(
            api_key,
            args.youtube_url,
            lang=args.lang,
        )

        if transcript_text:
            print("Transcript fetched successfully from Supadata.", file=sys.stderr)
        elif supadata_error:
            print(
                "Warning: Supadata transcript request failed. Trying fallback method.\n"
                f"{supadata_error}",
                file=sys.stderr,
            )
    else:
        print(
            "Note: SUPADATA_API_KEY is not set. Using fallback method directly.",
            file=sys.stderr,
        )

    if transcript_text is None:
        print("Fetching transcript with youtube-transcript-api (fallback)...", file=sys.stderr)
        try:
            transcript_text = fetch_transcript_youtube_transcript_api(
                video_id,
                lang=args.lang,
            )
        except RuntimeError as exc:
            print_both_methods_failed(
                supadata_attempted=supadata_attempted,
                supadata_error=supadata_error,
                fallback_error=str(exc),
            )
            sys.exit(1)

        if supadata_attempted:
            collection_method = (
                "Supadata API attempted; fallback method used: youtube-transcript-api"
            )
        else:
            collection_method = "youtube-transcript-api"

        print("Transcript fetched successfully with fallback method.", file=sys.stderr)

    markdown = build_markdown(
        video_title=metadata["title"],
        expert_name=expert_name,
        channel_name=channel_name,
        youtube_url=args.youtube_url,
        publish_date=metadata["publish_date"],
        collection_date=collection_date,
        collection_method=collection_method,
        transcript_text=transcript_text,
    )

    output_path = resolve_output_path(
        output_filename=args.output,
        expert_name=expert_name,
        video_id=video_id,
        video_title=metadata["title"],
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")

    print(f"Saved transcript to: {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
