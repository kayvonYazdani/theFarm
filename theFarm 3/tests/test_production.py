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

REAL_ROOT = Path('/Users/kayvonyazdani/Desktop/The Farm/theFarm 3')
PUBLICATION = {'title': 'A Real Reaction', 'description': 'A reaction in the clip.', 'hashtags': ['#Reaction', '#Video', '#Shorts']}


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_title_asset_is_not_overwritten_and_legacy_style_is_reused(self):
        asset = REAL_ROOT / 'editingMedia/assets/title.jpg'
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

    def test_thumbnail_uncertain_submission_is_not_duplicated(self):
        (self.root/'thumbnail_status.json').write_text(json.dumps({'status':'submitting'}))
        workflow=REAL_ROOT.parent/'tokens/basicImage.json'
        with patch('urllib.request.urlopen') as network:
            with self.assertRaises(RuntimeError):
                thumbnail._generate_thumbnail(SimpleNamespace(thumbnail_prompt='actual scene'),workflow,
                                              'http://localhost:8188',self.root,self.root/'final.mp4')
            network.assert_not_called()

    def test_clipper_calls_existing_isolated_worker_and_reuses_selection(self):
        import clipper
        source=self.root/'long.mp4';source.write_bytes(b'source')
        cache=self.root/'cache';cache.mkdir()
        def worker(command,**kwargs):
            Path(command[4]).write_text('[[0, 2]]')
        def cut(src,start,end,out): Path(out).write_bytes(b'clip')
        with patch.multiple(config, CACHE_FOLDER=cache, CLIPS_FOLDER=self.root/'clips',
                            LEGACY_FARM_DIR=REAL_ROOT.parent/'[vault]/theFarm2.2',
                            VIDEOHIGHLIGHTER_DIR=REAL_ROOT/'VideoHighlighter',
                            VIDEOHIGHLIGHTER_PYTHON=REAL_ROOT/'VideoHighlighter/.venv-videohighlighter/bin/python'), \
             patch.object(clipper,'get_duration',return_value=5.0), patch.object(clipper,'cut_clip',side_effect=cut), \
             patch.object(clipper.subprocess,'run',side_effect=worker) as call:
            first=clipper.run_clipper(source,'sourcehash')
            second=clipper.run_clipper(source,'sourcehash')
            self.assertEqual(first,second)
            self.assertEqual(call.call_count,1)
            self.assertIn('highlighter_worker.py',call.call_args.args[0][1])


if __name__ == '__main__':
    unittest.main()
