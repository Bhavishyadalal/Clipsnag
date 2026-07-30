import os
import json
import subprocess
import tempfile
import logging
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

COOKIES_FILE = os.environ.get('COOKIES_FILE', './cookies.txt')
COOKIES_AVAILABLE = os.path.exists(COOKIES_FILE)

def get_cookie_path():
    if not COOKIES_AVAILABLE:
        return None
    with open(COOKIES_FILE, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    if not content.startswith('# Netscape HTTP Cookie File'):
        content = '# Netscape HTTP Cookie File\n' + content
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
    tmp.write(content)
    tmp.close()
    return tmp.name

def get_ydl_opts():
    cookie_path = get_cookie_path()
    opts = {'quiet': True, 'no_warnings': True, 'skip_download': True}
    if cookie_path:
        opts['cookiefile'] = cookie_path
        app.logger.info("Using cookies")
    else:
        opts['extractor_args'] = {'youtube': {'player_client': ['android']}}
        app.logger.info("Using Android (no auth)")
    return opts

def fetch_formats(url):
    opts = get_ydl_opts()
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
        return jsonify({'error': 'Missing url'}), 400
    formats, err = fetch_formats(url)
    if err:
        return jsonify({'error': err}), 400
    return jsonify({'success': True, 'formats': formats})

@app.route('/download')
def download():
    url = request.args.get('url')
    format_id = request.args.get('format_id')
    if not url:
        return 'Missing url', 400

    # Re‑fetch formats using the **exact same method** as /fetch
    formats, err = fetch_formats(url)
    if err or not formats:
        return f"Error: {err}", 400

    # Find the requested format
    chosen = None
    if format_id:
        chosen = next((f for f in formats if f['format_id'] == format_id), None)
    if not chosen:
        # If not found, pick the best one (largest file)
        chosen = formats[-1] if formats else None
        if chosen:
            format_id = chosen['format_id']

    if not chosen:
        return 'No suitable format', 400

    # Build download command using the SAME method
    cookie_path = get_cookie_path()
    cmd = ['yt-dlp', '-f', format_id, '-o', '-', '--no-playlist', url]
    if cookie_path:
        cmd.extend(['--cookies', cookie_path])

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
            headers={'Content-Disposition': f'attachment; filename="video.{chosen["ext"]}"'}
        )
    except Exception as e:
        return str(e), 500

@app.route('/')
def home():
    return f"ClipSnag backend. Cookies: {'✅' if COOKIES_AVAILABLE else '❌'}"

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
