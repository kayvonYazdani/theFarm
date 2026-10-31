"""Manual YouTube uploader, adapted from Farm 2.2. Never called by run.py.

Preview: python post_to_youtube.py finished/CLIP_FOLDER
Upload:  python post_to_youtube.py finished/CLIP_FOLDER --upload
"""
from __future__ import annotations
import argparse
import fcntl
import json
import os
from pathlib import Path
import time

import config as settings
from brain import Publication, atomic_json, read_json, video_identity


def youtube_service():
    """Reuse Farm 2.2's OAuth credentials and token; no automatic login on preview."""
    from googleapiclient.discovery import build
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    token = Path(settings.YOUTUBE_TOKEN_FILE)
    scopes = ['https://www.googleapis.com/auth/youtube.upload']
    credentials = Credentials.from_authorized_user_file(str(token), scopes) if token.is_file() else None
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials or not credentials.valid:
        candidates = ([Path(settings.TOKEN_RESOURCE_DIR) / settings.YOUTUBE_CLIENT_FILE]
                      if settings.YOUTUBE_CLIENT_FILE else sorted(Path(settings.TOKEN_RESOURCE_DIR).glob('client_secret*.json')))
        if len(candidates) != 1 or not candidates[0].is_file():
            raise FileNotFoundError('Set YOUTUBE_CLIENT_FILE or keep one OAuth client_secret file in tokens')
        flow = InstalledAppFlow.from_client_secrets_file(str(candidates[0]), scopes)
        credentials = flow.run_local_server(port=0, prompt='consent', timeout_seconds=300)
    # Existing account token is shared with Farm 2.2; write securely only when explicitly uploading.
    fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(credentials.to_json())
    return build('youtube', 'v3', credentials=credentials, cache_discovery=False)


def prepare_upload(folder, privacy=None):
    folder = Path(folder).resolve()
    metadata = read_json(folder / 'metadata.json')
    if not metadata:
        raise FileNotFoundError('Completed clip metadata.json is required')
    publication = Publication.model_validate({k: metadata[k] for k in ('title', 'description', 'hashtags')})
    video = Path(metadata['video_file'])
    if not video.is_file():
        raise FileNotFoundError('Finished video is missing')
    privacy = privacy or settings.YOUTUBE_PRIVACY
    if privacy not in {'private', 'unlisted', 'public'}:
        raise ValueError('YouTube privacy must be private, unlisted, or public')
    description = publication.description + '\n\n' + ' '.join(publication.hashtags)
    description = description.replace('<', '').replace('>', '')
    description = description.encode('utf-8')[:4900].decode('utf-8', errors='ignore')
    title = publication.title.replace('<', '').replace('>', '').strip()
    if not title:
        raise ValueError('YouTube title is empty after removing unsupported characters')
    body = {'snippet': {'title': title, 'description': description, 'categoryId': settings.YOUTUBE_CATEGORY_ID},
            'status': {'privacyStatus': privacy, 'selfDeclaredMadeForKids': settings.YOUTUBE_MADE_FOR_KIDS}}
    thumbnail = metadata.get('thumbnail_file')
    if not thumbnail:
        candidate = folder / 'final_thumbnail.png'
        thumbnail = str(candidate) if candidate.is_file() else None
    return video, body, Path(thumbnail) if thumbnail else None


def upload(folder, privacy=None, *, service=None):
    """Explicit manual action. Record progress and refuse ambiguous duplicate uploads."""
    from googleapiclient.http import MediaFileUpload
    folder = Path(folder)
    video, body, thumbnail = prepare_upload(folder, privacy)
    state_path = folder / 'youtube_upload.json'
    with (folder / '.youtube_upload.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = read_json(state_path) or {}
        digest = video_identity(video)
        if state and state.get('video_sha256') != digest:
            raise ValueError('Video changed after an upload was started; review the existing upload record first')
        if state.get('status') in {'submitting', 'uploading'} and not state.get('video_id'):
            raise RuntimeError('An earlier upload has uncertain status. Check YouTube before creating another upload.')
        service = service or youtube_service()
        if not state.get('video_id'):
            state = {'status': 'submitting', 'video_sha256': digest, 'privacy': body['status']['privacyStatus']}
            atomic_json(state_path, state)
            request = service.videos().insert(part='snippet,status', body=body,
                                              media_body=MediaFileUpload(str(video), mimetype='video/mp4', resumable=True))
            response = None
            failures = 0
            while response is None:
                try:
                    progress, response = request.next_chunk()
                    if progress:
                        state.update(status='uploading', progress=progress.progress())
                        atomic_json(state_path, state)
                    failures = 0
                except Exception as error:
                    status = getattr(getattr(error, 'resp', None), 'status', 0)
                    if status not in {429, 500, 502, 503, 504} or failures >= 3:
                        # Do not reset this state: the server may have received the video.
                        state['error_type'] = type(error).__name__
                        atomic_json(state_path, state)
                        raise RuntimeError('YouTube upload stopped; inspect its saved state before retrying') from None
                    time.sleep(min(30, 2 ** failures))
                    failures += 1
            if not response.get('id'):
                raise RuntimeError('YouTube returned no video ID; inspect the account before retrying')
            state.update(status='video_uploaded', video_id=response['id'])
            atomic_json(state_path, state)
        if thumbnail and state.get('thumbnail_status') != 'complete':
            try:
                from PIL import Image
                upload_image = folder / 'youtube_thumbnail.jpg'
                with Image.open(thumbnail) as image:
                    image = image.convert('RGB')
                    for quality in (90, 80, 65, 50):
                        image.save(upload_image, 'JPEG', quality=quality, optimize=True)
                        if upload_image.stat().st_size < 2 * 1024 * 1024:
                            break
                    else:
                        raise ValueError('Thumbnail exceeds upload size limit')
                service.thumbnails().set(videoId=state['video_id'], media_body=MediaFileUpload(str(upload_image), mimetype='image/jpeg')).execute()
                state['thumbnail_status'] = 'complete'
            except Exception as error:
                state['thumbnail_status'] = 'failed'
                state['thumbnail_error_type'] = type(error).__name__
                atomic_json(state_path, state)
                raise RuntimeError('Video uploaded; thumbnail failed. Retry this command to upload only the thumbnail.') from None
        state['status'] = 'complete'
        atomic_json(state_path, state)
        return state['video_id']


def main():
    parser = argparse.ArgumentParser(description='Preview a YouTube upload; uploading requires --upload')
    parser.add_argument('folder', type=Path)
    parser.add_argument('--upload', action='store_true')
    parser.add_argument('--privacy', choices=['private', 'unlisted', 'public'])
    args = parser.parse_args()
    if not args.upload:
        video, body, thumbnail = prepare_upload(args.folder, args.privacy)
        print(json.dumps({'video': str(video), 'request': body, 'thumbnail': str(thumbnail) if thumbnail else None,
                          'action': 'preview only; nothing uploaded'}, indent=2))
    else:
        print('YouTube video ID:', upload(args.folder, args.privacy))


if __name__ == '__main__':
    main()
