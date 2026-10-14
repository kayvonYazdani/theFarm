from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path



# ============================================================
# FOLDER PATHS
# ============================================================




PROJECT_DIR = Path(__file__).resolve().parent

MEDIA_FOLDER = PROJECT_DIR / "media"

VIDS_FOLDER = MEDIA_FOLDER / "videos"
CLIPS_FOLDER = MEDIA_FOLDER / "clips"

DATABASE_FOLDER = PROJECT_DIR / "data"
CACHE_FOLDER = PROJECT_DIR / "cache"
YT_HISTORY = DATABASE_FOLDER / "YT_history.csv"
TTV_HISTORY = DATABASE_FOLDER / "TTV_histroy.csv"




# ============================================================
#                    🔎 FINDER SETTINGS 🔍
# ============================================================





#. "YT" for YouTube "TTV" for Twitch .#
PLATFORM = "TTV"

EVENT_TYPE = "completed"  # "completed" or "video"

DAYS_BACK = 14 # For both YT AND TTV whats the maximum age a video can be
VIDEO_MAX_DURATION = 180  # Maximum Video duration in minutes for both

DOWNLOAD_HEIGHT = 1080
VIDEOS_TO_PROCESS = 1

PICK_YT = "RANDOM" #Pick YouTuber (all caps) or select RANDOM
PICK_TWITCH = "RANDOM" #Pick streamer (all caps) or select RANDOM



EDITOR_FPS = 30

EDITOR_BACKGROUND_BLUR = 70

EDITOR_FOREGROUND_ZOOM = 1.8
EDITOR_BACKGROUND_ZOOM = 1.0

# FILTER SETTINGS
EDITOR_SATURATION = 1.50
EDITOR_CONTRAST = 1.25
EDITOR_BRIGHTNESS = 0.10

# CAPTION SETTINGS
EDITOR_WHISPER_MODEL = "large-v3"
EDITOR_WHISPER_LANGUAGE = "en"

EDITOR_WHISPERX_PYTHON = (
    Path(__file__).resolve().parent.parent
    / ".venv-whisperx313" / "bin" / "python"
)

EDITOR_FONT = "/System/Library/Fonts/Avenir Next.ttc"
EDITOR_CAPTION_SIZE = 64
EDITOR_CAPTION_Y = 0.78

EDITOR_CAPTION_MAX_CHARS = 28
EDITOR_CAPTION_MAX_WORDS = 6
EDITOR_CAPTION_PAUSE = 0.40
EDITOR_CAPTION_MAX_WIDTH = 0.85

EDITOR_CAPTION_COLOR = "#FFFFFF"
EDITOR_CAPTION_HIGHLIGHT = "#C391FF"
EDITOR_CAPTION_OUTLINE = 5
EDITOR_CAPTION_POP = 1.18


# ============================================================
#                        📊 LIST'S 📊
# ============================================================

YOUTUBE_CHANNELS = {
    "RONALDO": "UCav7eOwYM9Bwxlgnts0y74Q",
    "ADAPT":   "UCi87kxosIT3wrnEA-mv4jWA",
    "SPEED":   "UCWsDFcIhY2DBi3GB5uykGXA",
    "JYNXZI":  "UCjiXtODGCCulmhwypZAWSag",
    "TOGI":    "UCyOT1xkzkOCIp0t0LsiSXsA",
    "JASON":   "UCxoKaKMzulp6sfQGOhNJnzQ",
    "MARLON":  "UC7RNjS5nGShmMSuMcVgUyjQ",
    "LACY":    "UCeoZXkaJDtdTHafgoZeDcAA",
    "SILKY":   "UCsx2dUKBb5vAYuXq-bqlfuQ",
}

TWITCH_CHANNELS = {
    "RONALDO": "246450563",
    "ADAPT":   "211234859",
    "SPEED":   "220476955",
    "JYNXZI":  "411377640",
    "TOGI":    "69689829",
    "JASON":   "107117952",
    "MARLON":  "1019733647",
    "LACY":    "494543675",
    "SILKY":   "451786566",
}