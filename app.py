import os
import subprocess
import logging
from flask import Flask, request, Response, stream_with_context
from flask_cors import CORS

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

@app.route('/ping')
def ping():
    return 'OK', 200

@app.route('/download')
def download():
    url = request.args.get('url')
    if not url:
        return 'Missing url', 400

    # Force Android client, single combined format (no ffmpeg needed)
    cmd = [
        'yt-dlp',
        '-f', 'best',               # single file, no merge
        '-o', '-',
        '--no-playlist',
        '--extractor-args', 'youtube:player_client=android',
        '--no-check-certificate',
        '--user-agent', 'Mozilla/5.0 (Linux; Android 10; SM-G960F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36',
        url
    ]
    app.logger.info(f"Running: {' '.join(cmd)}")

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
