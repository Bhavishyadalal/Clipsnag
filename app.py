import os
import json
import tempfile
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp
import logging

app = Flask(__name__)
CORS(app)  # Allow your frontend (GitHub Pages) to access this API

logging.basicConfig(level=logging.INFO)

# ------------------------------------------------------------
# Helper: extract formats from a YouTube URL
# ------------------------------------------------------------
def fetch_formats(url):
    """
    Use yt-dlp to get video info and return a cleaned list of formats.
    Each format dict: { 'format_id', 'resolution', 'codec', 'filesize', 'ext', 'type' }
    """
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'skip_download': True,
        'force_generic_extractor': False,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info is None:
                return None, "No info retrieved"
    except Exception as e:
        return None, str(e)

    formats = []
    seen = set()
    # yt-dlp's 'formats' list contains both video and audio.
    for f in info.get('formats', []):
        # Skip formats without a resolution or filesize (some are just manifest)
        if f.get('filesize') is None and f.get('filesize_approx') is None:
            continue
        # Build a display label
        resolution = f.get('format_note') or f.get('resolution') or 'unknown'
        # Determine type: video or audio
        if f.get('vcodec') != 'none' and f.get('acodec') != 'none':
            ftype = 'video+audio'
        elif f.get('vcodec') != 'none':
            ftype = 'video'
        elif f.get('acodec') != 'none':
            ftype = 'audio'
        else:
            ftype = 'unknown'
        # Get file size (use approx if exact not available)
        size = f.get('filesize') or f.get('filesize_approx')
        if size is None:
            continue
        # Create a unique key for dedup (format_id is unique)
        format_id = f.get('format_id')
        if not format_id:
            continue
        if format_id in seen:
            continue
        seen.add(format_id)

        # Also include the extension
        ext = f.get('ext', 'bin')
        # Human-readable label
        label = f"{resolution} ({ext})"
        if ftype == 'audio':
            label = f"Audio: {f.get('abr', '?')}kbps ({ext})"
        # Bitrate for audio
        bitrate = f.get('abr') or f.get('vbr') or None
        formats.append({
            'format_id': format_id,
            'label': label,
            'resolution': resolution,
            'filesize': size,  # in bytes
            'filesize_mb': round(size / (1024 * 1024), 1),
            'ext': ext,
            'type': ftype,
            'codec': f.get('vcodec') or f.get('acodec') or 'unknown',
            'bitrate': bitrate,
        })
    # Sort by filesize ascending
    formats.sort(key=lambda x: x['filesize'])
    return formats, None

# ------------------------------------------------------------
# API endpoints
# ------------------------------------------------------------
@app.route('/ping', methods=['GET'])
def ping():
    return 'OK', 200

@app.route('/fetch', methods=['GET'])
def fetch_formats_endpoint():
    """
    Expects ?url=... (YouTube URL)
    Returns JSON with formats list.
    """
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
        'title': url,  # yt-dlp can give title but we skip for speed
    })

@app.route('/download', methods=['GET'])
def download_endpoint():
    """
    Expects ?url=...&format_id=...
    Streams the selected format directly to the browser.
    """
    url = request.args.get('url')
    format_id = request.args.get('format_id')
    if not url or not format_id:
        return 'Missing url or format_id', 400

    # Use yt-dlp to stream the selected format
    ydl_opts = {
        'quiet': True,
        'format': format_id,
        'outtmpl': '-',  # output to stdout
        'no_warnings': True,
    }
    try:
        # Use subprocess to pipe because yt-dlp's Python API streaming is tricky.
        import subprocess
        import sys
        # We'll use subprocess.Popen to get the output stream
        cmd = [
            'yt-dlp',
            '-f', format_id,
            '-o', '-',
            '--no-playlist',
            url
        ]
        # Start process, pipe stdout
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # We need to get the content-type from yt-dlp? We'll guess.
        # For video, common types: video/mp4, audio/mpeg, etc.
        # We can also get the file extension from the format_id.
        # We'll try to get the extension from the format list first.
        # But for simplicity, we'll fetch info again to get extension and filename.
        # To avoid double work, we can get info before streaming.
        # Let's fetch info once more to get the filename/extension.
        with yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            # Find the format
            chosen = None
            for f in info.get('formats', []):
                if f.get('format_id') == format_id:
                    chosen = f
                    break
            if chosen:
                ext = chosen.get('ext', 'mp4')
                filename = f"video.{ext}"
                # Determine content type
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

        # Now stream the process output
        def generate():
            try:
                while True:
                    chunk = proc.stdout.read(8192)
                    if not chunk:
                        break
                    yield chunk
                # Wait for process to finish, check return code
                proc.wait()
                if proc.returncode != 0:
                    # Log error but we can't send it now
                    app.logger.error(f"yt-dlp failed: {proc.stderr.read().decode()}")
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

# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
