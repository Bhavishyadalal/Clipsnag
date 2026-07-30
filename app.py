import os
import json
import subprocess
import tempfile
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
RAW_COOKIES_FILE = os.environ.get('COOKIES_FILE', './cookies.txt')
COOKIES_AVAILABLE = os.path.exists(RAW_COOKIES_FILE)

def get_cookie_file():
    """Read the raw cookies file, normalise it, and return a path to a clean temporary file."""
    if not COOKIES_AVAILABLE:
        return None

    with open(RAW_COOKIES_FILE, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()

    # Normalise line endings to LF
    content = content.replace('\r\n', '\n').replace('\r', '\n')

    # Ensure the Netscape header is present
    if not content.startswith('# Netscape HTTP Cookie File'):
        content = '# Netscape HTTP Cookie File\n' + content

    # Remove any blank lines at the beginning
    lines = content.splitlines(keepends=True)
    # Keep only lines that are not empty, and ensure each line has at least 6 tab-separated fields
    cleaned = []
    for line in lines:
        if line.strip() == '' or line.startswith('#'):
            cleaned.append(line)
        else:
            parts = line.strip().split('\t')
            # Netscape format: domain, flag, path, secure, expires, name, value
            if len(parts) >= 7:
                cleaned.append('\t'.join(parts) + '\n')
            else:
                # If invalid, we still keep it but yt-dlp will skip it
                cleaned.append(line)

    # Write to a temporary file
    tmp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
    tmp_file.writelines(cleaned)
    tmp_file.close()
    app.logger.info(f"Normalised cookie file written to {tmp_file.name}")
    return tmp_file.name

# ---------- Core Fetch ----------
def fetch_formats(url):
    cookie_path = get_cookie_file()
    opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
    }
    if cookie_path:
        opts['cookiefile'] = cookie_path
        app.logger.info("Using normalised cookies.")
    else:
        opts['extractor_args'] = {
            'youtube': {
                'player_client': ['android'],
            }
        }
        app.logger.info("Using Android client (no auth).")

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info is None:
                return None, "No info retrieved"
    except Exception as e:
        app.logger.error(f"yt-dlp fetch error: {e}")
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
    # Clean up temporary file (optional)
    return formats, None

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

    cookie_path = get_cookie_file()
    cmd = ['yt-dlp', '-f', format_id, '-o', '-', '--no-playlist', url]
    if cookie_path:
        cmd.extend(['--cookies', cookie_path])
    else:
        cmd.extend(['--extractor-args', 'youtube:player_client=android'])

    app.logger.info(f"Download command: {' '.join(cmd)}")

    try:
        # Determine content type
        with yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            chosen = next((f for f in info.get('formats', []) if f.get('format_id') == format_id), None)
            if not chosen:
                app.logger.warning(f"Format {format_id} not found. Falling back to bestvideo+bestaudio.")
                cmd = ['yt-dlp', '-f', 'bestvideo+bestaudio', '-o', '-', '--no-playlist', url]
                if cookie_path:
                    cmd.extend(['--cookies', cookie_path])
                # Determine filename from fallback
                with yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True}) as ydl2:
                    info2 = ydl2.extract_info(url, download=False)
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
                    app.logger.error(f"yt-dlp download error: {err}")
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
        app.logger.error(f"Download endpoint error: {e}")
        return f"Error: {str(e)}", 500

@app.route('/')
def home():
    return f"ClipSnag backend is running. Cookies: {'✅' if COOKIES_AVAILABLE else '❌'}"

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
