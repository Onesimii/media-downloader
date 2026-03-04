import os
import uuid
import glob
import time
import asyncio
import re
from pathlib import Path
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, HTTPException, BackgroundTasks, Body, Request
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

# Optimized yt-dlp options for speed and reliability, with bot detection countermeasures
BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "nocheckcertificate": True,
    "socket_timeout": 30,
    "retries": 10,
    "noplaylist": True,
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"]
        }
    },
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 10; Mobile) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/90.0.4430.91 Mobile Safari/537.36"
        ),
    },
}

# Configuration constants
MAX_FILE_SIZE_MB = 800
CLEANUP_INTERVAL_SEC = 60
FILE_EXPIRY_SEC = 20 * 60  # 20 minutes
RATE_LIMIT_REQUESTS = 5
RATE_LIMIT_WINDOW_SEC = 60

# In-memory stores
jobs: Dict[str, Dict[str, Any]] = {}
rate_limit_store: Dict[str, List[float]] = {}

# URL validation regex
URL_REGEX = re.compile(
    r'^(https?://)?(www\.)?(youtube\.com|youtu\.be|tiktok\.com|instagram\.com|facebook\.com)/.+$'
)

def _is_rate_limited(ip: str) -> bool:
    """Simple sliding window rate limiter."""
    now = time.time()
    if ip not in rate_limit_store:
        rate_limit_store[ip] = []
    
    # Filter out old requests
    rate_limit_store[ip] = [t for t in rate_limit_store[ip] if now - t < RATE_LIMIT_WINDOW_SEC]
    
    if len(rate_limit_store[ip]) >= RATE_LIMIT_REQUESTS:
        return True
    
    rate_limit_store[ip].append(now)
    return False

async def _cleanup_task():
    """Background task to delete old files and stale jobs."""
    while True:
        try:
            now = time.time()
            # 1. Cleanup jobs and files
            to_delete_jobs = []
            for job_id, job in jobs.items():
                created_at = job.get("created_at", 0)
                if created_at > 0 and now - created_at > FILE_EXPIRY_SEC:
                    # Delete actual file if it exists
                    filepath = job.get("filepath")
                    if filepath and os.path.exists(filepath):
                        try:
                            os.remove(filepath)
                        except Exception:
                            pass
                    to_delete_jobs.append(job_id)
            
            for job_id in to_delete_jobs:
                if job_id in jobs:
                    del jobs[job_id]

            # 2. Cleanup orphaned files in DOWNLOAD_DIR
            for f in glob.glob(str(DOWNLOAD_DIR / "*")):
                if now - os.path.getmtime(f) > FILE_EXPIRY_SEC:
                    try:
                        os.remove(f)
                    except Exception:
                        pass
                        
        except Exception:
            pass
        await asyncio.sleep(CLEANUP_INTERVAL_SEC)


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
def get_info(request: Request, url: str):
    """Return basic video info (title, thumbnail, duration)."""
    if _is_rate_limited(request.client.host):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Please wait a minute.")

    if not URL_REGEX.match(url):
        raise HTTPException(status_code=400, detail="Invalid video URL.")

    try:
        opts = {**BASE_OPTS, "extract_flat": False}
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # Size check
            filesize = info.get("filesize") or info.get("filesize_approx") or 0
            if filesize > MAX_FILE_SIZE_MB * 1024 * 1024:
                raise HTTPException(status_code=400, detail=f"File exceeds limit ({MAX_FILE_SIZE_MB}MB).")

            return {
                "title": info.get("title", "Unknown"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "uploader": info.get("uploader"),
                "filesize": filesize
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error fetching info: {str(e)}")


@app.get("/formats")
def get_formats(request: Request, url: str):
    """Return available video formats filtered and sorted."""
    if _is_rate_limited(request.client.host):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Please wait a minute.")

    if not URL_REGEX.match(url):
        raise HTTPException(status_code=400, detail="Invalid video URL.")

    try:
        with YoutubeDL(BASE_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
            formats_raw = info.get("formats", [])
            
            filtered_map = {} # height -> best_format_object
            
            for f in formats_raw:
                width = f.get("width")
                height = f.get("height")
                ext = f.get("ext", "")
                filesize = f.get("filesize") or f.get("filesize_approx")
                
                # 1. Basic filtering
                if not width or not height: continue
                if height < 360: continue # Only >= 360p
                if not filesize: continue # Exclude unknown size
                if ext == "mhtml": continue # Exclude mhtml
                
                # 2. Duplicate resolution handling (Prefer MP4)
                current_best = filtered_map.get(height)
                if not current_best:
                    filtered_map[height] = f
                else:
                    # If this is mp4 and previous wasn't, or if previous was mhtml? (already filtered)
                    # Simple rule: if new one is mp4, replace older non-mp4
                    if ext == "mp4" and current_best.get("ext") != "mp4":
                        filtered_map[height] = f

            # 3. Format the results
            final_formats = []
            for height, f in filtered_map.items():
                width = f.get("width")
                res_val = f"{height}p"
                if height >= 2160: res_val += " (4K)"
                elif height >= 1440: res_val += " (2K)"
                
                final_formats.append({
                    "format_id": f["format_id"],
                    "ext": f.get("ext"),
                    "resolution": f"{width}x{height} ({res_val})",
                    "filesize": f.get("filesize") or f.get("filesize_approx"),
                    "height": height,
                })
            
            # 4. Sort by height descending
            final_formats.sort(key=lambda x: x["height"], reverse=True)
            
            return {
                "title": info.get("title"), 
                "formats": final_formats, 
                "thumbnail": info.get("thumbnail")
            }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error fetching formats: {str(e)}")


@app.post("/start-download")
def start_download(
    request: Request,
    background_tasks: BackgroundTasks,
    url: str = Body(..., embed=True),
    format_id: Optional[str] = Body(None, embed=True),
    ext: Optional[str] = Body(None, embed=True),
    audio_only: bool = Body(False, embed=True)
):
    """Kicks off an asynchronous download job."""
    if _is_rate_limited(request.client.host):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Please wait a minute.")

    if not URL_REGEX.match(url):
        raise HTTPException(status_code=400, detail="Invalid video URL.")

    # Preliminary size check before starting download
    try:
        with YoutubeDL(BASE_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
            filesize = info.get("filesize") or info.get("filesize_approx") or 0
            if filesize > MAX_FILE_SIZE_MB * 1024 * 1024:
                raise HTTPException(status_code=400, detail=f"File too large ({MAX_FILE_SIZE_MB}MB limit).")
    except HTTPException:
        raise
    except Exception:
        pass # Ignore extract errors here, fallback to _background_download error handling

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "preparing", 
        "progress": 0, 
        "created_at": time.time()
    }
    
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
    # Robust path discovery for Railway (both Docker and Nixpacks)
    candidates = [
        BASE_DIR.parent / "frontend" / "index.html",  # Standard layout
        BASE_DIR / "frontend" / "index.html",         # Docker layout
        Path("frontend/index.html").absolute(),        # Direct reference
    ]
    for path in candidates:
        if path.is_file():
            return FileResponse(str(path))
    
    raise HTTPException(status_code=404, detail="frontend/index.html not found")

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(_cleanup_task())

if __name__ == "__main__":
    import uvicorn
    # Railway sets the PORT environment variable
    port = int(os.environ.get("PORT", 8000))
    # Pass the app object directly to avoid ModuleNotFoundError on Railway
    # which occurs when project structure differs (Docker vs. local)
    uvicorn.run(app, host="0.0.0.0", port=port)