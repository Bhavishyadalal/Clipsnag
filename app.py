import os
import requests
import logging
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from urllib.parse import quote

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

API_URL = "https://yt-vid.hazex.workers.dev/"

def fetch_formats_from_api(video_url):
    try:
        encoded_url = quote(video_url, safe='')
        request_url = f"{API_URL}?url={encoded_url}"
        app.logger.info(f"Calling API: {request_url}")
        
        response = requests.get(request_url, timeout=30)
        if response.status_code != 200:
            return None, f"API error: {response.status_code}"
        
        data = response.json()
        if data.get("error", True):
            return None, data.get("message", "Unknown API error")
        
        formats = []
        def add_formats(category, type_label):
            if category in data:
                for item in data[category]:
                    url = item.get("url", "")
                    if url:
                        formats.append({
                            "format_id": f"{type_label}_{len(formats)}",
                            "label": f"{type_label}: {item.get('label', 'Unknown')}",
                            "url": url,
                            "resolution": item.get('label', 'Unknown'),
                            "filesize_mb": 0,
                            "ext": "mp4" if "video" in type_label else "mp3",
                            "type": "video" if "video" in type_label else "audio"
                        })
        
        add_formats("video_with_audio", "Video+Audio")
        add_formats("video_only", "Video Only")
        add_formats("audio", "Audio")
        
        if not formats:
            return None, "No formats found"
        return formats, None
    except Exception as e:
        app.logger.error(f"API call failed: {e}")
        return None, str(e)

@app.route('/ping')
def ping():
    return 'OK', 200

@app.route('/fetch')
def fetch():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400
    formats, err = fetch_formats_from_api(url)
    if err:
        return jsonify({'error': err}), 400
    if not formats:
        return jsonify({'error': 'No formats found'}), 404
    return jsonify({'success': True, 'formats': formats})

@app.route('/download')
def download():
    url = request.args.get('url')
    if not url:
        return 'Missing url parameter', 400
    try:
        response = requests.get(url, stream=True, timeout=30)
        if response.status_code != 200:
            return f"Download failed: {response.status_code}", 400
        def generate():
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        return Response(
            stream_with_context(generate()),
            content_type=response.headers.get('Content-Type', 'video/mp4'),
            headers={
                'Content-Disposition': 'attachment; filename="video.mp4"',
                'Cache-Control': 'no-cache'
            }
        )
    except Exception as e:
        app.logger.error(f"Download error: {e}")
        return str(e), 500

@app.route('/')
def home():
    return "ClipSnag backend is running (Hazex API)."

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)