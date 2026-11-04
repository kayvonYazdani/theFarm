"""YouTube delivery for run.py and the explicit manual upload utility.

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
import re
from datetime import datetime,timedelta,timezone

import config as settings
from brain import Publication, atomic_json, read_json, video_identity


def youtube_service():
    """Reuse Farm 2.2's OAuth credentials and token; no automatic login on preview."""
    from googleapiclient.discovery import build
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    token = Path(settings.YOUTUBE_TOKEN_FILE)
    scopes = ['https://www.googleapis.com/auth/youtube.upload',
              'https://www.googleapis.com/auth/youtube.readonly']
    credentials = Credentials.from_authorized_user_file(str(token)) if token.is_file() else None
    # Preserve the current token until a successful consent flow grants processing-status access.
    if credentials and not (credentials.has_scopes(['https://www.googleapis.com/auth/youtube']) or
                            credentials.has_scopes(['https://www.googleapis.com/auth/youtubepartner']) or
                            credentials.has_scopes(scopes)):
        credentials = None
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


def prepare_upload(folder, privacy=None, *, publish_at=None):
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
    if publish_at is not None:
        stamp=parse_publish_at(publish_at)
        if stamp<=datetime.now(timezone.utc):raise ValueError('Publication time has passed; refresh the pending schedule before uploading')
        body['status'].update(privacyStatus='private',publishAt=stamp.isoformat().replace('+00:00','Z'))
    thumbnail = metadata.get('thumbnail_file')
    if not thumbnail:
        candidate = folder / 'final_thumbnail.png'
        thumbnail = str(candidate) if candidate.is_file() else None
    return video, body, Path(thumbnail) if thumbnail else None


def thumbnail_complete(state):
    # Terminal optional delivery outcomes must not re-enter the upload queue.
    return state.get('thumbnail_status') in {'complete', 'skipped'}


def wait_for_video_processing(service,video_id,timeout=120,poll_interval=5):
    """Shorts custom thumbnails must be set after processingStatus=succeeded.

    https://developers.google.com/youtube/v3/docs/thumbnails/set
    No separate Shorts-grid cover/frame-selection API is documented.
    """
    deadline=time.monotonic()+timeout
    while True:
        response=service.videos().list(part='processingDetails',id=video_id,
                    fields='items(id,processingDetails/processingStatus)').execute()
        if not isinstance(response,dict) or not isinstance(response.get('items'),list):
            raise RuntimeError('Unable to verify YouTube processing status')
        items=response['items']
        status=items[0].get('processingDetails',{}).get('processingStatus') if items else None
        if status=='succeeded':return
        if status in {'failed','terminated'}:
            raise RuntimeError('YouTube video processing failed; inspect the existing video in Studio')
        if items and status is None:
            raise RuntimeError('Processing status is unavailable; reconnect the owner account with read permissions')
        if time.monotonic()>=deadline:
            raise TimeoutError('Video is still processing; resume later to apply only the thumbnail')
        time.sleep(min(poll_interval,max(0,deadline-time.monotonic())))


def upload(folder, privacy=None, *, publish_at=None, service=None):
    """Explicit manual action. Record progress and refuse ambiguous duplicate uploads."""
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import ResumableUploadError
    folder = Path(folder)
    video, body, thumbnail = prepare_upload(folder, privacy)
    state_path = folder / 'youtube_upload.json'
    with (folder / '.youtube_upload.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = read_json(state_path) or {}
        digest = video_identity(video)
        if state and state.get('video_sha256') != digest:
            raise ValueError('Video changed after an upload was started; review the existing upload record first')
        # Older versions mislabeled a rejected session-start request as uncertain.
        # googleapiclient raises ResumableUploadError only before sending media.
        if (state.get('status') == 'submitting' and not state.get('video_id')
                and not state.get('progress') and state.get('error_type') == 'ResumableUploadError'):
            state['status'] = 'start_rejected'
            atomic_json(state_path, state)
        if state.get('status') in {'submitting', 'uploading'} and not state.get('video_id'):
            raise RuntimeError('An earlier upload has uncertain status. Check YouTube before creating another upload.')
        if state.get('video_id') and (not thumbnail or thumbnail_complete(state)):
            if state.get('status') != 'complete':
                state['status'] = 'complete'
                atomic_json(state_path, state)
            return state['video_id']
        if not state.get('video_id') and publish_at is not None:
            video,body,thumbnail=prepare_upload(folder,privacy,publish_at=publish_at)
        if not state.get('video_id'):
            service = service or youtube_service()
            state = {'status': 'submitting', 'video_sha256': digest, 'privacy': body['status']['privacyStatus'],
                     'publish_at': body['status'].get('publishAt')}
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
                        state['http_status'] = status
                        try:
                            details = json.loads(error.content).get('error', {})
                            reason = details.get('errors', [{}])[0].get('reason', 'unknown')
                        except (AttributeError, ValueError, TypeError, IndexError, KeyError):
                            reason = 'unknown'
                        state['error_reason'] = reason
                        if isinstance(error, ResumableUploadError):
                            # Session initiation was rejected: no video bytes were sent.
                            state['status'] = 'start_rejected'
                            atomic_json(state_path, state)
                            raise RuntimeError(f'YouTube rejected the upload start (HTTP {status}, {reason}). '
                                               'No video was uploaded by this attempt. Fix the reported issue, '
                                               'then resume to retry safely.') from None
                        atomic_json(state_path, state)
                        raise RuntimeError(f'YouTube upload stopped (HTTP {status}, {reason}); '
                                           'inspect its saved state before retrying') from None
                    time.sleep(min(30, 2 ** failures))
                    failures += 1
            if not response.get('id'):
                raise RuntimeError('YouTube returned no video ID; inspect the account before retrying')
            state.update(status='video_uploaded', video_id=response['id'])
            atomic_json(state_path, state)
        if thumbnail and not thumbnail_complete(state):
            # Persist a total budget across resumes, including interrupted attempts.
            while state.get('thumbnail_attempts', 0) < 3:
                state['thumbnail_attempts'] = state.get('thumbnail_attempts', 0) + 1
                state['status'] = 'complete'  # The video itself is already uploaded.
                atomic_json(state_path, state)
                try:
                    service = service or youtube_service()
                    wait_for_video_processing(service,state['video_id'])
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
                    response=service.thumbnails().set(videoId=state['video_id'], media_body=MediaFileUpload(str(upload_image), mimetype='image/jpeg')).execute()
                    if not isinstance(response,dict) or not response.get('items'):
                        raise RuntimeError('YouTube returned no thumbnail confirmation')
                    state['thumbnail_processing_verified'] = True
                    state.pop('thumbnail_error_type',None)
                    state['thumbnail_status'] = 'complete'
                    break
                except Exception as error:
                    state['thumbnail_error_type'] = type(error).__name__
                    status = getattr(getattr(error, 'resp', None), 'status', 0)
                    recoverable = (status in {429, 500, 502, 503, 504} or
                                   isinstance(error, (TimeoutError, ConnectionError)))
                    if recoverable and state['thumbnail_attempts'] < 3:
                        atomic_json(state_path, state)
                        time.sleep(2 ** (state['thumbnail_attempts'] - 1))
                        continue
                    break
            if state.get('thumbnail_status') != 'complete':
                state['thumbnail_status'] = 'skipped'
                print(f"Warning: video {state['video_id']} is uploaded; optional thumbnail "
                      f"was skipped after {state.get('thumbnail_attempts', 0)} attempt(s) "
                      f"({state.get('thumbnail_error_type', 'retry budget exhausted')}). "
                      f"Image kept at {thumbnail} for manual use in YouTube Studio. "
                      "API acceptance does not verify Shorts grid display.")
        state['status'] = 'complete'
        atomic_json(state_path, state)
        return state['video_id']



def validate_upload_settings():
    from zoneinfo import ZoneInfo
    ZoneInfo(settings.UPLOAD_TIMEZONE)
    if settings.UPLOAD_MODE not in {'immediate','scheduled'}:
        raise ValueError('UPLOAD_MODE must be immediate or scheduled')
    if settings.UPLOAD_IMMEDIATE_PRIVACY not in {'public','unlisted','private'}:
        raise ValueError('Invalid UPLOAD_IMMEDIATE_PRIVACY')
    times=list(settings.UPLOAD_TIMES)
    if not times or len(times)!=len(set(times)):
        raise ValueError('UPLOAD_TIMES must contain unique HH:MM times')
    for value in times:
        if not isinstance(value,str) or not re.fullmatch(r'\d{2}:\d{2}',value):
            raise ValueError('UPLOAD_TIMES must use HH:MM, such as 12:00 and 17:00')
        hour,minute=map(int,value.split(':'))
        if not 0<=hour<=23 or not 0<=minute<=59:raise ValueError('Invalid upload time')
        count=slot_capacity(value)
        if type(count) is not int or count<1:raise ValueError('Videos per upload slot must be a positive integer')
    if times!=sorted(times):raise ValueError('UPLOAD_TIMES must be ordered earliest to latest')


def slot_capacity(value):
    counts=settings.UPLOAD_VIDEOS_PER_SLOT
    if isinstance(counts,dict):
        if set(counts)!=set(settings.UPLOAD_TIMES):raise ValueError('Per-time batch sizes must match UPLOAD_TIMES')
        return counts[value]
    return counts


def parse_publish_at(value):
    parsed=datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value
    if not isinstance(parsed,datetime) or parsed.tzinfo is None:
        raise ValueError('Scheduled publication time must include a timezone')
    return parsed.astimezone(timezone.utc)


def ready_uploads():
    """All completed videos under finished, including earlier production runs."""
    result={}
    for metadata_file in sorted(Path(settings.OUTPUT_DIR).glob('*/metadata.json')):
        folder=metadata_file.parent
        production=read_json(folder/'production.json')
        if production and production.get('status') not in {'complete','video_complete_thumbnail_pending'}:continue
        video,body,thumbnail=prepare_upload(folder)
        digest=video_identity(video)
        item={'digest':digest,'folder':str(folder),'title':body['snippet']['title']}
        receipt=read_json(folder/'youtube_upload.json') or {}
        if receipt and receipt.get('video_sha256')!=digest:
            raise ValueError(f'{folder.name}: final video changed after upload; review its receipt before uploading')
        item['receipt']=receipt
        # If a duplicate file already has an upload receipt, prefer that record.
        if digest not in result or receipt.get('video_id'):
            result[digest]=item
    return list(result.values())


def plan_uploads(items, ledger, *, now=None):
    """Reserve slots persistently before any network request; never schedule in the past."""
    from zoneinfo import ZoneInfo
    validate_upload_settings()
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None:raise ValueError('now must include a timezone')
    zone=ZoneInfo(settings.UPLOAD_TIMEZONE)
    entries=ledger.setdefault('entries',{})
    if not isinstance(entries,dict):raise ValueError('Upload schedule ledger is invalid')
    # Import existing manual-upload receipts before counting any occupied slots.
    for item in items:
        receipt=item.get('receipt',{})
        if receipt.get('video_id'):
            entry=entries.setdefault(item['digest'],{'folder':item['folder']})
            entry.update(video_id=receipt['video_id'],status=receipt.get('status','video_uploaded'))
            if 'publish_at' in receipt:entry['publish_at']=receipt['publish_at']
    counts={}
    for entry in entries.values():
        if entry.get('publish_at'):
            key=parse_publish_at(entry['publish_at']).isoformat()
            counts[key]=counts.get(key,0)+1
    planned=[]
    tomorrow=now.astimezone(zone).date()+timedelta(days=1)
    for item in items:
        digest=item['digest'];receipt=item.get('receipt',{})
        entry=entries.setdefault(digest,{'folder':item['folder'],'status':'pending'})
        if receipt.get('video_id'):
            entry.update(video_id=receipt['video_id'],status=receipt.get('status','video_uploaded'),
                         publish_at=receipt.get('publish_at',entry.get('publish_at')))
        if entry.get('video_id'):
            # Retry a failed/missing thumbnail only through the existing receipt, never insert again.
            if receipt.get('video_id'):
                planned.append({**item,'publish_at':entry.get('publish_at'),'existing':True})
            continue
        old=entry.get('publish_at')
        keep=False
        if old and settings.UPLOAD_MODE=='scheduled':
            stamp=parse_publish_at(old);local=stamp.astimezone(zone);key=stamp.isoformat()
            slot=local.strftime('%H:%M')
            keep=(stamp>now and slot in settings.UPLOAD_TIMES and counts.get(key,0)<=slot_capacity(slot))
        if old and not keep:
            key=parse_publish_at(old).isoformat();counts[key]=max(0,counts.get(key,0)-1)
        publish_at=old if keep else None
        if settings.UPLOAD_MODE=='scheduled' and not keep:
            day=tomorrow
            while publish_at is None:
                for slot in settings.UPLOAD_TIMES:
                    hour,minute=map(int,slot.split(':'))
                    local=datetime(day.year,day.month,day.day,hour,minute,tzinfo=zone)
                    stamp=local.astimezone(timezone.utc);key=stamp.isoformat()
                    # Reject nonexistent local times during a DST jump instead of silently shifting them.
                    if stamp.astimezone(zone).replace(tzinfo=None)!=local.replace(tzinfo=None):continue
                    if stamp>now and counts.get(key,0)<slot_capacity(slot):
                        publish_at=stamp.isoformat();counts[key]=counts.get(key,0)+1;break
                day+=timedelta(days=1)
        entry.update(folder=item['folder'],publish_at=publish_at,mode=settings.UPLOAD_MODE,status='pending')
        planned.append({**item,'publish_at':publish_at,'existing':False})
    return planned


def upload_all(*, progress=None, now=None, service=None):
    """Deliver all finished clips once. Called only when explicitly enabled by run.py."""
    validate_upload_settings()
    path=Path(settings.UPLOAD_SCHEDULE_FILE)
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        ledger=read_json(path) if path.exists() else {'version':1,'entries':{}}
        if not isinstance(ledger,dict) or ledger.get('version')!=1:
            raise ValueError('Upload schedule is unreadable; restore it before retrying')
        items=ready_uploads()
        plan=plan_uploads(items,ledger,now=now)
        atomic_json(path,ledger)
        pending=[]
        for item in plan:
            folder=Path(item['folder'])
            receipt=read_json(folder/'youtube_upload.json') or {}
            _,_,thumb=prepare_upload(folder)
            if receipt.get('video_id') and (not thumb or thumbnail_complete(receipt)):
                ledger['entries'][item['digest']].update(status='complete',video_id=receipt['video_id'])
            else:pending.append(item)
        atomic_json(path,ledger)
        completed=0
        for index,item in enumerate(pending):
            entry=ledger['entries'][item['digest']]
            folder=Path(item['folder'])
            receipt=read_json(folder/'youtube_upload.json') or {}
            _,_,thumb=prepare_upload(folder)
            if receipt.get('video_id') and (not thumb or thumbnail_complete(receipt)):
                entry.update(status='complete',video_id=receipt['video_id'])
                atomic_json(path,ledger)
                continue
            if progress:progress(index+1,len(pending),folder.name,item['publish_at'])
            if service is None and not receipt.get('video_id'):service=youtube_service()
            try:
                video_id=upload(folder,privacy=settings.UPLOAD_IMMEDIATE_PRIVACY,
                                publish_at=item['publish_at'],service=service)
            except BaseException:
                # The per-video receipt owns ambiguous-upload protection and thumbnail recovery.
                saved=read_json(folder/'youtube_upload.json') or {}
                entry.update(status=saved.get('status','pending'))
                if saved.get('video_id'):entry['video_id']=saved['video_id']
                atomic_json(path,ledger)
                raise
            entry.update(status='complete',video_id=video_id)
            atomic_json(path,ledger);completed+=1
        return completed

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
