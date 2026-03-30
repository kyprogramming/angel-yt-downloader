"""
YouTube Downloader - Flask Backend (pytubefix)
===============================================
Local run:
    pip install -r requirements.txt
    gunicorn app:app --bind 0.0.0.0:5000 --workers 1 --threads 4 --worker-class gthread --timeout 600

Deploy to Render:
    Push to GitHub, connect repo on render.com — it reads render.yaml automatically.
"""

import os
import re
import threading
import uuid
import mimetypes
import logging
import traceback
import subprocess

from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS
from pytubefix import YouTube, Playlist

# ---------------------------------------------------------------------------
# Logging — goes to stdout so Render captures it in the dashboard
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)

_default_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", _default_dir)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
log.info("DOWNLOAD_DIR = %s", DOWNLOAD_DIR)

progress_store = {}   # job_id -> dict

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize(name):
    """Strip characters illegal in filenames and trim whitespace."""
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    name = name.strip().strip(".")
    return name or "video"


def is_playlist(url):
    return "playlist" in url.lower() or "list=" in url


def expand_urls(raw_urls):
    """
    Given a list of URLs (may include playlist URLs), return a flat list of
    individual video URLs.  Playlist URLs are expanded via pytubefix.Playlist.
    """
    expanded = []
    for url in raw_urls:
        url = url.strip()
        if not url:
            continue
        if is_playlist(url):
            try:
                pl = Playlist(url)
                video_urls = list(pl.video_urls)
                log.info("Playlist %s expanded to %d videos", url, len(video_urls))
                expanded.extend(video_urls)
            except Exception as e:
                log.error("Failed to expand playlist %s: %s\n%s", url, e, traceback.format_exc())
                # Fall through and try it as a plain video URL
                expanded.append(url)
        else:
            expanded.append(url)
    return expanded


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
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    with open(path, "r", encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html; charset=utf-8")


@app.route("/api/formats", methods=["POST"])
def get_formats():
    """
    Return available streams for the FIRST video URL provided.
    If a playlist URL is given, fetch info from the first video in the playlist.
    Also returns playlist metadata (title + count) if applicable.
    """
    body = request.json or {}
    url  = body.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    log.info("get_formats called: %s", url)

    try:
        playlist_info = None
        video_url     = url

        # ── Expand playlist to get first video URL + metadata ─────────────
        if is_playlist(url):
            try:
                pl = Playlist(url)
                video_urls = list(pl.video_urls)
                if not video_urls:
                    return jsonify({"error": "Playlist is empty or private"}), 400
                playlist_info = {
                    "title": getattr(pl, "title", None) or "Playlist",
                    "count": len(video_urls),
                    "urls":  video_urls,
                }
                video_url = video_urls[0]
                log.info("Playlist '%s' has %d videos; fetching formats from first", playlist_info["title"], len(video_urls))
            except Exception as e:
                log.error("Playlist expand error: %s\n%s", e, traceback.format_exc())
                return jsonify({"error": "Could not load playlist: " + str(e)}), 500

        # ── Fetch streams for the (first) video ───────────────────────────
        yt = YouTube(video_url)

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
            make_preset("merge_2160p", "4K  (2160p) + Audio", "2160p", " — very large file"),
            make_preset("merge_1440p", "2K  (1440p) + Audio", "1440p", " — large file"),
            make_preset("merge_1080p", "FHD (1080p) + Audio", "1080p"),
            make_preset("merge_720p",  "HD  (720p)  + Audio", "720p"),
            make_preset("merge_480p",  "SD  (480p)  + Audio", "480p"),
            make_preset("merge_360p",  "360p + Audio",         "360p"),
            make_preset("merge_240p",  "240p + Audio",         "240p"),
            make_preset("merge_144p",  "144p + Audio",         "144p", " — smallest size"),
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

        raw_fmts = []
        for s in yt.streams:
            try:
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
            except Exception as se:
                log.warning("Skipping stream itag=%s: %s", getattr(s, "itag", "?"), se)

        def sort_key(f):
            if f["type"] == "video":
                try:
                    return (0, -int(f["resolution"].replace("p", "")))
                except Exception:
                    return (0, 0)
            return (1, 0)

        raw_fmts.sort(key=sort_key)

        thumb    = getattr(yt, "thumbnail_url", "") or ""
        secs     = yt.length or 0
        duration = "{}:{:02d}".format(secs // 60, secs % 60)

        resp = {
            "title":    yt.title or "Unknown",
            "thumbnail": thumb,
            "duration": duration,
            "uploader": yt.author or "-",
            "formats":  presets + raw_fmts,
        }
        if playlist_info:
            resp["playlist"] = playlist_info

        log.info("get_formats OK: '%s', %d streams", resp["title"], len(raw_fmts))
        return jsonify(resp)

    except Exception as e:
        log.error("get_formats error for %s: %s\n%s", url, e, traceback.format_exc())
        return jsonify({"error": str(e), "detail": traceback.format_exc()}), 500


@app.route("/api/download", methods=["POST"])
def start_download():
    body      = request.json or {}
    raw_urls  = body.get("urls", [])
    format_id = body.get("format_id", "best_progressive")

    if not raw_urls:
        return jsonify({"error": "No URLs provided"}), 400

    # Expand playlists → flat video URL list
    try:
        urls = expand_urls(raw_urls)
    except Exception as e:
        log.error("expand_urls failed: %s\n%s", e, traceback.format_exc())
        return jsonify({"error": "Failed to expand URLs: " + str(e)}), 500

    if not urls:
        return jsonify({"error": "No downloadable URLs found"}), 400

    log.info("Download job started: %d URLs, format=%s", len(urls), format_id)

    job_id = str(uuid.uuid4())[:8]
    progress_store[job_id] = {
        "status":        "starting",
        "current":       0,
        "total":         len(urls),
        "video_title":   "",
        "video_percent": 0,
        "percent":       0,
        "note":          "",
        "files":         [],
        "error":         "",
        "errors":        [],
    }

    def run():

        def make_video_hook(jid):
            def hook(stream, chunk, bytes_remaining):
                total = stream.filesize or 0
                done  = total - bytes_remaining
                vpct  = int(done / total * 100) if total else 0
                progress_store[jid]["video_percent"] = vpct
                idx   = progress_store[jid]["current"]
                tot   = progress_store[jid]["total"]
                progress_store[jid]["percent"] = int(((idx - 1) + vpct / 100) / tot * 100) if tot else vpct
            return hook

        def pick_video_stream(yt, target_res):
            """Return best adaptive mp4 video stream at or below target_res."""
            vid_streams = list(
                yt.streams
                .filter(adaptive=True, only_video=True, file_extension="mp4")
                .order_by("resolution")
            )
            if not vid_streams:
                # Fallback: any adaptive video stream
                vid_streams = list(
                    yt.streams.filter(adaptive=True, only_video=True).order_by("resolution")
                )

            if target_res:
                # Exact match first
                for s in vid_streams:
                    if s.resolution == target_res:
                        return s
                # Best available at or below target
                target_int = int(target_res.replace("p", ""))
                candidates = []
                for s in vid_streams:
                    try:
                        if int(s.resolution.replace("p", "")) <= target_int:
                            candidates.append(s)
                    except Exception:
                        pass
                if candidates:
                    return max(candidates, key=lambda s: int(s.resolution.replace("p", "")))

            # Highest available
            return vid_streams[-1] if vid_streams else None

        def merge_video_audio(yt, title_safe, target_res):
            progress_store[job_id]["status"] = "downloading"

            v_stream = pick_video_stream(yt, target_res)
            if not v_stream:
                raise Exception("No video stream found (target={})".format(target_res))

            a_stream = yt.streams.filter(only_audio=True).order_by("abr").last()
            if not a_stream:
                raise Exception("No audio stream found")

            actual_res = v_stream.resolution or "video"
            log.info("  video stream: itag=%s res=%s  audio: itag=%s abr=%s",
                     v_stream.itag, actual_res, a_stream.itag, a_stream.abr)

            progress_store[job_id]["note"] = "Downloading {} video...".format(actual_res)
            v_tmp    = title_safe + "_vtmp.mp4"
            a_ext    = a_stream.mime_type.split("/")[-1]
            a_tmp    = title_safe + "_atmp." + a_ext
            out_name = "{}_{}_merged.mp4".format(title_safe, actual_res)
            v_path   = os.path.join(DOWNLOAD_DIR, v_tmp)
            a_path   = os.path.join(DOWNLOAD_DIR, a_tmp)
            out_path = os.path.join(DOWNLOAD_DIR, out_name)

            v_stream.download(output_path=DOWNLOAD_DIR, filename=v_tmp)
            log.info("  video downloaded: %s", v_path)

            progress_store[job_id]["note"] = "Downloading audio..."
            a_stream.download(output_path=DOWNLOAD_DIR, filename=a_tmp)
            log.info("  audio downloaded: %s", a_path)

            progress_store[job_id]["status"] = "processing"
            progress_store[job_id]["note"]   = "Merging with FFmpeg..."

            cmd = ["ffmpeg", "-y", "-i", v_path, "-i", a_path,
                   "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                   "-movflags", "+faststart", out_path]
            log.info("  FFmpeg cmd: %s", " ".join(cmd))
            result = subprocess.run(cmd, capture_output=True, text=True)

            for tmp in [v_path, a_path]:
                try:
                    os.remove(tmp)
                except Exception:
                    pass

            if result.returncode != 0:
                log.error("FFmpeg failed (rc=%d):\nSTDOUT: %s\nSTDERR: %s",
                          result.returncode, result.stdout[-500:], result.stderr[-500:])
                raise Exception("FFmpeg failed (rc={}): {}".format(result.returncode, result.stderr[-300:]))

            log.info("  merged: %s", out_path)
            return out_name

        MERGE_PRESETS = {
            "merge_2160p": "2160p",
            "merge_1440p": "1440p",
            "merge_1080p": "1080p",
            "merge_720p":  "720p",
            "merge_480p":  "480p",
            "merge_360p":  "360p",
            "merge_240p":  "240p",
            "merge_144p":  "144p",
            "best_video":  None,
        }

        saved_files = []
        total       = len(urls)

        for idx, url in enumerate(urls, start=1):
            log.info("Downloading video %d/%d: %s", idx, total, url)
            progress_store[job_id].update({
                "status":        "downloading",
                "current":       idx,
                "video_percent": 0,
                "note":          "Fetching video info...",
            })
            try:
                yt         = YouTube(url, on_progress_callback=make_video_hook(job_id))
                title      = yt.title or "video_{}".format(idx)
                title_safe = sanitize(title)
                progress_store[job_id]["video_title"] = title
                log.info("  title: %s", title)

                if format_id in MERGE_PRESETS:
                    fname = merge_video_audio(yt, title_safe, target_res=MERGE_PRESETS[format_id])

                elif format_id == "best_progressive":
                    stream = yt.streams.get_highest_resolution()
                    if not stream:
                        raise Exception("No progressive stream found")
                    fname = title_safe + "." + stream.mime_type.split("/")[-1]
                    stream.download(output_path=DOWNLOAD_DIR, filename=fname)

                elif format_id == "best_audio":
                    stream = yt.streams.filter(only_audio=True).order_by("abr").last()
                    if not stream:
                        raise Exception("No audio stream found")
                    fname = title_safe + "." + stream.mime_type.split("/")[-1]
                    stream.download(output_path=DOWNLOAD_DIR, filename=fname)

                else:
                    try:
                        itag = int(format_id)
                    except ValueError:
                        raise Exception("Unknown format: " + format_id)
                    stream = yt.streams.get_by_itag(itag)
                    if not stream:
                        raise Exception("Stream itag {} not found".format(itag))
                    fname = title_safe + "." + stream.mime_type.split("/")[-1]
                    stream.download(output_path=DOWNLOAD_DIR, filename=fname)

                saved_files.append(fname)
                progress_store[job_id]["percent"] = int(idx / total * 100)
                log.info("  saved: %s", fname)

            except Exception as e:
                err_msg = "Video {}/{} '{}' failed: {}".format(idx, total, url, str(e))
                log.error("%s\n%s", err_msg, traceback.format_exc())
                progress_store[job_id]["errors"].append(err_msg)

        if saved_files:
            progress_store[job_id].update({
                "status":  "done",
                "percent": 100,
                "note":    "Done! {} of {} saved.".format(len(saved_files), total),
                "files":   saved_files,
            })
            log.info("Job %s done: %d/%d files saved", job_id, len(saved_files), total)
        else:
            msg = "All {} video(s) failed. Check errors list.".format(total)
            progress_store[job_id].update({"status": "error", "error": msg})
            log.error("Job %s: %s", job_id, msg)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id, "total": len(urls)})


@app.route("/api/progress/<job_id>")
def get_progress(job_id):
    return jsonify(progress_store.get(job_id, {"status": "unknown"}))


@app.route("/api/files")
def list_files():
    files = []
    for fname in sorted(os.listdir(DOWNLOAD_DIR)):
        fp = os.path.join(DOWNLOAD_DIR, fname)
        if os.path.isfile(fp):
            files.append({"name": fname, "size": os.path.getsize(fp)})
    return jsonify(files)


@app.route("/api/files/clear", methods=["DELETE"])
def clear_all_files():
    deleted, errors = [], []
    for fname in os.listdir(DOWNLOAD_DIR):
        fp = os.path.join(DOWNLOAD_DIR, fname)
        if os.path.isfile(fp):
            try:
                os.remove(fp)
                deleted.append(fname)
            except Exception as e:
                errors.append("{}: {}".format(fname, str(e)))
    log.info("Cleared %d files", len(deleted))
    return jsonify({"deleted": len(deleted), "errors": errors})


@app.route("/api/files/delete/<path:filename>", methods=["DELETE"])
def delete_file(filename):
    fp = os.path.realpath(os.path.join(DOWNLOAD_DIR, filename))
    if not fp.startswith(DOWNLOAD_DIR):
        return jsonify({"error": "Forbidden"}), 403
    if not os.path.isfile(fp):
        return jsonify({"error": "File not found"}), 404
    os.remove(fp)
    log.info("Deleted file: %s", filename)
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


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info("Starting dev server on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)