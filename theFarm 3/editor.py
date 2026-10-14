
"""The Farm 3: vertical formatting, synchronized zoom, color, and karaoke captions."""

import json
import math
import subprocess
from pathlib import Path

import config as settings


class ZoomEditor:
    def __init__(self):
        """Store nonoverlapping zoom animations shared by both video layers."""
        self.events = []

    def add_zoom(self, start, end, from_scale=1.0, to_scale=1.2):
        """Add a smooth zoom transition that holds its ending scale."""
        if start < 0 or end <= start or not (1 <= from_scale <= 2 and 1 <= to_scale <= 2):
            raise ValueError("Invalid zoom times or scales (allowed: 1.0–2.0).")
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
        return expression

    def build_filter(self, width, height):
        """Zoom the image while keeping its output rectangle unchanged."""
        return (f"zoompan=z='{self.build_expression()}':"
                f"x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':"
                f"d=1:s={width}x{height}:fps={settings.EDITOR_FPS}")


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
    def __init__(self, video_path):
        """Load video information and editing components."""
        self.video_path = Path(video_path)
        if not self.video_path.is_file():
            raise FileNotFoundError(self.video_path)
        self.info = self.get_video_info()
        self.vertical = VerticalFormatter()
        self.zoom = ZoomEditor()
        self.filters = FilterEditor()

    def get_video_info(self):
        """Read video size, frame rate, and duration with FFprobe."""
        result = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,width,height,r_frame_rate",
            "-of", "json", str(self.video_path)
        ], capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        stream = next(s for s in data["streams"] if s["codec_type"] == "video")
        numerator, denominator = map(int, stream["r_frame_rate"].split("/"))
        return {"width": stream["width"], "height": stream["height"],
                "fps": numerator / denominator, "duration": float(data["format"]["duration"])}

    def render(self, output_path):
        """Export the zoomed, vertically formatted and color-corrected video."""
        output_path = Path(output_path)
        graph = ";".join((self.vertical.build_filter(self.zoom, self.info),
                          self.filters.build_filter()))
        subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-i", str(self.video_path),
            "-filter_complex", graph, "-map", "[final]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-r", str(settings.EDITOR_FPS), "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", "-shortest", str(output_path)
        ], check=True)
        return output_path


class CaptionEditor:
    def __init__(self):
        """Configure transcription, alignment, and one-line karaoke captions."""
        self.model_name = settings.EDITOR_WHISPER_MODEL
        self.language = settings.EDITOR_WHISPER_LANGUAGE
        self.whisperx_python = Path(settings.EDITOR_WHISPERX_PYTHON)
        self.font_path = settings.EDITOR_FONT
        self.font_size = settings.EDITOR_CAPTION_SIZE
        self.position = settings.EDITOR_CAPTION_Y
        self.max_chars = settings.EDITOR_CAPTION_MAX_CHARS
        self.max_words = settings.EDITOR_CAPTION_MAX_WORDS
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
        model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
        segments, _ = model.transcribe(str(video_path), language=self.language,
                                       vad_filter=True, beam_size=5,
                                       condition_on_previous_text=False)
        return [{"start": float(s.start), "end": float(s.end), "text": s.text.strip()}
                for s in segments if s.text.strip()]

    def align_words(self, video_path, segments, cache):
        """Align spoken words using WhisperX installed in its separate Python venv."""
        if not self.whisperx_python.is_file():
            raise FileNotFoundError(f"WhisperX Python not found: {self.whisperx_python}")
        transcript_file = cache / "transcript.json"
        aligned_file = cache / "aligned_words.json"
        transcript_file.write_text(json.dumps(segments, indent=2), encoding="utf-8")
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
Path(output).write_text(json.dumps(result["word_segments"], indent=2), encoding="utf-8")
'''
        subprocess.run([str(self.whisperx_python), "-c", worker, str(video_path),
                        str(transcript_file), str(aligned_file), self.language], check=True)
        return self._load_aligned(aligned_file)

    def _load_aligned(self, aligned_file):
        """Normalize WhisperX's word entries for caption rendering."""
        raw = json.loads(Path(aligned_file).read_text(encoding="utf-8"))
        self.words = [
            {"text": str(w.get("text", w.get("word", ""))).strip(),
             "start": float(w["start"]), "end": float(w["end"]),
             "speaker": w.get("speaker")}
            for w in raw
            if str(w.get("text", w.get("word", ""))).strip()
            and w.get("start") is not None and w.get("end") is not None
            and float(w["end"]) > float(w["start"])
        ]
        return self.words

    def prepare(self, video_path, cache):
        """Reuse existing aligned-word JSON or generate it from the input video."""
        cache = Path(cache)
        cache.mkdir(parents=True, exist_ok=True)
        aligned_file = cache / "aligned_words.json"
        if aligned_file.is_file():
            print("Loading cached WhisperX alignment...")
            words = self._load_aligned(aligned_file)
            print(f"Loaded {len(words)} aligned words.")
            return words
        print("Faster-Whisper: Transcribing...")
        segments = self.transcribe(video_path)
        if not segments:
            self.words = []
            print("No dialogue detected.")
            return []
        print("WhisperX: Aligning words...")
        words = self.align_words(video_path, segments, cache)
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
                gap = max(0.0, draw.textlength(" ", font=font))
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
        mapping = getattr(settings, "EDITOR_EMOJI_WORDS", {
            "bro": "💀", "crazy": "🤯", "insane": "🤯", "money": "💰",
            "love": "❤️", "fire": "🔥", "laugh": "😂", "wow": "😮",
            "mad": "😡", "sad": "😢", "goat": "🐐", "win": "🏆"})
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

    def render_captions(self, input_video, output_video, video_info, cache):
        """Stream Pillow captions and emojis directly to FFmpeg, avoiding huge PNG caches."""
        from PIL import ImageFont
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
            "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
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
                process.stdin.write(frame.tobytes())
                if frame_index % max(1, int(settings.EDITOR_FPS * 5)) == 0:
                    print(f"Captions: {seconds:.1f}s / {video_info['duration']:.1f}s", flush=True)
            process.stdin.close()
            error = process.stderr.read().decode("utf-8", errors="replace")
            code = process.wait()
        except Exception:
            process.kill()
            process.wait()
            raise
        if code:
            raise RuntimeError(f"FFmpeg caption overlay failed: {error[-2000:]}")
        return output_video

def main():
    """Render the test video and add karaoke captions using saved alignment."""
    desktop = Path.home() / "Desktop"
    source = desktop / "testTogi.mp4"
    cache = Path(__file__).resolve().parent / "cache" / "caption_test"

    editor = VideoEditor(source)
    # Add smooth zooms here later, for example:
    # editor.zoom.add_zoom(0.0, 0.5, from_scale=1.4, to_scale=1.0)
    # editor.zoom.add_zoom(5.0, 6.0, from_scale=1.0, to_scale=1.3)
    base_video = editor.render(desktop / "testTogi_edited.mp4")

    captions = CaptionEditor()
    captions.prepare(source, cache)
    result = captions.render_captions(
        base_video, desktop / "testTogi_captioned.mp4", editor.info, cache
    )
    print(f"Finished: {result}")


if __name__ == "__main__":
    main()