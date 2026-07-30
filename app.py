import os
import json
import subprocess
import logging
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import yt_dlp

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

# ---------- Rate Limiting ----------
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["5 per minute", "50 per hour"]
)
limiter.init_app(app)

# ---------- Cookies ----------
COOKIES_FILE = os.environ.get('COOKIES_FILE')
if COOKIES_FILE and os.path.exists(COOKIES_FILE):
    app.logger.info(f"✅ Cookies file found at {COOKIES_FILE}")
else:
    app.logger.warning("❌ Cookies file not found. Falling back to Android client (1080p).")
    COOKIES_FILE = None

# ---------- Core Fetch ----------
def fetch_formats(url):
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
    }
    if COOKIES_FILE:
        ydl_opts['cookiefile'] = COOKIES_FILE
    else:
        ydl_opts['extractor_args'] = {
            'youtube': {
                'player_client': ['android'],
            }
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info is None:
                return None, "No info retrieved"
    except Exception as e:
        app.logger.error(f"yt-dlp error: {e}")
        return None, str(e)

    formats = []
    seen = set()
    for f in info.get('formats', []):
        if f.get('filesize') is None and f.get('filesize_approx') is None:
            continue
        resolution = f.get('format_note') or f.get('resolution') or 'unknown'
        ftype = (
            'video+audio' if f.get('vcodec') != 'none' and f.get('acodec') != 'none' else
            'video' if f.get('vcodec') != 'none' else
            'audio' if f.get('acodec') != 'none' else
            'unknown'
        )
        size = f.get('filesize') or f.get('filesize_approx')
        if size is None:
            continue
        format_id = f.get('format_id')
        if not format_id or format_id in seen:
            continue
        seen.add(format_id)
        ext = f.get('ext', 'bin')
        label = f"{resolution} ({ext})" if ftype != 'audio' else f"Audio: {f.get('abr', '?')}kbps ({ext})"
        formats.append({
            'format_id': format_id,
            'label': label,
            'resolution': resolution,
            'filesize': size,
            'filesize_mb': round(size / (1024 * 1024), 1),
            'ext': ext,
            'type': ftype,
            'codec': f.get('vcodec') or f.get('acodec') or 'unknown',
            'bitrate': f.get('abr') or f.get('vbr') or None,
        })
    formats.sort(key=lambda x: x['filesize'])
    return formats, None

# ---------- Endpoints ----------
@app.route('/ping', methods=['GET'])
def ping():
    return 'OK', 200

@app.route('/fetch', methods=['GET'])
@limiter.limit("5 per minute")
def fetch_formats_endpoint():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400
    formats, err = fetch_formats(url)
    if err:
        return jsonify({'error': err}), 400
    if not formats:
        return jsonify({'error': 'No formats found'}), 404
    return jsonify({
        'success': True,
        'formats': formats,
        'title': url,
    })

@app.route('/download', methods=['GET'])
@limiter.limit("5 per minute")
def download_endpoint():
    url = request.args.get('url')
    format_id = request.args.get('format_id')
    if not url or not format_id:
        return 'Missing url or format_id', 400

    # Build command
    cmd = ['yt-dlp', '-f', format_id, '-o', '-', '--no-playlist', url]
    if COOKIES_FILE:
        cmd.extend(['--cookies', COOKIES_FILE])
    else:
        cmd.extend(['--extractor-args', 'youtube:player_client=android'])

    app.logger.info(f"Download command: {' '.join(cmd)}")

    try:
        # Get file extension
        with yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            chosen = next((f for f in info.get('formats', []) if f.get('format_id') == format_id), None)
            if not chosen:
                # Fallback: pick the best video+audio combo
                app.logger.warning(f"Format {format_id} not found. Falling back to bestvideo+bestaudio.")
                # Rerun the command with bestvideo+bestaudio
                cmd = ['yt-dlp', '-f', 'bestvideo+bestaudio', '-o', '-', '--no-playlist', url]
                if COOKIES_FILE:
                    cmd.extend(['--cookies', COOKIES_FILE])
                # Determine filename from the fallback
                with yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True}) as ydl2:
                    info2 = ydl2.extract_info(url, download=False)
                    # Find best formats
                    best_video = next((f for f in info2.get('formats', []) if f.get('vcodec') != 'none' and f.get('acodec') == 'none'), None)
                    best_audio = next((f for f in info2.get('formats', []) if f.get('acodec') != 'none' and f.get('vcodec') == 'none'), None)
                    if best_video and best_audio:
                        ext = best_video.get('ext', 'mp4')
                        filename = f"video.{ext}"
                        ct = {'mp4': 'video/mp4', 'm4v': 'video/mp4', 'webm': 'video/webm', 'mp3': 'audio/mpeg', 'm4a': 'audio/mp4'}.get(ext, 'application/octet-stream')
                    else:
                        filename = 'video.mp4'
                        ct = 'video/mp4'
            else:
                ext = chosen.get('ext', 'mp4')
                filename = f"video.{ext}"
                ct = {'mp4': 'video/mp4', 'm4v': 'video/mp4', 'webm': 'video/webm', 'mp3': 'audio/mpeg', 'm4a': 'audio/mp4'}.get(ext, 'application/octet-stream')

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def generate():
            try:
                while True:
                    chunk = proc.stdout.read(8192)
                    if not chunk:
                        break
                    yield chunk
                proc.wait()
                if proc.returncode != 0:
                    err = proc.stderr.read().decode()
                    app.logger.error(f"yt-dlp error: {err}")
            except Exception as e:
                app.logger.error(f"Streaming error: {e}")
            finally:
                proc.stdout.close()
                if proc.stderr:
                    proc.stderr.close()

        return Response(
            stream_with_context(generate()),
            content_type=ct,
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"',
                'Cache-Control': 'no-cache'
            }
        )
    except Exception as e:
        return f"Error: {str(e)}", 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
