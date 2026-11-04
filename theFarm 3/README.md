# The Farm 3

A Python pipeline that turns long videos into captioned vertical highlights, with
validated AI editing plans, local rendering, generated thumbnail artwork, and
optional YouTube delivery. `run.py` is the production entry point.

This is an existing macOS application, not a turnkey hosted service. It currently
uses POSIX file locks (`fcntl`), macOS fonts, and separately configured Python
runtimes. Windows support has not been implemented or verified.

## Pipeline and source map

```text
Local video or discovery → download → VideoHighlighter candidate selection
  → Faster-Whisper transcription → WhisperX word alignment
  → Gemini: judge → edit plan → publication metadata → thumbnail plan
  → FFmpeg: framing/zoom/color → captions/title → effects/audio
  → ComfyUI thumbnail → optional YouTube upload/schedule → success cleanup
```

- `run.py`: checkpoints, stage orchestration, cached rendering, resume and cleanup.
- `config.py`: paths, sources, quality thresholds, styling, models and delivery settings.
- `finder.py` / `downloader.py`: YouTube/Twitch discovery and yt-dlp downloads.
- `clipper.py`: isolated VideoHighlighter worker, quality gates and verified exports.
- `brain.py`: four Gemini stages, prompts, schemas, validation, retries and caches.
- `editor.py`: vertical framing, bounded zoom, captions, title header and audio effects.
- `thumbnail.py`: ComfyUI workflow submission and recovery.
- `post_to_youtube.py`: OAuth, duplicate protection, optional thumbnails and scheduling.
- `console.py`: operator progress display. `tests/`: automated regression tests.
- `reauth_youtube.py`: optional legacy authorization maintenance utility, not a
  second production entry point. It requests upload scope only; the uploader may
  request consent again for the read scope needed to inspect processing status.

Candidate selection returns up to ten qualifying clips, each at most 60 seconds;
it does not fill a quota with weak moments. Transcripts and final word timings are
clip-relative. Each clip has its own content-based cache key. AI plans select only
supported effects; they do not render video. Audio-triggered shake and the opening
riser are configured independently of AI. `title.jpg` is an **in-video header**,
not the YouTube thumbnail.

## Installation

The Git root is the parent `The Farm` folder. Run application commands from its
`theFarm 3` subfolder. Keep this layout unless you update the paths in `config.py`:

```text
The Farm/                         # Git root
  .gitignore
  tokens/                         # private; OAuth files and ComfyUI API workflow
  .venv/                          # main application environment
  .venv-whisperx313/               # separate WhisperX environment
  theFarm 3/
    run.py, config.py, ...
    .env                          # private; copy .env.example
    VideoHighlighter/             # separate external source installation
      pipeline.py
      .venv-videohighlighter/
    editingMedia/                 # locally supplied, licensed assets
    media/, cache/, data/, finished/  # generated/private; not committed
```

1. Install Python and FFmpeg (`ffmpeg` **and** `ffprobe` must be on `PATH`). On
   macOS, Homebrew is one option: `brew install ffmpeg`. The working installation
   uses Python 3.14 for the main environment, 3.13 for WhisperX and 3.12 for
   VideoHighlighter. Other Python versions are not verified by this repository.
2. From the Git root, create the main environment, then install the application:

   ```bash
   python3.14 -m venv .venv
   cd "theFarm 3"
   ../.venv/bin/python -m pip install -r requirements.txt
   cp .env.example .env
   ```

   The manifest records the installed direct dependencies, not a complete lockfile
   for the ML environments. Package availability and compatible wheels must be
   checked for your interpreter and machine. Installation requires network access.
3. Install [VideoHighlighter](https://github.com/Aseiel/VideoHighlighter) separately
   following its source installation instructions. Configure `VIDEOHIGHLIGHTER_DIR`
   and `VIDEOHIGHLIGHTER_PYTHON`. The worker requires `pipeline.run_highlighter`,
   `pipeline.build_auto_segments`, and `modules.system.app_paths`, plus the
   dependency's YAML configuration. An arbitrary newer upstream version is not
   guaranteed compatible; preserve your known-working installation. Its own
   environment handles its ML dependencies, PyYAML, models and optional features.
4. Install [WhisperX](https://github.com/m-bain/whisperX) in its separate environment
   and set `EDITOR_WHISPERX_PYTHON`. The worker needs `load_audio`,
   `load_align_model` and `align`. Alignment models and Faster-Whisper weights may
   download on first use; prefetch them for offline operation. Keep this separate
   from the main and VideoHighlighter environments to avoid ML dependency conflicts.
5. For generated thumbnails, run [ComfyUI](https://github.com/comfyanonymous/ComfyUI)
   at `COMFYUI_URL` (default `http://127.0.0.1:8188`). Export your working workflow in
   **API JSON format** to `../tokens/basicImage.json`, or set `THUMBNAIL_WORKFLOW`.
   It must contain `KSampler`, linked positive `CLIPTextEncode`, `SaveImage`, and
   compatible installed models/nodes. The workflow and model weights are private
   local resources and are not shipped here.
6. Supply the assets below and confirm the fonts configured in `EDITOR_FONT` and
   `TITLE_FONT_PATHS`. Emoji rendering defaults to Apple Color Emoji. These fonts
   are OS resources and are not redistributed.

### Required local media

| Location inside `editingMedia/` | Purpose |
| --- | --- |
| `assets/title.jpg` | Required when `TITLE_ENABLED=True` |
| `assets/flashbang.mp3` | Sound when a flashbang effect is selected |
| `clipIntro/quickRiser.MP3` | Universal opening riser; validated at startup |
| `clipEffect/` | Random sound effects, including vine boom |
| `clipSongs/chill : uplift/` | Chill/upbeat background music |
| `clipSongs/sad/` | Sad background music |
| `clipSongs/mystery/` | Mystery background music |

Use your own licensed files. Required folders and filenames are case-sensitive on
some filesystems. Effects selected by AI require the corresponding local assets.
The unusual `chill : uplift` name is the existing on-disk convention.

## Credentials and configuration

Fill only the credentials needed for enabled features in `.env`:

- `GEMINI_API_KEY`: Gemini analysis/planning when `AI_ENABLED=True`.
- `YOUTUBE_API_KEY`: YouTube discovery, not YouTube uploads.
- `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET`: Twitch discovery.

Discovery loads the project `.env`, then the parent's `.env`, without overriding
existing environment variables. Gemini reads the environment or project `.env`.
Use environment variables or these ignored local files, never source literals.

YouTube uploads need an OAuth desktop client in `../tokens/client_secret*.json`
and a private `youtube_token.json`. Configure `YOUTUBE_CLIENT_FILE` if more than
one client exists. The uploader handles consent for upload and readonly scopes;
Google Cloud project/API settings, OAuth consent and channel permissions must be
configured by the operator. Do not commit OAuth files or generated receipts.

Review `config.py` **before running**. Existing production settings are preserved:
AI, discovery, generated thumbnails, scheduled uploads and success cleanup may be
enabled. For an initial local run, explicitly set `UPLOAD_ENABLED=False` and
`CLEANUP_AFTER_SUCCESS=False` if you want to retain all intermediates.

Useful settings include `PLATFORM`, `PICK_YT`, `PICK_TWITCH`, `SOURCE_MODE`,
`INPUT_VIDEO`, `USE_CLIPPER`, quality thresholds, caption fonts, effect volumes,
`EDITOR_SHAKE_THRESHOLD_DB`, `GENERATE_THUMBNAIL` and `COMFYUI_URL`. Zoom limits
live in `ZoomEditor.MAX_SCALE`. Gemini model IDs are configurable; the current
primary/fallback values reflect the working setup and availability depends on
account access. AI calls can incur charges; valid stage caches are reused.

Delivery is controlled by `UPLOAD_ENABLED`, `UPLOAD_MODE`,
`UPLOAD_IMMEDIATE_PRIVACY`, `UPLOAD_TIMEZONE`, `UPLOAD_TIMES` and
`UPLOAD_VIDEOS_PER_SLOT`. Scheduled mode allocates slots starting tomorrow in the
configured timezone (currently noon and 5 PM), persists reservations and uploads
privately with `publishAt`. Review these settings before enabling delivery.

## Usage

From `theFarm 3`, with dependencies, configuration and assets ready:

```bash
../.venv/bin/python run.py --help
../.venv/bin/python run.py
../.venv/bin/python run.py --input /path/to/video.mp4 --no-thumbnail
../.venv/bin/python run.py --resume
../.venv/bin/python run.py --upload-only
```

`--input` still uses configured clipping and upload settings. `--no-thumbnail`
skips image generation, not uploads. To process an already cut clip directly,
set `USE_CLIPPER=False` first. `--upload-only` requires uploads enabled and will
not replace an unfinished analysis run. `--reprocess` permits completed sources
again while reusing valid caches; it is not a cache reset.

The checkpoint in `data/production_run.json` records progress. Per-video
`production.json`, AI caches and YouTube receipts retain completed work. Normal
launch resumes unfinished work; `--resume` explicitly requests it. Keep these
files private. After code changes, quit the old process and restart so it loads
the updated code. An interactive retry only reloads normal configuration.

With `CLEANUP_AFTER_SUCCESS=True`, a successful run deletes managed downloaded
inputs and intermediate caches after all enabled stages/delivery finish. Final
outputs and delivery receipts remain. Failed or paused jobs retain recovery data.
The operator also removes stale partial files under `TEMP_RETENTION_HOURS`.
Do not use a production command as an installation smoke test.

## Troubleshooting

- **Missing dependency or asset:** check `config.py`, environment paths, fonts,
  required media, FFmpeg and the separately installed worker environments.
- **Alignment errors:** confirm the correct clip/cache pair. The editor bounds
  Whisper's padded sentence windows before alignment and validates word timings;
  unexplained large offsets still pause. Do not disable validation or subtract
  guessed VOD offsets.
- **Gemini failures:** check model access, credentials, billing/quota and stage
  diagnostics. Fix the cause and resume; completed stages need not run again.
- **ComfyUI pending:** restart the same server and resume the saved job. Do not
  erase submission state while a generation may still be running.
- **Thumbnail not displayed:** thumbnail failures are optional and have a saved
  retry limit. The image remains local for manual use. API acceptance is not proof
  of Shorts grid display; account eligibility and YouTube presentation can differ.
- **YouTube upload start rejected:** inspect the HTTP status/reason (for example,
  a quota or upload limit), fix it or wait, then resume. A rejected session start
  is retryable. A genuinely uncertain upload requires checking Studio before
  proceeding; never delete its receipt merely to force another upload.
- **Logs:** `data/farm.log` contains diagnostics and is ignored. Known credential
  values are redacted by the session logger, but review logs before sharing.

## Safe validation

Run from this folder. These checks do not start production work:

```bash
../.venv/bin/python -m compileall -q *.py tests
../.venv/bin/python -m unittest discover -s tests -v
../.venv/bin/python -m pip check
```

Tests mock external APIs, rendering and ML workers. Their asset/workflow fixtures
are temporary, not your private production resources. Caption/title tests still
use the configured macOS fonts. `compileall` creates ignored bytecode.

## Publishing and third-party rights

The parent `.gitignore` excludes private shared resources, local assets, generated
media, models, caches, checkpoints, logs and credentials. Ignore rules do not
remove files already tracked or erase Git history. Review `git status`, staged
content and all history before publishing. A lightweight pattern scan is not a
security guarantee; rotate any credential found in committed history.

Do not redistribute videos, music, sound effects, fonts, model weights or workflow
assets without the necessary rights. Respect platform terms, creator permissions,
API quotas and model licenses. VideoHighlighter's local license is AGPL-3.0;
FFmpeg licensing depends on its build, and other dependencies have their own
licenses. External installations are deliberately not vendored. Review licensing
obligations before distributing a combined application or service. No license for
The Farm's own code has been selected here; the owner must choose one before
claiming that this project is open source.
