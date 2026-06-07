"""
Save That Moment — Video Render Server
Runs on Render.com free tier. Requires FFmpeg installed (see render.yaml).
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
CORS(app)  # Allow requests from your Netlify site

# ── Config ──────────────────────────────────────────────────────
MAX_FILES      = 200          # Legacy plan max
MAX_FILE_MB    = 50           # Per file
JOBS_DIR       = Path(tempfile.gettempdir()) / 'stm_jobs'
JOBS_DIR.mkdir(exist_ok=True)
OUTPUT_DIR     = Path(tempfile.gettempdir()) / 'stm_output'
OUTPUT_DIR.mkdir(exist_ok=True)
JOB_TTL_HOURS  = 2            # Delete output files after 2 hours
FPS            = 25
VIDEO_W        = 1280
VIDEO_H        = 720
SAMPLE_RATE    = 44100


# ── Job store (in-memory, sufficient for MVP) ────────────────────
jobs = {}   # job_id -> { status, progress, stage, output_path, error }
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
    """Render.com health check"""
    return jsonify({'ok': True, 'service': 'Save That Moment Render Server'})


@app.route('/render', methods=['POST'])
def start_render():
    """
    Accept uploaded photos + metadata, start render job, return job_id.
    Client polls /status/<job_id> for progress.
    """
    try:
        # ── Validate files ──
        if 'files' not in request.files:
            return jsonify({'error': 'No files provided'}), 400

        uploaded = request.files.getlist('files')
        if not uploaded:
            return jsonify({'error': 'No files provided'}), 400
        if len(uploaded) > MAX_FILES:
            return jsonify({'error': f'Too many files (max {MAX_FILES})'}), 400

        # ── Read metadata ──
        meta_raw   = request.form.get('meta', '{}')
        meta       = json.loads(meta_raw)
        mood       = meta.get('mood', 'cinematic')
        track      = meta.get('track', 'orchestral')
        title      = meta.get('title', 'My Moment')[:80]
        sequence   = meta.get('sequence', [])       # Colour-sorted order from client
        durations  = meta.get('durations', [])      # Per-slide ms from Claude
        highlights = meta.get('highlights', [])     # Indices of highlight shots

        # ── Create job directory ──
        job_id  = str(uuid.uuid4())
        job_dir = JOBS_DIR / job_id
        job_dir.mkdir()

        # ── Save uploaded files ──
        saved_paths = []
        for f in uploaded:
            if f.filename == '':
                continue
            ext      = Path(secure_filename(f.filename)).suffix.lower()
            filename = f'{len(saved_paths):04d}{ext}'
            dest     = job_dir / filename
            f.save(str(dest))
            # Check size
            if dest.stat().st_size > MAX_FILE_MB * 1024 * 1024:
                dest.unlink()
                continue
            saved_paths.append(dest)

        if not saved_paths:
            shutil.rmtree(job_dir, ignore_errors=True)
            return jsonify({'error': 'No valid files were uploaded'}), 400

        # Apply sequence ordering from client colour sort
        if sequence and len(sequence) == len(saved_paths):
            ordered = []
            for idx in sequence:
                if 0 <= idx < len(saved_paths):
                    ordered.append(saved_paths[idx])
            # Append any not in sequence
            in_seq = set(sequence)
            for i, p in enumerate(saved_paths):
                if i not in in_seq:
                    ordered.append(p)
            saved_paths = ordered

        # Register job
        set_job(job_id,
            status='queued',
            progress=0,
            stage='Queued',
            output_path=None,
            error=None,
            title=title,
            created_at=time.time()
        )

        # Start render in background thread
        thread = threading.Thread(
            target=render_job,
            args=(job_id, job_dir, saved_paths, mood, track, title, durations, highlights),
            daemon=True
        )
        thread.start()

        return jsonify({'job_id': job_id})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/status/<job_id>', methods=['GET'])
def job_status(job_id):
    """Poll for render progress"""
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
    """Download the finished MP4"""
    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    if job.get('status') != 'done':
        return jsonify({'error': 'Not ready yet'}), 400
    output_path = job.get('output_path')
    if not output_path or not Path(output_path).exists():
        return jsonify({'error': 'Output file missing'}), 500

    title    = job.get('title', 'SaveThatMoment')
    filename = ''.join(c for c in title if c.isalnum() or c in ' _-').strip()
    filename = filename[:60] + '.mp4'

    return send_file(
        output_path,
        mimetype='video/mp4',
        as_attachment=True,
        download_name=filename
    )


# ════════════════════════════════════════════════════════════════
# RENDER ENGINE
# ════════════════════════════════════════════════════════════════

def render_job(job_id, job_dir, image_paths, mood, track, title, durations, highlights):
    """Main render pipeline — runs in background thread"""
    try:
        set_job(job_id, status='running', progress=2, stage='Preparing images…')

        # ── 1. Normalise images ──
        normalised = []
        for i, src in enumerate(image_paths):
            ext = src.suffix.lower()
            if ext not in ('.jpg', '.jpeg', '.png', '.webp', '.heic', '.gif'):
                continue  # skip videos for now
            try:
                dest = job_dir / f'norm_{i:04d}.jpg'
                normalise_image(src, dest)
                normalised.append(dest)
            except Exception:
                pass  # skip unreadable images

            pct = 2 + int((i / len(image_paths)) * 20)
            set_job(job_id, progress=pct, stage=f'Preparing image {i+1} of {len(image_paths)}…')

        if not normalised:
            raise ValueError('No readable image files found')

        set_job(job_id, progress=22, stage='Generating music track…')

        # ── 2. Generate music WAV ──
        total_sec = sum(
            (durations[i] if i < len(durations) else get_slide_ms(i, highlights)) / 1000
            for i in range(len(normalised))
        )
        music_path = job_dir / 'music.wav'
        generate_music(music_path, mood, track, total_sec)

        set_job(job_id, progress=34, stage='Rendering frames with Ken Burns…')

        # ── 3. Render each slide to a video clip with Ken Burns + transitions ──
        clip_paths = []
        for i, img_path in enumerate(normalised):
            dur_ms  = durations[i] if i < len(durations) else get_slide_ms(i, highlights)
            dur_sec = dur_ms / 1000
            clip    = job_dir / f'clip_{i:04d}.mp4'
            is_highlight = i in highlights

            render_slide_clip(img_path, clip, dur_sec, i, is_highlight)
            clip_paths.append(clip)

            pct = 34 + int((i / len(normalised)) * 40)
            set_job(job_id, progress=pct, stage=f'Rendering slide {i+1} of {len(normalised)}…')

        set_job(job_id, progress=74, stage='Joining clips…')

        # ── 4. Concatenate clips ──
        concat_path = job_dir / 'concat.txt'
        with open(concat_path, 'w') as f:
            for clip in clip_paths:
                f.write(f"file '{clip}'\n")

        joined_path = job_dir / 'joined.mp4'
        run_ffmpeg([
            '-f', 'concat', '-safe', '0', '-i', str(concat_path),
            '-c', 'copy',
            str(joined_path)
        ])

        set_job(job_id, progress=82, stage='Mixing audio…')

        # ── 5. Mix music into video ──
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

        set_job(job_id, progress=96, stage='Finishing up…')

        # ── 6. Clean up job dir to save disk ──
        shutil.rmtree(job_dir, ignore_errors=True)

        set_job(job_id, status='done', progress=100,
                stage='Your film is ready', output_path=str(output_path))

        # Schedule output file deletion after TTL
        threading.Timer(JOB_TTL_HOURS * 3600, lambda: cleanup_output(job_id)).start()

    except Exception as e:
        set_job(job_id, status='error', error=str(e), stage='Failed')
        shutil.rmtree(job_dir, ignore_errors=True)


def normalise_image(src, dest):
    """Resize and normalise image to 1280×720, JPEG"""
    with Image.open(src) as img:
        img = img.convert('RGB')
        # EXIF-aware rotation
        try:
            from PIL import ImageOps
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        # Fit into 1280×720 with black bars
        img.thumbnail((VIDEO_W, VIDEO_H), Image.LANCZOS)
        bg = Image.new('RGB', (VIDEO_W, VIDEO_H), (0, 0, 0))
        offset = ((VIDEO_W - img.width) // 2, (VIDEO_H - img.height) // 2)
        bg.paste(img, offset)
        bg.save(str(dest), 'JPEG', quality=92)


def render_slide_clip(img_path, output_path, dur_sec, index, is_highlight):
    """
    Render a single image as a video clip with Ken Burns zoom/pan effect.
    Each slide gets a different effect for visual variety.
    """
    n_frames = max(int(dur_sec * FPS), 1)

    # Ken Burns parameters — vary by slide index
    effects = [
        # (zoom_start, zoom_end, pan_x_start, pan_x_end, pan_y_start, pan_y_end)
        (1.0,  1.08,  0,      0,      0,      0     ),   # zoom in, centre
        (1.08, 1.0,   0,      0,      0,      0     ),   # zoom out, centre
        (1.06, 1.06, -0.03,   0.03,   0,      0     ),   # pan right
        (1.06, 1.06,  0.03,  -0.03,   0,      0     ),   # pan left
        (1.0,  1.08,  0.02,  -0.02,   0.01,  -0.01  ),   # zoom in + drift
        (1.08, 1.0,  -0.02,   0.02,  -0.01,   0.01  ),   # zoom out + drift
    ]
    if is_highlight:
        # Highlights get the slowest, most dramatic zoom
        effect = (1.0, 1.12, 0, 0, 0, 0)
    else:
        effect = effects[index % len(effects)]

    zs, ze, pxs, pxe, pys, pye = effect

    # Build zoompan filter
    # FFmpeg zoompan: z=zoom, x=pan_x, y=pan_y, d=duration_frames, s=output_size
    zoom_expr  = f"'if(eq(on,1),{zs},min(max(zoom,{min(zs,ze)}),{max(zs,ze)})+" \
                 f"({ze}-{zs})/{n_frames})'"
    pan_x_expr = f"'iw/2-(iw/zoom/2)+({pxs}+({pxe}-{pxs})*on/{n_frames})*iw'"
    pan_y_expr = f"'ih/2-(ih/zoom/2)+({pys}+({pye}-{pys})*on/{n_frames})*ih'"

    zoompan = (
        f"zoompan=z={zoom_expr}:"
        f"x={pan_x_expr}:"
        f"y={pan_y_expr}:"
        f"d={n_frames}:s={VIDEO_W}x{VIDEO_H}:fps={FPS}"
    )

    # Add crossfade transition at end of each clip (except potentially last)
    fade_frames = min(int(FPS * 0.6), n_frames // 4)  # 0.6s fade
    fade_start  = n_frames - fade_frames
    video_filter = (
        f"{zoompan},"
        f"fade=t=out:st={fade_start/FPS:.3f}:d={fade_frames/FPS:.3f}"
    )
    if index == 0:
        # Also fade in on first slide
        video_filter = f"fade=t=in:st=0:d=0.5,{video_filter}"

    run_ffmpeg([
        '-loop', '1', '-i', str(img_path),
        '-vf', video_filter,
        '-t', str(dur_sec),
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '20',
        '-pix_fmt', 'yuv420p',
        '-r', str(FPS),
        str(output_path)
    ])


def get_slide_ms(index, highlights):
    """Default slide duration if not provided"""
    if index in highlights:
        return 5800
    return 3800 + (index % 3) * 300


def run_ffmpeg(args):
    """Run an FFmpeg command, raise on failure"""
    cmd = ['ffmpeg', '-y'] + args
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300
    )
    if result.returncode != 0:
        raise RuntimeError(
            f'FFmpeg failed: {result.stderr.decode()[-500:]}'
        )
    return result


def cleanup_output(job_id):
    """Delete output file after TTL"""
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
# Produces a real WAV file using pure Python — no external library
# ════════════════════════════════════════════════════════════════

MUSIC_CONFIGS = {
    # (bpm, scale_hz, chord_degrees, wave, attack, release, volume)
    'orchestral_cinematic': dict(
        bpm=72, vol=0.22, wave='sine',
        scale=[130.81,146.83,164.81,174.61,196,220,246.94,261.63],
        chords=[[0,2,4],[0,3,5],[1,3,5],[0,2,5]],
        atk=0.8, rel=2.5
    ),
    'orchestral_warm': dict(
        bpm=68, vol=0.20, wave='sine',
        scale=[146.83,164.81,185,196,220,246.94,261.63,293.66],
        chords=[[0,2,4],[0,2,5],[1,3,5],[0,2,4]],
        atk=1.0, rel=3.0
    ),
    'acoustic_warm': dict(
        bpm=85, vol=0.18, wave='triangle',
        scale=[329.63,369.99,392,440,493.88,523.25,587.33,659.26],
        chords=[[0,2,4],[0,3,4],[1,3,5],[0,2,5]],
        atk=0.04, rel=0.9
    ),
    'acoustic_nostalgic': dict(
        bpm=78, vol=0.19, wave='triangle',
        scale=[246.94,261.63,293.66,329.63,349.23,392,440,493.88],
        chords=[[0,2,4],[0,2,5],[1,3,5],[2,4,6]],
        atk=0.05, rel=1.1
    ),
    'electronic_adventure': dict(
        bpm=128, vol=0.10, wave='sawtooth',
        scale=[110,123.47,138.59,146.83,164.81,185,207.65,220],
        chords=[[0,3,5],[1,3,6],[0,2,5],[2,4,6]],
        atk=0.01, rel=0.12
    ),
    'electronic_sport': dict(
        bpm=138, vol=0.09, wave='sawtooth',
        scale=[82.41,87.31,98,110,116.54,130.81,146.83,164.81],
        chords=[[0,2,5],[0,3,5],[1,3,6],[0,2,4]],
        atk=0.005, rel=0.08
    ),
    'pop_party': dict(
        bpm=118, vol=0.11, wave='square',
        scale=[523.25,587.33,659.26,698.46,783.99,880,987.77,1046.5],
        chords=[[0,2,4],[0,3,5],[1,3,5],[0,2,5]],
        atk=0.02, rel=0.22
    ),
    'ambient_chill': dict(
        bpm=55, vol=0.20, wave='sine',
        scale=[130.81,146.83,164.81,196,220,261.63,293.66,329.63],
        chords=[[0,2,4,6],[0,3,5,7],[1,3,5,7],[0,2,4,7]],
        atk=1.5, rel=4.5
    ),
    'ambient_romantic': dict(
        bpm=52, vol=0.21, wave='sine',
        scale=[146.83,164.81,185,196,220,246.94,293.66,329.63],
        chords=[[0,2,4,6],[0,3,5,7],[1,3,5,7],[2,4,6,0]],
        atk=1.8, rel=5.0
    ),
    'jazz_chill': dict(
        bpm=92, vol=0.17, wave='sine',
        scale=[261.63,311.13,329.63,369.99,392,440,466.16,493.88,523.25],
        chords=[[0,2,4,6],[0,3,5,7],[1,3,5,8],[0,2,5,7]],
        atk=0.04, rel=0.6
    ),
}

def get_music_config(mood, track):
    key = f'{track}_{mood}'
    if key in MUSIC_CONFIGS:
        return MUSIC_CONFIGS[key]
    # Fallback: find any config for this track
    for k, v in MUSIC_CONFIGS.items():
        if k.startswith(track):
            return v
    return MUSIC_CONFIGS['ambient_chill']


def generate_tone_samples(freq, wave_type, num_samples):
    """Generate raw samples for one oscillator note"""
    samples = []
    for i in range(num_samples):
        t = i / SAMPLE_RATE
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
    """Apply ADSR-style attack/release envelope"""
    n      = len(samples)
    atk_n  = min(int(atk_sec * SAMPLE_RATE), n)
    rel_n  = min(int(rel_sec * SAMPLE_RATE), n)
    out    = []
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
    """Generate a full music WAV file for the given duration"""
    cfg        = get_music_config(mood, track)
    total_samp = int(SAMPLE_RATE * duration_sec)
    buffer     = [0.0] * total_samp

    beats_per_note = 3 if track == 'ambient' else (0.5 if track in ('electronic','pop') else 1)
    beat_sec       = (60 / cfg['bpm']) * beats_per_note

    t         = 0.1  # start offset
    chord_idx = 0
    note_in   = 0
    import random
    random.seed(42)  # reproducible

    while t < duration_sec - 0.5:
        chord     = cfg['chords'][chord_idx % len(cfg['chords'])]
        scale_idx = chord[note_in % len(chord)]
        base_freq = cfg['scale'][scale_idx % len(cfg['scale'])]
        octave    = 2 if (random.random() > 0.75 and track != 'orchestral') else 1
        freq      = base_freq * octave * (1 + (random.random() - 0.5) * 0.002)

        note_dur   = cfg['atk'] + cfg['rel']
        note_samp  = int(note_dur * SAMPLE_RATE)
        start_samp = int(t * SAMPLE_RATE)

        if start_samp + note_samp <= total_samp:
            raw = generate_tone_samples(freq, cfg['wave'], note_samp)
            env = apply_envelope(raw, cfg['atk'], cfg['rel'], cfg['vol'])
            for j, s in enumerate(env):
                buffer[start_samp + j] += s

            # Bass note on chord root for orchestral/ambient/jazz
            if track in ('orchestral', 'ambient', 'jazz') and note_in == 0:
                bass_freq = cfg['scale'][chord[0]] * 0.5
                bass_dur  = cfg['atk'] * 1.5 + cfg['rel'] * 1.1
                bass_samp = int(bass_dur * SAMPLE_RATE)
                if start_samp + bass_samp <= total_samp:
                    raw_b = generate_tone_samples(bass_freq, 'sine', bass_samp)
                    env_b = apply_envelope(raw_b, cfg['atk'] * 1.5, cfg['rel'] * 1.1, cfg['vol'] * 0.55)
                    for j, s in enumerate(env_b):
                        buffer[start_samp + j] += s

        note_in += 1
        if note_in >= len(chord) * 2:
            note_in = 0
            chord_idx += 1
        t += beat_sec + (random.random() - 0.5) * beat_sec * 0.06

    # Fade out last 3 seconds
    fade_start = max(0, total_samp - int(3 * SAMPLE_RATE))
    for i in range(fade_start, total_samp):
        factor = (total_samp - i) / (total_samp - fade_start)
        buffer[i] *= factor

    # Normalise to prevent clipping
    peak = max(abs(s) for s in buffer) if buffer else 1.0
    if peak > 0.95:
        buffer = [s / peak * 0.92 for s in buffer]

    # Write WAV
    with wave.open(str(output_path), 'w') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        raw_bytes = struct.pack(f'<{len(buffer)}h',
                                *[int(max(-32768, min(32767, s * 32767)) )for s in buffer])
        wf.writeframes(raw_bytes)


# ════════════════════════════════════════════════════════════════
# STARTUP
# ════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'Save That Moment render server starting on port {port}')
    app.run(host='0.0.0.0', port=port, threaded=True)
