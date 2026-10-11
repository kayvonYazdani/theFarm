import os
import html
from datetime import datetime, timezone
import requests
import re

from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from googleapiclient.discovery import build

import config as settings

load_dotenv()

CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")

API_KEY = os.getenv("YOUTUBE_API_KEY")
youtube = build("youtube", "v3", developerKey=API_KEY)

SPEED_CHANNEL_ID = "UCWsDFcIhY2DBi3GB5uykGXA"



def search_videos():
    """Find YouTube completed streams of any specific streamer with certain filters"""

    now = datetime.now(timezone.utc)
    start_date = now - timedelta(days=settings.DAYS_BACK)

    response = youtube.search().list(
        part="snippet",
        channelId=SPEED_CHANNEL_ID,
        type="video",
        eventType="completed",
        publishedAfter=start_date.isoformat(),
        order="viewCount",
        regionCode="US",
        maxResults=25
    ).execute()

    return response["items"]


def print_videos(videos):
    """Print each livestream's title and YouTube URL."""

    for i, video in enumerate(videos, start=1):
        title = html.unescape(video["snippet"]["title"])
        video_id = video["id"]["videoId"]

        print(f"\n{i}. {title}")
        print(f"   URL: https://youtube.com/watch?v={video_id}")

    if not videos:
        print("No matching livestreams found.")

def search_vods(username):
    """Find a streamer's completed VODs from this month under 3 hours, ranked by views."""

    # Authenticate with Twitch
    auth = requests.post(
        "https://id.twitch.tv/oauth2/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials"
        },
        timeout=15
    )
    auth.raise_for_status()
    token = auth.json()["access_token"]

    headers = {
        "Client-ID": CLIENT_ID,
        "Authorization": f"Bearer {token}"
    }

    # Find the streamer's Twitch user ID
    user_response = requests.get(
        "https://api.twitch.tv/helix/users",
        headers=headers,
        params={"login": username},
        timeout=15
    )
    user_response.raise_for_status()

    users = user_response.json()["data"]

    if not users:
        return []

    user_id = users[0]["id"]

    # Retrieve previous livestreams
    response = requests.get(
        "https://api.twitch.tv/helix/videos",
        headers=headers,
        params={
            "user_id": user_id,
            "type": "archive",
            "period": "month",
            "first": 100
        },
        timeout=15
    )
    response.raise_for_status()

    videos = response.json()["data"]

    # Beginning of the current calendar month
    now = datetime.now(timezone.utc)
    month_start = now.replace(
        day=1, hour=0, minute=0,
        second=0, microsecond=0
    )

    valid_videos = []

    for video in videos:

        published = datetime.fromisoformat(
            video["published_at"].replace("Z", "+00:00")
        )

        # Convert Twitch duration (e.g. 2h15m30s) to seconds
        duration = video["duration"]

        hours = re.search(r"(\d+)h", duration)
        minutes = re.search(r"(\d+)m", duration)
        seconds = re.search(r"(\d+)s", duration)

        total_seconds = (
            (int(hours.group(1)) * 3600 if hours else 0)
            + (int(minutes.group(1)) * 60 if minutes else 0)
            + (int(seconds.group(1)) if seconds else 0)
        )

        if published >= month_start and total_seconds < 10800:
            valid_videos.append(video)

    # Highest view count first
    valid_videos.sort(
        key=lambda video: video["view_count"],
        reverse=True
    )

    return valid_videos


def print_vods(videos):
    """Print each Twitch VOD's title, views, duration, and URL."""

    if not videos:
        print("No matching Twitch VODs found.")
        return

    for i, video in enumerate(videos, start=1):
        print(f"\n{i}. {video['title']}")
        print(f"   Views: {video['view_count']:,}")
        print(f"   Duration: {video['duration']}")
        print(f"   URL: {video['url']}")


if __name__ == "__main__":
    if settings.PLATFORM == "YT":
        videos = search_videos()
        print_videos(videos)
    if settings.PLATFORM == "TTV":
        videos = search_vods("ishowspeed")
        print_vods(videos)
