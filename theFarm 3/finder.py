import os
import html
from datetime import datetime, timezone
import requests
import re
import config as settings
import random

from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from googleapiclient.discovery import build


load_dotenv()

CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")

API_KEY = os.getenv("YOUTUBE_API_KEY")
youtube = build("youtube", "v3", developerKey=API_KEY)


PICK_YT = settings.PICK_YT
YOUTUBE_CHANNELS = settings.YOUTUBE_CHANNELS

def get_youtube_channel():
    """Return the selected YouTube channel name and ID."""

    if PICK_YT.upper() == "RANDOM":
        name = random.choice(list(YOUTUBE_CHANNELS.keys()))
    else:
        name = PICK_YT.upper()

    if name not in YOUTUBE_CHANNELS:
        raise ValueError(f"Unknown YouTube channel: {name}")

    return name, YOUTUBE_CHANNELS[name]

def search_videos():
    """Find completed YouTube streams within the configured date and duration limits."""

    # Select YouTube streamer
    channel_name, channel_id = get_youtube_channel()
    print(f"Searching YouTube streams from: {channel_name}")

    # Calculate filters
    now = datetime.now(timezone.utc)
    start_date = now - timedelta(days=settings.DAYS_BACK)
    max_duration = settings.VIDEO_MAX_DURATION * 60

    # Search completed livestreams
    response = youtube.search().list(
        part="snippet",
        channelId=channel_id,
        type="video",
        **({"eventType": "completed"} if settings.EVENT_TYPE == "completed" else {}),
        publishedAfter=start_date.isoformat(),
        order="viewCount",
        regionCode="US",
        maxResults=50
    ).execute()

    videos = response.get("items", [])

    if not videos:
        return []

    # Collect video IDs
    video_ids = [
        video["id"]["videoId"]
        for video in videos
    ]

    # Retrieve exact video durations
    details = youtube.videos().list(
        part="contentDetails",
        id=",".join(video_ids)
    ).execute()

    # Convert YouTube ISO 8601 durations into seconds
    durations = {}

    for video in details.get("items", []):
        duration = video["contentDetails"]["duration"]

        match = re.fullmatch(
            r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
            duration
        )

        if not match:
            continue

        days, hours, minutes, seconds = (
            int(value or 0) for value in match.groups()
        )

        total_seconds = (
            days * 86400
            + hours * 3600
            + minutes * 60
            + seconds
        )

        durations[video["id"]] = total_seconds

    # Filter videos by maximum duration
    valid_videos = [
        video for video in videos
        if video["id"]["videoId"] in durations
        and durations[video["id"]["videoId"]] <= max_duration
    ]

    return valid_videos



def print_videos(videos):
    """Print each livestream's title and YouTube URL."""

    for i, video in enumerate(videos, start=1):
        title = html.unescape(video["snippet"]["title"])
        video_id = video["id"]["videoId"]

        print(f"\n{i}. {title}")
        print(f"   URL: https://youtube.com/watch?v={video_id}")

    if not videos:
        print("No matching livestreams found.")

def search_vods():
    """Find Twitch VODs within the configured date and duration limits, ranked by views."""

    # Select Twitch streamer
    if settings.PICK_TWITCH.upper() == "RANDOM":
        name = random.choice(list(settings.TWITCH_CHANNELS))
    else:
        name = settings.PICK_TWITCH.upper()

    if name not in settings.TWITCH_CHANNELS:
        raise ValueError(f"Unknown Twitch streamer: {name}")

    user_id = settings.TWITCH_CHANNELS[name]
    print(f"Searching Twitch VODs from: {name}")

    # Calculate filters
    now = datetime.now(timezone.utc)
    start_date = now - timedelta(days=settings.DAYS_BACK)
    max_duration = settings.VIDEO_MAX_DURATION * 60

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

    valid_videos = []
    cursor = None

    # Retrieve Twitch VODs, newest first
    while True:

        params = {
            "user_id": user_id,
            "type": "archive",
            "period": "all",
            "sort": "time",
            "first": 100
        }

        if cursor:
            params["after"] = cursor

        response = requests.get(
            "https://api.twitch.tv/helix/videos",
            headers=headers,
            params=params,
            timeout=15
        )

        response.raise_for_status()
        data = response.json()
        videos = data.get("data", [])

        if not videos:
            break

        for video in videos:

            # Convert publication date
            published = datetime.fromisoformat(
                video["published_at"].replace("Z", "+00:00")
            )

            # Stop when videos become too old
            if published < start_date:
                break

            # Convert duration into seconds
            duration = video["duration"]

            hours = re.search(r"(\d+)h", duration)
            minutes = re.search(r"(\d+)m", duration)
            seconds = re.search(r"(\d+)s", duration)

            total_seconds = (
                (int(hours.group(1)) * 3600 if hours else 0)
                + (int(minutes.group(1)) * 60 if minutes else 0)
                + (int(seconds.group(1)) if seconds else 0)
            )

            # Apply date and duration filters
            if start_date <= published <= now and total_seconds <= max_duration:
                valid_videos.append(video)

        # Stop pagination if the oldest video is outside DAYS_BACK
        oldest_video = datetime.fromisoformat(
            videos[-1]["published_at"].replace("Z", "+00:00")
        )

        if oldest_video < start_date:
            break

        # Get next page
        cursor = data.get("pagination", {}).get("cursor")

        if not cursor:
            break

    # Sort eligible VODs by views
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
        videos = search_vods()
        print_vods(videos)



