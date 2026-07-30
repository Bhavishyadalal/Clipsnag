import os
import subprocess
import logging
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

# ---------- Configuration ----------
# List of clients to try in order (Android is usually best, but fallbacks matter)
CLIENTS = ['android', 'mweb', 'web']

def fetch_formats_with_fallback(url):
    """
    Tries multiple YouTube clients to fetch formats.
    Returns (formats, error) – formats is None if all fail.
    """
    last_error = None
    for client in CLIENTS:
        opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'extractor_args': {
                'youtube': {
                    'player_client': [client],
                }
            },
            'user_agent': 'Mozilla/5.0 (Linux; Android 10; SM-G960F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36'
        }
        app.logger.info(f"🔄 Trying client: {client}")
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info is None:
                    continue
                formats = []
                for f in info.get('formats', []):
                    fid = f.get('format_id')
                    if not fid:
                        continue
                    res = f.get('format_note') or f.get('resolution') or 'unknown'
                    size = f.get('filesize') or f.get('filesize_approx')
                    formats.append({
                        'format_id': fid,
                        'label': f"{res} ({f.get('ext','bin')})",
                        'resolution': res,
                        'filesize_mb': round(size/(1024*1024),1) if size else 0,
                        'ext': f.get('ext','bin'),
                    })
                formats.sort(key=lambda x: x['filesize_mb'])
                app.logger.info(f"✅ Client {client} succeeded with {len(formats)} formats")
                return formats, None
        except Exception as e:
            app.logger.warning(f"❌ Client {client} failed: {str(e)}")
            last_error = str(e)
            continue
    return None, f"All clients failed. Last error: {last_error}"

@app.route('/ping')
def ping():
    return 'OK', 200

@app.route('/fetch')
def fetch():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400
    formats, err = fetch_formats_with_fallback(url)
    if err:
        return jsonify({'error': err}), 400
    if not formats:
        return jsonify({'error': 'No formats found'}), 404
    return jsonify({'success': True, 'formats': formats})

@app.route('/download')
def download():
    url = request.args.get('url')
    format_id = request.args.get('format_id')
    if not url:
        return 'Missing url', 400

    # Try the specified format with Android first, fallback to best
    if format_id:
        # Try with Android first
        cmd = [
            'yt-dlp',
            '-f', format_id,
            '-o', '-',
            '--no-playlist',
            '--extractor-args', 'youtube:player_client=android',
            '--user-agent', 'Mozilla/5.0 (Linux; Android 10; SM-G960F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36',
            '--no-check-certificate',
            url
        ]
        app.logger.info(f"⬇️ Downloading format {format_id} with Android")
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            def generate():
                while True:
                    chunk = proc.stdout.read(8192)
                    if not chunk:
                        break
                    yield chunk
                proc.wait()
                if proc.returncode != 0:
                    err = proc.stderr.read().decode()
                    app.logger.error(f"yt-dlp error: {err}")
            return Response(
                stream_with_context(generate()),
                headers={'Content-Disposition': 'attachment; filename="video.mp4"'}
            )
        except Exception as e:
            app.logger.error(f"Download with format_id failed: {e}")
            # Fall through to fallback

    # Fallback: download best single format using the first working client
    # Re‑fetch formats to get a working client
    formats, err = fetch_formats_with_fallback(url)
    if err or not formats:
        return f"Error: {err}", 400

    # Pick the best format (largest file size) from the successful list
    best = formats[-1]
    format_id = best['format_id']
    # Determine which client succeeded (we can simply reuse the same method)
    # But for simplicity, we'll use Android again – if it fails, the user gets an error.
    # Better: use the same fallback in download as well.
    # We'll just re‑run the command with the best format, no client override (use default)
    cmd = [
        'yt-dlp',
        '-f', format_id,
        '-o', '-',
        '--no-playlist',
        '--no-check-certificate',
        url
    ]
    app.logger.info(f"⬇️ Fallback downloading best format: {format_id}")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def generate():
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                yield chunk
            proc.wait()
            if proc.returncode != 0:
                err = proc.stderr.read().decode()
                app.logger.error(f"yt-dlp error: {err}")
        return Response(
            stream_with_context(generate()),
            headers={'Content-Disposition': f'attachment; filename="video.{best["ext"]}"'}
        )
    except Exception as e:
        app.logger.error(f"Download fallback failed: {e}")
        return str(e), 500

@app.route('/')
def home():
    return "ClipSnag backend is running."

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
