import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brain
import editor
import run
import thumbnail
import post_to_youtube as youtube
import config

PUBLICATION = {'title': 'A Real Reaction', 'description': 'A reaction in the clip.', 'hashtags': ['#Reaction', '#Video', '#Shorts']}


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_title_asset_is_not_overwritten_and_legacy_style_is_reused(self):
        from PIL import Image
        asset = self.root / 'title.jpg'
        Image.new('RGB', (850, 123)).save(asset)
        before = asset.read_bytes()
        with patch.object(config, 'TITLE_HEADER', asset):
            captions = editor.CaptionEditor()
            target = captions.prepare_title('Test production title', self.root,
                                            {'width':1920,'height':1080,'duration':10.0})
        self.assertEqual(asset.read_bytes(), before)
        from PIL import Image
        with Image.open(target) as image:
            self.assertEqual(image.size, (850,123))
        self.assertEqual(captions.TITLE_VISIBLE_Y, 257)
        self.assertEqual(captions.title_opacity(0), 0)
        self.assertEqual(captions.title_opacity(4), 1)
        self.assertEqual(captions.title_opacity(9), 0)

    def test_ai_schema_and_capabilities_have_no_shake(self):
        self.assertNotIn('shake', brain.EditPlan.model_fields)
        self.assertNotIn('shake', brain.editor_capabilities())
        with self.assertRaises(brain.ValidationError):
            brain.EditPlan(rationale='test', zooms=[], effects=[], background_music=None, shake=True)

    def test_config_controls_shake_independently(self):
        with patch.object(config, 'EDITOR_SHAKE_ENABLED', True):
            self.assertTrue(editor.EffectsEditor().shake_enabled)
        with patch.object(config, 'EDITOR_SHAKE_ENABLED', False):
            self.assertFalse(editor.EffectsEditor().shake_enabled)

    def test_render_pass_resume_and_atomic_commit(self):
        target = self.root / 'base.mp4'
        state = {}
        calls = []
        def render(path):
            calls.append(path)
            path.write_bytes(b'valid_video')
            return path
        with patch.object(run,'VideoEditor'):
            run.render_pass('base',target,'key1',state,self.root/'state.json',render)
            run.render_pass('base',target,'key1',state,self.root/'state.json',render)
        self.assertEqual(len(calls),1)
        self.assertEqual(target.read_bytes(), b'valid_video')
        def fail(path):
            path.write_bytes(b'broken')
            raise RuntimeError('interrupted')
        with self.assertRaises(RuntimeError), patch.object(run,'VideoEditor'):
            run.render_pass('base',target,'key2',state,self.root/'state.json',fail)
        self.assertEqual(target.read_bytes(), b'valid_video')

    def test_manual_production_uses_three_passes_and_does_not_upload(self):
        source = self.root / 'input.mp4'; source.write_bytes(b'source')
        cache = self.root / 'cache'; cache.mkdir()
        class FakeVideo:
            def __init__(self, path):
                self.info = {'duration':3.0, 'width':160, 'height':90}
                self.effects = SimpleNamespace(render=lambda src,out: self.render(out))
            def render(self, out):
                Path(out).write_bytes(b'video')
                return out
        class FakeCaptions:
            words = [{'text':'Hi','start':0.0,'end':0.5}]
            def prepare(self,*args,**kwargs): pass
            def prepare_title(self,title,folder,info):
                out=folder/'title_header.png';out.write_bytes(b'title');return out
            def render_captions(self,src,out,*args,**kwargs):
                Path(out).write_bytes(b'captioned');return out
        with patch.multiple(config, CACHE_FOLDER=cache, OUTPUT_DIR=self.root/'finished', INPUT_VIDEO=source,
                            INPUT_TRANSCRIPT=None, AI_ENABLED=False, GENERATE_THUMBNAIL=False), \
             patch.object(run,'VideoEditor',FakeVideo), patch.object(run,'CaptionEditor',FakeCaptions), \
             patch.object(run,'apply_edit_plan',return_value={}), patch.object(youtube,'youtube_service') as upload:
            folder = run.process_clip(source, None)
            metadata=json.loads((folder/'metadata.json').read_text())
            state=json.loads((folder/'production.json').read_text())
            self.assertTrue(Path(metadata['video_file']).exists())
            self.assertEqual(set(state['passes']), {'foundation','captions','effects'})
            self.assertEqual(state['status'],'complete')
            upload.assert_not_called()

    def make_upload_folder(self):
        video=self.root/'final.mp4';video.write_bytes(b'video')
        (self.root/'metadata.json').write_text(json.dumps({**PUBLICATION,'video_file':str(video),'thumbnail_file':None}))
        return video

    def test_youtube_preview_never_authenticates(self):
        self.make_upload_folder()
        with patch.object(youtube,'youtube_service') as service:
            _,body,_=youtube.prepare_upload(self.root)
            self.assertEqual(body['status']['privacyStatus'],'private')
            service.assert_not_called()

    def test_youtube_completed_upload_is_not_duplicated(self):
        video=self.make_upload_folder()
        (self.root/'youtube_upload.json').write_text(json.dumps({'video_sha256':brain.video_identity(video),
                                                              'status':'complete','video_id':'existing'}))
        service=MagicMock()
        self.assertEqual(youtube.upload(self.root,service=service),'existing')
        service.videos.assert_not_called()

    def test_youtube_ambiguous_upload_stops_before_new_request(self):
        video=self.make_upload_folder()
        (self.root/'youtube_upload.json').write_text(json.dumps({'video_sha256':brain.video_identity(video),
                                                              'status':'submitting'}))
        service=MagicMock()
        with self.assertRaises(RuntimeError): youtube.upload(self.root,service=service)
        service.videos.assert_not_called()

    def test_legacy_rejected_start_can_retry_without_clearing_receipt(self):
        video=self.make_upload_folder()
        (self.root/'youtube_upload.json').write_text(json.dumps({'video_sha256':brain.video_identity(video),
            'status':'submitting','error_type':'ResumableUploadError'}))
        service=MagicMock()
        service.videos.return_value.insert.return_value.next_chunk.return_value=(None,{'id':'recovered'})
        self.assertEqual(youtube.upload(self.root,service=service),'recovered')
        self.assertEqual(youtube.upload(self.root,service=service),'recovered')
        self.assertEqual(service.videos.return_value.insert.call_count,1)

    def test_rejected_session_start_records_reason_and_remains_retryable(self):
        from googleapiclient.errors import ResumableUploadError
        self.make_upload_folder()
        service=MagicMock()
        service.videos.return_value.insert.return_value.next_chunk.side_effect=ResumableUploadError(
            SimpleNamespace(status=403,reason='forbidden'),
            b'{"error":{"errors":[{"reason":"uploadLimitExceeded"}]}}')
        with self.assertRaisesRegex(RuntimeError,'uploadLimitExceeded'):
            youtube.upload(self.root,service=service)
        receipt=json.loads((self.root/'youtube_upload.json').read_text())
        self.assertEqual(receipt['status'],'start_rejected')
        self.assertEqual(receipt['http_status'],403)
        self.assertEqual(receipt['error_reason'],'uploadLimitExceeded')
        service.videos.return_value.insert.return_value.next_chunk.side_effect=None
        service.videos.return_value.insert.return_value.next_chunk.return_value=(None,{'id':'retry'})
        self.assertEqual(youtube.upload(self.root,service=service),'retry')

    def test_interrupted_media_upload_remains_protected(self):
        from googleapiclient.errors import HttpError
        self.make_upload_folder()
        service=MagicMock()
        service.videos.return_value.insert.return_value.next_chunk.side_effect=HttpError(
            SimpleNamespace(status=400,reason='bad request'),b'{}')
        with self.assertRaises(RuntimeError):youtube.upload(self.root,service=service)
        with self.assertRaisesRegex(RuntimeError,'uncertain'):youtube.upload(self.root,service=service)
        self.assertEqual(service.videos.return_value.insert.call_count,1)

    def test_thumbnail_uncertain_submission_is_not_duplicated(self):
        (self.root/'thumbnail_status.json').write_text(json.dumps({'status':'submitting'}))
        workflow=self.root/'workflow.json'
        workflow.write_text(json.dumps({
            '1':{'class_type':'KSampler','inputs':{'positive':['2',0]}},
            '2':{'class_type':'CLIPTextEncode','inputs':{'text':''}},
            '3':{'class_type':'SaveImage','inputs':{'filename_prefix':''}}}))
        with patch('urllib.request.urlopen') as network:
            with self.assertRaises(RuntimeError):
                thumbnail._generate_thumbnail(SimpleNamespace(thumbnail_prompt='actual scene'),workflow,
                                              'http://localhost:8188',self.root,self.root/'final.mp4')
            network.assert_not_called()

    def test_clipper_calls_existing_isolated_worker_and_reuses_selection(self):
        import clipper
        source=self.root/'long.mp4';source.write_bytes(b'source')
        cache=self.root/'cache';cache.mkdir()
        highlighter=self.root/'VideoHighlighter';highlighter.mkdir()
        (highlighter/'pipeline.py').touch();(highlighter/'python').touch()
        def worker(command,**kwargs):
            Path(command[5]).write_text('[[0, 2]]')
        def cut(src,start,end,out): Path(out).write_bytes(b'clip')
        with patch.multiple(config, CACHE_FOLDER=cache, CLIPS_FOLDER=self.root/'clips',
                            VIDEOHIGHLIGHTER_DIR=highlighter,
                            VIDEOHIGHLIGHTER_PYTHON=highlighter/'python'), \
             patch.object(clipper,'get_duration',side_effect=lambda path: 5.0 if Path(path)==source else 2.0), patch.object(clipper,'cut_clip',side_effect=cut), patch.object(clipper,'validate_export',return_value=2.0), \
             patch.object(clipper.subprocess,'run',side_effect=worker) as call:
            first=clipper.run_clipper(source,'sourcehash')
            second=clipper.run_clipper(source,'sourcehash')
            self.assertEqual(first,second)
            self.assertEqual(call.call_count,1)
            self.assertTrue(call.call_args.args[0][1].endswith('clipper.py'))
            self.assertEqual(call.call_args.args[0][2],'--worker')

    def test_quality_selector_returns_only_qualified_peaks_not_quota(self):
        import clipper
        scores=[0.0]*600
        for t,height in ((60,8),(180,12),(420,10)):
            scores[t]=height
        clips,regions=clipper.select_candidates(600,scores,audio_peaks=[60,180,420],max_candidates=10,max_clip=60,log_fn=lambda *_:None)
        self.assertEqual(len(clips),3)
        self.assertTrue(all(0 < b-a <= 60 for a,b in clips))
        self.assertEqual(sorted(r.score for r in regions),[8,10,12])

    def test_quality_selector_limits_top_ten_and_rejects_flat_loud_signal(self):
        import clipper
        scores=[0.0]*2000
        peaks=list(range(60,1900,80))
        for i,t in enumerate(peaks):scores[t]=6+i
        clips,regions=clipper.select_candidates(2000,scores,audio_peaks=peaks,max_candidates=10,max_clip=60,log_fn=lambda *_:None)
        self.assertEqual(len(clips),10)
        self.assertEqual(min(r.score for r in regions),6+len(peaks)-10)
        self.assertEqual(clipper.select_candidates(2000,[20.0]*2000,audio_peaks=peaks,log_fn=lambda *_:None)[0],[])
        self.assertTrue(all(a[1]<=b[0] for a,b in zip(clips,clips[1:])))

    def test_quality_thresholds_and_zero_highlights(self):
        import clipper
        scores=[0.0]*200;scores[80]=5
        self.assertEqual(clipper.select_candidates(200,scores,audio_peaks=[80],min_score=6,log_fn=lambda *_:None)[0],[])
        self.assertEqual(clipper.select_candidates(200,scores,audio_peaks=[80],min_prominence=6,log_fn=lambda *_:None)[0],[])
        self.assertEqual(clipper.select_candidates(200,[0.0]*200,log_fn=lambda *_:None)[0],[])

    def test_dynamic_highlights_are_bounded_to_sixty_seconds(self):
        import clipper
        scores=[0.0]*300
        for i in range(80,125): scores[i]=10
        clips,_=clipper.select_candidates(300,scores,audio_peaks=[100],max_clip=60,context_before=20,context_after=20,log_fn=lambda *_:None)
        self.assertEqual(len(clips),1)
        self.assertLessEqual(clips[0][1]-clips[0][0],60)
        with self.assertRaises(ValueError): clipper.validate_segments([(0,61)],100,max_clip=180)

    def test_settings_reject_more_than_ten_and_over_sixty(self):
        for values in ({'MAX_CLIP_DURATION':61},{'VIDEOHIGHLIGHTER_MAX_CANDIDATES':11},
                       {'VIDEOHIGHLIGHTER_MIN_PROMINENCE':0}):
            with self.subTest(values=values),patch.multiple(config,**values),self.assertRaises(ValueError):
                run.validate_settings()

    @__import__('contextlib').contextmanager
    def operator_settings(self,source):
        manager=patch.multiple(config,CACHE_FOLDER=self.root/'cache',DATABASE_FOLDER=self.root/'data',
               OUTPUT_DIR=self.root/'finished',VIDS_FOLDER=self.root/'videos',INPUT_VIDEO=source,
               INPUT_TRANSCRIPT=None,CHECKPOINT_FILE=self.root/'data/job.json',LOG_FILE=self.root/'data/farm.log',
               INTERACTIVE_RETRY=False,USE_CLIPPER=True,VIDEOS_TO_PROCESS=1,
               UPLOAD_ENABLED=False,UPLOAD_SCHEDULE_FILE=self.root/'data/youtube_schedule.json',CLEANUP_AFTER_SUCCESS=False)

        with manager,patch.object(run,'VideoEditor'):
            yield

    def test_operator_zero_highlights_is_complete_and_not_repeated(self):
        source=self.root/'source.mp4';source.write_bytes(b'source')
        import clipper
        with self.operator_settings(source),patch.object(run,'validate_settings'), \
             patch.object(clipper,'run_clipper',return_value=[]) as clips, \
             patch.object(run,'process_clip') as process,patch.object(sys,'argv',['run.py']), \
             patch.object(run,'Brain'),patch('sys.stdout',new_callable=__import__('io').StringIO):
            self.assertEqual(run.main(),0)
            self.assertEqual(run.main(),0)
            self.assertEqual(clips.call_count,1)
            process.assert_not_called()
        job=json.loads((self.root/'data/job.json').read_text())
        self.assertEqual(job['status'],'complete')
        self.assertEqual(len(job['history']),1)

    def test_operator_pause_and_resume_keeps_candidates_and_completed_clips(self):
        source=self.root/'source.mp4';source.write_bytes(b'source')
        a=self.root/'a.mp4';a.write_bytes(b'clip_a')
        b=self.root/'b.mp4';b.write_bytes(b'clip_b')
        import clipper
        calls=[]
        failing=[True]
        def process(path,brain,**kwargs):
            path=Path(path);calls.append(path.name)
            folder=self.root/'finished'/path.stem;folder.mkdir(parents=True,exist_ok=True)
            callback=kwargs['on_stage'];callback('Gemini Publishing Writer',2,{'accepted':True},folder)
            if path==b and failing[0]:raise brain_module.BrainError('Gemini publishing: API quota exceeded; fix billing and resume.')
            (folder/'production.json').write_text(json.dumps({'status':'complete','accepted':True,'passes':{'effects':{}}}))
            return folder
        brain_module=brain
        with self.operator_settings(source),patch.object(run,'validate_settings'), \
             patch.object(clipper,'run_clipper',return_value=[{'raw_clip_file':a},{'raw_clip_file':b}]) as clips, \
             patch.object(run,'process_clip',side_effect=process),patch.object(run,'Brain'), \
             patch.object(sys,'argv',['run.py']),patch('sys.stdout',new_callable=__import__('io').StringIO):
            self.assertEqual(run.main(),2)
            paused=json.loads((self.root/'data/job.json').read_text())
            self.assertEqual(paused['status'],'paused')
            self.assertEqual(paused['stage'],'Gemini Publishing Writer')
            self.assertEqual(paused['sources'][0]['candidates'][0]['status'],'complete')
            failing[0]=False
            with patch.object(sys,'argv',['run.py','--resume']):self.assertEqual(run.main(),0)
            self.assertEqual(clips.call_count,1)
            self.assertEqual(calls,['a.mp4','b.mp4','b.mp4'])

    def test_corrupt_operator_checkpoint_is_preserved(self):
        source=self.root/'source.mp4';source.write_bytes(b'source')
        (self.root/'data').mkdir();checkpoint=self.root/'data/job.json';checkpoint.write_text('broken')
        with self.operator_settings(source),patch.object(run,'validate_settings'),patch.object(sys,'argv',['run.py']), \
             patch('sys.stdout',new_callable=__import__('io').StringIO):
            self.assertEqual(run.main(),2)
        self.assertEqual(checkpoint.read_text(),'broken')

    def test_interrupt_preserves_operator_checkpoint(self):
        source=self.root/'source.mp4';source.write_bytes(b'source')
        import clipper
        with self.operator_settings(source),patch.object(run,'validate_settings'), \
             patch.object(clipper,'run_clipper',side_effect=KeyboardInterrupt),patch.object(run,'Brain'), \
             patch.object(sys,'argv',['run.py']),patch('sys.stdout',new_callable=__import__('io').StringIO):
            self.assertEqual(run.main(),2)
        state=json.loads((self.root/'data/job.json').read_text())
        self.assertEqual(state['status'],'paused')
        self.assertEqual(state['reason'],'Interrupted by user')

    def test_operator_log_redacts_known_credentials(self):
        import io
        stream=io.StringIO()
        with patch.dict('os.environ',{'GEMINI_API_KEY':'secret_sentinel_value'}):
            writer=run.SessionLog(stream);writer.write('error: secret_sentinel_value')
        self.assertNotIn('secret_sentinel_value',stream.getvalue())
        self.assertIn('[REDACTED]',stream.getvalue())

    def test_cleanup_preserves_outputs_and_only_removes_owned_old_partials(self):
        import os,time
        cache=self.root/'cache';cache.mkdir()
        partial=cache/('a'*24+'_base.partial.mp4');partial.write_bytes(b'partial')
        valid=cache/('a'*24+'_base.mp4');valid.write_bytes(b'completed')
        unknown=cache/'user.partial.mp4';unknown.write_bytes(b'user')
        for p in (partial,valid,unknown): os.utime(p,(time.time()-1000000,)*2)
        with patch.multiple(config,CACHE_FOLDER=cache,CLIPS_FOLDER=self.root/'clips',OUTPUT_DIR=self.root/'finished',TEMP_RETENTION_HOURS=48):
            self.assertEqual(run.cleanup_temporary(),1)
        self.assertTrue(valid.exists());self.assertTrue(unknown.exists())

    def test_no_editor_demo_effects_remain(self):
        text=Path(editor.__file__).read_text()
        self.assertNotIn('testTogi',text)
        self.assertNotIn('effects.flashbang(15.0)',text)
        self.assertNotIn('effects.vine_boom(8.0)',text)

    def test_isolated_worker_contract_with_native_signal_arguments(self):
        import subprocess
        fake=self.root/'vh';(fake/'modules/system').mkdir(parents=True)
        for path in (fake/'modules/__init__.py',fake/'modules/system/__init__.py'):path.write_text('')
        (fake/'modules/system/app_paths.py').write_text('def config_path(name):\n    return "missing.yaml"\n')
        (fake/'pipeline.py').write_text('''
def build_auto_segments(**kw): raise AssertionError('native selector should be replaced')
def run_highlighter(source,gui_config,timeline_fn):
    scores=[0.0]*300
    for t in (60,160,250):scores[t]=12
    segments,_=build_auto_segments(video_duration=300,score=scores,audio_peaks=[60,160,250],max_clip=60,min_clip=20,target_duration=99999)
    return segments
''')
        options=self.root/'options.json';out=self.root/'segments.json'
        options.write_text(json.dumps({'farm_max_candidates':10,'auto_max_clip':60,'farm_min_score':3,
                                       'farm_min_prominence':2,'farm_context_before':12,'farm_context_after':18}))
        import clipper
        subprocess.run([sys.executable,str(Path(clipper.__file__)),'--worker',str(fake),str(self.root/'source.mp4'),str(out),str(options)],
                       check=True,capture_output=True,text=True,timeout=30)
        segments=json.loads(out.read_text())
        self.assertEqual(len(segments),3)
        self.assertTrue(all(end-start<=60 for start,end in segments))
        self.assertTrue(out.with_suffix('.quality.json').exists())

    def test_interactive_retry_reuses_the_same_candidate_selection(self):
        source=self.root/'source.mp4';source.write_bytes(b'source')
        candidate=self.root/'candidate.mp4';candidate.write_bytes(b'candidate')
        import clipper
        attempts=[]
        def process(path,brain,**kwargs):
            folder=self.root/'finished/clip';folder.mkdir(parents=True,exist_ok=True)
            kwargs['on_stage']('Gemini Clip Judge',2,{},folder)
            attempts.append(path)
            if len(attempts)==1:raise brain_module.BrainError('Fix the API key and retry')
            (folder/'production.json').write_text(json.dumps({'status':'complete','accepted':True,'passes':{'effects':{}}}))
            return folder
        brain_module=brain
        with self.operator_settings(source),patch.object(config,'INTERACTIVE_RETRY',True),patch.object(run,'validate_settings'), \
             patch.object(clipper,'run_clipper',return_value=[{'raw_clip_file':candidate}]) as clips, \
             patch.object(run,'process_clip',side_effect=process),patch.object(run,'Brain'), \
             patch.object(sys,'argv',['run.py']),patch('sys.stdin.isatty',return_value=True), \
             patch('builtins.input',return_value='r') as prompt,patch('importlib.reload',side_effect=lambda module:module), \
             patch('sys.stdout',new_callable=__import__('io').StringIO):
            self.assertEqual(run.main(),0)
            self.assertEqual(clips.call_count,1)
            self.assertEqual(len(attempts),2)
            prompt.assert_called_once()

    def test_requested_source_is_checkpointed_before_analysis(self):
        source=self.root/'source.mp4';source.write_bytes(b'source')
        import clipper
        with self.operator_settings(None),patch.object(run,'validate_settings'), \
             patch.object(clipper,'run_clipper',side_effect=KeyboardInterrupt),patch.object(run,'Brain'), \
             patch.object(sys,'argv',['run.py','--input',str(source)]),patch('sys.stdout',new_callable=__import__('io').StringIO):
            self.assertEqual(run.main(),2)
        job=json.loads((self.root/'data/job.json').read_text())
        self.assertEqual(job['input'],str(source.resolve()))
        self.assertEqual(job['source_paths'],[str(source.resolve())])

    def test_whisper_padding_is_bounded_before_alignment(self):
        segments=[{'start':30.0,'end':59.98,'text':'Thank you.'}]
        self.assertEqual(editor.CaptionEditor._bounded_segments(segments,31.0)[0]['end'],31.0)
        self.assertEqual(segments[0]['end'],59.98)
        with self.assertRaises(brain.BrainError):
            editor.CaptionEditor._bounded_segments([{'start':368.,'end':369.,'text':'wrong'}],31.)

    def test_padded_cached_alignment_is_repaired_from_saved_transcript(self):
        (self.root/'repair_transcript.json').write_text(json.dumps([{'start':30.,'end':59.98,'text':'Thank you.'}]))
        (self.root/'repair_aligned_words.json').write_text(json.dumps([{'word':'Thank','start':33.671,'end':42.237}]))
        captions=editor.CaptionEditor()
        with patch.object(captions,'transcribe',side_effect=AssertionError('Repeated transcription')), patch.object(captions,'align_words',return_value=[]) as align:
            captions.prepare(self.root/'clip.mp4',self.root,prefix='repair_',duration=31.)
        self.assertEqual(align.call_args.args[1][0]['end'],31.)

    def test_shared_prefix_is_isolated_by_clip_content(self):
        captions=editor.CaptionEditor()
        first=self.root/'first.mp4';first.write_bytes(b'first clip')
        second=self.root/'second.mp4';second.write_bytes(b'second clip')
        with patch.object(captions,'transcribe',return_value=[]) as transcribe:
            for path in (first,second,first,second):
                captions.prepare(path,self.root,prefix='shared_',duration=31.)
        self.assertEqual(transcribe.call_count,2)
        self.assertEqual(len(list(self.root.glob('*_aligned_words.json'))),2)

    def test_alignment_retry_reuses_saved_transcription(self):
        c=editor.CaptionEditor()
        segment=[{'start':0.0,'end':1.0,'text':'A real sentence'}]
        with patch.object(c,'transcribe',return_value=segment) as transcribe, \
             patch.object(c,'align_words',side_effect=[RuntimeError('alignment interrupted'),[]]):
            with self.assertRaises(RuntimeError):c.prepare(self.root/'candidate.mp4',self.root,prefix='candidate_',duration=2.0)
            c.prepare(self.root/'candidate.mp4',self.root,prefix='candidate_',duration=2.0)
            self.assertEqual(transcribe.call_count,1)

    def test_empty_transcript_is_cached(self):
        c=editor.CaptionEditor()
        with patch.object(c,'transcribe',return_value=[]) as transcribe:
            c.prepare(self.root/'silent.mp4',self.root,prefix='silent_',duration=2.0)
            c.prepare(self.root/'silent.mp4',self.root,prefix='silent_',duration=2.0)
            self.assertEqual(transcribe.call_count,1)
            self.assertEqual(json.loads((self.root/'silent_aligned_words.json').read_text()),[])

    def test_audio_settings_only_change_final_pass_implementation(self):
        # Implementation keys are per class/pass, rather than hashing the entire editor.
        first=run.implementation_key('ZoomEditor','VerticalFormatter','FilterEditor','VideoEditor')
        text=Path(editor.__file__).read_text()
        changed=text.replace('FLASH_VOLUME = 0.65','FLASH_VOLUME = 0.5')
        second=run._implementation(changed,('ZoomEditor','VerticalFormatter','FilterEditor','VideoEditor'))
        self.assertEqual(first,second)
        self.assertNotEqual(run.implementation_key('EffectsEditor'),run._implementation(changed,('EffectsEditor',)))

    def test_completed_unchanged_vod_skips_probe_and_full_file_hash(self):
        source=(self.root/'source.mp4').resolve();source.write_bytes(b'source')
        state={'sources':[],'history':{'known_digest':{'path':str(source),'stamp':run.file_stamp(source)}}}
        with patch.object(config,'INPUT_VIDEO',source),patch.object(run,'VideoEditor',side_effect=AssertionError('Repeated probe')), \
             patch.object(run,'video_identity',side_effect=AssertionError('Repeated VOD hash')):
            run.source_jobs(state,SimpleNamespace(input=source,reprocess=False),lambda:None,__import__('contextlib').nullcontext)
        self.assertEqual(state['sources'],[])
        self.assertTrue(state['sources_ready'])

    def upload_config(self, **extra):
        values={'UPLOAD_MODE':'scheduled','UPLOAD_TIMES':('12:00','17:00'),
                'UPLOAD_TIMEZONE':'America/New_York','UPLOAD_VIDEOS_PER_SLOT':2,
                'UPLOAD_IMMEDIATE_PRIVACY':'public'}
        values.update(extra)
        return patch.multiple(config,**values)

    def test_upload_schedule_fills_each_batch_then_advances_day(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now=datetime(2026,11,3,10,tzinfo=ZoneInfo('America/New_York'))
        items=[{'digest':str(i),'folder':str(self.root/str(i)),'receipt':{}} for i in range(7)]
        with self.upload_config():
            plan=youtube.plan_uploads(items,{'version':1,'entries':{}},now=now)
        slots=[youtube.parse_publish_at(i['publish_at']).astimezone(ZoneInfo('America/New_York')).strftime('%m-%d %H:%M') for i in plan]
        self.assertEqual(slots,['11-04 12:00']*2+['11-04 17:00']*2+['11-05 12:00']*2+['11-05 17:00'])

    def test_upload_schedule_resume_and_new_runs_do_not_overbook(self):
        from datetime import datetime,timezone
        now=datetime(2026,11,3,15,tzinfo=timezone.utc)
        a=[{'digest':str(i),'folder':str(self.root/str(i)),'receipt':{}} for i in range(3)]
        ledger={'version':1,'entries':{}}
        with self.upload_config():
            first=youtube.plan_uploads(a,ledger,now=now)
            again=youtube.plan_uploads(a,ledger,now=now)
            added=youtube.plan_uploads([{'digest':'new','folder':'new','receipt':{}}],ledger,now=now)
        self.assertEqual([p['publish_at'] for p in first],[p['publish_at'] for p in again])
        self.assertEqual(added[0]['publish_at'],first[2]['publish_at'])

    def test_per_time_batch_sizes_and_dst(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now=datetime(2026,10,31,10,tzinfo=ZoneInfo('America/New_York'))
        items=[{'digest':str(i),'folder':str(i),'receipt':{}} for i in range(4)]
        with self.upload_config(UPLOAD_VIDEOS_PER_SLOT={'12:00':1,'17:00':2}):
            plan=youtube.plan_uploads(items,{'entries':{}},now=now)
        self.assertEqual(plan[0]['publish_at'],'2026-11-01T17:00:00+00:00')
        self.assertEqual(plan[1]['publish_at'],plan[2]['publish_at'])
        self.assertTrue(plan[3]['publish_at'].startswith('2026-11-02'))

    def test_expired_unsent_slot_is_replanned_not_published_immediately(self):
        from datetime import datetime,timezone
        ledger={'entries':{'one':{'publish_at':'2020-01-01T17:00:00Z','status':'pending'}}}
        with self.upload_config():
            plan=youtube.plan_uploads([{'digest':'one','folder':'one','receipt':{}}],ledger,
                                     now=datetime(2026,11,3,15,tzinfo=timezone.utc))
        self.assertTrue(plan[0]['publish_at'].startswith('2026-11-04'))

    def test_immediate_mode_has_no_publish_at(self):
        from datetime import datetime,timezone
        with self.upload_config(UPLOAD_MODE='immediate'):
            plan=youtube.plan_uploads([{'digest':'one','folder':'one','receipt':{}}],{'entries':{}},
                                     now=datetime(2026,11,3,tzinfo=timezone.utc))
        self.assertIsNone(plan[0]['publish_at'])

    def test_scheduled_api_request_is_private_with_future_publish_at(self):
        self.make_upload_folder()
        _,body,_=youtube.prepare_upload(self.root,'public',publish_at='2099-11-04T17:00:00+00:00')
        self.assertEqual(body['status']['privacyStatus'],'private')
        self.assertEqual(body['status']['publishAt'],'2099-11-04T17:00:00Z')

    def test_schedule_rejects_bad_settings(self):
        for values in ({'UPLOAD_VIDEOS_PER_SLOT':0},{'UPLOAD_TIMES':('25:00',)},
                       {'UPLOAD_MODE':'bad'},{'UPLOAD_VIDEOS_PER_SLOT':{'12:00':1}}):
            with self.upload_config(**values),self.assertRaises(ValueError):youtube.validate_upload_settings()

    def test_manual_receipt_reserves_its_slot_before_new_items(self):
        from datetime import datetime,timezone
        items=[{'digest':'new','folder':'new','receipt':{}},
               {'digest':'old','folder':'old','receipt':{'video_id':'existing','status':'complete','publish_at':'2026-11-04T17:00:00Z'}}]
        with self.upload_config(UPLOAD_VIDEOS_PER_SLOT=1):
            plan=youtube.plan_uploads(items,{'entries':{}},now=datetime(2026,11,3,15,tzinfo=timezone.utc))
        self.assertEqual(plan[0]['publish_at'],'2026-11-04T22:00:00+00:00')

    def test_upload_queue_resumes_without_duplicate_video_insert(self):
        from datetime import datetime,timezone
        self.make_upload_folder()
        ledger=self.root/'schedule.json'
        item={'digest':brain.video_identity(self.root/'final.mp4'),'folder':str(self.root),'receipt':{}}
        service=MagicMock()
        service.videos.return_value.insert.return_value.next_chunk.return_value=(None,{'id':'new_video'})
        with self.upload_config(),patch.object(config,'UPLOAD_SCHEDULE_FILE',ledger), \
             patch.object(youtube,'ready_uploads',return_value=[item]):
            self.assertEqual(youtube.upload_all(service=service,now=datetime(2099,11,3,15,tzinfo=timezone.utc)),1)
            self.assertEqual(youtube.upload_all(service=service,now=datetime(2099,11,3,15,tzinfo=timezone.utc)),0)
        self.assertEqual(service.videos.return_value.insert.call_count,1)
        status=service.videos.return_value.insert.call_args.kwargs['body']['status']
        self.assertEqual(status['privacyStatus'],'private')
        self.assertIn('publishAt',status)

    def test_disabled_delivery_never_authenticates(self):
        with patch.object(config,'UPLOAD_ENABLED',False),patch.object(youtube,'youtube_service') as auth:
            with self.assertRaises(ValueError):run.deliver_uploads({},lambda:None,MagicMock(),__import__('contextlib').nullcontext)
            auth.assert_not_called()

    def test_scheduled_video_thumbnail_retry_does_not_reinsert_or_reschedule(self):
        from PIL import Image
        video=self.make_upload_folder()
        image=self.root/'final_thumbnail.png';Image.new('RGB',(32,32)).save(image)
        (self.root/'youtube_upload.json').write_text(json.dumps({'video_sha256':brain.video_identity(video),
              'status':'video_uploaded','video_id':'existing','publish_at':'2000-01-01T17:00:00Z','thumbnail_status':'failed'}))
        service=MagicMock()
        service.videos.return_value.list.return_value.execute.return_value={'items':[{'processingDetails':{'processingStatus':'succeeded'}}]}
        service.thumbnails.return_value.set.return_value.execute.return_value={'items':[{'url':'accepted'}]}
        self.assertEqual(youtube.upload(self.root,publish_at='2000-01-01T17:00:00Z',service=service),'existing')
        service.videos.return_value.insert.assert_not_called()
        service.thumbnails.return_value.set.assert_called_once()

    def test_upload_only_skips_discovery_clipping_and_ai(self):
        with self.operator_settings(None),patch.object(config,'UPLOAD_ENABLED',True), \
             patch.object(run,'validate_settings'),patch.object(youtube,'validate_upload_settings'), \
             patch.object(run,'deliver_uploads') as deliver,patch.object(run,'source_jobs',side_effect=AssertionError('Unexpected source processing')), \
             patch.object(run,'Brain') as ai,patch.object(sys,'argv',['run.py','--upload-only']), \
             patch('sys.stdout',new_callable=__import__('io').StringIO):
            self.assertEqual(run.main(),0)
            deliver.assert_called_once()
            ai.return_value.judge_clip.assert_not_called()

    def test_upload_only_does_not_replace_an_unfinished_production_run(self):
        source=self.root/'source.mp4';source.write_bytes(b'source')
        (self.root/'data').mkdir()
        checkpoint=self.root/'data/job.json'
        job={'version':1,'status':'paused','history':{},'sources':[{'path':str(source),'digest':'abc'}]}
        checkpoint.write_text(json.dumps(job))
        with self.operator_settings(source),patch.object(config,'UPLOAD_ENABLED',True), \
             patch.object(youtube,'validate_upload_settings'),patch.object(run,'deliver_uploads') as deliver, \
             patch.object(sys,'argv',['run.py','--upload-only']),patch('sys.stdout',new_callable=__import__('io').StringIO):
            self.assertEqual(run.main(),2)
            deliver.assert_not_called()
        self.assertEqual(json.loads(checkpoint.read_text()),job)

    def test_whisperx_small_boundary_overshoot_is_clamped(self):
        raw=[{'word':'No.','start':30.989,'end':31.22,'speaker':'person'}]
        words=brain.normalize_transcript(raw,31.0,preserve_metadata=True)
        self.assertEqual(words,[{'text':'No.','start':30.989,'end':31.0,'speaker':'person'}])
        self.assertEqual(raw[0]['end'],31.22)
        self.assertEqual(brain.normalize_transcript(words,31.0),[{'text':'No.','start':30.989,'end':31.0}])

    def test_small_negative_start_is_clamped_and_outside_words_discarded(self):
        raw=[{'word':'before','start':-0.2,'end':-0.1},
             {'word':'edge','start':-0.1,'end':0.2},
             {'word':'after','start':3.05,'end':3.2}]
        self.assertEqual(brain.normalize_transcript(raw,3.0),[{'text':'edge','start':0.0,'end':0.2}])

    def test_explicit_vod_timeline_is_converted_and_windowed(self):
        raw={'timeline':'source','clip_start':368.0,'words':[
            {'word':'before','start':10.0,'end':11.0},
            {'word':'inside','start':371.695,'end':371.837},
            {'word':'No.','start':398.989,'end':399.22},
            {'word':'after','start':500.0,'end':501.0}]}
        words=brain.normalize_transcript(raw,31.0)
        self.assertEqual([w['text'] for w in words],['inside','No.'])
        self.assertAlmostEqual(words[0]['start'],3.695)
        self.assertEqual(words[-1]['end'],31.0)

    def test_large_offsets_are_not_guessed_or_silently_clamped(self):
        for raw in ([{'word':'wrong VOD','start':371.0,'end':372.0}],
                    [{'word':'bad end','start':30.0,'end':40.0}]):
            with self.assertRaises(brain.BrainError):brain.normalize_transcript(raw,31.0)
        with self.assertRaises(brain.BrainError):
            brain.normalize_transcript([{'word':'x','start':368.0,'end':369.0}],31.0,timeline='source')

    def test_corrupt_alignment_is_not_filtered_into_apparent_success(self):
        for start,end in ((1.0,1.0),(2.0,1.0),(float('nan'),1.0),(0.0,float('inf')),(None,1.0),('0',1.0)):
            with self.subTest(start=start,end=end),self.assertRaises(brain.BrainError):
                brain.normalize_transcript([{'word':'bad','start':start,'end':end}],3.0)

    def test_large_source_word_crossing_boundary_is_rejected(self):
        with self.assertRaises(brain.BrainError):
            brain.normalize_transcript([{'word':'bad','start':398.0,'end':410.0}],31.0,
                                       timeline='source',clip_start=368.0)

    def test_cached_caption_alignment_reuses_corrected_boundaries(self):
        source=self.root/'boundary_aligned_words.json'
        source.write_text(json.dumps([{'word':'No.','start':30.989,'end':31.22}]))
        original=source.read_bytes()
        captions=editor.CaptionEditor()
        with patch.object(captions,'transcribe',side_effect=AssertionError('Repeated transcription')), \
             patch.object(captions,'align_words',side_effect=AssertionError('Repeated alignment')):
            words=captions.prepare(self.root/'clip.mp4',self.root,prefix='boundary_',duration=31.0)
        self.assertEqual(words[-1]['end'],31.0)
        self.assertEqual(source.read_bytes(),original)

    def test_cached_large_mismatch_pauses_instead_of_realigning_silently(self):
        source=self.root/'wrong_aligned_words.json'
        source.write_text(json.dumps([{'word':'wrong','start':368.0,'end':369.0}]))
        captions=editor.CaptionEditor()
        with patch.object(captions,'transcribe') as transcribe,patch.object(captions,'align_words') as align:
            with self.assertRaises(brain.BrainError):
                captions.prepare(self.root/'clip.mp4',self.root,prefix='wrong_',duration=31.0)
            transcribe.assert_not_called();align.assert_not_called()

    def thumbnail_fixture(self):
        from PIL import Image
        video=self.make_upload_folder()
        Image.new('RGB',(32,48)).save(self.root/'final_thumbnail.png')
        service=MagicMock()
        service.videos.return_value.insert.return_value.next_chunk.return_value=(None,{'id':'existing'})
        service.videos.return_value.list.return_value.execute.return_value={'items':[{'processingDetails':{'processingStatus':'succeeded'}}]}
        service.thumbnails.return_value.set.return_value.execute.return_value={'items':[{'url':'accepted'}]}
        return video,service

    def test_thumbnail_is_set_only_after_processing_succeeds(self):
        _,service=self.thumbnail_fixture()
        events=[]
        def poll():
            state='processing' if not events else 'succeeded'
            events.append(state)
            return {'items':[{'processingDetails':{'processingStatus':state}}]}
        service.videos.return_value.list.return_value.execute.side_effect=poll
        def apply():
            events.append('thumbnail_set')
            return {'items':[{'url':'accepted'}]}
        service.thumbnails.return_value.set.return_value.execute.side_effect=apply
        with patch.object(youtube.time,'sleep'):
            self.assertEqual(youtube.upload(self.root,service=service),'existing')
        self.assertEqual(events,['processing','succeeded','thumbnail_set'])
        receipt=json.loads((self.root/'youtube_upload.json').read_text())
        self.assertTrue(receipt['thumbnail_processing_verified'])
        self.assertEqual(receipt['thumbnail_status'],'complete')
        self.assertNotIn('shorts_grid_verified',receipt)

    def test_processing_timeout_is_bounded_and_resume_does_not_reupload(self):
        _,service=self.thumbnail_fixture()
        image=self.root/'final_thumbnail.png';original=image.read_bytes()
        with patch.object(youtube,'wait_for_video_processing',side_effect=TimeoutError('still processing')) as wait, patch.object(youtube.time,'sleep'):
            self.assertEqual(youtube.upload(self.root,service=service),'existing')
        receipt=json.loads((self.root/'youtube_upload.json').read_text())
        self.assertEqual(receipt['status'],'complete')
        self.assertEqual(receipt['thumbnail_status'],'skipped')
        self.assertEqual(wait.call_count,3)
        self.assertEqual(receipt['thumbnail_attempts'],3)
        self.assertEqual(image.read_bytes(),original)
        service.thumbnails.assert_not_called()
        with patch.object(youtube,'youtube_service',side_effect=AssertionError('Unnecessary auth')):
            youtube.upload(self.root)
        self.assertEqual(service.videos.return_value.insert.call_count,1)

    def test_temporary_thumbnail_errors_retry_and_succeed(self):
        from googleapiclient.errors import HttpError
        _,service=self.thumbnail_fixture()
        service.thumbnails.return_value.set.return_value.execute.side_effect=[
            HttpError(SimpleNamespace(status=503,reason='unavailable'),b'{}'),{'items':[{'url':'ok'}]}]
        with patch.object(youtube.time,'sleep'):
            youtube.upload(self.root,service=service)
        receipt=json.loads((self.root/'youtube_upload.json').read_text())
        self.assertEqual(receipt['thumbnail_status'],'complete')
        self.assertEqual(receipt['thumbnail_attempts'],2)
        self.assertEqual(service.videos.return_value.insert.call_count,1)

    def test_unsupported_thumbnail_continues_batch_and_is_not_retried(self):
        from googleapiclient.errors import HttpError
        video,service=self.thumbnail_fixture()
        service.thumbnails.return_value.set.return_value.execute.side_effect=HttpError(
            SimpleNamespace(status=403,reason='forbidden'),b'{}')
        item={'digest':brain.video_identity(video),'folder':str(self.root),'receipt':{}}
        with self.upload_config(),patch.object(config,'UPLOAD_SCHEDULE_FILE',self.root/'queue.json'), patch.object(youtube,'ready_uploads',return_value=[item]):
            self.assertEqual(youtube.upload_all(service=service),1)
            item['receipt']=json.loads((self.root/'youtube_upload.json').read_text())
            self.assertEqual(youtube.upload_all(service=service),0)
        self.assertEqual(item['receipt']['status'],'complete')
        self.assertEqual(item['receipt']['thumbnail_status'],'skipped')
        self.assertEqual(service.thumbnails.return_value.set.call_count,1)
        self.assertEqual(service.videos.return_value.insert.call_count,1)
        self.assertEqual(json.loads((self.root/'queue.json').read_text())['entries'][item['digest']]['status'],'complete')

    def test_exhausted_temporary_thumbnail_failures_stop_across_resumes(self):
        from googleapiclient.errors import HttpError
        _,service=self.thumbnail_fixture()
        service.thumbnails.return_value.set.return_value.execute.side_effect=HttpError(
            SimpleNamespace(status=503,reason='unavailable'),b'{}')
        with patch.object(youtube.time,'sleep'):
            youtube.upload(self.root,service=service)
            youtube.upload(self.root,service=service)
        self.assertEqual(service.thumbnails.return_value.set.call_count,3)
        self.assertEqual(service.videos.return_value.insert.call_count,1)
        self.assertEqual(json.loads((self.root/'youtube_upload.json').read_text())['status'],'complete')

    def test_thumbnail_only_auth_failure_does_not_pause_batch(self):
        video,_=self.thumbnail_fixture()
        receipt={'video_sha256':brain.video_identity(video),'video_id':'existing','status':'video_uploaded','thumbnail_status':'failed'}
        (self.root/'youtube_upload.json').write_text(json.dumps(receipt))
        item={'digest':receipt['video_sha256'],'folder':str(self.root),'receipt':receipt}
        with self.upload_config(),patch.object(config,'UPLOAD_SCHEDULE_FILE',self.root/'queue.json'), patch.object(youtube,'ready_uploads',return_value=[item]), patch.object(youtube,'youtube_service',side_effect=RuntimeError('auth failed')):
            self.assertEqual(youtube.upload_all(),1)
        self.assertEqual(json.loads((self.root/'youtube_upload.json').read_text())['thumbnail_status'],'skipped')

    def test_saved_thumbnail_budget_survives_resume(self):
        video,service=self.thumbnail_fixture()
        (self.root/'youtube_upload.json').write_text(json.dumps({'video_sha256':brain.video_identity(video),
            'video_id':'existing','status':'video_uploaded','thumbnail_status':'failed','thumbnail_attempts':2}))
        with patch.object(youtube,'wait_for_video_processing',side_effect=TimeoutError('pending')) as wait:
            youtube.upload(self.root,service=service)
        self.assertEqual(wait.call_count,1)
        service.videos.return_value.insert.assert_not_called()
        self.assertEqual(json.loads((self.root/'youtube_upload.json').read_text())['thumbnail_status'],'skipped')

    def test_failed_or_unknown_processing_never_sets_thumbnail(self):
        for response in ({'items':[{'processingDetails':{'processingStatus':'failed'}}]},
                         {'items':[{'id':'existing'}]},{}):
            with self.subTest(response=response):
                service=MagicMock();service.videos.return_value.list.return_value.execute.return_value=response
                with self.assertRaises(RuntimeError):youtube.wait_for_video_processing(service,'existing',timeout=0)
                service.thumbnails.assert_not_called()

    def test_legacy_completed_thumbnail_is_preserved_on_existing_video(self):
        video,service=self.thumbnail_fixture()
        receipt={'video_sha256':brain.video_identity(video),'status':'complete',
                 'video_id':'existing','thumbnail_status':'complete'}
        (self.root/'youtube_upload.json').write_text(json.dumps(receipt))
        item={'digest':receipt['video_sha256'],'folder':str(self.root),'receipt':receipt}
        with self.upload_config(),patch.object(config,'UPLOAD_SCHEDULE_FILE',self.root/'queue.json'), \
             patch.object(youtube,'ready_uploads',return_value=[item]):
            self.assertEqual(youtube.upload_all(service=service),0)
            self.assertEqual(youtube.upload_all(service=service),0)
        service.videos.return_value.insert.assert_not_called()
        service.thumbnails.assert_not_called()
        service.videos.return_value.list.assert_not_called()

    def test_verified_thumbnail_receipt_is_reused_without_auth(self):
        video,service=self.thumbnail_fixture()
        (self.root/'youtube_upload.json').write_text(json.dumps({'video_sha256':brain.video_identity(video),
              'status':'complete','video_id':'existing','thumbnail_status':'complete','thumbnail_processing_verified':True}))
        with patch.object(youtube,'youtube_service',side_effect=AssertionError('Unnecessary auth')):
            self.assertEqual(youtube.upload(self.root),'existing')

    def test_empty_thumbnail_confirmation_is_not_marked_success(self):
        _,service=self.thumbnail_fixture()
        service.thumbnails.return_value.set.return_value.execute.return_value={'items':[]}
        self.assertEqual(youtube.upload(self.root,service=service),'existing')
        receipt=json.loads((self.root/'youtube_upload.json').read_text())
        self.assertEqual(receipt['status'],'complete')
        self.assertEqual(receipt['thumbnail_status'],'skipped')
        self.assertFalse(receipt.get('thumbnail_processing_verified',False))
        self.assertEqual(receipt['video_id'],'existing')

    def test_failed_scope_upgrade_preserves_existing_oauth_token(self):
        token=self.root/'token.json';token.write_text('original_token')
        (self.root/'client_secret_test.json').write_text('{}')
        old=MagicMock();old.has_scopes.return_value=False
        flow=MagicMock();flow.run_local_server.side_effect=RuntimeError('consent cancelled')
        with patch.multiple(config,YOUTUBE_TOKEN_FILE=token,TOKEN_RESOURCE_DIR=self.root,YOUTUBE_CLIENT_FILE=''), \
             patch('google.oauth2.credentials.Credentials.from_authorized_user_file',return_value=old) as load, \
             patch('google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file',return_value=flow) as consent:
            with self.assertRaises(RuntimeError):youtube.youtube_service()
        self.assertEqual(token.read_text(),'original_token')
        self.assertEqual(load.call_args.args,(str(token),))
        self.assertIn('https://www.googleapis.com/auth/youtube.readonly',consent.call_args.args[1])

    def test_cut_uses_preroll_and_output_duration_not_absolute_end(self):
        import clipper
        with patch.object(clipper.subprocess,'run') as command:
            clipper.cut_clip(self.root/'vod.mp4',89.0,120.0,self.root/'clip.mp4')
        args=command.call_args.args[0]
        self.assertNotIn('-to',args)
        self.assertEqual(args[args.index('-t')+1],'31.0')
        first=args.index('-ss');second=args.index('-ss',first+1)
        self.assertLess(first,args.index('-i'));self.assertGreater(second,args.index('-i'))
        self.assertEqual(float(args[first+1])+float(args[second+1]),89.0)

    def test_export_rejects_wrong_duration_and_leading_video_gap(self):
        import clipper
        for actual,video_duration,video_start in ((32,31,1),(31,30,1),(29,29,0),(60.2,60.2,0)):
            data={'format':{'duration':str(actual)},'streams':[{'codec_type':'video','duration':str(video_duration),'start_time':str(video_start)}]}
            response=SimpleNamespace(stdout=json.dumps(data))
            with patch.object(clipper.subprocess,'run',return_value=response),self.assertRaisesRegex(ValueError,'requested=.*actual=.*video_duration='):
                clipper.validate_export(self.root/'clip.mp4',89.0,120.0)

    def test_export_cache_reuses_selected_intervals_after_code_change(self):
        import clipper
        cache=self.root/'cache';cache.mkdir()
        opts={'some':'same settings'};run_id='sourcehash'
        (cache/'sourcehash_old_options.json').write_text(json.dumps(opts))
        output=cache/'sourcehash_old_segments.json';output.write_text('[[10, 41]]')
        output.with_suffix('.quality.json').write_text(json.dumps({'duration':100,'regions':[{'start':10,'end':41,'score':10,'prominence':5}]}))
        with patch.object(config,'CACHE_FOLDER',cache):
            self.assertEqual(clipper.existing_selection(opts,run_id,100),output)
            self.assertIsNone(clipper.existing_selection({'some':'changed'},run_id,100))

    def cleanup_fixture(self):
        cache=self.root/'cache';cache.mkdir();videos=self.root/'videos';videos.mkdir();clips=self.root/'clips';clips.mkdir()
        source=videos/'TTV_123.mp4';source.write_bytes(b'vod')
        other=videos/'TTV_456.mp4';other.write_bytes(b'pending other vod')
        raw=clips/'candidate.mp4';raw.write_bytes(b'raw')
        folder=self.root/'finished/clip';folder.mkdir(parents=True)
        (folder/'final.mp4').write_bytes(b'finished')
        brain.atomic_json(folder/'production.json',{'source_sha256':'b'*64})
        (folder/'youtube_upload.json').write_text('{"video_id":"keep"}')
        (cache/('a'*24+'_segments.json')).write_text('[]')
        (cache/('b'*24+'_base.mp4')).write_bytes(b'intermediate')
        (cache/('ai_'+'b'*24+'_judge.json')).write_text('{}')
        unrelated=cache/'unrelated_user_file.txt';unrelated.write_text('keep')
        job={'status':'finishing','sources':[{'digest':'a'*64,'path':str(source),'status':'complete',
             'candidates':[{'path':str(raw),'output':str(folder),'status':'complete'}]}],'history':{}}
        settings_patch=patch.multiple(config,CACHE_FOLDER=cache,VIDS_FOLDER=videos,CLIPS_FOLDER=clips,
                                      OUTPUT_DIR=self.root/'finished',CLEANUP_AFTER_SUCCESS=True)
        return job,settings_patch,source,raw,other,folder,unrelated

    def test_success_cleanup_deletes_only_owned_work_and_preserves_finished_outputs(self):
        job,settings_patch,source,raw,other,folder,unrelated=self.cleanup_fixture()
        with settings_patch,patch.object(run,'VideoEditor'):
            run.cleanup_successful_job(job,lambda:None)
            run.cleanup_successful_job(job,lambda:None)
        self.assertFalse(source.exists());self.assertFalse(raw.exists())
        self.assertTrue(other.exists());self.assertTrue(unrelated.exists())
        self.assertTrue((folder/'final.mp4').exists());self.assertTrue((folder/'youtube_upload.json').exists())
        self.assertEqual(job['cleanup']['status'],'complete')

    def test_paused_run_and_missing_finished_output_do_not_cleanup_inputs(self):
        job,settings_patch,source,raw,other,folder,unrelated=self.cleanup_fixture()
        with settings_patch,patch.object(run,'VideoEditor'):
            job['status']='paused';run.cleanup_successful_job(job,lambda:None)
            self.assertTrue(source.exists());self.assertTrue(raw.exists())
            job['status']='finishing';(folder/'final.mp4').unlink()
            with self.assertRaises(ValueError):run.cleanup_successful_job(job,lambda:None)
            self.assertTrue(source.exists());self.assertTrue(raw.exists())

    def test_cleanup_interruption_can_resume_idempotently(self):
        job,settings_patch,source,raw,other,folder,unrelated=self.cleanup_fixture()
        real_unlink=Path.unlink
        calls=[]
        def interrupted(path,*args,**kwargs):
            calls.append(path)
            if len(calls)==2:raise PermissionError('temporary failure')
            return real_unlink(path,*args,**kwargs)
        with settings_patch,patch.object(run,'VideoEditor'):
            with patch.object(Path,'unlink',interrupted),self.assertRaises(PermissionError):run.cleanup_successful_job(job,lambda:None)
            self.assertIn('files',job['cleanup'])
            run.cleanup_successful_job(job,lambda:None)
        self.assertEqual(job['cleanup']['status'],'complete')
        self.assertTrue((folder/'final.mp4').exists())

    def test_success_cleanup_does_not_run_when_upload_delivery_fails(self):
        source=self.root/'source.mp4';source.write_bytes(b'source')
        import clipper
        with self.operator_settings(source),patch.object(config,'UPLOAD_ENABLED',True),patch.object(run,'validate_settings'), \
             patch.object(clipper,'run_clipper',return_value=[]),patch.object(run,'deliver_uploads',side_effect=RuntimeError('upload failed')), \
             patch.object(run,'cleanup_successful_job') as cleanup,patch.object(run,'Brain'), \
             patch.object(sys,'argv',['run.py']),patch('sys.stdout',new_callable=__import__('io').StringIO):
            self.assertEqual(run.main(),2)
            cleanup.assert_not_called()
        self.assertTrue(source.exists())


if __name__ == '__main__':
    unittest.main()
