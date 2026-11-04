"""The Farm operator: processing and optional, checkpointed YouTube delivery."""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import ast
from functools import lru_cache
from pathlib import Path
import shutil
import sys

import config as settings
from brain import Brain, BrainError, EditPlan, Publication, atomic_json, fingerprint, normalize_transcript, read_json, video_identity
from editor import VideoEditor, CaptionEditor, apply_edit_plan


@lru_cache(maxsize=16)
def _implementation(source,names):
    tree=ast.parse(source)
    return fingerprint([ast.dump(node,include_attributes=False) for node in tree.body
                        if isinstance(node,(ast.ClassDef,ast.FunctionDef)) and node.name in names])


def implementation_key(*names):
    return _implementation(Path(__file__).with_name('editor.py').read_text(),names)


def setting_values(prefixes):
    return {name: str(value) if isinstance(value, Path) else value
            for name, value in vars(settings).items() if any(name.startswith(p) for p in prefixes)}


def render_pass(name, destination, key, state, state_path, render):
    """Commit only successfully probed videos; resume completed matching passes."""
    destination = Path(destination)
    saved = state.setdefault('passes', {}).get(name, {})
    if destination.is_file() and saved.get('key') == key:
        try:
            VideoEditor(destination)  # FFprobe only; no encoding.
            print(f'  {name}: cached', flush=True)
            return destination
        except Exception:
            pass
    temporary = destination.with_suffix('.partial.mp4')
    print(f'  {name}: rendering', flush=True)
    result = Path(render(temporary))
    if result != temporary:
        shutil.copyfile(result, temporary)
    VideoEditor(temporary)
    temporary.replace(destination)
    state['passes'][name] = {'key': key, 'file': str(destination)}
    atomic_json(state_path, state)
    return destination


def process_clip(source, brain, *, no_thumbnail=False, on_stage=None):
    source = Path(source).resolve()
    digest = video_identity(source)
    safe_name = ''.join(c if c.isalnum() or c in '-_' else '_' for c in source.stem)[:70]
    folder = Path(settings.OUTPUT_DIR) / f'{safe_name}_{digest[:12]}'
    folder.mkdir(parents=True, exist_ok=True)
    state_path = folder / 'production.json'
    state = read_json(state_path) or {}
    if not isinstance(state, dict):
        state = {}
    state['source'] = str(source)
    state['source_sha256'] = digest
    def advance(stage,phase):
        state.update(status='running',pending_stage=stage)
        atomic_json(state_path,state)
        if on_stage: on_stage(stage,phase,state,folder)
    def finish(stage):
        completed=state.setdefault('completed_stages',[])
        if stage not in completed: completed.append(stage)
        state['models']=dict(getattr(brain,'models_used',{}))
    try:
        advance('Transcription',2)
        editor = VideoEditor(source)
        if editor.info['duration'] > settings.MAX_CLIP_DURATION + 0.001:
            raise ValueError('Candidate exceeds MAX_CLIP_DURATION. Enable USE_CLIPPER for long videos.')
        captions = CaptionEditor()
        prefix = f'{digest[:24]}_'
        aligned = Path(settings.CACHE_FOLDER) / f'{prefix}aligned_words.json'
        transcript_path = getattr(settings, 'INPUT_TRANSCRIPT', None)
        if (not aligned.exists() and not settings.USE_CLIPPER and transcript_path and
                source == Path(settings.INPUT_VIDEO).resolve() and Path(transcript_path).is_file()):
            # Explicitly configured candidate transcript, validated before reuse.
            words = normalize_transcript(Path(transcript_path), editor.info['duration'],preserve_metadata=True)
            atomic_json(aligned, words)
        print(f'Processing {source.name}', flush=True)
        captions.prepare(source, Path(settings.CACHE_FOLDER), prefix=prefix,duration=editor.info['duration'])
        captions.words = normalize_transcript(captions.words,editor.info['duration'],preserve_metadata=True)
        words = normalize_transcript(captions.words, editor.info['duration'])
        finish('transcription')
        thumbnail_plan = None
        if settings.AI_ENABLED:
            print('  AI: judge', flush=True)
            advance('Gemini Clip Judge',2)
            analysis = brain.judge_clip(source, words, duration=editor.info['duration'])
            finish('judge')
            atomic_json(folder / 'judgment.json', analysis.model_dump())
            if analysis.rating == 'stinker':
                state['accepted'] = False
                state['status'] = 'rejected'
                state['reason'] = analysis.explanation
                atomic_json(state_path, state)
                print('  Skipped: stinker', flush=True)
                return folder
            state['accepted']=True
            advance('Gemini Creative Editor',2)
            plan = brain.plan_edits(analysis)
            finish('edit_plan')
            advance('Gemini Publishing Writer',2)
            publication = brain.write_publication(analysis)
            finish('publishing')
            advance('Gemini Thumbnail Director',2)
            thumbnail_plan = brain.plan_thumbnail(analysis)
            finish('thumbnail_plan')
            atomic_json(folder / 'thumbnail_plan.json', thumbnail_plan.model_dump())
            state['models'] = dict(brain.models_used)
        else:
            plan = EditPlan(rationale='Manual configuration; AI disabled.', zooms=[], effects=[], background_music=None)
            publication = Publication(title=settings.MANUAL_TITLE or source.stem[:100],
                                      description=settings.MANUAL_DESCRIPTION or 'Manually edited video.',
                                      hashtags=settings.MANUAL_HASHTAGS)
            state['mode'] = 'manual'
            state['accepted'] = True
        atomic_json(folder / 'edit_plan.json', plan.model_dump())
        atomic_json(folder / 'publishing.json', publication.model_dump())
        (folder / 'title.txt').write_text(publication.title)
        (folder / 'description.txt').write_text(publication.description)
        (folder / 'hashtags.txt').write_text(' '.join(publication.hashtags))
        asset_key = fingerprint(plan.model_dump())
        assets = state.get('selected_assets', {}) if state.get('asset_plan_key') == asset_key else {}
        state['selected_assets'] = apply_edit_plan(editor, plan, selected_assets=assets)
        state['asset_plan_key'] = asset_key
        atomic_json(state_path, state)
        title_path = captions.prepare_title(publication.title, folder, editor.info) if settings.TITLE_ENABLED else None
        code_hash = implementation_key('ZoomEditor','VerticalFormatter','FilterEditor','VideoEditor')
        base_settings = {name: getattr(settings, name) for name in (
            'EDITOR_FPS', 'EDITOR_BACKGROUND_BLUR', 'EDITOR_FOREGROUND_ZOOM', 'EDITOR_BACKGROUND_ZOOM',
            'EDITOR_SATURATION', 'EDITOR_CONTRAST', 'EDITOR_BRIGHTNESS')}
        base_key = fingerprint({'source': digest, 'code': code_hash, 'zooms': [z.model_dump() for z in plan.zooms], 'settings': base_settings})
        advance('Foundation Render',3)
        base = render_pass('foundation', Path(settings.CACHE_FOLDER) / f'{prefix}base.mp4', base_key, state, state_path,
                           lambda out: editor.render(out))
        caption_key = fingerprint({'implementation':implementation_key('CaptionEditor'),'base': base_key, 'words': words, 'settings': setting_values(('EDITOR_CAPTION', 'EDITOR_EMOJI', 'EDITOR_FONT', 'TITLE_')),
                                   'title': video_identity(title_path) if title_path else None})
        advance('Caption/Title Render',3)
        captioned = render_pass('captions', Path(settings.CACHE_FOLDER) / f'{prefix}captioned.mp4', caption_key, state, state_path,
                                lambda out: captions.render_captions(base, out, editor.info, settings.CACHE_FOLDER, title_path=title_path))
        final_key = fingerprint({'implementation':implementation_key('EffectsEditor'),'captions': caption_key, 'effects': [e.model_dump() for e in plan.effects],
                                 'music': plan.background_music, 'shake': setting_values(('EDITOR_SHAKE',)),
                                 'intro': {'settings': setting_values(('EDITOR_INTRO',)),
                                           'sha256': video_identity(Path(settings.EDITOR_INTRO_SOUND))},
                                 'assets': {key: video_identity(Path(path)) for key, path in assets.items()}})
        advance('Effects/Audio Render',3)
        final = render_pass('effects', folder / 'final.mp4', final_key, state, state_path,
                            lambda out: editor.effects.render(captioned, out))
        metadata = {**publication.model_dump(), 'video_file': str(final), 'source_video': str(source),
                    'source_sha256': digest, 'thumbnail_file': None}
        if thumbnail_plan is not None and settings.GENERATE_THUMBNAIL and not no_thumbnail:
            try:
                from thumbnail import generate_thumbnail
                advance('Thumbnail Image',4)
                print('  Thumbnail: ComfyUI', flush=True)
                target = generate_thumbnail(thumbnail_plan, folder)
                metadata['thumbnail_file'] = str(target)
                state['thumbnail_status'] = 'complete'
                state.pop('thumbnail_error_type',None)
            except Exception as error:
                state['thumbnail_status'] = 'pending'
                state['thumbnail_error_type'] = type(error).__name__
                print(f'  Thumbnail pending ({type(error).__name__}); retry with thumbnail.py after checking ComfyUI.', flush=True)
        else:
            state['thumbnail_status'] = 'disabled' if not settings.GENERATE_THUMBNAIL or no_thumbnail else 'no_ai_plan'
        atomic_json(folder / 'metadata.json', metadata)
        state['status'] = 'complete' if state['thumbnail_status'] != 'pending' else 'video_complete_thumbnail_pending'
        state.pop('error_type', None)
        if state['status']=='complete': state.pop('pending_stage',None)
        atomic_json(state_path, state)
        print(f'  Finished: {final}', flush=True)
        return folder
    except BaseException as error:
        state['status'] = 'paused'
        state['error_type'] = type(error).__name__
        atomic_json(state_path, state)
        raise


def validate_settings():
    import math
    if type(settings.VIDEOHIGHLIGHTER_MAX_CANDIDATES) is not int or not 1 <= settings.VIDEOHIGHLIGHTER_MAX_CANDIDATES <= 10:
        raise ValueError('VIDEOHIGHLIGHTER_MAX_CANDIDATES must be 1–10')
    if not 0 < settings.MAX_CLIP_DURATION <= 60:
        raise ValueError('MAX_CLIP_DURATION must be greater than 0 and at most 60 seconds')
    for name in ('VIDEOHIGHLIGHTER_MIN_SCORE','VIDEOHIGHLIGHTER_MIN_PROMINENCE','GEMINI_TIMEOUT_SECONDS','VIDEOHIGHLIGHTER_TIMEOUT','DOWNLOAD_TIMEOUT'):
        value=getattr(settings,name)
        if not math.isfinite(value) or value <= 0: raise ValueError(f'{name} must be finite and positive')
    for name in ('HIGHLIGHT_CONTEXT_BEFORE','HIGHLIGHT_CONTEXT_AFTER','TEMP_RETENTION_HOURS'):
        value=getattr(settings,name)
        if not math.isfinite(value) or value < 0: raise ValueError(f'{name} must be finite and nonnegative')
    for name in ('GEMINI_MAX_RETRIES','AI_VALIDATION_RETRIES'):
        value=getattr(settings,name)
        if type(value) is not int or not 0 <= value <= 8: raise ValueError(f'{name} must be an integer from 0 to 8')
    for name in ('FFMPEG_THREADS','WHISPER_CPU_THREADS'):
        value=getattr(settings,name)
        if type(value) is not int or not 1 <= value <= 16: raise ValueError(f'{name} must be an integer from 1 to 16')
    if type(settings.VIDEOS_TO_PROCESS) is not int or settings.VIDEOS_TO_PROCESS <= 0:
        raise ValueError('VIDEOS_TO_PROCESS must be a positive integer')
    for key,value in settings.VIDEOHIGHLIGHTER_OPTIONS.items():
        if key.endswith('_points') and (not isinstance(value,(int,float)) or not math.isfinite(value) or value < 0):
            raise ValueError(f'Invalid highlight weight: {key}')
    if settings.SOURCE_MODE not in {'folder','discover'}: raise ValueError('SOURCE_MODE must be folder or discover')
    for name in ('ffmpeg','ffprobe'):
        if not shutil.which(name): raise FileNotFoundError(f'{name} is missing from PATH')
    if settings.UPLOAD_ENABLED:
        from post_to_youtube import validate_upload_settings
        validate_upload_settings()
    if settings.AI_ENABLED and not str(settings.GEMINI_PRIMARY_MODEL).strip(): raise ValueError('Set GEMINI_PRIMARY_MODEL')
    for path in (Path(settings.EDITOR_FONT),Path(settings.EDITOR_WHISPERX_PYTHON),Path(settings.EDITOR_INTRO_SOUND)):
        if not path.is_file(): raise FileNotFoundError(f'Required production dependency missing: {path}')
    if settings.TITLE_ENABLED and not Path(settings.TITLE_HEADER).is_file(): raise FileNotFoundError('TITLE_HEADER asset is missing')
    if settings.USE_CLIPPER:
        for path in (Path(settings.VIDEOHIGHLIGHTER_PYTHON),Path(settings.VIDEOHIGHLIGHTER_DIR)/'pipeline.py'):
            if not path.is_file(): raise FileNotFoundError(f'VideoHighlighter dependency missing: {path}')


class SessionLog:
    """Normal technical log, with known credentials redacted before writing."""
    def __init__(self,stream):
        import os
        from dotenv import dotenv_values
        secrets={**dotenv_values(settings.PROJECT_DIR.parent/'.env'),**dotenv_values(settings.PROJECT_DIR/'.env'),**os.environ}
        self.secrets=[str(v) for k,v in secrets.items() if v and any(token in k.upper() for token in ('KEY','TOKEN','SECRET','PASSWORD')) and len(str(v))>=8]
        self.stream=stream
    def write(self,text):
        import re
        for secret in self.secrets: text=text.replace(secret,'[REDACTED]')
        text=re.sub(r'AIza[\w-]+','[REDACTED]',text)
        return self.stream.write(text)
    def flush(self): self.stream.flush()
    def fileno(self): return self.stream.fileno()


def job_counts(job):
    clips=[clip for source in job.get('sources',[]) for clip in source.get('candidates',[])]
    return {'Selected':len(clips),'Accepted':sum(bool(c.get('accepted')) for c in clips),
            'Rejected':sum(c.get('status')=='rejected' for c in clips),
            'Edited':sum(bool(c.get('edited')) for c in clips),
            'Completed':sum(c.get('status')=='complete' for c in clips)}


def cleanup_temporary():
    """Only recognized uncommitted render files are disposable; never clear caches."""
    import re,time
    cutoff=time.time()-settings.TEMP_RETENTION_HOURS*3600
    removed=0
    candidates=[]
    for folder,pattern in ((settings.CACHE_FOLDER,r'[0-9a-f]{24}_(base|captioned)\.partial\.mp4'),
                           (settings.CLIPS_FOLDER,r'[0-9a-f]{24}_[0-9a-f]{12}_\d+\.partial\.mp4')):
        if Path(folder).is_dir():
            candidates.extend(p for p in Path(folder).iterdir() if p.is_file() and re.fullmatch(pattern,p.name))
    for state in Path(settings.OUTPUT_DIR).glob('*/production.json'):
        partial=state.parent/'final.partial.mp4'
        if isinstance(read_json(state),dict) and partial.is_file(): candidates.append(partial)
    for path in candidates:
        if path.stat().st_mtime < cutoff:
            path.unlink();removed+=1
    return removed


def file_stamp(path):
    stat=Path(path).stat()
    return [stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,stat.st_ino]


def cleanup_successful_job(job,save):
    """Idempotent cleanup of completed-run working files, never finished deliverables."""
    if not settings.CLEANUP_AFTER_SUCCESS:return
    if job.get('status') not in {'finishing','complete'}:return
    if any(source.get('status')!='complete' for source in job.get('sources',[])):return
    sources={source['digest']:source for source in job.get('sources',[])}
    for digest,record in job.get('history',{}).items():
        sources.setdefault(digest,{'digest':digest,'path':record['path'],'status':'complete','candidates':record.get('clips',[])})
    cache=Path(settings.CACHE_FOLDER).resolve()
    videos=Path(settings.VIDS_FOLDER).resolve()
    clips=Path(settings.CLIPS_FOLDER).resolve()
    outputs=Path(settings.OUTPUT_DIR).resolve()
    def managed(path,folder):
        return path.resolve().is_relative_to(folder) and not path.is_symlink()
    state=job.setdefault('cleanup',{})
    if state.get('status')=='complete':return
    if 'files' not in state:
        paths=set();prefixes=set();source_paths=set()
        for digest,source in sources.items():
            prefixes.add(digest[:24]);path=Path(source['path'])
            source_paths.add(str(path.resolve()))
            if path.is_file() and managed(path,videos):paths.add(path)
            for candidate in source.get('candidates',[]):
                if candidate.get('status') not in {'complete','rejected'}:
                    raise ValueError('Cleanup refused: a selected clip is unfinished')
                folder=Path(candidate.get('output',''))
                if candidate.get('status')=='complete':
                    if not managed(folder,outputs) or not (folder/'final.mp4').is_file():
                        raise ValueError('Cleanup refused: a completed video is missing; inputs have been retained')
                    VideoEditor(folder/'final.mp4')
                clip_state=read_json(folder/'production.json') if managed(folder,outputs) else None
                if isinstance(clip_state,dict) and clip_state.get('source_sha256'):
                    prefixes.add(clip_state['source_sha256'][:24])
                raw=Path(candidate['path'])
                if raw.is_file() and managed(raw,clips):paths.add(raw)
                partial=raw.with_suffix('.partial.mp4')
                if partial.is_file() and managed(partial,clips):paths.add(partial)
        emoji=cache/'emoji_events.json'
        if emoji.is_file():paths.add(emoji)
        for prefix in prefixes:
            for pattern in (prefix+'_*','ai_'+prefix+'_*'):
                paths.update(path for path in cache.glob(pattern) if path.is_file() and managed(path,cache))
        # Native VH caches identify their source explicitly; keep unrelated cached analyses.
        for path in cache.glob('*.cache.json'):
            payload=read_json(path)
            if isinstance(payload,dict) and str(Path(payload.get('video_path','')).resolve()) in source_paths:
                paths.add(path)
                native_hash=payload.get('video_hash','')
                if len(native_hash)==64 and all(c in '0123456789abcdef' for c in native_hash):
                    folder=cache/'highlights'/native_hash
                    paths.update(p for p in folder.rglob('*') if p.is_file() and managed(p,cache))
        state.update(status='pending',files=sorted(str(path.resolve()) for path in paths));save()
    for name in state['files']:
        path=Path(name)
        if path.name=='.production.lock' or path.resolve().is_relative_to(outputs) or not any(managed(path,folder) for folder in (cache,clips,videos)):
            raise ValueError('Cleanup refused an unmanaged path')
        path.unlink(missing_ok=True)
    state.update(status='complete',removed_files=len(state.pop('files')));save()


def source_jobs(job, args, save, capture):
    """Checkpoint discovery/downloads first; never rediscover successful downloads on resume."""
    if job.get('sources_ready'): return
    job['stage']='Finding Videos';save()
    if job.get('source_paths') is not None:
        paths=[Path(p) for p in job['source_paths']]
    elif job.get('input') or args.input or settings.INPUT_VIDEO:
        paths=[Path(job.get('input') or args.input or settings.INPUT_VIDEO).resolve()]
    elif job.get('source_mode',settings.SOURCE_MODE)=='folder':
        paths=sorted(p.resolve() for p in Path(settings.VIDS_FOLDER).glob('*')
                     if p.is_file() and p.suffix.lower() in {'.mp4','.mkv','.webm','.mov'})
    else:
        from downloader import get_video_candidates,get_processed_ids,download_video
        if not job.get('discovery_complete'):
            with capture(): platform,candidates=get_video_candidates()
            posted=get_processed_ids(platform)
            # Successful cleanup removes local VODs; history must prevent downloading them again.
            if not args.reprocess:
                for record in job.get('history',{}).values():
                    stem=Path(record['path']).stem
                    if stem.startswith(platform+'_'):posted.add(stem[len(platform)+1:])
            unique={str(v['id']):v for v in candidates if str(v['id']) not in posted}
            job['downloads']=[{'platform':platform,'video':v,'status':'pending'} for v in list(unique.values())[:settings.VIDEOS_TO_PROCESS]]
            job['discovery_complete']=True;save()
        paths=[]
        for download in job.get('downloads',[]):
            job['current_source']=download['video']['url'];save()
            if download.get('path') and Path(download['path']).is_file():
                path=Path(download['path'])
            else:
                with capture(): path=download_video(download['platform'],download['video'])
                if not path: raise RuntimeError('Download failed. Check the source URL, connection and farm.log, then resume.')
                download.update(path=str(path),status='complete');save()
            paths.append(path)
    job['source_paths']=[str(path) for path in paths];save()
    known={source['digest'] for source in job.get('sources',[])}
    existing_paths={source['path'] for source in job.get('sources',[])}
    history=job.get('history',{})
    for path in paths:
        if len(known)>=settings.VIDEOS_TO_PROCESS: break
        if str(path) in existing_paths: continue
        if not path.is_file(): raise FileNotFoundError(f'Input video missing: {path}')
        stamp=file_stamp(path)
        previous=next((digest for digest,record in history.items()
                       if record.get('path')==str(path) and record.get('stamp')==stamp),None)
        if previous and not args.reprocess: continue
        VideoEditor(path)  # Reject corrupted downloads before binding them to a resumable job.
        digest=previous or video_identity(path)
        if digest in known or (digest in history and not args.reprocess): continue
        known.add(digest)
        job.setdefault('sources',[]).append({'path':str(path),'digest':digest,'status':'pending'})
        if len(known)>=settings.VIDEOS_TO_PROCESS: break
    job['sources_ready']=True;save()


def execute_job(job,args,save,ui,capture,brain):
    source_jobs(job,args,save,capture)
    for source in job.get('sources',[]):
        if source.get('status')=='complete': continue
        path=Path(source['path'])
        job['current_source']=path.name;job['stage']='VideoHighlighter';save()
        if video_identity(path)!=source['digest']: raise ValueError('A pending source changed on disk; restore the original file before resuming')
        if 'candidates' not in source:
            ui.show(source=path.name,phase=1,counts=job_counts(job))
            if job['use_clipper']:
                from clipper import run_clipper
                with capture(): selected=run_clipper(path,source['digest'][:24])
                source['candidates']=[{'path':str(c['raw_clip_file']),'status':'pending'} for c in selected]
            else: source['candidates']=[{'path':str(path),'status':'pending'}]
            save()
            if not source['candidates']:
                ui.show(source=path.name,phase=1,status='COMPLETE',counts=job_counts(job),detail='No qualifying highlights. Moving on.')
        for clip in source['candidates']:
            if clip.get('status') in {'complete','rejected'}: continue
            def event(stage,phase,state,folder):
                job['stage']=stage;job['current_source']=path.name
                clip.update(stage=stage,output=str(folder),accepted=bool(state.get('accepted')),
                            completed_stages=list(state.get('completed_stages',[]))+list(state.get('passes',{})))
                save();ui.show(source=path.name,phase=phase,counts=job_counts(job),detail=stage+' — '+Path(clip['path']).name)
            with capture(): folder=process_clip(clip['path'],brain,no_thumbnail=args.no_thumbnail,on_stage=event)
            state=read_json(folder/'production.json') or {}
            clip.update(output=str(folder),accepted=bool(state.get('accepted')),edited=bool(state.get('passes',{}).get('effects')),
                        completed_stages=list(state.get('completed_stages',[]))+list(state.get('passes',{})))
            if state.get('status')=='video_complete_thumbnail_pending':
                clip['status']='pending';save()
                raise RuntimeError('Video is saved; thumbnail generation needs attention. Start/check ComfyUI and resume, or use --no-thumbnail.')
            clip['status']='rejected' if state.get('status')=='rejected' else 'complete';save()
        source['status']='complete'
        job.setdefault('history',{})[source['digest']]={'path':str(path),'clips':source['candidates'],'stamp':file_stamp(path)}
        save()
    if settings.UPLOAD_ENABLED:
        deliver_uploads(job,save,ui,capture)
    job.update(status='finishing',stage='Final Output');save()
    cleanup_successful_job(job,save)
    job.update(status='complete');save()
    ui.show(phase=5,status='COMPLETE',counts=job_counts(job),
            detail=('Finished. YouTube delivery complete.' if settings.UPLOAD_ENABLED else 'Finished. Uploading disabled.') if job.get('sources') else
            ('Finished checking completed videos for YouTube delivery.' if settings.UPLOAD_ENABLED else 'No new source videos. Add videos to media/videos or configure discovery.'))


def deliver_uploads(job,save,ui,capture):
    if not settings.UPLOAD_ENABLED:
        raise ValueError('YouTube delivery is disabled. Enable UPLOAD_ENABLED to resume this upload queue.')
    from post_to_youtube import upload_all
    job['stage']='YouTube Uploads';save()
    def progress(index,total,folder,publish_at):
        detail=f'Uploading {index}/{total}: {folder}'
        if publish_at:
            from post_to_youtube import parse_publish_at
            from zoneinfo import ZoneInfo
            local=parse_publish_at(publish_at).astimezone(ZoneInfo(settings.UPLOAD_TIMEZONE))
            detail+=' — publishes '+local.strftime('%Y-%m-%d %I:%M %p %Z')
        else: detail+=' — immediate'
        ui.show(phase=4,status='RUNNING',counts=job_counts(job),detail=detail)
    with capture():job['uploaded_this_run']=upload_all(progress=progress)
    save()


def main():
    import traceback
    import importlib
    from console import Terminal
    parser=argparse.ArgumentParser(description='THE FARM — production operator; resumes saved work automatically')
    parser.add_argument('--upload-only',action='store_true',help='Only deliver all completed videos; requires UPLOAD_ENABLED=True')
    parser.add_argument('--input',type=Path,help='Process a local file instead of configured sources')
    parser.add_argument('--resume',action='store_true',help='Resume the saved run (also automatic on normal launch)')
    parser.add_argument('--reprocess',action='store_true',help='Allow previously completed sources, reusing valid AI/render caches')
    parser.add_argument('--no-thumbnail',action='store_true',help='Save thumbnail plans without generating images')
    args=parser.parse_args()
    if args.upload_only and not settings.UPLOAD_ENABLED:
        print('Set UPLOAD_ENABLED=True in config.py before using --upload-only.');return 2
    if args.upload_only and args.input:
        parser.error('--upload-only cannot be combined with --input')
    try:
        if args.upload_only:
            from post_to_youtube import validate_upload_settings
            validate_upload_settings()
        else:validate_settings()
    except (ValueError,TypeError,AttributeError,FileNotFoundError) as error:
        print('THE FARM — CONFIGURATION NEEDS ATTENTION\n'+str(error));return 2
    settings.CACHE_FOLDER.mkdir(parents=True,exist_ok=True)
    settings.DATABASE_FOLDER.mkdir(parents=True,exist_ok=True)
    checkpoint=Path(settings.CHECKPOINT_FILE)
    if checkpoint.exists():
        job=read_json(checkpoint)
        if (not isinstance(job,dict) or job.get('version')!=1 or not isinstance(job.get('sources'),list)
                or not isinstance(job.get('history',{}),dict)
                or any(not isinstance(source,dict) or not isinstance(source.get('path'),str)
                       or not isinstance(source.get('digest'),str)
                       or not isinstance(source.get('candidates',[]),list)
                       or any(not isinstance(clip,dict) or not isinstance(clip.get('path'),str) for clip in source.get('candidates',[]))
                       for source in job.get('sources',[]))):
            print('Checkpoint is unreadable or unsupported. Restore its backup before proceeding; it has not been overwritten.');return 2
    else: job=None
    if args.resume and (not job or job.get('status')=='complete'):
        print('No unfinished production run to resume. Start with python run.py.');return 0
    if not job or job.get('status')=='complete':
        job={'version':1,'status':'running','sources':[],'history':job.get('history',{}) if job else {},'use_clipper':settings.USE_CLIPPER,
             'input':str(Path(args.input or settings.INPUT_VIDEO).resolve()) if args.input or settings.INPUT_VIDEO else None,
             'source_mode':settings.SOURCE_MODE,'upload_only':args.upload_only}
    elif args.upload_only and not job.get('upload_only'):
        print('A production run is unfinished. Resume it first; --upload-only will not start or replace its analysis work.');return 2
    elif args.input and str(args.input.resolve()) != job.get('input') and str(args.input.resolve()) not in [s['path'] for s in job.get('sources',[])]:
        print('An unfinished run is saved. Use python run.py --resume before selecting a different input.');return 2
    def save(): atomic_json(checkpoint,job)
    ui=Terminal()
    with (settings.CACHE_FOLDER/'.production.lock').open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another Farm production run is active.');return 2
        save();cleanup_temporary()
        log_path=Path(settings.LOG_FILE)
        if log_path.exists() and log_path.stat().st_size > 10*1024*1024:
            log_path.replace(log_path.with_suffix('.previous.log'))
        with log_path.open('a',buffering=1) as stream:
            log=SessionLog(stream)
            @contextlib.contextmanager
            def capture():
                # Keep the dashboard visible, while normal diagnostics go to one log.
                with contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
                    yield
            settings._LOG_STREAM=stream
            while True:
                brain=Brain()
                original_show=ui.show
                try:
                    job['status']='running';save()
                    ui.show(phase=0,counts=job_counts(job),detail='Resuming saved work' if args.resume else 'Preparing sources')
                    # Dashboard updates use the original terminal, even inside captured stage output.
                    terminal=sys.stdout
                    def show(**kwargs):
                        with contextlib.redirect_stdout(terminal): original_show(**kwargs)
                    ui.show=show
                    if job.get('upload_only'):
                        deliver_uploads(job,save,ui,capture)
                        job.update(status='finishing',stage='Final Output');save()
                        cleanup_successful_job(job,save)
                        job.update(status='complete');save()
                        ui.show(phase=5,status='COMPLETE',counts=job_counts(job),detail='YouTube delivery complete.')
                    else:execute_job(job,args,save,ui,capture,brain)
                    return 0
                except (Exception,KeyboardInterrupt) as error:
                    if not isinstance(error,KeyboardInterrupt):
                        log.write(''.join(traceback.format_exception(type(error),error,error.__traceback__)))
                    job.update(status='paused',reason='Interrupted by user' if isinstance(error,KeyboardInterrupt) else
                               str(error) if isinstance(error,(BrainError,FileNotFoundError,ValueError,RuntimeError)) else
                               f'{type(error).__name__}; check farm.log for diagnostics')
                    save()
                    ui.show(source=job.get('current_source',''),phase=2 if 'gemini' in job.get('stage','').lower() else 1,
                            status='PAUSED',counts=job_counts(job),detail=f"Stage: {job.get('stage','startup')}\nReason: {job['reason']}\nProgress saved. Fix the problem, then: python run.py --resume")
                    if isinstance(error,KeyboardInterrupt) or not settings.INTERACTIVE_RETRY or not sys.stdin.isatty(): return 2
                    try: answer=input('[r] Retry after fixing the issue / [q] Save and quit: ').strip().lower()
                    except (EOFError,KeyboardInterrupt): return 2
                    if answer!='r': return 2
                    # Reload normal settings so model/key/config repairs work without another operator.
                    importlib.reload(settings)
                    try: validate_settings()
                    except Exception as validation:
                        print(f'Configuration still needs attention: {validation}');return 2
                finally:
                    brain.close()
                    ui.show=original_show
                    settings._LOG_STREAM=stream


if __name__=='__main__':
    try:
        result=main()
    except KeyboardInterrupt:
        print('\nTHE FARM — STOPPED. Existing checkpoints have been preserved.')
        result=130
    except Exception as error:
        print(f'THE FARM — STARTUP NEEDS ATTENTION: {type(error).__name__}. Check configured folders, permissions and the Python environment.')
        result=2
    raise SystemExit(result)
