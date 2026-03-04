import os
import uuid
import glob
from pathlib import Path
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException, BackgroundTasks, Body
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from yt_dlp import YoutubeDL

# Define absolute paths for reliability
BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

app = FastAPI(title="Media Downloader API")

# CORS setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Optimized yt-dlp options for speed and reliability
BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "nocheckcertificate": True,
    "socket_timeout": 30,
    "retries": 10,
    "noplaylist": True,
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
    },
}

# In-memory store for jobs
jobs: Dict[str, Dict[str, Any]] = {}


def _find_downloaded_file(file_id: str) -> str:
    """Find the actual downloaded file by scanning the downloads directory."""
    matches = glob.glob(str(DOWNLOAD_DIR / f"{file_id}.*"))
    if not matches:
        raise FileNotFoundError(f"No downloaded file found for id {file_id}")
    
    # Priority for choosing the best file if multiple exist (e.g., source + merged)
    priority = [".mp4", ".webm", ".mkv", ".m4a", ".mp3", ".opus"]
    matches.sort(key=lambda p: next(
        (i for i, ext in enumerate(priority) if p.endswith(ext)), 999
    ))
    return matches[0]


def _progress_hook(d: Dict[str, Any], job_id: str):
    """Callback for yt-dlp progress updates."""
    if job_id not in jobs:
        return
    
    if d['status'] == 'downloading':
        p_str = d.get('_percent_str', '0%').replace('%', '').strip()
        try:
            jobs[job_id]["progress"] = float(p_str)
            jobs[job_id]["status"] = "downloading"
        except (ValueError, TypeError):
            pass
    elif d['status'] == 'finished':
        jobs[job_id]["status"] = "merging"
        jobs[job_id]["progress"] = 100


def _postprocessor_hook(d: Dict[str, Any], job_id: str):
    """Callback for yt-dlp post-processor updates (e.g., merging)."""
    if job_id not in jobs:
        return
    
    if d['status'] == 'started':
        jobs[job_id]["status"] = "processing"


def _background_download(job_id: str, url: str, format_id: Optional[str] = None, ext: Optional[str] = None, audio_only: bool = False):
    """Function to run yt-dlp in a background thread."""
    try:
        file_id = str(uuid.uuid4())
        output_template = str(DOWNLOAD_DIR / f"{file_id}.%(ext)s")

        ydl_opts = {
            **BASE_OPTS,
            "quiet": False,
            "outtmpl": output_template,
            "retries": 10,
            "merge_output_format": ext or "mp4",
            "progress_hooks": [lambda d: _progress_hook(d, job_id)],
            "postprocessor_hooks": [lambda d: _postprocessor_hook(d, job_id)],
        }

        if audio_only:
            ydl_opts.update({
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            })
        elif format_id:
            # Force container compatibility
            if ext == "mp4":
                audio_selector = "bestaudio[ext=m4a]/bestaudio/best"
            elif ext == "webm":
                audio_selector = "bestaudio[ext=webm]/bestaudio/best"
            else:
                audio_selector = "bestaudio/best"
            ydl_opts["format"] = f"{format_id}+{audio_selector}/best"
        else:
            ydl_opts["format"] = "bestvideo+bestaudio/best"

        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_title = info.get("title", "video")

        filepath = _find_downloaded_file(file_id)
        final_ext = Path(filepath).suffix
        safe_title = "".join(c for c in video_title if c.isalnum() or c in " -_()").strip()
        download_name = f"{safe_title[:80]}{final_ext}" if safe_title else f"download{final_ext}"

        jobs[job_id].update({
            "status": "ready",
            "progress": 100,
            "filepath": filepath,
            "filename": download_name
        })

    except Exception as e:
        jobs[job_id].update({
            "status": "error",
            "error": str(e)
        })


@app.get("/info")
def get_info(url: str):
    """Return basic video info (title, thumbnail, duration)."""
    try:
        opts = {**BASE_OPTS, "extract_flat": False}
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "title": info.get("title", "Unknown"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "uploader": info.get("uploader"),
            }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/formats")
def get_formats(url: str):
    """Return available video formats filtered and sorted."""
    try:
        with YoutubeDL(BASE_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
            formats: List[Dict[str, Any]] = []
            seen = set()
            
            for f in info.get("formats", []):
                width, height = f.get("width"), f.get("height")
                if not width or not height:
                    continue
                
                res_val = f"{height}p"
                if height >= 2160: res_val += " (4K)"
                elif height >= 1440: res_val += " (2K)"
                
                key = (height, f.get("ext"))
                if key in seen: continue
                seen.add(key)
                
                formats.append({
                    "format_id": f["format_id"],
                    "ext": f.get("ext"),
                    "resolution": f"{width}x{height} ({res_val})",
                    "filesize": f.get("filesize") or f.get("filesize_approx"),
                    "height": height,
                })
            
            formats.sort(key=lambda x: x["height"], reverse=True)
            return {"title": info.get("title"), "formats": formats, "thumbnail": info.get("thumbnail")}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/start-download")
def start_download(
    background_tasks: BackgroundTasks,
    url: str = Body(..., embed=True),
    format_id: Optional[str] = Body(None, embed=True),
    ext: Optional[str] = Body(None, embed=True),
    audio_only: bool = Body(False, embed=True)
):
    """Kicks off an asynchronous download job."""
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "preparing", "progress": 0}
    
    background_tasks.add_task(
        _background_download, job_id, url, format_id, ext, audio_only
    )
    return {"job_id": job_id}


@app.get("/progress/{job_id}")
def get_progress(job_id: str):
    """Return job progress or 404."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs[job_id]


@app.get("/download-file/{job_id}")
def download_file(job_id: str):
    """Serve completed download file."""
    if job_id not in jobs or jobs[job_id]["status"] != "ready":
        raise HTTPException(status_code=404, detail="File not ready or job not found")
    
    job = jobs[job_id]
    return FileResponse(
        job["filepath"],
        media_type="application/octet-stream",
        filename=job["filename"]
    )


@app.get("/")
def root():
    return FileResponse("frontend/index.html")

if __name__ == "__main__":
    import uvicorn
    # Railway sets the PORT environment variable
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("backend.main:app", host="0.0.0.0", port=port)