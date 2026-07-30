import os
import json
import tempfile
import subprocess
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp
import logging

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

# ------------------------------------------------------------
# PO Token setup – you need to provide these from your browser
# Get them using the "Get Cookies.txt" extension or via browser console:
# po_token and visitor_data are available in the yt-player request.
# ------------------------------------------------------------
PO_TOKEN = os.environ.get('PO_TOKEN', None)
VISITOR_DATA = os.environ.get('VISITOR_DATA', None)

def get_ydl_opts():
    opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'force_generic_extractor': False,
        'extractor_args': {
            'youtube': {
                'player_client': ['web'],  # Use web client
                'po_token': [PO_TOKEN] if PO_TOKEN else [],
                'visitor_data': [VISITOR_DATA] if VISITOR_DATA else [],
            }
        }
    }
    # Fallback to cookies if PO token is missing
    if not PO_TOKEN and os.environ.get('COOKIES_FILE'):
        opts['cookiefile'] = os.environ.get('COOKIES_FILE')
    return opts

def fetch_formats(url):
    ydl_opts = get_ydl_opts()
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
        if f.get('vcodec') != 'none' and f.get('acodec') != 'none':
            ftype = 'video+audio'
        elif f.get('vcodec') != 'none':
            ftype = 'video'
        elif f.get('acodec') != 'none':
            ftype = 'audio'
        else:
            ftype = 'unknown'
        size = f.get('filesize') or f.get('filesize_approx')
        if size is None:
            continue
        format_id = f.get('format_id')
        if not format_id or format_id in seen:
            continue
        seen.add(format_id)
        ext = f.get('ext', 'bin')
        label = f"{resolution} ({ext})"
        if ftype == 'audio':
            label = f"Audio: {f.get('abr', '?')}kbps ({ext})"
        bitrate = f.get('abr') or f.get('vbr') or None
        formats.append({
            'format_id': format_id,
            'label': label,
            'resolution': resolution,
            'filesize': size,
            'filesize_mb': round(size / (1024 * 1024), 1),
            'ext': ext,
            'type': ftype,
            'codec': f.get('vcodec') or f.get('acodec') or 'unknown',
            'bitrate': bitrate,
        })
    formats.sort(key=lambda x: x['filesize'])
    return formats, None

@app.route('/ping', methods=['GET'])
def ping():
    return 'OK', 200

@app.route('/fetch', methods=['GET'])
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
def download_endpoint():
    url = request.args.get('url')
    format_id = request.args.get('format_id')
    if not url or not format_id:
        return 'Missing url or format_id', 400

    # Build command with PO token or cookies
    cmd = ['yt-dlp', '-f', format_id, '-o', '-', '--no-playlist', url]
    # If PO token is set, add extractor args
    if PO_TOKEN:
        cmd.extend(['--extractor-args', f'youtube:player_client=web;po_token={PO_TOKEN};visitor_data={VISITOR_DATA}'])
    elif os.environ.get('COOKIES_FILE'):
        cmd.extend(['--cookies', os.environ.get('COOKIES_FILE')])

    try:
        # Get file extension and content-type
        with yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            chosen = next((f for f in info.get('formats', []) if f.get('format_id') == format_id), None)
            if chosen:
                ext = chosen.get('ext', 'mp4')
                filename = f"video.{ext}"
                if ext in ['mp4', 'm4v']:
                    ct = 'video/mp4'
                elif ext in ['webm']:
                    ct = 'video/webm'
                elif ext in ['mp3']:
                    ct = 'audio/mpeg'
                elif ext in ['m4a']:
                    ct = 'audio/mp4'
                else:
                    ct = 'application/octet-stream'
            else:
                ext = 'bin'
                filename = 'download.bin'
                ct = 'application/octet-stream'

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
