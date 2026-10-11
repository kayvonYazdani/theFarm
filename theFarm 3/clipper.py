"""Transcribe, score and select raw clips."""

from __future__ import annotations
import math, re, wave
from pathlib import Path
from typing import Any
import numpy as np
from faster_whisper import WhisperModel
import config as settings
from console import section


def clean_caption(text: str) -> str:
    """Normalize whitespace, capitalization and sentence-ending punctuation."""
    text = re.sub("\\s+", " ", text).strip()
    text = re.sub("\\s+([?.!,])", "\\1", text)
    if not text:
        return ""
    if text[-1] not in ".?!":
        text += "."
    return text[0].upper() + text[1:]


def short_caption(text: str, max_words: int = 12) -> str:
    """Produce a word-limited caption with an ellipsis when truncated."""
    words = clean_caption(text).split()
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]).rstrip(".,!?;") + "..."


def count_words(text: str) -> int:
    """Count whitespace-separated words in a transcript."""
    return len(text.split())


def repeated_letter_score(text: str) -> float:
    """Score exaggerated repeated letters as a possible reaction cue."""
    matches = re.findall("([a-zA-Z])\\1{2,}", text.lower())
    return float(min(len(matches) * 3, 12))


def uppercase_ratio(text: str) -> float:
    """Measure the fraction of alphabetic characters that are uppercase."""
    letters = [character for character in text if character.isalpha()]
    if not letters:
        return 0.0
    return sum((character.isupper() for character in letters)) / len(letters)


def get_duration(input_file: Path) -> float:
    """Read media duration in seconds using ffprobe."""
    result = settings.run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(input_file),
        ],
        capture_output=True,
    )
    return float(result.stdout.strip())


def extract_audio_once(input_file: Path, run_id: str) -> Path:
    """Extract one mono analysis track for all clip-detection passes on a source."""
    audio_file = (
        settings.CACHE_FOLDER
        / f"{input_file.stem}_{run_id}_{settings.SAMPLE_RATE}hz.wav"
    )
    settings.run_command(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(input_file),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(settings.SAMPLE_RATE),
            "-acodec",
            "pcm_s16le",
            str(audio_file),
        ]
    )
    return audio_file


def wav_to_numbers(audio_file: Path) -> np.ndarray:
    """Decode 16-bit PCM analysis audio into normalized floating-point samples."""
    with wave.open(str(audio_file), "rb") as wav_file:
        frames = wav_file.readframes(wav_file.getnframes())
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
    if audio.size == 0:
        return np.array([], dtype=np.float32)
    return audio / 32768.0


def build_rms(audio: np.ndarray) -> np.ndarray:
    """Compute frame-level RMS loudness for efficient window scoring."""
    frame_size = max(1, int(settings.SAMPLE_RATE * settings.RMS_FRAME_MS / 1000))
    if audio.size < frame_size:
        return np.array([], dtype=np.float32)
    frame_count = audio.size // frame_size
    frames = audio[: frame_count * frame_size].reshape(frame_count, frame_size)
    return np.sqrt(np.mean(frames * frames, axis=1) + 1e-12).astype(np.float32)


def rms_slice(rms: np.ndarray, start: float, end: float) -> np.ndarray:
    """Select RMS frames covering a requested time interval."""
    seconds_per_frame = settings.RMS_FRAME_MS / 1000
    first = max(0, int(start / seconds_per_frame))
    last = max(first + 1, int(end / seconds_per_frame))
    return rms[first : min(last, rms.size)]


def transcript_score(text: str) -> float:
    """Score transcript words and reaction cues for candidate selection."""
    normalized = text.lower().strip()
    words = normalized.split()
    score = sum(
        (
            normalized.count(phrase) * points
            for phrase, points in settings.HYPE_WORDS.items()
        )
    )
    if 0 < len(words) <= 12:
        score += 1
    if "?" in text:
        score += 2
    if "!" in text:
        score += 2
    if uppercase_ratio(text) > 0.6 and len(words) >= 2:
        score += 4
    score += repeated_letter_score(text)
    if (
        sum(
            (
                normalized.count(word)
                for word in ["what", "wait", "no", "nah", "bro", "stop"]
            )
        )
        >= 3
    ):
        score += 5
    return float(score)


def rms_volume_score(values: np.ndarray) -> float:
    """Score the loudness statistics of an audio window."""
    if values.size == 0:
        return 0.0
    mean_db = 20 * math.log10(float(np.mean(values)) + 1e-09)
    max_db = 20 * math.log10(float(np.max(values)) + 1e-09)
    score = 0
    if mean_db > -25:
        score += 2
    if mean_db > -18:
        score += 4
    if mean_db > -14:
        score += 6
    if max_db > -8:
        score += 3
    if max_db > -4:
        score += 6
    if max_db > -2:
        score += 8
    return float(score)


def text_laugh_score(text: str) -> float:
    """Score laughter cues found in the transcript."""
    normalized = text.lower().strip()
    score = sum((6 for cue in settings.LAUGHTER_TEXT_CUES if cue in normalized))
    ha_count = normalized.count("ha")
    if ha_count >= 2:
        score += min(ha_count * 2, 12)
    return float(score)


def audio_laugh_score(values: np.ndarray) -> float:
    """Estimate laughter-like activity from loudness patterns."""
    if values.size < 4:
        return 0.0
    mean_rms = float(np.mean(values))
    std_rms = float(np.std(values))
    max_rms = float(np.max(values))
    if mean_rms <= 0.002:
        return 0.0
    peaks = int(np.sum(values > mean_rms + std_rms * 0.75))
    differences = np.abs(np.diff(values))
    pulses = float(np.sum(differences > std_rms * 0.6)) if std_rms > 0 else 0
    score = 0
    if peaks >= 3:
        score += 4
    if peaks >= 5:
        score += 4
    if pulses >= 3:
        score += 4
    if pulses >= 6:
        score += 4
    if max_rms > 0.12:
        score += 2
    return float(score)


def make_clip_info(
    kind: str,
    start: float,
    end: float,
    center: float,
    text: str,
    text_score: float,
    volume_score: float,
    laughter_score: float,
) -> dict[str, Any]:
    """Assemble a candidate interval, component scores and combined ranking score."""
    everything_score = (
        text_score * settings.WORD_LIFT
        + volume_score * settings.SOUND_LIFT
        + laughter_score * settings.LAUGHTER_LIFT
    )
    score = {
        "everything": everything_score,
        "word": text_score,
        "volume": volume_score,
        "laughter": laughter_score,
    }[kind]
    return {
        "kind": kind,
        "start": float(start),
        "end": float(end),
        "center": float(center),
        "duration": float(max(0.1, end - start)),
        "text": text,
        "text_score": float(text_score),
        "volume_score": float(volume_score),
        "laughter_score": float(laughter_score),
        "everything_score": float(everything_score),
        "score": float(score),
        "word_count": count_words(text),
        "has_question": int("?" in text),
        "has_exclamation": int("!" in text),
        "uppercase_ratio": float(uppercase_ratio(text)),
        "repeated_letter_score": float(repeated_letter_score(text)),
    }


def limit_clip_length(clip: dict[str, Any]) -> dict[str, Any]:
    """Clamp a candidate interval to the configured maximum length."""
    if clip["end"] - clip["start"] <= settings.MAX_CLIP_DURATION:
        return clip
    center = clip.get("center", (clip["start"] + clip["end"]) / 2)
    clip["start"] = max(0.0, center - settings.MAX_CLIP_DURATION / 2)
    clip["end"] = clip["start"] + settings.MAX_CLIP_DURATION
    return clip


def merge_overlapping_clips(
    clips: list[dict[str, Any]], min_gap: float = 2.0
) -> list[dict[str, Any]]:
    """Combine overlapping candidate intervals before selection."""
    if not clips:
        return []
    merged = [dict(sorted(clips, key=lambda clip: clip["start"])[0])]
    for current_original in sorted(clips, key=lambda clip: clip["start"])[1:]:
        current = dict(current_original)
        last = merged[-1]
        can_merge = current["start"] <= last["end"] + min_gap
        too_long = (
            max(last["end"], current["end"]) - last["start"]
            > settings.MAX_CLIP_DURATION
        )
        if can_merge and (not too_long):
            old_start = last["start"]
            last["end"] = max(last["end"], current["end"])
            if current["score"] > last["score"]:
                replacement = dict(current)
                replacement["start"] = old_start
                replacement["end"] = last["end"]
                merged[-1] = replacement
        else:
            merged.append(current)
    return [limit_clip_length(clip) for clip in merged]


def dedupe_by_time(
    clips: list[dict[str, Any]], min_distance: float = 6.0
) -> list[dict[str, Any]]:
    """Remove candidates too close in time to already retained candidates."""
    selected: list[dict[str, Any]] = []
    for clip in sorted(clips, key=lambda item: item["score"], reverse=True):
        center = clip.get("center", (clip["start"] + clip["end"]) / 2)
        if all(
            (
                abs(center - other.get("center", (other["start"] + other["end"]) / 2))
                >= min_distance
                for other in selected
            )
        ):
            selected.append(clip)
    return selected


def transcribe_detection_audio(audio_file: Path) -> list[dict[str, Any]]:
    """Transcribe source audio for clip detection using faster-whisper."""
    print(f"Transcribing source · {settings.DETECTION_WHISPER_MODEL}")
    model = WhisperModel(
        settings.DETECTION_WHISPER_MODEL, device="auto", compute_type="auto"
    )
    segments_iterator, _ = model.transcribe(
        str(audio_file),
        language="en",
        vad_filter=True,
        beam_size=1,
        best_of=1,
        temperature=0,
        condition_on_previous_text=False,
        no_speech_threshold=0.35,
    )
    segments = [
        {
            "start": float(segment.start),
            "end": float(segment.end),
            "text": segment.text.strip(),
        }
        for segment in segments_iterator
        if segment.text.strip()
    ]
    print(f"Speech segments: {len(segments)}")
    return segments


def caption_from_segments(
    segments: list[dict[str, Any]], start: float, end: float
) -> str:
    """Collect transcript text overlapping a selected interval."""
    text = " ".join(
        (
            segment["text"]
            for segment in segments
            if max(0.0, min(end, segment["end"]) - max(start, segment["start"])) > 0
        )
    )
    return clean_caption(text)


def build_clip_label(clip: dict[str, Any]) -> str:
    """Build a human-readable label for a selected clip."""
    return {
        "everything": "Best overall moment",
        "word": "Word-based moment",
        "volume": "Loud audio spike",
        "laughter": "Laughter/reaction moment",
    }.get(clip["kind"], "Detected moment")


def build_reason_label(clip: dict[str, Any]) -> str:
    """Describe why the clip scored as a candidate."""
    reasons = []
    if clip["text_score"] >= settings.SPEECH_BENCH:
        reasons.append("strong words")
    if clip["volume_score"] >= settings.VOLUME_BENCH:
        reasons.append("audio spike")
    if clip["laughter_score"] >= settings.LAUGHTER_BENCH:
        reasons.append("laughter/reaction")
    return " + ".join(reasons or [clip["kind"]])


def cut_clip(input_file: Path, start: float, end: float, output_file: Path) -> None:
    """Export a selected source interval with FFmpeg."""
    settings.run_command(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(start),
            "-to",
            str(end),
            "-i",
            str(input_file),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-c:a",
            "aac",
            str(output_file),
        ]
    )


def run_clipper(video_file: Path, run_id: str) -> list[dict[str, Any]]:
    """Analyze a source, rank candidate intervals and export selected raw clips."""
    section("2 · Find clips")
    duration = get_duration(video_file)
    audio_file = extract_audio_once(video_file, run_id)
    rms = build_rms(wav_to_numbers(audio_file))
    segments = transcribe_detection_audio(audio_file)
    candidates: dict[str, list[dict[str, Any]]] = {
        "everything": [],
        "word": [],
        "volume": [],
        "laughter": [],
    }
    for segment in segments:
        text = segment["text"]
        segment_start = segment["start"]
        segment_end = segment["end"]
        center = (segment_start + segment_end) / 2
        start = max(0.0, segment_start - settings.PADDING_BEFORE)
        end = min(duration, segment_end + settings.PADDING_AFTER)
        text_score = transcript_score(text)
        volume_score = rms_volume_score(rms_slice(rms, segment_start, segment_end))
        laughter_score = text_laugh_score(text) + audio_laugh_score(
            rms_slice(rms, start, end)
        )
        everything = make_clip_info(
            "everything",
            start,
            end,
            center,
            text,
            text_score,
            volume_score,
            laughter_score,
        )
        if everything["everything_score"] >= settings.MIX_BENCH:
            candidates["everything"].append(everything)
        if text_score >= settings.SPEECH_BENCH:
            candidates["word"].append(
                make_clip_info(
                    "word",
                    start,
                    end,
                    center,
                    text,
                    text_score,
                    volume_score,
                    laughter_score,
                )
            )
        if laughter_score >= settings.LAUGHTER_BENCH:
            candidates["laughter"].append(
                make_clip_info(
                    "laughter",
                    start,
                    end,
                    center,
                    text,
                    text_score,
                    volume_score,
                    laughter_score,
                )
            )
    current = 0.0
    while current < duration:
        end = min(current + settings.VOLUME_WINDOW, duration)
        values = rms_slice(rms, current, end)
        volume_score = rms_volume_score(values)
        laughter_score = audio_laugh_score(values)
        if volume_score >= settings.VOLUME_BENCH:
            candidates["volume"].append(
                make_clip_info(
                    "volume",
                    max(0.0, current - settings.PADDING_BEFORE),
                    min(duration, end + settings.PADDING_AFTER),
                    (current + end) / 2,
                    "LOUD MOMENT",
                    0,
                    volume_score,
                    laughter_score,
                )
            )
        current += settings.VOLUME_STEP
    current = 0.0
    while current < duration:
        end = min(current + settings.LAUGHTER_WINDOW, duration)
        values = rms_slice(rms, current, end)
        laughter_score = audio_laugh_score(values)
        volume_score = rms_volume_score(values)
        if laughter_score >= settings.LAUGHTER_BENCH:
            candidates["laughter"].append(
                make_clip_info(
                    "laughter",
                    max(0.0, current - settings.PADDING_BEFORE),
                    min(duration, end + settings.PADDING_AFTER),
                    (current + end) / 2,
                    "AUDIO LAUGHTER MOMENT",
                    0,
                    volume_score,
                    laughter_score,
                )
            )
        current += settings.LAUGHTER_STEP
    limits = {
        "everything": settings.MIXED_CLIPS,
        "word": settings.SPEECH_CLIPS,
        "volume": settings.VOLUME_CLIPS,
        "laughter": settings.LAUGHTER_CLIPS,
    }
    selected: list[dict[str, Any]] = []
    for kind in ["everything", "word", "volume", "laughter"]:
        processed = dedupe_by_time(merge_overlapping_clips(candidates[kind]))[
            : limits[kind]
        ]
        selected.extend(processed)
    if not selected:
        print("No clips passed the current score thresholds.")
        return []
    exported: list[dict[str, Any]] = []
    for index, clip_original in enumerate(selected, start=1):
        clip = limit_clip_length(dict(clip_original))
        raw_clip = settings.CLIPS_FOLDER / f"{run_id}_{clip['kind']}_clip_{index}.mp4"
        print(f"Exporting raw clip {index}/{len(selected)}: {raw_clip.name}")
        cut_clip(video_file, clip["start"], clip["end"], raw_clip)
        clip["clip_id"] = f"{run_id}_{clip['kind']}_{index}"
        clip["raw_clip_file"] = raw_clip
        clip["clip_label"] = build_clip_label(clip)
        clip["reason_label"] = build_reason_label(clip)
        clip["caption_full"] = caption_from_segments(
            segments, clip["start"], clip["end"]
        )
        clip["caption_short"] = short_caption(clip["caption_full"])
        exported.append(clip)
    return exported
