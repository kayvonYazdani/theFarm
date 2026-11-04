
"""Download ranked YouTube or Twitch videos for The Farm."""

import csv
import subprocess
from pathlib import Path

import config as settings
from finder import search_videos, search_vods


# ============================================================
#                    HISTORY CHECKER
# ============================================================

def get_processed_ids(platform):
    """Read posted video IDs from the selected platform's history CSV."""

    history_file = (
        settings.YT_HISTORY
        if platform == "YT"
        else settings.TTV_HISTORY
    )

    processed_ids = set()

    if not history_file.exists():
        return processed_ids

    with history_file.open(
        "r", encoding="utf-8-sig", newline=""
    ) as file:

        reader = csv.DictReader(file)

        for row in reader:
            video_id = row.get("video_id", "").strip()

            if video_id:
                processed_ids.add(video_id)

    return processed_ids


# ============================================================
#                    VIDEO FINDER
# ============================================================

def get_video_candidates():
    """Get ranked videos from the selected platform's finder."""

    platform = settings.PLATFORM.upper()

    if platform == "YT":
        videos = search_videos()

        candidates = [
            {
                "id": video["id"]["videoId"],
                "url": (
                    "https://www.youtube.com/watch?v="
                    + video["id"]["videoId"]
                )
            }
            for video in videos
        ]

    elif platform == "TTV":
        videos = search_vods()

        candidates = [
            {
                "id": str(video["id"]),
                "url": video["url"]
            }
            for video in videos
        ]

    else:
        raise ValueError("PLATFORM must be YT or TTV")

    return platform, candidates


# ============================================================
#                    VIDEO DOWNLOADER
# ============================================================

def download_video(platform, video):
    """Download a video at or below the configured resolution."""

    import re
    video_id = str(video["id"])
    if not re.fullmatch(r"[A-Za-z0-9_-]+",video_id):
        raise ValueError("Invalid source video identifier")

    # Create videos folder if missing
    settings.VIDS_FOLDER.mkdir(parents=True, exist_ok=True)

    # Check whether the video is already downloaded
    prefix = f"{platform}_{video_id}"

    existing_files = [
        file
        for file in settings.VIDS_FOLDER.glob(f"{prefix}.*")
        if file.is_file() and file.suffix.lower() in {
            ".mp4", ".mkv", ".webm", ".mov"
        }
    ]

    if existing_files:
        print(f"Already downloaded: {video_id}")
        from editor import VideoEditor
        for existing in existing_files:
            try:
                VideoEditor(existing)
                return existing
            except Exception:
                # Preserve damaged data for inspection; yt-dlp must not silently reuse it.
                raise ValueError(f"Existing download is invalid: {existing.name}; move it aside before retrying")

    # Set maximum download resolution
    height = int(settings.DOWNLOAD_HEIGHT)

    output = str(
        settings.VIDS_FOLDER / f"{prefix}.%(ext)s"
    )

    command = [
        "yt-dlp",
        "--no-playlist", "--no-progress",
        "--no-overwrites",

        # Best available quality up to the configured height
        "-f",
        f"bv*[height<={height}]+ba/b[height<={height}]",

        # Produce MP4 output for the clipper
        "--merge-output-format", "mp4",
        "--remux-video", "mp4",

        "-o", output,
        "--print", "after_move:filepath",
        video["url"]
    ]

    print(f"Downloading: {video['url']}")

    result = subprocess.run(
        command,
        capture_output=True,
        text=True, timeout=settings.DOWNLOAD_TIMEOUT
    )

    if result.returncode != 0:
        print(f"Download failed: {video_id}")
        # Details are captured by the production session log, not shown on the dashboard.
        print(result.stderr)
        return None

    # Retrieve actual downloaded file path
    for line in reversed(result.stdout.splitlines()):
        path = Path(line.strip())

        if path.is_file():
            print(f"Saved: {path.name}")
            return path

    # Fallback: check output folder
    files = [
        file
        for file in settings.VIDS_FOLDER.glob(f"{prefix}.*")
        if file.is_file() and file.suffix.lower() in {
            ".mp4", ".mkv", ".webm", ".mov"
        }
    ]

    return files[0] if files else None


# ============================================================
#                    DOWNLOAD MANAGER
# ============================================================

if __name__ == "__main__":
    print("Start The Farm with: python run.py")
