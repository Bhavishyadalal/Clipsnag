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

def fetch_formats(url):
    cookie_path = get_cookie_path()
    opts = {'quiet': True, 'no_warnings': True, 'skip_download': True}
    if cookie_path:
        opts['cookiefile'] = cookie_path
    else:
        opts['extractor_args'] = {'youtube': {'player_client': ['android']}}
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

    cookie_path = get_cookie_path()

    # Build the base command
    base_cmd = ['yt-dlp', '-o', '-', '--no-playlist', url]
    if cookie_path:
        base_cmd.extend(['--cookies', cookie_path])

    # List of formats to try in order (specific ID -> bestvideo+bestaudio -> best)
    format_selectors = [format_id, 'bestvideo+bestaudio', 'best'] if format_id else ['bestvideo+bestaudio', 'best']

    last_error = None
    chosen_selector = None
    for selector in format_selectors:
        cmd = base_cmd + ['-f', selector]
        app.logger.info(f"Trying format: {selector}")
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            # Check if it fails immediately by reading a small chunk
            # We'll just run it and stream, but if it fails, we catch the error.
            # We need to test if it runs successfully without downloading a huge file.
            # We'll use the `--get-url` trick to test quickly, but simpler: just try to stream.
            # Actually, we can just start the process and if it fails, we catch the return code.
            # We'll use a timeout to test quickly, but that's messy.
            # Instead, we use `subprocess.run` with `check=False` and capture stderr to test.
            test_cmd = cmd + ['--get-url']
            test_result = subprocess.run(test_cmd, capture_output=True, text=True, timeout=10)
            if test_result.returncode == 0 and test_result.stdout.strip():
                # This selector works!
                chosen_selector = selector
                break
            else:
                last_error = test_result.stderr.strip()
                app.logger.warning(f"Selector {selector} failed: {last_error}")
        except Exception as e:
            last_error = str(e)
            app.logger.warning(f"Selector {selector} exception: {last_error}")
            continue

    if not chosen_selector:
        return f"Error: No format works. Last error: {last_error}", 400

    # Now actually download with the working selector
    cmd = base_cmd + ['-f', chosen_selector]
    app.logger.info(f"Downloading with: {' '.join(cmd)}")
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
                app.logger.error(f"yt-dlp final error: {err}")
        return Response(
            stream_with_context(generate()),
            headers={'Content-Disposition': f'attachment; filename="video.mp4"'}
        )
    except Exception as e:
        return str(e), 500

@app.route('/')
def home():
    return f"ClipSnag backend. Cookies: {'✅' if COOKIES_AVAILABLE else '❌'}"

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
