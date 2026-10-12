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