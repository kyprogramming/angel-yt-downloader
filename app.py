"""
YouTube Downloader - Flask Backend (pytubefix)
===============================================
Install:
    pip install flask flask-cors pytubefix

For merging video+audio streams you also need FFmpeg:
    Windows : https://ffmpeg.org/download.html  (add to PATH)
    Mac     : brew install ffmpeg
    Linux   : sudo apt install ffmpeg

Run:
    python app.py

Open:   http://localhost:5000
Put app.py and index.html in the SAME folder.
"""

import os
import re
import threading
import uuid
import mimetypes
import sys
from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS
import logging
import traceback

from pytubefix import YouTube, Playlist
from pytubefix.cli import on_progress

# ========== SETUP PROPER LOGGING ==========
# Force stdout to flush immediately
sys.stdout.reconfigure(line_buffering=True)

# Configure logging to both file and console
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),  # Print to console
        logging.FileHandler('app_errors.log')  # Also save to file
    ]
)

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = os.path.abspath("downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# job_id -> { status, percent, speed, filename, error, filepath }
progress_store = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize(name):
    """Remove characters that are illegal in filenames."""
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip()


def build_progress_hook(job_id):
    """Return a pytubefix on_progress callback that updates progress_store."""
    def hook(stream, chunk, bytes_remaining):
        total = stream.filesize
        done  = total - bytes_remaining
        pct   = int(done / total * 100) if total else 0
        # rough speed string (bytes/s not directly available, so we skip it)
        progress_store[job_id].update({
            "status":  "downloading",
            "percent": pct,
            "speed":   "",
            "eta":     "",
        })
    return hook


def format_size(n):
    if n is None:
        return "-"
    if n < 1024:
        return "{} B".format(n)
    if n < 1048576:
        return "{:.1f} KB".format(n / 1024)
    return "{:.1f} MB".format(n / 1048576)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
        with open(path, "r", encoding="utf-8") as f:
            return Response(f.read(), mimetype="text/html; charset=utf-8")
    except Exception as e:
        logging.error(f"Error serving index: {str(e)}")
        logging.error(traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/formats", methods=["POST"])
def get_formats():
    """
    Return all available streams for a YouTube URL.
    """
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        logging.info(f"Fetching formats for URL: {url}")
        yt = YouTube(url, use_oauth=True, allow_oauth_cache=True)

        # ── Preset "smart" formats ────────────────────────────────────────
        def make_preset(fid, label, res, note_extra=""):
            return {
                "format_id":  fid,
                "label":      label,
                "ext":        "mp4",
                "note":       "{} video + best audio merged via FFmpeg{}".format(res, note_extra),
                "type":       "video",
                "resolution": res,
                "filesize":   "-",
                "raw":        False,
            }

        presets = [
            make_preset("merge_2160p", "4K  (2160p) + Audio",  "2160p", " — very large file"),
            make_preset("merge_1440p", "2K  (1440p) + Audio",  "1440p", " — large file"),
            make_preset("merge_1080p", "FHD (1080p) + Audio",  "1080p"),
            make_preset("merge_720p",  "HD  (720p)  + Audio",  "720p"),
            make_preset("merge_480p",  "SD  (480p)  + Audio",  "480p"),
            make_preset("merge_360p",  "360p + Audio",          "360p"),
            make_preset("merge_240p",  "240p + Audio",          "240p"),
            make_preset("merge_144p",  "144p + Audio",          "144p", " — smallest size"),
            {
                "format_id":  "best_video",
                "label":      "Best Available + Audio",
                "ext":        "mp4",
                "note":       "Highest available resolution + best audio merged via FFmpeg",
                "type":       "video",
                "resolution": "Best",
                "filesize":   "-",
                "raw":        False,
            },
            {
                "format_id":  "best_progressive",
                "label":      "Best Progressive (No FFmpeg)",
                "ext":        "mp4",
                "note":       "Best single-file stream, no FFmpeg needed",
                "type":       "video",
                "resolution": "Progressive",
                "filesize":   "-",
                "raw":        False,
            },
            {
                "format_id":  "best_audio",
                "label":      "Audio Only (Best)",
                "ext":        "webm",
                "note":       "Highest quality audio stream, no video",
                "type":       "audio",
                "resolution": "-",
                "filesize":   "-",
                "raw":        False,
            },
        ]

        # ── Raw streams from pytubefix ────────────────────────────────────
        raw_fmts = []
        for s in yt.streams:
            if s.includes_video_track:
                ftype = "video"
                res   = s.resolution or "-"
                label = "{} {} {}fps {}".format(
                    res,
                    s.mime_type.split("/")[-1].upper(),
                    s.fps or "?",
                    "(+audio)" if s.is_progressive else "(video only)",
                )
                note = "progressive" if s.is_progressive else "video only"
            else:
                ftype = "audio"
                res   = s.abr or "-"
                label = "{} {}".format(res, s.mime_type.split("/")[-1].upper())
                note  = "audio only"

            raw_fmts.append({
                "format_id":  str(s.itag),
                "label":      label,
                "ext":        s.mime_type.split("/")[-1],
                "note":       note + " · " + format_size(s.filesize),
                "type":       ftype,
                "resolution": res,
                "filesize":   format_size(s.filesize),
                "raw":        True,
            })

        # Sort: video streams by resolution desc, then audio
        def sort_key(f):
            if f["type"] == "video":
                try:
                    return (0, -int(f["resolution"].replace("p", "")))
                except Exception:
                    return (0, 0)
            return (1, 0)

        raw_fmts.sort(key=sort_key)

        # Thumbnail — pytubefix gives thumbnail_url
        thumb = getattr(yt, "thumbnail_url", "") or ""

        # Duration
        secs = yt.length or 0
        duration = "{}:{:02d}".format(secs // 60, secs % 60)

        logging.info(f"Successfully fetched formats for: {yt.title}")
        
        return jsonify({
            "title":     yt.title or "Unknown",
            "thumbnail": thumb,
            "duration":  duration,
            "uploader":  yt.author or "-",
            "formats":   presets + raw_fmts,
        })

    except Exception as e:
        logging.error("=" * 60)
        logging.error("ERROR in /api/formats:")
        logging.error(f"URL: {url}")
        logging.error(f"Error type: {type(e).__name__}")
        logging.error(f"Error message: {str(e)}")
        logging.error("Full traceback:")
        logging.error(traceback.format_exc())
        logging.error("=" * 60)
        return jsonify({"error": str(e)}), 500


@app.route("/api/download", methods=["POST"])
def start_download():
    data      = request.json or {}
    urls      = data.get("urls", [])
    format_id = data.get("format_id", "best_progressive")

    if not urls:
        return jsonify({"error": "No URLs provided"}), 400

    job_id = str(uuid.uuid4())[:8]
    progress_store[job_id] = {
        "status":   "starting",
        "percent":  0,
        "speed":    "",
        "eta":      "",
        "note":     "",
        "files":    [],
        "error":    "",
    }

    def run():
        import subprocess

        def merge_video_audio(yt, title_safe, target_res=None):
            """
            Download the best video stream at target_res (e.g. '1080p', '720p')
            and the best audio stream, then merge them into one MP4 with FFmpeg.
            Falls back to next available resolution if exact match not found.
            Returns the saved filename.
            """
            progress_store[job_id]["status"] = "downloading"

            # ── Pick video stream ─────────────────────────────────────────
            vid_streams = (
                yt.streams
                .filter(adaptive=True, only_video=True, file_extension="mp4")
                .order_by("resolution")
            )

            v_stream = None
            if target_res:
                # Try exact resolution first
                for s in vid_streams:
                    if s.resolution == target_res:
                        v_stream = s
                        break
                # Fallback: best stream at or below target
                if not v_stream:
                    target_int = int(target_res.replace("p", ""))
                    for s in reversed(vid_streams.fmt_streams):
                        try:
                            if int(s.resolution.replace("p", "")) <= target_int:
                                v_stream = s
                                break
                        except Exception:
                            continue
            # Final fallback: highest available
            if not v_stream:
                v_stream = vid_streams.last()

            if not v_stream:
                raise Exception("No adaptive video stream found.")

            # ── Pick audio stream ─────────────────────────────────────────
            a_stream = (
                yt.streams
                .filter(only_audio=True)
                .order_by("abr")
                .last()
            )
            if not a_stream:
                raise Exception("No audio stream found.")

            actual_res = v_stream.resolution or "video"
            progress_store[job_id]["note"] = "Downloading {}p video...".format(actual_res)

            v_tmp = title_safe + "_vtmp.mp4"
            a_tmp = title_safe + "_atmp" + "." + a_stream.mime_type.split("/")[-1]
            out_name = "{}_{}merged.mp4".format(title_safe, actual_res + "_")

            v_path   = os.path.join(DOWNLOAD_DIR, v_tmp)
            a_path   = os.path.join(DOWNLOAD_DIR, a_tmp)
            out_path = os.path.join(DOWNLOAD_DIR, out_name)

            # Download video
            v_stream.download(output_path=DOWNLOAD_DIR, filename=v_tmp)

            progress_store[job_id]["note"] = "Downloading audio..."
            progress_store[job_id]["percent"] = 60

            # Download audio
            a_stream.download(output_path=DOWNLOAD_DIR, filename=a_tmp)

            # Merge with FFmpeg
            progress_store[job_id]["status"]  = "processing"
            progress_store[job_id]["percent"] = 85
            progress_store[job_id]["note"]    = "Merging with FFmpeg..."

            result = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", v_path,
                    "-i", a_path,
                    "-c:v", "copy",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-movflags", "+faststart",
                    out_path,
                ],
                capture_output=True,
                text=True,
            )

            # Clean up temp files
            for tmp in [v_path, a_path]:
                try:
                    os.remove(tmp)
                except Exception:
                    pass

            if result.returncode != 0:
                raise Exception("FFmpeg error: " + result.stderr[-300:])

            return out_name

        saved_files = []
        try:
            logging.info(f"Starting download job {job_id} for {len(urls)} URL(s)")
            
            for url in urls:
                yt = YouTube(url, use_oauth=True, allow_oauth_cache=True, on_progress_callback=build_progress_hook(job_id))
                title_safe = sanitize(yt.title or "video")

                # Map of preset format_id → target resolution string
                MERGE_PRESETS = {
                    "merge_2160p": "2160p",
                    "merge_1440p": "1440p",
                    "merge_1080p": "1080p",
                    "merge_720p":  "720p",
                    "merge_480p":  "480p",
                    "merge_360p":  "360p",
                    "merge_240p":  "240p",
                    "merge_144p":  "144p",
                    "best_video":  None,   # None = highest available
                }

                if format_id in MERGE_PRESETS:
                    fname = merge_video_audio(yt, title_safe, target_res=MERGE_PRESETS[format_id])
                    saved_files.append(fname)

                # ── Best progressive (no FFmpeg) ──────────────────────────
                elif format_id == "best_progressive":
                    stream = yt.streams.get_highest_resolution()
                    if not stream:
                        raise Exception("No progressive stream found")
                    fname = title_safe + "." + stream.mime_type.split("/")[-1]
                    stream.download(output_path=DOWNLOAD_DIR, filename=fname)
                    saved_files.append(fname)

                # ── Audio only ────────────────────────────────────────────
                elif format_id == "best_audio":
                    stream = yt.streams.filter(only_audio=True).order_by("abr").last()
                    if not stream:
                        raise Exception("No audio stream found")
                    fname = title_safe + "." + stream.mime_type.split("/")[-1]
                    stream.download(output_path=DOWNLOAD_DIR, filename=fname)
                    saved_files.append(fname)

                # ── Raw itag ──────────────────────────────────────────────
                else:
                    try:
                        itag = int(format_id)
                    except ValueError:
                        raise Exception("Unknown format: " + format_id)
                    stream = yt.streams.get_by_itag(itag)
                    if not stream:
                        raise Exception("Stream itag {} not found".format(itag))
                    ext   = stream.mime_type.split("/")[-1]
                    fname = title_safe + "." + ext
                    stream.download(output_path=DOWNLOAD_DIR, filename=fname)
                    saved_files.append(fname)

            progress_store[job_id].update({
                "status":  "done",
                "percent": 100,
                "files":   saved_files,
            })
            logging.info(f"Download job {job_id} completed successfully. Files: {saved_files}")

        except Exception as e:
            logging.error(f"Download job {job_id} failed: {str(e)}")
            logging.error(traceback.format_exc())
            progress_store[job_id].update({
                "status": "error",
                "error":  str(e),
            })

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/progress/<job_id>")
def get_progress(job_id):
    return jsonify(progress_store.get(job_id, {"status": "unknown"}))


@app.route("/api/files")
def list_files():
    try:
        files = []
        for fname in sorted(os.listdir(DOWNLOAD_DIR), reverse=True):
            fp = os.path.join(DOWNLOAD_DIR, fname)
            if os.path.isfile(fp):
                files.append({"name": fname, "size": os.path.getsize(fp)})
        return jsonify(files[:20])  # Only return last 20 files
    except Exception as e:
        logging.error(f"Error listing files: {str(e)}")
        return jsonify([])


@app.route("/api/files/clear", methods=["DELETE"])
def clear_all_files():
    """Delete every file inside the downloads folder."""
    deleted, errors = [], []
    for fname in os.listdir(DOWNLOAD_DIR):
        fp = os.path.join(DOWNLOAD_DIR, fname)
        if os.path.isfile(fp):
            try:
                os.remove(fp)
                deleted.append(fname)
            except Exception as e:
                errors.append("{}: {}".format(fname, str(e)))
    return jsonify({"deleted": len(deleted), "errors": errors})


@app.route("/api/files/delete/<path:filename>", methods=["DELETE"])
def delete_file(filename):
    """Delete a single file from the downloads folder."""
    fp = os.path.realpath(os.path.join(DOWNLOAD_DIR, filename))
    if not fp.startswith(DOWNLOAD_DIR):
        return jsonify({"error": "Forbidden"}), 403
    if not os.path.isfile(fp):
        return jsonify({"error": "File not found"}), 404
    os.remove(fp)
    return jsonify({"deleted": filename})


@app.route("/api/files/<path:filename>")
def serve_file(filename):
    fp = os.path.realpath(os.path.join(DOWNLOAD_DIR, filename))
    if not fp.startswith(DOWNLOAD_DIR):
        return jsonify({"error": "Forbidden"}), 403
    if not os.path.isfile(fp):
        return jsonify({"error": "File not found"}), 404
    mime, _ = mimetypes.guess_type(fp)
    return send_file(
        fp,
        mimetype=mime or "application/octet-stream",
        as_attachment=True,
        download_name=os.path.basename(fp),
    )


# ========== GLOBAL ERROR HANDLER WITH LOGGING ==========
@app.errorhandler(Exception)
def handle_error(e):
    """Catch ALL unhandled exceptions and log them"""
    logging.error("=" * 60)
    logging.error("UNHANDLED EXCEPTION CAUGHT BY GLOBAL HANDLER")
    logging.error(f"Error type: {type(e).__name__}")
    logging.error(f"Error message: {str(e)}")
    logging.error("Full traceback:")
    logging.error(traceback.format_exc())
    logging.error("=" * 60)
    
    # Also print to stdout as backup
    print("\n" + "!" * 60, flush=True)
    print(f"ERROR: {type(e).__name__}: {str(e)}", flush=True)
    print(traceback.format_exc(), flush=True)
    print("!" * 60 + "\n", flush=True)
    
    return jsonify({"error": str(e), "type": type(e).__name__}), 500


@app.before_request
def log_request_info():
    """Log all incoming requests"""
    logging.info(f"Request: {request.method} {request.path}")
    if request.is_json:
        logging.debug(f"Request data: {request.get_json()}")


# ========== RUN THE APP ==========
if __name__ == "__main__":
    print("=" * 50)
    print("  YouTube Downloader (pytubefix)")
    print("  Open: http://localhost:5000")
    print("  Files: ./downloads/")
    print("=" * 50)
    
    # Configure for Render.com
    port = int(os.environ.get("PORT", 5000))
    
    # Set debug=False for production, but keep error logging
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)