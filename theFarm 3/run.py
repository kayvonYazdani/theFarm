"""The Farm 3 operator. Run python run.py; uploading is a separate manual command."""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys

import config as settings
from brain import Brain, BrainError, EditPlan, Publication, atomic_json, fingerprint, normalize_transcript, read_json, video_identity
from editor import VideoEditor, CaptionEditor, apply_edit_plan


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


def process_clip(source, brain, *, no_thumbnail=False):
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
    try:
        editor = VideoEditor(source)
        captions = CaptionEditor()
        prefix = f'{digest[:24]}_'
        aligned = Path(settings.CACHE_FOLDER) / f'{prefix}aligned_words.json'
        transcript_path = getattr(settings, 'INPUT_TRANSCRIPT', None)
        if (not aligned.exists() and not settings.USE_CLIPPER and transcript_path and
                source == Path(settings.INPUT_VIDEO).resolve() and Path(transcript_path).is_file()):
            # Explicitly configured existing transcript (testTogi), locally validated before reuse.
            words = normalize_transcript(Path(transcript_path), editor.info['duration'])
            atomic_json(aligned, words)
        print(f'Processing {source.name}', flush=True)
        captions.prepare(source, Path(settings.CACHE_FOLDER), prefix=prefix)
        words = normalize_transcript(captions.words, editor.info['duration'])
        thumbnail_plan = None
        if settings.AI_ENABLED:
            print('  AI: judge', flush=True)
            analysis = brain.judge_clip(source, words)
            atomic_json(folder / 'judgment.json', analysis.model_dump())
            if analysis.rating == 'stinker':
                state['status'] = 'rejected'
                state['reason'] = analysis.explanation
                atomic_json(state_path, state)
                print('  Skipped: stinker', flush=True)
                return folder
            print('  AI: edits, publishing, thumbnail plan', flush=True)
            plan = brain.plan_edits(analysis)
            publication = brain.write_publication(analysis)
            thumbnail_plan = brain.plan_thumbnail(analysis)
            atomic_json(folder / 'thumbnail_plan.json', thumbnail_plan.model_dump())
            state['models'] = dict(brain.models_used)
        else:
            plan = EditPlan(rationale='Manual configuration; AI disabled.', zooms=[], effects=[], background_music=None)
            publication = Publication(title=settings.MANUAL_TITLE or source.stem[:100],
                                      description=settings.MANUAL_DESCRIPTION or 'Manually edited video.',
                                      hashtags=settings.MANUAL_HASHTAGS)
            state['mode'] = 'manual'
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
        code_hash = video_identity(Path(__file__).with_name('editor.py'))
        base_settings = {name: getattr(settings, name) for name in (
            'EDITOR_FPS', 'EDITOR_BACKGROUND_BLUR', 'EDITOR_FOREGROUND_ZOOM', 'EDITOR_BACKGROUND_ZOOM',
            'EDITOR_SATURATION', 'EDITOR_CONTRAST', 'EDITOR_BRIGHTNESS')}
        base_key = fingerprint({'source': digest, 'code': code_hash, 'zooms': [z.model_dump() for z in plan.zooms], 'settings': base_settings})
        base = render_pass('foundation', Path(settings.CACHE_FOLDER) / f'{prefix}base.mp4', base_key, state, state_path,
                           lambda out: editor.render(out))
        caption_key = fingerprint({'base': base_key, 'words': words, 'settings': setting_values(('EDITOR_CAPTION', 'EDITOR_EMOJI', 'EDITOR_FONT', 'TITLE_')),
                                   'title': video_identity(title_path) if title_path else None})
        captioned = render_pass('captions', Path(settings.CACHE_FOLDER) / f'{prefix}captioned.mp4', caption_key, state, state_path,
                                lambda out: captions.render_captions(base, out, editor.info, settings.CACHE_FOLDER, title_path=title_path))
        final_key = fingerprint({'captions': caption_key, 'effects': [e.model_dump() for e in plan.effects],
                                 'music': plan.background_music, 'shake': setting_values(('EDITOR_SHAKE',)),
                                 'assets': {key: video_identity(Path(path)) for key, path in assets.items()}})
        final = render_pass('effects', folder / 'final.mp4', final_key, state, state_path,
                            lambda out: editor.effects.render(captioned, out))
        metadata = {**publication.model_dump(), 'video_file': str(final), 'source_video': str(source),
                    'source_sha256': digest, 'thumbnail_file': None}
        if thumbnail_plan is not None and settings.GENERATE_THUMBNAIL and not no_thumbnail:
            try:
                from thumbnail import generate_thumbnail
                print('  Thumbnail: ComfyUI', flush=True)
                target = generate_thumbnail(thumbnail_plan, folder)
                metadata['thumbnail_file'] = str(target)
                state['thumbnail_status'] = 'complete'
            except Exception as error:
                state['thumbnail_status'] = 'pending'
                state['thumbnail_error_type'] = type(error).__name__
                print(f'  Thumbnail pending ({type(error).__name__}); retry with thumbnail.py after checking ComfyUI.', flush=True)
        else:
            state['thumbnail_status'] = 'disabled' if not settings.GENERATE_THUMBNAIL or no_thumbnail else 'no_ai_plan'
        atomic_json(folder / 'metadata.json', metadata)
        state['status'] = 'complete' if state['thumbnail_status'] != 'pending' else 'video_complete_thumbnail_pending'
        state.pop('error_type', None)
        atomic_json(state_path, state)
        print(f'  Finished: {final}', flush=True)
        return folder
    except Exception as error:
        state['status'] = 'failed'
        state['error_type'] = type(error).__name__
        atomic_json(state_path, state)
        raise


def main():
    parser = argparse.ArgumentParser(description='The Farm 3 production pipeline; never uploads automatically')
    parser.add_argument('--input', type=Path, help='Override INPUT_VIDEO')
    parser.add_argument('--no-thumbnail', action='store_true', help='Keep the thumbnail plan but skip ComfyUI generation')
    args = parser.parse_args()
    source = (args.input or Path(settings.INPUT_VIDEO)).resolve()
    if not source.is_file():
        print(f'Input video missing: {source}')
        return 1
    settings.CACHE_FOLDER.mkdir(parents=True, exist_ok=True)
    # Only one production operator at a time: no duplicate paid jobs or shared-cache writes.
    with (settings.CACHE_FOLDER / '.production.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another production run is already active.')
            return 1
        brain = Brain()
        try:
            if settings.USE_CLIPPER:
                from clipper import run_clipper
                print('VideoHighlighter: selecting candidate clips', flush=True)
                candidates = run_clipper(source, video_identity(source)[:24])
                sources = [Path(candidate['raw_clip_file']) for candidate in candidates]
            else:
                sources = [source]
            failed = 0
            for candidate in sources:
                try:
                    process_clip(candidate, brain, no_thumbnail=args.no_thumbnail)
                except Exception as error:
                    failed += 1
                    if isinstance(error, BrainError):
                        print(f'Failed {candidate.name}: {error}', flush=True)
                    else:
                        print(f'Failed {candidate.name}: {type(error).__name__}; see production.json and check media/dependencies.', flush=True)
            if not sources:
                print('No qualifying clips selected.')
            return 1 if failed else 0
        finally:
            brain.close()


if __name__ == '__main__':
    raise SystemExit(main())
