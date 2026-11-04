"""Quality-gated VideoHighlighter selection and isolated worker; no legacy runtime dependency."""
from pathlib import Path
import json
import os
import subprocess
import sys
import hashlib
import shutil
import config as settings

import math
import statistics
from types import SimpleNamespace


def validate_segments(segments, duration, max_candidates=10, max_clip=60):
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Invalid source duration')
    if len(segments) > min(10,max_candidates):
        raise ValueError('VideoHighlighter exceeded the candidate ceiling; refusing export')
    result = []
    for segment in segments:
        a, b = map(float, segment)
        if not all(map(math.isfinite, (a, b))) or not 0 <= a < b <= duration + .001 or b-a > min(60,max_clip) + .001:
            raise ValueError(f'Invalid candidate interval: {segment}')
        result.append((a, min(b, duration)))
    ordered = sorted(result)
    if any(a[1] > b[0] for a, b in zip(ordered, ordered[1:])):
        raise ValueError('Overlapping candidate moments; refusing duplicate exports')
    # A long VOD must never silently become an almost-full-video highlight reel.
    if duration >= 600 and sum(b-a for a,b in result) / duration >= .8:
        raise ValueError('Highlights cover >=80% of a long VOD; refusing export')
    return result


def select_candidates(video_duration, score, *, scenes=None, keyword_matches=None,
                      action_sequences=None, motion_events=None, motion_peaks=None,
                      audio_peaks=None, object_detections=None, max_candidates=10, max_clip=60,
                      min_score=3.0, min_prominence=2.0, context_before=12.0,
                      context_after=18.0, log_fn=print, **_native_options):
    """Rank distinct local peaks; thresholds qualify candidates before the count limit.

    Native target_duration/min_clip/merge_gap options intentionally have no effect.
    A flat signal (even a high one) is not evidence of a standout moment.
    """
    if type(max_candidates) is not int or not 1 <= max_candidates <= 10:
        raise ValueError('Candidate ceiling must be an integer from 1 to 10')
    if not math.isfinite(video_duration) or video_duration <= 0:
        raise ValueError('Invalid source duration')
    if not 0 < max_clip <= 60 or min_score <= 0 or min_prominence <= 0:
        raise ValueError('Invalid highlight quality/duration limits')
    values = [float(x) for x in score[:math.ceil(video_duration)]]
    if not values or not all(math.isfinite(x) and x >= 0 for x in values):
        if not values:return [], []
        raise ValueError('Invalid highlight scores')
    background = statistics.median(values)
    mad = statistics.median(abs(x-background) for x in values)
    threshold = max(min_score, background + max(min_prominence, 3*mad))
    anchors = [float(t) for group in (motion_events, motion_peaks, audio_peaks) for t in (group or [])]
    anchors += [float(t) for t in (object_detections or {})]
    anchors += [float(a) for a,b in (scenes or [])]
    spans = [(float(s[0]),float(s[1])) for s in (action_sequences or [])]
    spans += [(float(m.get('main_segment',{}).get('start',0)),
               float(m.get('main_segment',{}).get('end',0))) for m in (keyword_matches or [])]
    candidates = []
    # Collapse a plateau to one peak instead of producing one clip per second.
    i = 0
    while i < len(values):
        j = i+1
        while j < len(values) and values[j] == values[i]:j += 1
        height = values[i]
        left = values[i-1] if i else background
        right = values[j] if j < len(values) else background
        anchored = any(i-1 <= t <= j for t in anchors) or any(a < j and b >= i for a,b in spans)
        if anchored and height >= threshold and height > left and height > right:
            peak = (i+j-1)/2
            nearby = values[max(0,i-30):min(len(values),j+30)]
            prominence = height - statistics.median(nearby)
            if prominence >= min_prominence:
                start, end = max(0, i-context_before), min(video_duration, j+context_after)
                # Extend for a matching transcript/action beat, never a whole long scene.
                for match in keyword_matches or []:
                    segment=match.get('main_segment',{})
                    a,b=float(segment.get('start',0)),float(segment.get('end',0))
                    if a <= peak <= b and b-a <= max_clip:
                        start,end=min(start,max(0,a-1)),max(end,min(video_duration,b+1))
                for sequence in action_sequences or []:
                    a,b=map(float,sequence[:2])
                    if a <= peak <= b and b-a <= max_clip:
                        start,end=min(start,max(0,a-1)),max(end,min(video_duration,b+1))
                # Small scene-boundary adjustments add context without chaining scenes.
                for a,b in scenes or []:
                    if a <= peak < b:
                        if 0 <= start-a <= 3:start=max(0,float(a))
                        if 0 <= b-end <= 3:end=min(video_duration,float(b))
                        break
                if end-start > max_clip:
                    start=max(0,min(peak-max_clip/2,video_duration-max_clip))
                    end=min(video_duration,start+max_clip)
                mass=sum(max(0,x-background) for x in values[max(0,i-3):min(len(values),j+3)])
                candidates.append(SimpleNamespace(start=start,end=end,score=height,
                    prominence=prominence,peak=peak,rank=(prominence,height,mass),
                    sources=['scored_peak'],duration=end-start))
        i=j
    selected=[]
    for candidate in sorted(candidates,key=lambda c:(c.rank,-c.peak),reverse=True):
        if any(candidate.start < old.end+5 and old.start < candidate.end+5 for old in selected):
            continue
        selected.append(candidate)
        if len(selected) == max_candidates:break
    selected.sort(key=lambda c:c.start)
    segments=validate_segments([(c.start,c.end) for c in selected],video_duration,max_candidates,max_clip)
    log_fn(f'Farm highlights: {len(candidates)} qualifying peaks; {len(segments)} distinct candidates '
           f'(ceiling {max_candidates}, each <= {max_clip}s; NO duration target). Threshold={threshold:.2f}')
    return segments,selected


def get_duration(path):
    result = subprocess.run(['ffprobe','-v','error','-show_entries','format=duration',
                             '-of','default=noprint_wrappers=1:nokey=1',str(path)],
                            capture_output=True,text=True,check=True,timeout=30)
    value=float(result.stdout)
    if not math.isfinite(value) or value <= 0:
        raise ValueError('Invalid source duration')
    return value


def cut_clip(source,start,end,target):
    duration=float(end)-float(start)
    if not all(math.isfinite(v) for v in (start,end,duration)) or start<0 or not 0<duration<=min(60,settings.MAX_CLIP_DURATION):
        raise ValueError('Invalid requested highlight interval or duration')
    # Decode a short preroll so TS seeks do not lose video until the next keyframe.
    seek_start=max(0.0,float(start)-5.0)
    subprocess.run(['ffmpeg','-y','-v','error','-ss',str(seek_start),'-i',str(source),
                    '-ss',str(float(start)-seek_start),'-t',str(duration),
                    '-c:v','libx264','-preset','veryfast','-threads',str(settings.FFMPEG_THREADS),
                    '-c:a','aac',str(target)],check=True)


def existing_selection(options,run_id,duration):
    """An export-only code change must not rerun already-valid highlight selection."""
    for config_file in sorted(settings.CACHE_FOLDER.glob(f'{run_id}_*_options.json')):
        try:
            if json.loads(config_file.read_text())!=options:continue
            output=config_file.with_name(config_file.name.replace('_options.json','_segments.json'))
            segments=validate_segments(json.loads(output.read_text()),duration,
                                      settings.VIDEOHIGHLIGHTER_MAX_CANDIDATES,settings.MAX_CLIP_DURATION)
            report=json.loads(output.with_suffix('.quality.json').read_text())
            regions=report['regions']
            if len(regions)!=len(segments):continue
            if not all(abs(r['start']-a)<0.001 and abs(r['end']-b)<0.001 and
                       r['score']>=settings.VIDEOHIGHLIGHTER_MIN_SCORE and
                       r['prominence']>=settings.VIDEOHIGHLIGHTER_MIN_PROMINENCE
                       for r,(a,b) in zip(regions,segments)):continue
            return output
        except (OSError,ValueError,KeyError,TypeError):continue
    return None


def validate_export(path,start,end):
    requested=end-start
    try:
        probe=subprocess.run(['ffprobe','-v','error','-show_entries',
                              'format=duration:stream=codec_type,start_time,duration','-of','json',str(path)],
                             capture_output=True,text=True,check=True,timeout=30)
        data=json.loads(probe.stdout)
        actual=float(data['format']['duration'])
        video=next(stream for stream in data['streams'] if stream['codec_type']=='video')
        video_start=float(video.get('start_time',0))
        video_duration=float(video['duration']) if video.get('duration') not in (None,'N/A') else actual-video_start
        if not all(math.isfinite(v) for v in (actual,video_start,video_duration)):raise ValueError('Nonfinite media timing')
    except Exception as error:
        message=f'Exported highlight is unreadable: start={start:.6f}s end={end:.6f}s requested={requested:.6f}s actual=unavailable ({type(error).__name__}) file={path}'
        print(message,flush=True);raise ValueError(message) from error
    if (abs(actual-requested)>0.1 or abs(video_duration-requested)>0.1 or abs(video_start)>0.1
            or actual>min(60,settings.MAX_CLIP_DURATION)+0.05):
        message=f'Exported highlight has the wrong duration: start={start:.6f}s end={end:.6f}s requested={requested:.6f}s actual={actual:.6f}s video_duration={video_duration:.6f}s video_start={video_start:.6f}s difference={actual-requested:+.6f}s file={path}'
        print(message,flush=True);raise ValueError(message)
    return actual


def run_clipper(video_file,run_id):
    source=Path(video_file).resolve()
    root=Path(settings.VIDEOHIGHLIGHTER_DIR)
    worker=Path(__file__).resolve()
    for path in (Path(settings.VIDEOHIGHLIGHTER_PYTHON),root/'pipeline.py'):
        if not path.is_file(): raise FileNotFoundError(f'VideoHighlighter dependency missing: {path}')
    duration=get_duration(source)
    options=dict(settings.VIDEOHIGHLIGHTER_OPTIONS)
    options.update(clip_time=0,auto_min_clip=0,auto_max_clip=settings.MAX_CLIP_DURATION,exact_duration=None,
                   farm_max_candidates=settings.VIDEOHIGHLIGHTER_MAX_CANDIDATES,
                   farm_min_score=settings.VIDEOHIGHLIGHTER_MIN_SCORE,
                   farm_min_prominence=settings.VIDEOHIGHLIGHTER_MIN_PROMINENCE,
                   farm_context_before=settings.HIGHLIGHT_CONTEXT_BEFORE,
                   farm_context_after=settings.HIGHLIGHT_CONTEXT_AFTER,
                   cache_dir=str(settings.CACHE_FOLDER),output_file=str(settings.CACHE_FOLDER/f'{run_id}_highlight.mp4'))
    key=hashlib.sha256(json.dumps(options,sort_keys=True).encode()+worker.read_bytes()).hexdigest()[:12]
    output=settings.CACHE_FOLDER/f'{run_id}_{key}_segments.json'
    config_file=settings.CACHE_FOLDER/f'{run_id}_{key}_options.json'
    settings.CACHE_FOLDER.mkdir(parents=True,exist_ok=True)
    if not output.is_file():
        cached=existing_selection(options,run_id,duration)
        if cached is not None:
            output=cached
            key=output.name[len(run_id)+1:-len('_segments.json')]
    if not output.is_file():
        config_file.write_text(json.dumps(options))
        env=os.environ.copy()
        ffmpeg=shutil.which('ffmpeg')
        if not ffmpeg: raise FileNotFoundError('FFmpeg is not installed or not on PATH')
        env['PATH']=str(Path(ffmpeg).parent)+os.pathsep+env.get('PATH','')
        log=settings.CACHE_FOLDER/f'{run_id}_highlighter.log'
        with log.open('a') as stream:
            subprocess.run([str(settings.VIDEOHIGHLIGHTER_PYTHON),str(worker),'--worker',str(root),
                            str(source),str(output),str(config_file)],cwd=root,
                           timeout=settings.VIDEOHIGHLIGHTER_TIMEOUT,check=True,env=env,stdout=stream,stderr=stream)
    segments=validate_segments(json.loads(output.read_text()),duration,
                               settings.VIDEOHIGHLIGHTER_MAX_CANDIDATES,settings.MAX_CLIP_DURATION)
    result=[]
    settings.CLIPS_FOLDER.mkdir(parents=True,exist_ok=True)
    for index,(start,end) in enumerate(segments,1):
        target=settings.CLIPS_FOLDER/f'{run_id}_{key}_{index:02d}.mp4'
        if target.is_file():
            try: validate_export(target,start,end);valid=True
            except Exception: valid=False
        else: valid=False
        if not valid:
            partial=target.with_suffix('.partial.mp4')
            cut_clip(source,start,end,partial)
            validate_export(partial,start,end)
            partial.replace(target)
        result.append({'raw_clip_file':target,'start':start,'end':end})
    return result


def worker_main(root,source,output,config_file):
    """Runs inside the existing VH environment; only the selection hook is replaced."""
    sys.path.insert(0,root)
    import pipeline
    import yaml
    from modules.system import app_paths
    config=json.loads(Path(config_file).read_text())
    config['report_only']=True
    original=app_paths.config_path
    defaults_path=Path(original('config.yaml'))
    defaults=yaml.safe_load(defaults_path.read_text()) if defaults_path.is_file() else {}
    defaults=defaults or {}
    defaults.setdefault('highlights',{}).update(clip_time=0,exact_duration=None,auto_min_clip=0)
    local=Path(output).with_suffix('.defaults.yaml')
    local.write_text(yaml.safe_dump(defaults))
    app_paths.config_path=lambda name,*a,**kw: str(local) if name=='config.yaml' else original(name,*a,**kw)
    selected=[]
    def selector(**signals):
        signals.pop("max_clip",None)
        intervals,regions=select_candidates(**signals,max_candidates=config['farm_max_candidates'],
                         max_clip=config['auto_max_clip'],min_score=config['farm_min_score'],
                         min_prominence=config['farm_min_prominence'],context_before=config['farm_context_before'],
                         context_after=config['farm_context_after'])
        selected.append({'duration':float(signals['video_duration']),'regions':[vars(r) for r in regions]})
        return intervals,regions
    pipeline.build_auto_segments=selector
    segments=pipeline.run_highlighter(source,gui_config=config,timeline_fn=lambda *_:None)
    if segments is None or len(selected)!=1: raise RuntimeError('VideoHighlighter did not complete quality-gated selection')
    segments=validate_segments(segments,selected[0]['duration'],config['farm_max_candidates'],config['auto_max_clip'])
    report=Path(output).with_suffix('.quality.json')
    report_temp=report.with_suffix('.tmp')
    report_temp.write_text(json.dumps(selected[0],indent=2));report_temp.replace(report)
    temporary=Path(output).with_suffix('.tmp')
    temporary.write_text(json.dumps(segments));temporary.replace(output)


if __name__=='__main__':
    if len(sys.argv)==6 and sys.argv[1]=='--worker':
        worker_main(*sys.argv[2:])
    else:
        print('Start The Farm with: python run.py')
