import os
import json
import subprocess
import logging
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

COOKIES_FILE = os.environ.get('COOKIES_FILE', './cookies.txt')
COOKIES_AVAILABLE = os.path.exists(COOKIES_FILE)

def fetch_formats(url):
    opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
    }
    if COOKIES_AVAILABLE:
        opts['cookiefile'] = COOKIES_FILE
    
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info is None:
                return None, "No info retrieved"
    except Exception as e:
        logging.error(f"yt-dlp error: {e}")
        return None, str(e)

    formats = []
    seen = set()
    for f in info.get('formats', []):
        if f.get('filesize') is None and f.get('filesize_approx') is None:
            continue
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
    return formats, None

@app.route('/ping', methods=['GET'])
def ping():
    return 'OK', 200

@app.route('/fetch', methods=['GET'])
def fetch_formats_endpoint():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url'}), 400
    formats, err = fetch_formats(url)
    if err:
        return jsonify({'error': err}), 400
    return jsonify({'success': True, 'formats': formats})

@app.route('/download', methods=['GET'])
def download_endpoint():
    url = request.args.get('url')
    format_id = request.args.get('format_id')
    if not url or not format_id:
        return 'Missing url or format_id', 400

    cmd = ['yt-dlp', '-f', format_id, '-o', '-', '--no-playlist', url]
    if COOKIES_AVAILABLE:
        cmd.extend(['--cookies', COOKIES_FILE])

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def generate():
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                yield chunk
            proc.wait()
        return Response(
            stream_with_context(generate()),
            headers={'Content-Disposition': 'attachment; filename="video.mp4"'}
        )
    except Exception as e:
        return f"Error: {str(e)}", 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
