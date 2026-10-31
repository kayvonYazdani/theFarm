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
EDITOR_CAPTION_SIZE = 50
EDITOR_CAPTION_Y = 0.78

EDITOR_CAPTION_MAX_CHARS = 28
EDITOR_CAPTION_MAX_WORDS = 6
EDITOR_CAPTION_PAUSE = 0.40
EDITOR_CAPTION_MAX_WIDTH = 0.85

EDITOR_CAPTION_COLOR = "#FFFFFF"
EDITOR_CAPTION_HIGHLIGHT = "#C391FF"
EDITOR_CAPTION_OUTLINE = 5
EDITOR_CAPTION_POP = 1.05
# where do the captions go on a vertical scale 0 - 1 (high - low)
EDITOR_CAPTION_Y = 0.65
EDITOR_CAPTION_WORD_SPACING = 15
EDITOR_CAPTION_MAX_WORDS = 3


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

#editor

EDITOR_EMOJI_WORDS = {
    "bro": "💀",
    "crazy": "🤯",
    "insane": "🤯",
    "money": "💰",
    "love": "❤️",
    "fire": "🔥",
    "laugh": "😂",
    "wow": "😮",
    "mad": "😡",
    "sad": "😢",
    "goat": "🐐",
    "win": "🏆",
}


# AI BRAIN — optional creative planning; never starts video rendering.
AI_ENABLED = True
AI_DEBUG = False
GEMINI_PRIMARY_MODEL = "gemini-3.8-flash"
GEMINI_FALLBACK_MODEL = "gemini-3.5-flash-lite"
GEMINI_MAX_RETRIES = 3  # Additional attempts per model for transient errors.
GEMINI_TIMEOUT_SECONDS = 180
AI_VALIDATION_RETRIES = 1
AI_CACHE_ENABLED = True

# PRODUCTION — run with: python run.py
INPUT_VIDEO = Path.home() / "Desktop" / "testTogi.mp4"
INPUT_TRANSCRIPT = CACHE_FOLDER / "caption_test" / "aligned_words.json"
USE_CLIPPER = False
OUTPUT_DIR = PROJECT_DIR / "finished"
MANUAL_TITLE = ""  # Optional when AI_ENABLED is False; otherwise use the source filename.
MANUAL_DESCRIPTION = ""
MANUAL_HASHTAGS = ["#Shorts", "#Video", "#Clip"]

# Automatic audio-triggered shake, independent of AI decisions.
EDITOR_SHAKE_ENABLED = True
EDITOR_SHAKE_THRESHOLD_DB = -5.0
EDITOR_SHAKE_PIXELS = 5
EDITOR_SHAKE_SPEED = 2.0

# Farm 2.2's title.jpg is an IN-VIDEO header, separate from the thumbnail.
TITLE_ENABLED = True
TITLE_HEADER = PROJECT_DIR / "editingMedia" / "assets" / "title.jpg"
TITLE_VISIBLE_Y = None  # Original automatic placement immediately above foreground video.
TITLE_DELAY = 3.0
TITLE_MAX_SECONDS = 5.0
TITLE_FADE_SECONDS = 0.6
TITLE_GLOW_STRENGTH = 0.10
TITLE_GLOW_PULSE_HZ = 0.18
TITLE_FONT_PATHS = ["/System/Library/Fonts/HelveticaNeue.ttc", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]
TITLE_FONT_SIZE = 34
TITLE_MIN_FONT_SIZE = 22
TITLE_TEXT_LEFT = 135
TITLE_TEXT_RIGHT = 40
TITLE_TEXT_Y = 60
TITLE_TEXT_COLOR = "#ADD8E6"

# Reuse the installed VideoHighlighter and Farm 2.2's existing isolated worker/selector.
LEGACY_FARM_DIR = PROJECT_DIR.parent / "[vault]" / "theFarm2.2"
VIDEOHIGHLIGHTER_DIR = PROJECT_DIR / "VideoHighlighter"
VIDEOHIGHLIGHTER_PYTHON = VIDEOHIGHLIGHTER_DIR / ".venv-videohighlighter" / "bin" / "python"
VIDEOHIGHLIGHTER_TIMEOUT = 7200
VIDEOHIGHLIGHTER_MAX_CANDIDATES = 10
MAX_CLIP_DURATION = 180
VIDEOHIGHLIGHTER_MIN_SCORE = 3.0
VIDEOHIGHLIGHTER_MIN_PROMINENCE = 2.0
VIDEOHIGHLIGHTER_OPTIONS = {
    "clip_time": 0, "auto_min_clip": 0.0, "auto_max_clip": 180,
    "scene_points": 1, "motion_event_points": 1,
    "motion_peak_points": 3, "audio_peak_points": 3,
    "object_points": 0, "action_points": 0, "keyword_points": 0,
    "highlight_objects": [], "interesting_actions": [],
    "use_transcript": False, "write_highlight_report": False,
}

# Separate thumbnail generator: preserve the old ComfyUI API workflow/model settings.
TOKEN_RESOURCE_DIR = PROJECT_DIR.parent / "tokens"
GENERATE_THUMBNAIL = True
THUMBNAIL_WORKFLOW = TOKEN_RESOURCE_DIR / "basicImage.json"
COMFYUI_URL = "http://127.0.0.1:8188"
THUMBNAIL_WIDTH = 720
THUMBNAIL_HEIGHT = 1280
THUMBNAIL_TIMEOUT = 900

# Uploading is manual only: post_to_youtube.py --upload. run.py never uploads.
YOUTUBE_CLIENT_FILE = ""  # Auto-find a single client_secret*.json in tokens.
YOUTUBE_TOKEN_FILE = TOKEN_RESOURCE_DIR / "youtube_token.json"
YOUTUBE_CATEGORY_ID = "24"
YOUTUBE_MADE_FOR_KIDS = False
YOUTUBE_PRIVACY = "private"
