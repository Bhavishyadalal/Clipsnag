import os
import json
import subprocess
import logging
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import yt_dlp
import tempfile

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

# ---------- Rate Limiting ----------
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["10 per minute", "100 per hour"]
)
limiter.init_app(app)

# ---------- Cookies ----------
RAW_COOKIES_FILE = os.environ.get('COOKIES_FILE', './cookies.txt')
COOKIES_AVAILABLE = os.path.exists(RAW_COOKIES_FILE)

def get_cookie_file():
    """Normalise cookies and return a temporary file path."""
    if not COOKIES_AVAILABLE:
        return None
    
    with open(RAW_COOKIES_FILE, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    
    # Normalise line endings
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    
    # Ensure header
    if not content.startswith('# Netscape HTTP Cookie File'):
        content = '# Netscape HTTP Cookie File\n' + content
    
    # Write to temp file
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
    tmp.write(content)
    tmp.close()
    return tmp.name

def get_ydl_opts():
    cookie_path = get_cookie_file()
    opts = {
        'quiet': True,
        'no_warnings': False,  # Set to False to see warnings
        'skip_download': True,
    }
    if cookie_path:
        opts['cookiefile'] = cookie_path
        app.logger.info("✅ Using cookies from: %s", cookie_path)
    else:
        opts['extractor_args'] = {
            'youtube': {
                'player_client': ['android'],
            }
        }
        app.logger.info("ℹ️ No cookies – using Android client.")
    return opts, cookie_path

def fetch_formats(url):
    opts, cookie_path = get_ydl_opts()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info is None:
                return None, "No info retrieved"
    except Exception as e:
        app.logger.error(f"❌ yt-dlp fetch error: {e}")
        return None, str(e)

    formats = []
    seen = set()
    for f in info.get('formats', []):
        format_id = f.get('format_id')
        if not format_id or format_id in seen:
            continue
        seen.add(format_id)
        resolution = f.get('format_note') or f.get('resolution') or 'unknown'
        size = f.get('filesize') or f.get('filesize_approx')
        ext = f.get('ext', 'bin')
        formats.append({
            'format_id': format_id,
            'label': f"{resolution} ({ext})",
            'resolution': resolution,
            'filesize_mb': round(size / (1024 * 1024), 1) if size else 0,
            'ext': ext,
        })
    formats.sort(key=lambda x: x['filesize_mb'])
    app.logger.info(f"✅ Found {len(formats)} formats")
    return formats, None

# ---------- Endpoints ----------
@app.route('/ping', methods=['GET'])
def ping():
    return 'OK', 200

@app.route('/debug', methods=['GET'])
def debug():
    """Check if cookies are loaded and show first few formats."""
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url'}), 400
    formats, err = fetch_formats(url)
    if err:
        return jsonify({'error': err}), 400
    return jsonify({
        'cookie_file_exists': COOKIES_AVAILABLE,
        'cookie_path': RAW_COOKIES_FILE,
        'total_formats': len(formats),
        'sample_formats': formats[:5],
    })

@app.route('/fetch', methods=['GET'])
@limiter.limit("10 per minute")
def fetch_formats_endpoint():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url'}), 400
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
@limiter.limit("10 per minute")
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

    app.logger.info(f"⬇️ Download command: {' '.join(cmd)}")

    try:
        # Get content type and filename
        opts = {'quiet': True, 'skip_download': True}
        if cookie_path:
            opts['cookiefile'] = cookie_path
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            chosen = next((f for f in info.get('formats', []) if f.get('format_id') == format_id), None)
            if not chosen:
                # Fallback to best
                app.logger.warning(f"Format {format_id} not found. Falling back to bestvideo+bestaudio.")
                cmd = ['yt-dlp', '-f', 'bestvideo+bestaudio', '-o', '-', '--no-playlist', url]
                if cookie_path:
                    cmd.extend(['--cookies', cookie_path])
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
                    app.logger.error(f"❌ yt-dlp download error: {err}")
            except Exception as e:
                app.logger.error(f"❌ Streaming error: {e}")
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
        app.logger.error(f"❌ Download endpoint error: {e}")
        return f"Error: {str(e)}", 500

@app.route('/')
def home():
    return f"ClipSnag backend running. Cookies: {'✅' if COOKIES_AVAILABLE else '❌'}"

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
