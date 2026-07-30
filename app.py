import os
import subprocess
import logging
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

# ---------- Fetch formats (Android client) ----------
def fetch_formats(url):
    opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android'],
            }
        }
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info is None:
                return None, "No info"
    except Exception as e:
        return None, str(e)

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
    return formats, None

@app.route('/ping')
def ping():
    return 'OK', 200

@app.route('/fetch')
def fetch():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400
    formats, err = fetch_formats(url)
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

    # Build command – always use Android client, single combined format
    cmd = [
        'yt-dlp',
        '-f', format_id if format_id else 'best',
        '-o', '-',
        '--no-playlist',
        '--extractor-args', 'youtube:player_client=android',
        '--no-check-certificate',
        '--user-agent', 'Mozilla/5.0 (Linux; Android 10; SM-G960F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36',
        url
    ]
    app.logger.info(f"Downloading: {' '.join(cmd)}")

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
        app.logger.error(f"Exception: {e}")
        return str(e), 500

@app.route('/')
def home():
    return "ClipSnag backend is running."

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
