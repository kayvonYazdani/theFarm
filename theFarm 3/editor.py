
"""The Farm 3: vertical formatting, synchronized zoom, color, and karaoke captions."""

import array
import json
import sys
import math
import random
import subprocess
import tempfile
from pathlib import Path

import config as settings


class ZoomEditor:
    # Maximum animated zoom: 1.20 = up to 20% closer. Use 1.0 to disable zoom.
    MAX_SCALE = 1.20

    def __init__(self):
        """Store nonoverlapping zoom animations shared by both video layers."""
        self.max_scale = float(self.MAX_SCALE)
        if not math.isfinite(self.max_scale) or not 1 <= self.max_scale <= 2:
            raise ValueError("MAX_SCALE must be between 1.0 and 2.0")
        self.events = []

    def add_zoom(self, start, end, from_scale=1.0, to_scale=1.2):
        """Add a smooth zoom, capping AI-requested scales at the configured limit."""
        start, end = float(start), float(end)
        from_scale, to_scale = float(from_scale), float(to_scale)
        if (not all(math.isfinite(value) for value in (start, end, from_scale, to_scale))
                or start < 0 or end <= start or from_scale < 1 or to_scale < 1):
            raise ValueError("Zoom times must be finite and ordered; scales must be >= 1.0")
        # Clamp both endpoints so consecutive AI zooms still join smoothly.
        from_scale = min(from_scale, self.max_scale)
        to_scale = min(to_scale, self.max_scale)
        if self.events:
            previous = self.events[-1]
            if start < previous["end"]:
                raise ValueError("Zoom events must not overlap or be out of order.")
            if abs(from_scale - previous["to_scale"]) > 0.001:
                raise ValueError("Next zoom must begin at the previous zoom's ending scale.")
        elif start > 0 and from_scale != 1.0:
            raise ValueError("A zoom starting after 0 seconds must begin at 1.0.")
        self.events.append(dict(start=start, end=end, from_scale=from_scale, to_scale=to_scale))

    def build_expression(self):
        """Make one time-based, smoothly eased FFmpeg zoom expression."""
        initial = self.events[0]["from_scale"] if self.events and self.events[0]["start"] == 0 else 1.0
        expression = str(initial)
        for event in self.events:
            progress = (f"clip((on/{settings.EDITOR_FPS}-{event['start']})/"
                        f"({event['end']}-{event['start']}),0,1)")
            eased = f"(3*pow({progress},2)-2*pow({progress},3))"
            expression += f"+({event['to_scale'] - event['from_scale']})*{eased}"
        # Enforce the limit in the actual render as well as when scheduling.
        return f"clip({expression},1.0,{self.max_scale})"

    def build_filter(self, width, height):
        """Zoom the image while keeping its output rectangle unchanged."""
        return (f"zoompan=z='{self.build_expression()}':"
                f"x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':"
                f"d=1:s={width}x{height}:fps={settings.EDITOR_FPS}")


class EffectsEditor:
    """Schedule flashes and trigger shake from the clip audio level."""

    FLASH_SECONDS = 0.65
    FLASH_VOLUME = 0.65
    VINE_BOOM_VOLUME = 0.65  # Volume multiplier for random clip effects.

    def __init__(self):
        self.music = None
        self.songs_path = Path(__file__).resolve().parent / "editingMedia" / "clipSongs"
        self.flash_times = []
        self.vine_boom_events = []
        self.clip_effects_path = (
            Path(__file__).resolve().parent / "editingMedia" / "clipEffect"
        )
        self.shake_enabled = bool(getattr(settings, "EDITOR_SHAKE_ENABLED", True))
        self.sound_path = (
            Path(__file__).resolve().parent
            / "editingMedia" / "assets" / "flashbang.mp3"
        )

    def flashbang(self, time):
        """Trigger a fixed white flash + flashbang.mp3 at a video timestamp."""
        time = float(time)
        if not math.isfinite(time) or time < 0:
            raise ValueError("Flashbang timestamp must be finite and nonnegative")
        # Avoid duplicate sounds/flashes from repeated AI decisions.
        if not any(abs(existing - time) < 0.001 for existing in self.flash_times):
            self.flash_times.append(time)
            self.flash_times.sort()

    def vine_boom(self, time):
        """Choose a random clipEffect sound once and schedule it at time (seconds)."""
        time = float(time)
        if not math.isfinite(time) or time < 0:
            raise ValueError("Vine boom timestamp must be finite and nonnegative")
        # Repeated AI decisions at the same timestamp keep the original selection.
        for event in self.vine_boom_events:
            if abs(event["time"] - time) < 0.001:
                return event["path"]
        if not self.clip_effects_path.is_dir():
            raise FileNotFoundError(f"Clip effects folder not found: {self.clip_effects_path}")
        extensions = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".aiff", ".aif", ".opus"}
        sounds = sorted(path for path in self.clip_effects_path.iterdir()
                        if path.is_file() and path.suffix.lower() in extensions)
        if not sounds:
            raise FileNotFoundError(f"No supported sound files in: {self.clip_effects_path}")
        sound = random.choice(sounds)
        self.vine_boom_events.append({"time": time, "path": sound})
        self.vine_boom_events.sort(key=lambda event: event["time"])
        return sound

    # Background music volume multiplier; lower values keep speech clearer.
    MUSIC_VOLUME = 0.12

    def background_music(self, mood):
        """Choose one random song for the whole clip: chill/upbeat, sad, or mystery."""
        mood = str(mood).strip().lower()
        aliases = {"chill": "chill/upbeat", "upbeat": "chill/upbeat",
                   "chill/upbeat": "chill/upbeat", "sad": "sad", "mystery": "mystery"}
        if mood not in aliases:
            raise ValueError("Music mood must be 'chill/upbeat', 'sad', or 'mystery'")
        mood = aliases[mood]
        if self.music is not None and self.music["mood"] == mood:
            return self.music["path"]
        folders = {"chill/upbeat": "chill : uplift", "sad": "sad", "mystery": "mystery"}
        folder = self.songs_path / folders[mood]
        if not folder.is_dir():
            raise FileNotFoundError(f"Music folder not found: {folder}")
        extensions = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".aiff", ".aif", ".opus"}
        songs = sorted(path for path in folder.iterdir()
                       if path.is_file() and path.suffix.lower() in extensions)
        if not songs:
            raise FileNotFoundError(f"No supported songs in: {folder}")
        song = random.choice(songs)
        self.music = {"mood": mood, "path": song}
        return song

    # Shake controls: digital RMS dBFS (0 = maximum; more negative = more sensitive).
    SHAKE_THRESHOLD_DB = -5.0
    SHAKE_PIXELS = 5
    SHAKE_SPEED = 2.0

    def shake(self):
        """Enable shake only while the clip audio meets SHAKE_THRESHOLD_DB."""
        self.shake_enabled = True

    def _audio_shake_events(self, input_video):
        """Measure stereo RMS every 20 ms; no fixed duration or release tail."""
        threshold = float(getattr(settings, "EDITOR_SHAKE_THRESHOLD_DB", self.SHAKE_THRESHOLD_DB))
        if not math.isfinite(threshold) or threshold > 0:
            raise ValueError("Shake threshold must be finite and <= 0 dBFS")
        # Stream 20 ms PCM windows; never hold the full decoded track in memory.
        events = []
        start = None
        offset = 0
        limit = 10 ** (threshold / 20)
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(input_video),
                "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1",
            ], stdout=subprocess.PIPE, stderr=errors)
            try:
                while True:
                    chunk = process.stdout.read(960 * 2 * 4)
                    if not chunk:
                        break
                    while len(chunk) < 960 * 2 * 4:
                        more = process.stdout.read(960 * 2 * 4 - len(chunk))
                        if not more: break
                        chunk += more
                    samples = array.array("f")
                    samples.frombytes(chunk[:len(chunk) // 8 * 8])
                    if sys.byteorder != "little": samples.byteswap()
                    frames = len(samples) // 2
                    if not frames: break
                    rms = max(math.sqrt(sum(v*v for v in samples[channel::2]) / frames) for channel in (0,1))
                    if rms >= limit and start is None:
                        start = offset / 48000
                    elif rms < limit and start is not None:
                        events.append((start,offset / 48000-start));start=None
                    offset += frames
                if process.wait():
                    raise RuntimeError("Audio-volume analysis failed; check the source audio stream")
            except BaseException:
                process.kill();process.wait();raise
            finally:
                process.stdout.close()
        if start is not None: events.append((start,offset / 48000-start))
        return events

    def render(self, input_video, output_video):
        """Apply flashes and shakes after captions; let every effect sound finish."""
        input_video = Path(input_video)
        output_video = Path(output_video)
        # Universal opening sound: never selected or controlled by the AI plan.
        intro_path = Path(settings.EDITOR_INTRO_SOUND)
        intro_volume = float(settings.EDITOR_INTRO_VOLUME)
        if not intro_path.is_file():
            raise FileNotFoundError(f"Opening riser sound not found: {intro_path}")
        if not math.isfinite(intro_volume) or intro_volume < 0:
            raise ValueError("EDITOR_INTRO_VOLUME must be finite and nonnegative")
        source_info = VideoEditor(input_video).info
        shake_events = self._audio_shake_events(input_video) if self.shake_enabled and source_info["has_audio"] else []
        if input_video.resolve() == output_video.resolve():
            raise ValueError("Effect input and output must be different files")
        if self.flash_times and not self.sound_path.is_file():
            raise FileNotFoundError(f"Flashbang MP3 not found: {self.sound_path}")

        # Full white immediately, followed by a smooth 0.65-second recovery.
        # Escape commas used inside FFmpeg's blend expression.
        pulses = [
            f"gte(T,{t})*lt(T,{t + self.FLASH_SECONDS})*"
            f"pow(1-(T-{t})/{self.FLASH_SECONDS},2)"
            for t in self.flash_times
        ]
        opacity = "min(1\\," + "+".join(f"({p})" for p in pulses) + ")"

        graph = []
        video_label = "[0:v]"
        if self.flash_times:
            graph += [
                "[0:v]format=gbrp,split=2[original][flash_base]",
                "[original][flash_base]"
                f"blend=all_expr='A+(255-A)*({opacity})',format=yuv420p[flashed]",
            ]
            video_label = "[flashed]"
        if shake_events:
            strength = float(getattr(settings, "EDITOR_SHAKE_PIXELS", self.SHAKE_PIXELS))
            speed = float(getattr(settings, "EDITOR_SHAKE_SPEED", self.SHAKE_SPEED))
            if not all(math.isfinite(v) and v >= 0 for v in (strength, speed)):
                raise ValueError("Shake strength and speed must be finite and nonnegative")
            margin = max(2, math.ceil(strength / 2) * 2)
            active = "+".join(
                f"gte(t,{start})*lt(t,{start + duration})"
                for start, duration in shake_events
            )
            # Overscan hides exposed edges; the original frame stays intact outside events.
            graph += [
                f"{video_label}split=2[steady][shake_source]",
                f"[shake_source]scale=iw+{2 * margin}:ih+{2 * margin},"
                f"crop=iw-{2 * margin}:ih-{2 * margin}:"
                f"x='{margin}+{strength}*sin(2*PI*{speed}*t)':"
                f"y='{margin}+{strength}*sin(2*PI*{speed * 1.37}*t)'[shaking]",
                f"[steady][shaking]overlay=0:0:enable='{active}'[shaken]",
            ]
            video_label = "[shaken]"
        # Extend the final frame only if a delayed sound outlasts the input video.
        # amix ends after the longest input, so the MP3 is never trimmed by the flash.
        graph.append(f"{video_label}tpad=stop_mode=clone:stop=-1,format=yuv420p[v]")
        if source_info["has_audio"]:
            graph.append("[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[dialogue]")
        else:
            graph.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={source_info['duration']}[dialogue]")
        inputs = ["-i", str(input_video)]
        audio_labels = ["[dialogue]"]
        sounds = [(0.0, intro_path, intro_volume)]
        sounds += [(time, self.sound_path, self.FLASH_VOLUME) for time in self.flash_times]
        sounds += [(event["time"], event["path"], self.VINE_BOOM_VOLUME)
                   for event in self.vine_boom_events]
        for index, (time, sound_path, volume) in enumerate(sounds, start=1):
            if not sound_path.is_file():
                raise FileNotFoundError(f"Effect sound not found: {sound_path}")
            inputs += ["-i", str(sound_path)]
            graph.append(
                f"[{index}:a]aresample=48000,"
                "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"volume={volume},"
                f"adelay={round(time * 1000)}:all=1[sound{index}]"
            )
            audio_labels.append(f"[sound{index}]")
        if self.music is not None:
            music_path = self.music["path"]
            if not music_path.is_file():
                raise FileNotFoundError(f"Music file not found: {music_path}")
            volume = float(self.MUSIC_VOLUME)
            if not math.isfinite(volume) or volume < 0:
                raise ValueError("MUSIC_VOLUME must be finite and nonnegative")
            probe = subprocess.run([
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=duration:format=duration", "-of", "json",
                str(input_video),
            ], capture_output=True, text=True, check=True)
            info = json.loads(probe.stdout)
            duration = info.get("streams", [{}])[0].get("duration")
            if duration in (None, "N/A"):
                duration = info["format"]["duration"]
            duration = float(duration)
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("Cannot determine the clip duration for music")
            index = len(sounds) + 1
            # Loop short songs and trim long ones; music never extends the clip.
            inputs += ["-stream_loop", "-1", "-i", str(music_path)]
            graph.append(
                f"[{index}:a]aresample=48000,"
                "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"atrim=duration={duration},asetpts=PTS-STARTPTS,"
                f"volume={volume}[music]"
            )
            audio_labels.append("[music]")
        graph.append(
            "".join(audio_labels)
            + f"amix=inputs={len(audio_labels)}:duration=longest:normalize=0,"
            "alimiter=limit=0.95[a]"
        )
        output_video.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            *inputs, "-filter_complex", ";".join(graph),
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-threads", str(settings.FFMPEG_THREADS),
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", "-shortest", str(output_video),
        ], check=True)
        return output_video


def apply_edit_plan(editor, plan, *, selected_assets=None):
    """Schedule validated decisions only. Shake remains controlled by configuration."""
    from brain import EditPlan, editor_capabilities
    plan = EditPlan.model_validate(plan.model_dump() if hasattr(plan, "model_dump") else plan,
                                  context={"duration": editor.info["duration"],
                                           "capabilities": editor_capabilities()})
    if editor.zoom.events or editor.effects.flash_times or editor.effects.vine_boom_events:
        raise ValueError("Apply an AI plan to a fresh VideoEditor to avoid duplicate edits")
    saved = selected_assets if selected_assets is not None else {}
    for event in plan.zooms:
        editor.zoom.add_zoom(event.start, event.end, event.from_scale, event.to_scale)
    for event in plan.effects:
        if event.kind == "flashbang":
            editor.effects.flashbang(event.time)
        elif event.kind == "vine_boom":
            key = f"vine_boom:{event.time}"
            if key in saved:
                path = Path(saved[key])
                if not path.is_file() or path.parent.resolve() != editor.effects.clip_effects_path.resolve():
                    raise FileNotFoundError("Saved clip-effect asset is missing or outside clipEffect")
                editor.effects.vine_boom_events.append({"time": event.time, "path": path})
            else:
                saved[key] = str(editor.effects.vine_boom(event.time))
    if plan.background_music:
        if "music" in saved:
            path = Path(saved["music"])
            folders = {"chill/upbeat": "chill : uplift", "sad": "sad", "mystery": "mystery"}
            expected = editor.effects.songs_path / folders[plan.background_music]
            if not path.is_file() or path.parent.resolve() != expected.resolve():
                raise FileNotFoundError("Saved music asset is missing or has the wrong category")
            editor.effects.music = {"mood": plan.background_music, "path": path}
        else:
            saved["music"] = str(editor.effects.background_music(plan.background_music))
    return saved


class VerticalFormatter:
    def __init__(self, width=1080, height=1920,
                 blur=settings.EDITOR_BACKGROUND_BLUR,
                 foreground_zoom=settings.EDITOR_FOREGROUND_ZOOM,
                 background_zoom=settings.EDITOR_BACKGROUND_ZOOM, position=0.5):
        """Configure the fixed vertical background and foreground composition."""
        self.width = width
        self.height = height
        self.blur = blur
        self.foreground_zoom = foreground_zoom
        self.background_zoom = background_zoom
        self.position = position

    def build_filter(self, zoom, video_info):
        """Build two time-synchronized layers with one shared zoom timeline."""
        if not 0 <= self.position <= 1 or not 0 <= self.blur <= 100:
            raise ValueError("Position must be 0–1 and blur must be 0–100.")
        if self.background_zoom < 1 or self.foreground_zoom < 1:
            raise ValueError("Base zooms cannot be less than 1.0.")
        source_width, source_height = video_info["width"], video_info["height"]
        scale = min(self.width / source_width, self.height / source_height)
        fg_width = max(2, round(source_width * scale * self.foreground_zoom / 2) * 2)
        fg_height = max(2, round(source_height * scale * self.foreground_zoom / 2) * 2)
        background_zoom_filter = zoom.build_filter(self.width, self.height)
        foreground_zoom_filter = zoom.build_filter(fg_width, fg_height)
        return (
            f"[0:v]setpts=PTS-STARTPTS,fps={settings.EDITOR_FPS},"
            f"split=2[background_source][foreground_source];"
            f"[background_source]"
            f"scale={self.width}:{self.height}:force_original_aspect_ratio=increase,"
            f"crop={self.width}:{self.height},"
            f"scale=trunc(iw*{self.background_zoom}/2)*2:"
            f"trunc(ih*{self.background_zoom}/2)*2,"
            f"crop={self.width}:{self.height},setsar=1,"
            f"gblur=sigma={self.blur},{background_zoom_filter}[background];"
            f"[foreground_source]scale={fg_width}:{fg_height},setsar=1,"
            f"{foreground_zoom_filter}[foreground];"
            f"[background][foreground]overlay=x=(W-w)/2:"
            f"y=(H-h)*{self.position}:shortest=1[vertical]"
        )


class FilterEditor:
    def __init__(self, saturation=settings.EDITOR_SATURATION,
                 contrast=settings.EDITOR_CONTRAST,
                 brightness=settings.EDITOR_BRIGHTNESS):
        """Configure final video color correction."""
        self.saturation = saturation
        self.contrast = contrast
        self.brightness = brightness

    def build_filter(self):
        """Apply color correction and convert to compatible MP4 pixel format."""
        return (f"[vertical]eq=saturation={self.saturation}:"
                f"contrast={self.contrast}:brightness={self.brightness},"
                f"format=yuv420p[final]")


class VideoEditor:
    _probe_cache = {}
    def __init__(self, video_path):
        """Load video information and editing components."""
        self.video_path = Path(video_path)
        if not self.video_path.is_file():
            raise FileNotFoundError(self.video_path)
        self.info = self.get_video_info()
        self.vertical = VerticalFormatter()
        self.zoom = ZoomEditor()
        self.filters = FilterEditor()
        self.effects = EffectsEditor()

    def get_video_info(self):
        """Read video size, frame rate, and duration with FFprobe."""
        stat = self.video_path.stat()
        key = (str(self.video_path.resolve()),stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,stat.st_ino)
        if key in self._probe_cache:
            return dict(self._probe_cache[key])
        result = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,width,height,r_frame_rate,duration",
            "-of", "json", str(self.video_path)
        ], capture_output=True, text=True, check=True, timeout=30)
        data = json.loads(result.stdout)
        stream = next(s for s in data["streams"] if s["codec_type"] == "video")
        numerator, denominator = map(int, stream["r_frame_rate"].split("/"))
        info = {"width": stream["width"], "height": stream["height"],
                "fps": numerator / denominator,
                "duration": float(stream["duration"] if stream.get("duration") not in (None,"N/A") else data["format"]["duration"]),
                "has_audio": any(s["codec_type"]=="audio" for s in data["streams"])}
        if not math.isfinite(info["duration"]) or info["duration"] <= 0 or denominator == 0:
            raise ValueError("Invalid video timing")
        if len(self._probe_cache) >= 64: self._probe_cache.clear()
        self._probe_cache[key] = dict(info)
        return info

    def render(self, output_path):
        """Export the zoomed, vertically formatted and color-corrected video."""
        output_path = Path(output_path)
        graph = ";".join((self.vertical.build_filter(self.zoom, self.info),
                          self.filters.build_filter()))
        subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(self.video_path),
            "-filter_complex", graph, "-map", "[final]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-threads", str(settings.FFMPEG_THREADS),
            "-r", str(settings.EDITOR_FPS), "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", "-shortest", str(output_path)
        ], check=True)
        return output_path


class CaptionEditor:
    _models = {}
    def __init__(self):
        """Configure transcription, alignment, and one-line karaoke captions."""
        self.model_name = settings.EDITOR_WHISPER_MODEL
        self.language = settings.EDITOR_WHISPER_LANGUAGE
        self.whisperx_python = Path(settings.EDITOR_WHISPERX_PYTHON)
        self.font_path = settings.EDITOR_FONT
        self.font_size = settings.EDITOR_CAPTION_SIZE
        # Vertical caption center: 0.0 = top, 0.5 = middle, 1.0 = bottom.
        self.position = float(settings.EDITOR_CAPTION_Y)
        # Additional pixel spacing beyond the font's ordinary space character.
        self.word_spacing = float(getattr(settings, "EDITOR_CAPTION_WORD_SPACING", 0))
        if not 0 <= self.position <= 1:
            raise ValueError("EDITOR_CAPTION_Y must be between 0.0 and 1.0")
        if self.word_spacing < 0:
            raise ValueError("EDITOR_CAPTION_WORD_SPACING must be >= 0")
        self.max_chars = settings.EDITOR_CAPTION_MAX_CHARS
        self.max_words = int(settings.EDITOR_CAPTION_MAX_WORDS)
        if self.max_words < 1:
            raise ValueError("EDITOR_CAPTION_MAX_WORDS must be at least 1")
        self.pause = settings.EDITOR_CAPTION_PAUSE
        self.max_width = settings.EDITOR_CAPTION_MAX_WIDTH
        self.color = settings.EDITOR_CAPTION_COLOR
        self.highlight = settings.EDITOR_CAPTION_HIGHLIGHT
        self.outline = settings.EDITOR_CAPTION_OUTLINE
        self.pop = settings.EDITOR_CAPTION_POP
        self.words = []
        self.phrases = []

    def transcribe(self, video_path):
        """Transcribe spoken sentences with Faster-Whisper."""
        from faster_whisper import WhisperModel
        if self.model_name not in self._models:
            self._models[self.model_name] = WhisperModel(self.model_name, device="cpu", compute_type="int8",
                                                       cpu_threads=settings.WHISPER_CPU_THREADS, num_workers=1)
        model = self._models[self.model_name]
        segments, _ = model.transcribe(str(video_path), language=self.language,
                                       vad_filter=True, beam_size=5,
                                       condition_on_previous_text=False)
        return [{"start": float(s.start), "end": float(s.end), "text": s.text.strip()}
                for s in segments if s.text.strip()]

    @staticmethod
    def _bounded_segments(segments, duration):
        """Remove Whisper's final 30-second window padding before forced alignment.

        WhisperX scales word positions using the declared sentence duration,
        even when its audio slice is shorter. Never pass padded intervals to it.
        """
        from brain import BrainError
        if not isinstance(segments, list):
            raise BrainError("Transcript segments must be a list")
        bounded = []
        padded_end = math.ceil(duration / 30.0) * 30.0 + 0.25
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                raise BrainError(f"Invalid transcript segment {index}")
            start, end, text = (segment.get(k) for k in ('start', 'end', 'text'))
            if (not isinstance(text, str) or not text.strip() or
                    any(isinstance(x, bool) or not isinstance(x, (int, float)) or
                        not math.isfinite(x) for x in (start, end)) or
                    start < 0 or end <= start or end > padded_end):
                raise BrainError(f"Invalid clip-relative transcript segment {index}: {start}–{end}")
            if start >= duration:
                continue
            bounded.append({**segment, 'end': min(end, duration)})
        return bounded

    def align_words(self, video_path, segments, cache, prefix="", duration=None):
        """Align spoken words using WhisperX installed in its separate Python venv."""
        if not self.whisperx_python.is_file():
            raise FileNotFoundError(f"WhisperX Python not found: {self.whisperx_python}")
        transcript_file = cache / f"{prefix}transcript.json"
        # A caller-supplied shared prefix must never mix different clips.
        from brain import BrainError, video_identity
        if Path(video_path).is_file():
            prefix = video_identity(video_path)[:24] + "_"
        aligned_file = cache / f"{prefix}aligned_words.json"
        duration = VideoEditor(video_path).info["duration"] if duration is None else duration
        segments = self._bounded_segments(segments, duration)
        from brain import atomic_json
        atomic_json(transcript_file, segments)
        worker = '''
import json
import sys
from pathlib import Path
import whisperx
video, transcript, output, language = sys.argv[1:5]
segments = json.loads(Path(transcript).read_text(encoding="utf-8"))
audio = whisperx.load_audio(video)
model, metadata = whisperx.load_align_model(language_code=language, device="cpu")
result = whisperx.align(segments, model, metadata, audio, "cpu", return_char_alignments=False)
temporary = Path(output).with_suffix(".tmp")
temporary.write_text(json.dumps(result["word_segments"], indent=2), encoding="utf-8")
temporary.replace(output)
'''
        subprocess.run([str(self.whisperx_python), "-c", worker, str(video_path),
                        str(transcript_file), str(aligned_file), self.language], check=True,
                       stdout=getattr(settings,"_LOG_STREAM",None),stderr=getattr(settings,"_LOG_STREAM",None))
        return self._load_aligned(aligned_file, duration=duration if duration is not None else VideoEditor(video_path).info["duration"])

    def _load_aligned(self, aligned_file, *, duration):
        """Validate raw cached words before filtering, and bound the caption timeline."""
        from brain import normalize_transcript
        raw = json.loads(Path(aligned_file).read_text(encoding="utf-8"))
        self.words = normalize_transcript(raw,duration,preserve_metadata=True)
        return self.words

    def prepare(self, video_path, cache, prefix="", duration=None):
        """Reuse existing aligned-word JSON or generate it from the input video."""
        cache = Path(cache)
        cache.mkdir(parents=True, exist_ok=True)
        # A caller-supplied shared prefix must never mix different clips.
        from brain import BrainError, video_identity
        if Path(video_path).is_file():
            prefix = video_identity(video_path)[:24] + "_"
        aligned_file = cache / f"{prefix}aligned_words.json"
        duration = VideoEditor(video_path).info["duration"] if duration is None else duration
        if aligned_file.is_file():
            try:
                words = self._load_aligned(aligned_file, duration=duration)
                print(f"Loaded {len(words)} aligned words from cache.")
                return words
            except BrainError:
                # Repair only the diagnosed padded-sentence failure. Unexplained
                # mismatches still pause, rather than concealing a wrong cache.
                saved = cache / f"{prefix}transcript.json"
                segments = json.loads(saved.read_text()) if saved.is_file() else None
                bounded = self._bounded_segments(segments, duration)
                if bounded == segments:
                    raise
                print("Rebuilding alignment with clip-bounded transcript segments.")
                return self.align_words(video_path, bounded, cache, prefix, duration=duration)
            except (ValueError,KeyError,TypeError):
                print("Cached alignment is invalid; rebuilding from the saved transcript.")
        print("Faster-Whisper: Transcribing...")
        transcript_file = cache / f"{prefix}transcript.json"
        try:
            segments = json.loads(transcript_file.read_text()) if transcript_file.is_file() else None
            if segments is not None and (not isinstance(segments,list) or any(not isinstance(x,dict) or not all(k in x for k in ('start','end','text')) for x in segments)):
                segments = None
        except (OSError,ValueError):
            segments = None
        if segments is None:
            segments = self.transcribe(video_path)
            from brain import atomic_json
            atomic_json(transcript_file,segments)
        segments = self._bounded_segments(segments, duration)
        if not segments:
            self.words = []
            from brain import atomic_json
            atomic_json(aligned_file,[])
            return []
        print("WhisperX: Aligning words...")
        words = self.align_words(video_path, segments, cache, prefix, duration=duration)
        print(f"Successfully aligned {len(words)} words.")
        return words

    def group_phrases(self):
        """Group words using the old Farm's pauses, punctuation, and speaker changes."""
        self.phrases = []
        phrase = []
        for word in sorted(self.words, key=lambda item: item["start"]):
            if phrase:
                gap = word["start"] - phrase[-1]["end"]
                length = len(" ".join(w["text"] for w in phrase + [word]))
                if (word.get("speaker") != phrase[-1].get("speaker")
                        or gap >= self.pause
                        or len(phrase) >= self.max_words
                        or length > self.max_chars):
                    self.phrases.append(phrase)
                    phrase = []
            phrase.append(word)
            if word["text"].rstrip('"”’').endswith((".", "?", "!", ",", ";", ":")):
                self.phrases.append(phrase)
                phrase = []
        if phrase:
            self.phrases.append(phrase)
        return self.phrases

    def smoothstep(self, value):
        """Ease a value between zero and one without harsh movement."""
        value = max(0.0, min(1.0, value))
        return value * value * (3 - 2 * value)

    def active_animation(self, seconds, word):
        """Animate only the spoken word with the old Farm's rise-and-settle pop."""
        duration = max(0.001, word["end"] - word["start"])
        progress = max(0.0, min(1.0, (seconds - word["start"]) / duration))
        peak = max(self.pop, 1.0) + 0.08
        if progress < 0.25:
            eased = self.smoothstep(progress / 0.25)
            return 1.0 + (peak - 1.0) * eased, -8 * eased
        if progress < 0.55:
            eased = self.smoothstep((progress - 0.25) / 0.30)
            return peak + (self.pop - peak) * eased, -8 * (1 - eased)
        return self.pop, 0

    def build_phrase_layouts(self):
        """Measure captions ONCE and freeze each word center to prevent sentence shifting."""
        from PIL import Image, ImageDraw, ImageFont
        self.group_phrases()
        draw = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
        layouts = []
        for phrase in self.phrases:
            size = self.font_size
            while size >= 16:
                font = ImageFont.truetype(str(self.font_path), size)
                gap = max(0.0, draw.textlength(" ", font=font)) + self.word_spacing
                widths = [draw.textlength(word["text"].upper(), font=font) for word in phrase]
                total = sum(widths) + gap * max(0, len(phrase) - 1)
                if total <= 1080 * self.max_width:
                    break
                size -= 2
            else:
                raise ValueError("Phrase too long for one line. Lower EDITOR_CAPTION_MAX_CHARS.")
            x = (1080 - total) / 2
            positioned = []
            for word, width in zip(phrase, widths):
                positioned.append({**word, "text": word["text"].upper(),
                                   "x": x + width / 2, "y": 1920 * self.position,
                                   "size": size})
                x += width + gap
            layouts.append({"start": phrase[0]["start"],
                            "end": max(word["end"] for word in phrase),
                            "words": positioned})
        return layouts

    def build_emoji_events(self, layouts):
        """Trigger the old Farm's emoji pops on matching spoken words."""
        import re
        # All trigger words are controlled from config.py. An empty dict disables triggers.
        mapping = {str(word).lower(): emoji for word, emoji in settings.EDITOR_EMOJI_WORDS.items()}
        min_gap = float(getattr(settings, "EDITOR_EMOJI_GAP", 2.5))
        last = -min_gap
        events = []
        for phrase in layouts:
            for word in phrase["words"]:
                token = re.sub("[^a-z]", "", word["text"].lower())
                emoji = mapping.get(token)
                if emoji and word["start"] - last >= min_gap:
                    events.append({**word, "emoji": emoji})
                    last = word["start"]
        return events

    def emoji_sprite(self, emoji):
        """Rasterize colored emoji glyphs once using the macOS emoji font."""
        from PIL import Image, ImageDraw, ImageFont
        font_path = getattr(settings, "EDITOR_EMOJI_FONT",
                            "/System/Library/Fonts/Apple Color Emoji.ttc")
        font = ImageFont.truetype(str(font_path), 160)
        sprite = Image.new("RGBA", (240, 240), (0, 0, 0, 0))
        ImageDraw.Draw(sprite).text((30, 30), emoji, font=font, embedded_color=True)
        bounds = sprite.getbbox()
        if bounds is None:
            raise RuntimeError(f"Cannot render emoji {emoji}; check EDITOR_EMOJI_FONT")
        return sprite.crop(bounds)

    def draw_emoji_effects(self, frame, seconds, events, sprites):
        """Pop emojis above their words, float them upward, and softly fade them out."""
        from PIL import Image, ImageFilter
        duration = float(getattr(settings, "EDITOR_EMOJI_DURATION", 1.0))
        target = int(getattr(settings, "EDITOR_EMOJI_SIZE", 125))
        for event in events:
            age = seconds - event["start"]
            if not 0 <= age < duration:
                continue
            progress = age / duration
            if progress < 0.18:
                scale = 0.3 + 0.88 * self.smoothstep(progress / 0.18)
            elif progress < 0.33:
                scale = 1.18 - 0.18 * self.smoothstep((progress - 0.18) / 0.15)
            else:
                scale = 1.0
            fade = self.smoothstep(max(0.0, (progress - 0.48) / 0.52))
            original = sprites[event["emoji"]]
            height = max(1, round(target * scale))
            width = max(1, round(height * original.width / original.height))
            sprite = original.resize((width, height), Image.Resampling.LANCZOS)
            sprite = sprite.filter(ImageFilter.GaussianBlur(2.5 * fade))
            sprite.putalpha(sprite.getchannel("A").point(lambda a: round(a * (1 - fade))))
            x = round(max(0, min(frame.width - width, event["x"] - width / 2)))
            y = round(max(0, min(frame.height - height,
                                 event["y"] - 34 - 85 * progress - height)))
            frame.alpha_composite(sprite, (x, y))

    def render_caption_frame(self, seconds, layout, font_cache):
        """Draw stationary white caption words and pop only the active word, without glow."""
        from PIL import Image, ImageDraw, ImageFont
        frame = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
        if layout is None:
            return frame
        draw = ImageDraw.Draw(frame)
        active = None
        for index, word in enumerate(layout["words"]):
            if word["start"] <= seconds < word["end"]:
                active = index
                break
        for index, word in enumerate(layout["words"]):
            size = word["size"]
            if size not in font_cache:
                font_cache[size] = ImageFont.truetype(str(self.font_path), size)
            # ALL words occupy their fixed position, whether currently active or not.
            if index != active:
                draw.text((word["x"], word["y"]), word["text"],
                          anchor="mm", font=font_cache[size], fill=self.color,
                          stroke_width=self.outline, stroke_fill="black")
        if active is not None:
            word = layout["words"][active]
            scale, lift = self.active_animation(seconds, word)
            size = max(1, round(word["size"] * scale))
            if size not in font_cache:
                font_cache[size] = ImageFont.truetype(str(self.font_path), size)
            draw.text((word["x"], word["y"] + lift), word["text"],
                      anchor="mm", font=font_cache[size], fill=self.highlight,
                      stroke_width=self.outline, stroke_fill="black")
        return frame

    def title_opacity(self, current_time: float) -> float:
        age = current_time - self.TITLE_DELAY
        if age < 0:
            return 0.0
        if age < self.TITLE_FADE_SECONDS:
            return self.smoothstep(age / self.TITLE_FADE_SECONDS)
        age -= self.TITLE_FADE_SECONDS
        if age < self.TITLE_HOLD_SECONDS:
            return 1.0
        return 1.0 - self.smoothstep(
            (age - self.TITLE_HOLD_SECONDS) / self.TITLE_FADE_SECONDS
        )

    def draw_title_overlay(
        self, frame, title, current_time: float
    ) -> None:
        """Stationary title drawn last, above video, camera frame, captions and emojis."""
        from PIL import Image, ImageChops, ImageFilter
        opacity = self.title_opacity(current_time)
        if opacity <= 0:
            return
        pulse = 0.5 - 0.5 * math.cos(
            2
            * math.pi
            * self.TITLE_GLOW_PULSE_HZ
            * max(0.0, current_time - self.TITLE_DELAY)
        )
        strength = self.TITLE_GLOW_STRENGTH * pulse
        rgb = title.convert("RGB")
        purple = Image.new("RGB", title.size, (135, 65, 220))
        layer = Image.blend(rgb, ImageChops.screen(rgb, purple), strength).convert(
            "RGBA"
        )
        layer.putalpha(title.getchannel("A"))
        padding = 24
        halo = Image.new(
            "RGBA",
            (title.width + 2 * padding, title.height + 2 * padding),
            (135, 65, 220, 0),
        )
        mask = Image.new("L", halo.size, 0)
        mask.paste(title.getchannel("A"), (padding, padding))
        mask = mask.filter(ImageFilter.GaussianBlur(10)).point(
            lambda a: round(a * strength * opacity)
        )
        halo.putalpha(mask)
        frame.alpha_composite(
            halo,
            (
                (self.OUTPUT_WIDTH - title.width) // 2 - padding,
                self.TITLE_VISIBLE_Y - padding,
            ),
        )
        layer.putalpha(layer.getchannel("A").point(lambda a: round(a * opacity)))
        frame.alpha_composite(
            layer, ((self.OUTPUT_WIDTH - layer.width) // 2, self.TITLE_VISIBLE_Y)
        )

    def prepare_title(self, title, folder, video_info):
        """Reuse Farm 2.2's branded video header; never modifies title.jpg."""
        from PIL import Image, ImageDraw, ImageFont
        self.TITLE_HEADER = Path(settings.TITLE_HEADER)
        if not self.TITLE_HEADER.is_file():
            raise FileNotFoundError(f"Legacy title header missing: {self.TITLE_HEADER}")
        self.OUTPUT_WIDTH = 1080
        self.TITLE_DELAY = min(max(0.0, settings.TITLE_DELAY), max(0.0, video_info["duration"] - 0.1))
        available = min(settings.TITLE_MAX_SECONDS, video_info["duration"] - self.TITLE_DELAY)
        if available <= 0 or settings.TITLE_FADE_SECONDS <= 0:
            raise ValueError("Title duration and fade must be positive")
        self.TITLE_FADE_SECONDS = min(settings.TITLE_FADE_SECONDS, available / 3)
        self.TITLE_HOLD_SECONDS = available - 2 * self.TITLE_FADE_SECONDS
        self.TITLE_GLOW_STRENGTH = settings.TITLE_GLOW_STRENGTH
        self.TITLE_GLOW_PULSE_HZ = settings.TITLE_GLOW_PULSE_HZ
        if not 0 <= self.TITLE_GLOW_STRENGTH <= 1 or self.TITLE_GLOW_PULSE_HZ < 0:
            raise ValueError("Invalid title glow settings")
        with Image.open(self.TITLE_HEADER) as source:
            image = source.convert("RGBA")
        header_height = round(image.height * 1080 / image.width)
        if settings.TITLE_VISIBLE_Y is None:
            scale = min(1080 / video_info["width"], 1920 / video_info["height"])
            height = round(video_info["height"] * scale * settings.EDITOR_FOREGROUND_ZOOM / 2) * 2
            video_top = max(0, (1920 - height) // 2)
            self.TITLE_VISIBLE_Y = max(0, video_top - header_height)
        else:
            self.TITLE_VISIBLE_Y = int(settings.TITLE_VISIBLE_Y)
        draw = ImageDraw.Draw(image)
        font_path = next((str(p) for p in settings.TITLE_FONT_PATHS if Path(p).is_file()), None)
        if font_path is None:
            raise FileNotFoundError("Legacy title font unavailable; set TITLE_FONT_PATHS")
        text = " ".join(str(title).split())
        if not text:
            raise ValueError("Title cannot be empty")
        max_width = image.width - settings.TITLE_TEXT_LEFT - settings.TITLE_TEXT_RIGHT
        for size in range(settings.TITLE_FONT_SIZE, settings.TITLE_MIN_FONT_SIZE - 1, -1):
            font = ImageFont.truetype(font_path, size)
            if draw.textlength(text, font=font) <= max_width:
                break
        # Preserve the legacy single-line treatment without clipping longer AI titles.
        if draw.textlength(text, font=font) > max_width:
            while text and draw.textlength(text + "…", font=font) > max_width:
                text = text[:-1]
            text = text.rstrip() + "…"
        draw.text((settings.TITLE_TEXT_LEFT, settings.TITLE_TEXT_Y), text, font=font, fill=settings.TITLE_TEXT_COLOR)
        output = Path(folder) / "title_header.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        image.save(output)
        return output


    def render_captions(self, input_video, output_video, video_info, cache, title_path=None):
        """Stream Pillow captions and emojis directly to FFmpeg, avoiding huge PNG caches."""
        from PIL import ImageFont
        title_image = None
        if title_path is not None:
            from PIL import Image
            with Image.open(title_path) as source:
                title_image = source.convert("RGBA")
            title_image = title_image.resize((1080, round(title_image.height * 1080 / title_image.width)), Image.Resampling.LANCZOS)
        layouts = self.build_phrase_layouts()
        emojis = self.build_emoji_events(layouts) if getattr(settings, "EDITOR_EMOJIS", True) else []
        sprites = {event["emoji"]: self.emoji_sprite(event["emoji"]) for event in emojis}
        cache = Path(cache)
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "emoji_events.json").write_text(
            json.dumps(emojis, indent=2, ensure_ascii=False), encoding="utf-8")
        frames = math.ceil(float(video_info["duration"]) * settings.EDITOR_FPS)
        output_video = Path(output_video)
        command = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgba", "-s", "1080x1920",
            "-r", str(settings.EDITOR_FPS), "-i", "pipe:0",
            "-i", str(input_video),
            "-filter_complex", "[1:v][0:v]overlay=0:0:format=auto:shortest=1[v]",
            "-map", "[v]", "-map", "1:a?", "-c:v", "libx264",
            "-preset", "fast", "-crf", "20", "-threads", str(settings.FFMPEG_THREADS), "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-movflags", "+faststart", str(output_video)
        ]
        print(f"Rendering {len(layouts)} fixed-position caption phrases and "
              f"{len(emojis)} emoji events...", flush=True)
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        if process.stdin is None:
            raise RuntimeError("Could not open FFmpeg caption stream")
        index = 0
        font_cache = {}
        try:
            for frame_index in range(frames):
                seconds = (frame_index + 0.5) / settings.EDITOR_FPS
                while index < len(layouts) and seconds >= layouts[index]["end"]:
                    index += 1
                layout = layouts[index] if (index < len(layouts) and
                       layouts[index]["start"] <= seconds < layouts[index]["end"]) else None
                frame = self.render_caption_frame(seconds, layout, font_cache)
                self.draw_emoji_effects(frame, seconds, emojis, sprites)
                if title_image is not None:
                    self.draw_title_overlay(frame, title_image, seconds)
                process.stdin.write(frame.tobytes())
                if frame_index % max(1, int(settings.EDITOR_FPS * 5)) == 0:
                    print(f"Captions: {seconds:.1f}s / {video_info['duration']:.1f}s", flush=True)
            process.stdin.close()
            error = process.stderr.read().decode("utf-8", errors="replace")
            code = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
        if code:
            raise RuntimeError(f"FFmpeg caption overlay failed: {error[-2000:]}")
        return output_video


if __name__ == "__main__":
    print("Start The Farm with: python run.py")
