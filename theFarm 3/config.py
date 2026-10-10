from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent

MEDIA_FOLDER = PROJECT_DIR / "media"

VIDS_FOLDER = MEDIA_FOLDER / "videos"
CLIPS_FOLDER = MEDIA_FOLDER / "clips"

DATABASE_FOLDER = PROJECT_DIR / "data"
CACHE_FOLDER = PROJECT_DIR / "cache"

#-------------------------------------#
#. "YT" for YouTube "TTV" for Twitch .#
PLATFORM = "TTV"

# ------------ FOR YOUTUBE ------------ #
MAX_RESULTS_PER_QUERY = 50

DAYS_BACK = 14

MIN_VIDEO_MINUTES = 15
MAX_VIDEO_MINUTES = 120

DOWNLOAD_HEIGHT = 1080

VIDEOS_TO_PROCESS = 1

