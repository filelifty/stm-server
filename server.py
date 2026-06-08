"""
Save That Moment — Video Render Server
Runs on Render.com. Requires FFmpeg installed (see render.yaml / Dockerfile).
Supports photos AND video clips in the same timeline.
"""

import os
import uuid
import json
import math
import struct
import wave
import subprocess
import tempfile
import shutil
import threading
import time
from pathlib import Path
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename
from PIL import Image

app = Flask(__name__)
CORS(app)  # Open CORS — safe for static-asset site

# ── Config ──────────────────────────────────────────────────────
MAX_FILES       = 200
MAX_FILE_MB     = 100
MAX_VIDEO_SEC   = 20       # Trim video clips longer than this
JOBS_DIR        = Path(tempfile.gettempdir()) / 'stm_jobs'
OUTPUT_DIR      = Path(tempfile.gettempdir()) / 'stm_output'
JOBS_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
JOB_TTL_HOURS   = 2
FPS             = 25
VIDEO_W         = 1280
VIDEO_H         = 720
SAMPLE_RATE     = 44100

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.heic', '.gif', '.bmp', '.tiff'}
VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.m4v', '.wmv', '.3gp', '.webm'}

# ── Job store ────────────────────────────────────────────────────
jobs = {}
jobs_lock = threading.Lock()

def set_job(job_id, **kwargs):
    with jobs_lock:
        if job_id not in jobs:
            jobs[job_id] = {}
        jobs[job_id].update(kwargs)
        jobs[job_id]['updated_at'] = time.time()

def get_job(job_id):
    with jobs_lock:
        return dict(jobs.get(job_id, {}))


# ════════════════════════════════════════════════════════════════
# ROUTES
# ════════════════════════════════════════════════════════════════

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'ok': True, 'service': 'Save That Moment Render Server v2'})


@app.route('/render', methods=['POST'])
def start_render():
    try:
        if 'files' not in request.files:
            return jsonify({'error': 'No files provided'}), 400

        uploaded = request.files.getlist('files')
        if not uploaded or len(uploaded) > MAX_FILES:
            return jsonify({'error': f'Invalid file count'}), 400

        meta       = json.loads(request.form.get('meta', '{}'))
        mood       = meta.get('mood', 'cinematic')
        track      = meta.get('track', 'orchestral')
        title      = meta.get('title', 'My Moment')[:80]
        sequence   = meta.get('sequence', [])
        durations  = meta.get('durations', [])
        highlights = meta.get('highlights', [])

        job_id  = str(uuid.uuid4())
        job_dir = JOBS_DIR / job_id
        job_dir.mkdir()

        # Save all files
        saved_paths = []
        for f in uploaded:
            if not f.filename:
                continue
            ext      = Path(secure_filename(f.filename)).suffix.lower()
            filename = f'{len(saved_paths):04d}{ext}'
            dest     = job_dir / filename
            f.save(str(dest))
            if dest.stat().st_size > MAX_FILE_MB * 1024 * 1024:
                dest.unlink()
                continue
            saved_paths.append(dest)

        if not saved_paths:
            shutil.rmtree(job_dir, ignore_errors=True)
            return jsonify({'error': 'No valid files uploaded'}), 400

        # Apply colour-sort sequence from client
        if sequence and len(sequence) <= len(saved_paths):
            ordered = [saved_paths[i] for i in sequence if i < len(saved_paths)]
            in_seq  = set(sequence)
            ordered += [p for i, p in enumerate(saved_paths) if i not in in_seq]
            saved_paths = ordered

        set_job(job_id, status='queued', progress=0, stage='Queued',
                output_path=None, error=None, title=title, created_at=time.time())

        threading.Thread(
            target=render_job,
            args=(job_id, job_dir, saved_paths, mood, track, title, durations, highlights),
            daemon=True
        ).start()

        return jsonify({'job_id': job_id})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/status/<job_id>', methods=['GET'])
def job_status(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify({
        'status':   job.get('status', 'unknown'),
        'progress': job.get('progress', 0),
        'stage':    job.get('stage', ''),
        'error':    job.get('error'),
    })


@app.route('/download/<job_id>', methods=['GET'])
def download_file(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    if job.get('status') != 'done':
        return jsonify({'error': 'Not ready yet'}), 400
    output_path = job.get('output_path')
    if not output_path or not Path(output_path).exists():
        return jsonify({'error': 'Output file missing'}), 500

    title    = job.get('title', 'SaveThatMoment')
    filename = ''.join(c for c in title if c.isalnum() or c in ' _-').strip()[:60] + '.mp4'
    return send_file(output_path, mimetype='video/mp4',
                     as_attachment=True, download_name=filename)


# ════════════════════════════════════════════════════════════════
# RENDER ENGINE — Photos + Videos
# ════════════════════════════════════════════════════════════════

def render_job(job_id, job_dir, all_paths, mood, track, title, durations, highlights):
    try:
        set_job(job_id, status='running', progress=2, stage='Analysing files…')

        # ── Separate photos and videos ──
        photo_paths = [p for p in all_paths if p.suffix.lower() in IMAGE_EXTS]
        video_paths = [p for p in all_paths if p.suffix.lower() in VIDEO_EXTS]

        total_files = len(photo_paths) + len(video_paths)
        if total_files == 0:
            raise ValueError('No supported files found')

        clip_paths = []
        clip_index = 0

        # ── 1. Process each file in sequence order ──
        for i, src in enumerate(all_paths):
            ext = src.suffix.lower()
            pct = 5 + int((i / total_files) * 55)

            if ext in IMAGE_EXTS:
                # Photo → normalise + Ken Burns clip
                set_job(job_id, progress=pct,
                        stage=f'Rendering photo {clip_index+1}…')
                try:
                    norm = job_dir / f'norm_{clip_index:04d}.jpg'
                    normalise_image(src, norm)
                    dur_ms = durations[i] if i < len(durations) else get_slide_ms(i, highlights)
                    dur_sec = max(2.0, dur_ms / 1000)
                    clip = job_dir / f'clip_{clip_index:04d}.mp4'
                    render_photo_clip(norm, clip, dur_sec, clip_index, clip_index in highlights)
                    clip_paths.append(clip)
                    clip_index += 1
                except Exception as e:
                    print(f'Photo {i} failed: {e}')

            elif ext in VIDEO_EXTS:
                # Video → trim + re-encode to match spec
                set_job(job_id, progress=pct,
                        stage=f'Processing video clip {clip_index+1}…')
                try:
                    clip = job_dir / f'clip_{clip_index:04d}.mp4'
                    process_video_clip(src, clip)
                    clip_paths.append(clip)
                    clip_index += 1
                except Exception as e:
                    print(f'Video {i} failed: {e}')

        if not clip_paths:
            raise ValueError('No clips could be rendered')

        # ── 2. Select real music track ──
        total_sec = get_total_duration(clip_paths)
        set_job(job_id, progress=62, stage='Selecting music track…')

        music_path = select_music_track(mood, track, job_dir)
        if music_path is None:
            # Fallback to generated music if no files found
            music_path = job_dir / 'music.wav'
            generate_music(music_path, mood, track, max(total_sec, 5))

        # ── 3. Concatenate all clips ──
        set_job(job_id, progress=72, stage='Assembling your film…')

        concat_path = job_dir / 'concat.txt'
        with open(concat_path, 'w') as f:
            for clip in clip_paths:
                f.write(f"file '{clip.as_posix()}'\n")

        joined_path = job_dir / 'joined.mp4'
        run_ffmpeg([
            '-f', 'concat', '-safe', '0', '-i', str(concat_path),
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '20',
            '-pix_fmt', 'yuv420p', '-r', str(FPS),
            '-vf', f'scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease,'
                   f'pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2:black',
            str(joined_path)
        ])

        # ── 4. Mix music ──
        set_job(job_id, progress=86, stage='Mixing soundtrack…')

        output_path = OUTPUT_DIR / f'{job_id}.mp4'
        run_ffmpeg([
            '-i', str(joined_path),
            '-i', str(music_path),
            '-c:v', 'copy',
            '-c:a', 'aac', '-b:a', '192k',
            '-shortest',
            '-movflags', '+faststart',
            str(output_path)
        ])

        shutil.rmtree(job_dir, ignore_errors=True)
        set_job(job_id, status='done', progress=100,
                stage='Your film is ready', output_path=str(output_path))
        threading.Timer(JOB_TTL_HOURS * 3600, lambda: cleanup_output(job_id)).start()

    except Exception as e:
        set_job(job_id, status='error', error=str(e), stage='Failed')
        shutil.rmtree(job_dir, ignore_errors=True)


def normalise_image(src, dest):
    """Resize image to 1280x720, correct EXIF rotation, save as JPEG."""
    with Image.open(src) as img:
        img = img.convert('RGB')
        try:
            from PIL import ImageOps
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        img.thumbnail((VIDEO_W, VIDEO_H), Image.LANCZOS)
        bg = Image.new('RGB', (VIDEO_W, VIDEO_H), (0, 0, 0))
        bg.paste(img, ((VIDEO_W - img.width) // 2, (VIDEO_H - img.height) // 2))
        bg.save(str(dest), 'JPEG', quality=92)


def render_photo_clip(img_path, output_path, dur_sec, index, is_highlight):
    """
    Render a photo as a video clip with Ken Burns motion and fade transitions.
    Each clip gets a different motion effect for visual variety.
    """
    n_frames = max(int(dur_sec * FPS), FPS)

    # Ken Burns effects — (zoom_start, zoom_end, pan_x_start, pan_x_end, pan_y_start, pan_y_end)
    effects = [
        (1.0,  1.08,  0,      0,      0,      0     ),  # zoom in centre
        (1.08, 1.0,   0,      0,      0,      0     ),  # zoom out centre
        (1.06, 1.06, -0.03,   0.03,   0,      0     ),  # pan right
        (1.06, 1.06,  0.03,  -0.03,   0,      0     ),  # pan left
        (1.05, 1.10,  0.02,  -0.01,   0.01,  -0.01  ),  # zoom in + drift
        (1.10, 1.0,  -0.02,   0.01,  -0.01,   0.01  ),  # zoom out + drift
        (1.0,  1.06,  0,      0.02,   0.02,  -0.01  ),  # slow drift up-right
        (1.06, 1.0,   0.01,  -0.01,  -0.01,   0.02  ),  # slow drift down-left
    ]
    effect = (1.0, 1.14, 0, 0, 0, 0) if is_highlight else effects[index % len(effects)]
    zs, ze, pxs, pxe, pys, pye = effect

    # FFmpeg zoompan filter
    zoom_expr  = f"'min(max(zoom,{min(zs,ze)})+({ze}-{zs})/{n_frames},{max(zs,ze)})'"
    pan_x_expr = f"'iw/2-(iw/zoom/2)+({pxs}+({pxe}-{pxs})*on/{n_frames})*iw'"
    pan_y_expr = f"'ih/2-(ih/zoom/2)+({pys}+({pye}-{pys})*on/{n_frames})*ih'"

    zoompan = (
        f"zoompan=z={zoom_expr}:x={pan_x_expr}:y={pan_y_expr}"
        f":d={n_frames}:s={VIDEO_W}x{VIDEO_H}:fps={FPS}"
    )

    # Fade in on first clip, fade out on all clips for smooth dissolves
    fade_dur  = min(0.5, dur_sec * 0.15)
    fade_start = dur_sec - fade_dur
    vf = f"{zoompan},fade=t=out:st={fade_start:.3f}:d={fade_dur:.3f}"
    if index == 0:
        vf = f"fade=t=in:st=0:d=0.4,{vf}"

    run_ffmpeg([
        '-loop', '1', '-i', str(img_path),
        '-vf', vf,
        '-t', str(dur_sec),
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '20',
        '-pix_fmt', 'yuv420p', '-r', str(FPS),
        str(output_path)
    ])


def process_video_clip(src_path, output_path):
    """
    Re-encode a video clip to match the output spec:
    - Trim to MAX_VIDEO_SEC
    - Scale to 1280x720 with letterbox/pillarbox
    - Match FPS, codec, pixel format
    - Add fade in/out
    """
    # Get video duration
    duration = get_video_duration(src_path)
    trim_sec  = min(duration, MAX_VIDEO_SEC) if duration > 0 else MAX_VIDEO_SEC

    fade_dur   = min(0.4, trim_sec * 0.1)
    fade_start = trim_sec - fade_dur

    vf = (
        f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fps={FPS},"
        f"fade=t=in:st=0:d={fade_dur:.3f},"
        f"fade=t=out:st={fade_start:.3f}:d={fade_dur:.3f}"
    )

    run_ffmpeg([
        '-i', str(src_path),
        '-t', str(trim_sec),
        '-vf', vf,
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '20',
        '-pix_fmt', 'yuv420p',
        '-an',  # Remove original audio — music track replaces it
        str(output_path)
    ])


def get_video_duration(path):
    """Get video duration in seconds using ffprobe."""
    try:
        result = subprocess.run([
            'ffprobe', '-v', 'quiet',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            str(path)
        ], capture_output=True, text=True, timeout=30)
        return float(result.stdout.strip())
    except Exception:
        return 0


def get_total_duration(clip_paths):
    """Sum durations of all clips."""
    total = 0
    for clip in clip_paths:
        total += get_video_duration(clip)
    return total


def get_slide_ms(index, highlights):
    if index in highlights:
        return 5800
    return 3800 + (index % 3) * 300


def run_ffmpeg(args):
    cmd    = ['ffmpeg', '-y'] + args
    result = subprocess.run(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f'FFmpeg failed: {result.stderr.decode()[-600:]}')
    return result


def cleanup_output(job_id):
    job = get_job(job_id)
    if job.get('output_path'):
        try:
            Path(job['output_path']).unlink(missing_ok=True)
        except Exception:
            pass
    with jobs_lock:
        jobs.pop(job_id, None)


# ════════════════════════════════════════════════════════════════
# MUSIC GENERATOR
# ════════════════════════════════════════════════════════════════

MUSIC_CONFIGS = {
    'orchestral_cinematic': dict(bpm=72, vol=0.22, wave='sine',
        scale=[130.81,146.83,164.81,174.61,196,220,246.94,261.63],
        chords=[[0,2,4],[0,3,5],[1,3,5],[0,2,5]], atk=0.8, rel=2.5),
    'orchestral_warm': dict(bpm=68, vol=0.20, wave='sine',
        scale=[146.83,164.81,185,196,220,246.94,261.63,293.66],
        chords=[[0,2,4],[0,2,5],[1,3,5],[0,2,4]], atk=1.0, rel=3.0),
    'acoustic_warm': dict(bpm=85, vol=0.18, wave='triangle',
        scale=[329.63,369.99,392,440,493.88,523.25,587.33,659.26],
        chords=[[0,2,4],[0,3,4],[1,3,5],[0,2,5]], atk=0.04, rel=0.9),
    'acoustic_nostalgic': dict(bpm=78, vol=0.19, wave='triangle',
        scale=[246.94,261.63,293.66,329.63,349.23,392,440,493.88],
        chords=[[0,2,4],[0,2,5],[1,3,5],[2,4,6]], atk=0.05, rel=1.1),
    'electronic_adventure': dict(bpm=128, vol=0.10, wave='sawtooth',
        scale=[110,123.47,138.59,146.83,164.81,185,207.65,220],
        chords=[[0,3,5],[1,3,6],[0,2,5],[2,4,6]], atk=0.01, rel=0.12),
    'electronic_sport': dict(bpm=138, vol=0.09, wave='sawtooth',
        scale=[82.41,87.31,98,110,116.54,130.81,146.83,164.81],
        chords=[[0,2,5],[0,3,5],[1,3,6],[0,2,4]], atk=0.005, rel=0.08),
    'pop_party': dict(bpm=118, vol=0.11, wave='square',
        scale=[523.25,587.33,659.26,698.46,783.99,880,987.77,1046.5],
        chords=[[0,2,4],[0,3,5],[1,3,5],[0,2,5]], atk=0.02, rel=0.22),
    'ambient_chill': dict(bpm=55, vol=0.20, wave='sine',
        scale=[130.81,146.83,164.81,196,220,261.63,293.66,329.63],
        chords=[[0,2,4,6],[0,3,5,7],[1,3,5,7],[0,2,4,7]], atk=1.5, rel=4.5),
    'ambient_romantic': dict(bpm=52, vol=0.21, wave='sine',
        scale=[146.83,164.81,185,196,220,246.94,293.66,329.63],
        chords=[[0,2,4,6],[0,3,5,7],[1,3,5,7],[2,4,6,0]], atk=1.8, rel=5.0),
    'jazz_chill': dict(bpm=92, vol=0.17, wave='sine',
        scale=[261.63,311.13,329.63,369.99,392,440,466.16,493.88,523.25],
        chords=[[0,2,4,6],[0,3,5,7],[1,3,5,8],[0,2,5,7]], atk=0.04, rel=0.6),
}

def select_music_track(mood, track, job_dir):
    """
    Pick the best real MP3 track for the given mood/track combination.
    Files are stored alongside server.py in the same directory.
    Returns path to the selected MP3, or None if not found.
    """
    base = Path(__file__).parent

    # Mood → music file mapping
    MOOD_MAP = {
        'cinematic':  'music_cinematic.mp3',
        'adventure':  'music_adventure.mp3',
        'warm':       'music_warm.mp3',
        'romantic':   'music_warm.mp3',      # warm track fits romantic too
        'nostalgic':  'music_warm.mp3',      # warm track fits nostalgic
        'chill':      'music_chill.mp3',
        'ambient':    'music_chill.mp3',     # chill fits ambient
        'party':      'music_party.mp3',
        'sport':      'music_party.mp3',     # party energy fits sport
    }

    # Track genre → music file fallback
    TRACK_MAP = {
        'orchestral': 'music_cinematic.mp3',
        'acoustic':   'music_warm.mp3',
        'ambient':    'music_chill.mp3',
        'jazz':       'music_chill.mp3',
        'electronic': 'music_party.mp3',
        'pop':        'music_party.mp3',
    }

    # Try mood first, then track genre, then cinematic as default
    for filename in [
        MOOD_MAP.get(mood),
        TRACK_MAP.get(track),
        'music_cinematic.mp3',
    ]:
        if filename:
            path = base / filename
            if path.exists():
                return path

    return None



    key = f'{track}_{mood}'
    if key in MUSIC_CONFIGS:
        return MUSIC_CONFIGS[key]
    for k, v in MUSIC_CONFIGS.items():
        if k.startswith(track):
            return v
    return MUSIC_CONFIGS['ambient_chill']


def generate_tone_samples(freq, wave_type, num_samples):
    samples = []
    for i in range(num_samples):
        t     = i / SAMPLE_RATE
        phase = 2 * math.pi * freq * t
        if wave_type == 'sine':
            s = math.sin(phase)
        elif wave_type == 'triangle':
            s = 2 * abs(2 * (t * freq - math.floor(t * freq + 0.5))) - 1
        elif wave_type == 'sawtooth':
            s = 2 * (t * freq - math.floor(t * freq + 0.5))
        elif wave_type == 'square':
            s = 1.0 if math.sin(phase) >= 0 else -1.0
        else:
            s = math.sin(phase)
        samples.append(s)
    return samples


def apply_envelope(samples, atk_sec, rel_sec, volume):
    n     = len(samples)
    atk_n = min(int(atk_sec * SAMPLE_RATE), n)
    rel_n = min(int(rel_sec * SAMPLE_RATE), n)
    out   = []
    for i, s in enumerate(samples):
        if i < atk_n:
            env = i / max(atk_n, 1)
        elif i >= n - rel_n:
            env = (n - i) / max(rel_n, 1)
        else:
            env = 1.0
        out.append(s * env * volume)
    return out


def generate_music(output_path, mood, track, duration_sec):
    import random
    cfg        = get_music_config(mood, track)
    total_samp = int(SAMPLE_RATE * duration_sec)
    buffer     = [0.0] * total_samp

    beats_per_note = 3 if track == 'ambient' else (0.5 if track in ('electronic','pop') else 1)
    beat_sec       = (60 / cfg['bpm']) * beats_per_note

    t = 0.1; chord_idx = 0; note_in = 0
    random.seed(42)

    while t < duration_sec - 0.5:
        chord     = cfg['chords'][chord_idx % len(cfg['chords'])]
        scale_idx = chord[note_in % len(chord)]
        base_freq = cfg['scale'][scale_idx % len(cfg['scale'])]
        octave    = 2 if (random.random() > 0.75 and track != 'orchestral') else 1
        freq      = base_freq * octave * (1 + (random.random() - 0.5) * 0.002)

        note_dur  = cfg['atk'] + cfg['rel']
        note_samp = int(note_dur * SAMPLE_RATE)
        start_s   = int(t * SAMPLE_RATE)

        if start_s + note_samp <= total_samp:
            raw = generate_tone_samples(freq, cfg['wave'], note_samp)
            env = apply_envelope(raw, cfg['atk'], cfg['rel'], cfg['vol'])
            for j, s in enumerate(env):
                buffer[start_s + j] += s

            if track in ('orchestral', 'ambient', 'jazz') and note_in == 0:
                bass_freq = cfg['scale'][chord[0]] * 0.5
                bass_dur  = cfg['atk'] * 1.5 + cfg['rel'] * 1.1
                bass_samp = int(bass_dur * SAMPLE_RATE)
                if start_s + bass_samp <= total_samp:
                    raw_b = generate_tone_samples(bass_freq, 'sine', bass_samp)
                    env_b = apply_envelope(raw_b, cfg['atk']*1.5, cfg['rel']*1.1, cfg['vol']*0.55)
                    for j, s in enumerate(env_b):
                        buffer[start_s + j] += s

        note_in += 1
        if note_in >= len(chord) * 2:
            note_in = 0; chord_idx += 1
        t += beat_sec + (random.random() - 0.5) * beat_sec * 0.06

    # Fade out last 3 seconds
    fade_start = max(0, total_samp - int(3 * SAMPLE_RATE))
    for i in range(fade_start, total_samp):
        buffer[i] *= (total_samp - i) / (total_samp - fade_start)

    # Normalise
    peak = max(abs(s) for s in buffer) if buffer else 1.0
    if peak > 0.95:
        buffer = [s / peak * 0.92 for s in buffer]

    with wave.open(str(output_path), 'w') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        raw_bytes = struct.pack(f'<{len(buffer)}h',
                    *[int(max(-32768, min(32767, s * 32767))) for s in buffer])
        wf.writeframes(raw_bytes)


# ════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'Save That Moment render server v2 starting on port {port}')
    app.run(host='0.0.0.0', port=port, threaded=True)
