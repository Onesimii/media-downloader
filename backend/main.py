import os
import uuid
import glob
import time
import asyncio
import re
from pathlib import Path
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, HTTPException, BackgroundTasks, Body, Request, Depends, Response
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from yt_dlp import YoutubeDL
import stripe
import requests
import json
from sqlalchemy import Column, String, Boolean, Integer, Float, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from sqlalchemy import create_engine
import datetime

# Define absolute paths for reliability
BASE_DIR = Path(__file__).resolve().parent
DATABASE_URL = f"sqlite:///{BASE_DIR}/downloader.db"

# Stripe Setup
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "sk_test_51P...")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "whsec_...")
PRO_PLAN_PRICE_ID = os.environ.get("PRO_PLAN_PRICE_ID", "price_...")
stripe.api_key = STRIPE_SECRET_KEY

# Spotify API Setup (Optional)
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")
_spotify_token: Dict[str, Any] = {"token": None, "expires_at": 0}

# Database Setup
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True, index=True) # Browser UUID
    ip_address = Column(String, index=True, nullable=True) # Tracked for security
    is_pro = Column(Boolean, default=False)
    downloads_today = Column(Integer, default=0)
    last_download_date = Column(DateTime, default=datetime.datetime.utcnow)
    stripe_customer_id = Column(String, nullable=True)

Base.metadata.create_all(bind=engine)
DOWNLOAD_DIR = BASE_DIR / "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Create background cleanup task
    asyncio.create_task(_cleanup_task())
    yield
    # Shutdown logic (if any) could go here

app = FastAPI(title="Media Downloader API", lifespan=lifespan)

# CORS setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

@app.middleware("http")
async def ensure_user_id_cookie(request: Request, call_next):
    user_id = request.cookies.get("user_id")
    response = await call_next(request)
    if not user_id:
        # If no user_id in request, check if one was generated or just give a new one
        # For simplicity, if it's missing, we generate one here for the next request
        new_id = str(uuid.uuid4())
        response.set_cookie(key="user_id", value=new_id, max_age=31536000, httponly=True, samesite="lax")
    return response

# Cookies file path for YouTube authentication (place cookies.txt in backend/ dir)
COOKIES_FILE = BASE_DIR / "cookies.txt"

# yt-dlp configuration with bot detection countermeasures
BASE_OPTS = {
    "format": "bestvideo+bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "geo_bypass": True,
    "nocheckcertificate": True,
    "http_headers": {
        "User-Agent": "com.google.android.youtube"
    },
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"]
        }
    }
}

# If cookies.txt exists, use it for YouTube authentication
if COOKIES_FILE.exists():
    BASE_OPTS["cookiefile"] = str(COOKIES_FILE)
    print(f"[yt-dlp] Using cookies from: {COOKIES_FILE}")
else:
    print(f"[yt-dlp] WARNING: No cookies.txt found at {COOKIES_FILE}. YouTube may block requests.")
    print("[yt-dlp] To fix: export cookies from your browser and place cookies.txt in the backend/ directory.")
    print("[yt-dlp] See: https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp")

# Proxy support: set YOUTUBE_PROXY env var (e.g. http://user:pass@host:port)
YOUTUBE_PROXY = os.environ.get("YOUTUBE_PROXY")
if YOUTUBE_PROXY:
    BASE_OPTS["proxy"] = YOUTUBE_PROXY
    print(f"[yt-dlp] Using proxy: {YOUTUBE_PROXY.split('@')[-1] if '@' in YOUTUBE_PROXY else YOUTUBE_PROXY}")
else:
    print("[yt-dlp] No proxy configured. Using direct connection.")

def clean_youtube_url(url: str) -> str:
    """Strip tracking parameters (like ?si=) from YouTube URLs."""
    if "youtube.com" in url or "youtu.be" in url:
        # Simple split to remove query params
        return url.split('?')[0].split('&')[0]
    return url

# Configuration constants
MAX_FILE_SIZE_MB = 800
CLEANUP_INTERVAL_SEC = 60
FILE_EXPIRY_SEC = 20 * 60  # 20 minutes
RATE_LIMIT_REQUESTS = 5
RATE_LIMIT_WINDOW_SEC = 60

# In-memory stores
jobs: Dict[str, Dict[str, Any]] = {}
rate_limit_store: Dict[str, List[float]] = {}

import zipfile
import tempfile
import shutil

# Helper for DB sessions
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def _get_or_create_user(db, user_id: str, ip: Optional[str] = None) -> User:
    # 1. Try finding by user_id
    user = db.query(User).filter(User.id == user_id).first()
    
    # 2. If not found by ID, try finding by IP (to prevent simple cookie clearing)
    if not user and ip:
        user = db.query(User).filter(User.ip_address == ip).first()
    
    if not user:
        user = User(id=user_id, ip_address=ip)
        db.add(user)
    else:
        # Update user_id or IP if one changed but other matched
        if not user.ip_address:
            user.ip_address = ip
        if not user.id:
            user.id = user_id
            
    db.commit()
    db.refresh(user)
    
    # Reset daily downloads if new day
    now = datetime.datetime.utcnow()
    if user.last_download_date.date() < now.date():
        user.downloads_today = 0
        user.last_download_date = now
        db.commit()
    
    return user

async def get_secure_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency to get or create user based on secure cookies and IP."""
    user_id = request.cookies.get("user_id")
    ip = request.client.host
    
    if not user_id:
        user_id = str(uuid.uuid4())
        # We can't set cookie directly in dependency return, 
        # so we'll handle it in the response if needed, 
        # but for logic we just use this generated ID.
        
    user = _get_or_create_user(db, user_id, ip)
    return user

def _get_spotify_access_token() -> Optional[str]:
    """Get temporary Spotify API token using Client Credentials flow."""
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    
    now = time.time()
    if _spotify_token["token"] and now < _spotify_token["expires_at"]:
        return _spotify_token["token"]
        
    try:
        url = "https://accounts.spotify.com/api/token"
        data = {"grant_type": "client_credentials"}
        response = requests.post(
            url, 
            auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET), 
            data=data, 
            timeout=10
        )
        response.raise_for_status()
        res_data = response.json()
        _spotify_token["token"] = res_data["access_token"]
        _spotify_token["expires_at"] = now + res_data["expires_in"] - 60
        return _spotify_token["token"]
    except Exception as e:
        print(f"Spotify token error: {e}")
        return None

def _get_spotify_metadata(url: str) -> Dict[str, str]:
    """Extract metadata from Spotify API or Embed endpoint."""
    try:
        # Extract track ID
        track_id_match = re.search(r'track/([a-zA-Z0-9]+)', url)
        if not track_id_match:
            raise ValueError("Invalid Spotify track URL")
        track_id = track_id_match.group(1)

        token = _get_spotify_access_token()
        if token:
            # Use official API
            api_url = f"https://api.spotify.com/v1/tracks/{track_id}"
            headers = {"Authorization": f"Bearer {token}"}
            response = requests.get(api_url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                return {
                    "title": data.get("name"),
                    "artist": data.get("artists", [{}])[0].get("name"),
                    "thumbnail": data.get("album", {}).get("images", [{}])[0].get("url")
                }

        # Fallback to Embed endpoint (token-less)
        embed_url = f"https://open.spotify.com/embed/track/{track_id}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(embed_url, headers=headers, timeout=10)
        response.raise_for_status()
        html = response.text
        
        # OG tags or state data
        title_match = re.search(r'<meta property="og:title" content="([^"]+)">', html)
        artist_match = re.search(r'<meta property="og:description" content="([^·]+) · [^"]+">', html)
        thumb_match = re.search(r'<meta property="og:image" content="([^"]+)">', html)

        title = title_match.group(1) if title_match else ""
        artist = artist_match.group(1).strip() if artist_match else ""
        thumbnail = thumb_match.group(1) if thumb_match else ""

        # Cleanup generic suffixes
        if " - song by " in title.lower():
            title = title.split(" - song by ")[0]
            
        if not title or not artist:
            # Final attempts at parsing script id="resource"
            state_match = re.search(r'<script id="resource" type="application/json">([^<]+)</script>', html)
            if state_match:
                data = json.loads(state_match.group(1))
                title = data.get("name", title)
                artist = data.get("artists", [{}])[0].get("name", artist)
                thumbnail = data.get("album", {}).get("images", [{}])[0].get("url", thumbnail)

        if not title or not artist:
            raise ValueError("Metadata not found in Spotify page")

        return {"title": title, "artist": artist, "thumbnail": thumbnail}
    except Exception as e:
        print(f"Spotify metadata fetch error: {e}")
        raise e

# URL validation regex (Updated for Spotify & SoundCloud)
URL_REGEX = re.compile(
    r'^(https?://)?(www\.|m\.)?(youtube\.com|youtu\.be|tiktok\.com|instagram\.com|facebook\.com|open\.spotify\.com|soundcloud\.com)/.+$'
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
    priority = [".mp4", ".webm", ".mkv", ".m4a", ".mp3", ".opus", ".zip"]
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
            "merge_output_format": "mp4",
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
            # Merges requested video with best audio
            ydl_opts["format"] = f"{format_id}+bestaudio/best"
        else:
            ydl_opts["format"] = "bestvideo+bestaudio/best"

        if "spotify.com/track/" in url:
            # For Spotify, fetch metadata via API/Embed (bypass yt-dlp DRM errors)
            try:
                meta = _get_spotify_metadata(url)
                title = meta["title"]
                artist = meta["artist"]
            except Exception as e:
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["error"] = f"Failed to get Spotify metadata: {str(e)}"
                return

            # Refined search query for better accuracy
            search_query = f"ytsearch1:{artist} - {title} official audio"
                
            # Now download from YouTube search result
            with YoutubeDL(ydl_opts) as ydl:
                search_info = ydl.extract_info(search_query, download=True)
                if "entries" in search_info:
                    search_info = search_info["entries"][0]
                
                # After download, rename to a clean filename: "Artist - Title.mp3"
                temp_filepath = _find_downloaded_file(file_id)
                final_filename = f"{artist} - {title}.mp3".replace("/", "_") # Sanitize
                final_filepath = DOWNLOAD_DIR / final_filename
                
                if os.path.exists(temp_filepath):
                    os.rename(temp_filepath, final_filepath)
                
                jobs[job_id]["filepath"] = str(final_filepath)
                video_title = f"{artist} - {title}"
        elif "spotify.com/playlist/" in url:
             # Handle playlist (already implemented fallback logic)
             with YoutubeDL({**BASE_OPTS, "extract_flat": True}) as ydl:
                info = ydl.extract_info(url, download=False)
                video_title = info.get("title", "Spotify Playlist")
        else:
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

def _playlist_download_task(job_id: str, playlist_name: str, tracks: List[str]):
    """Background task to download multiple tracks and ZIP them."""
    try:
        temp_dir = Path(tempfile.mkdtemp(dir=DOWNLOAD_DIR))
        track_files = []
        total = len(tracks)

        for i, track_name in enumerate(tracks):
            jobs[job_id]["status"] = "downloading"
            jobs[job_id]["progress"] = (i / total) * 90
            jobs[job_id]["progress_text"] = f"Track {i+1} of {total}: {track_name}"

            track_id = str(uuid.uuid4())
            track_template = str(temp_dir / f"{track_id}.%(ext)s")

            ydl_opts = {
                **BASE_OPTS,
                "outtmpl": track_template,
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            }

            # Search on YouTube
            search_query = f"ytsearch1:{track_name}"
            try:
                with YoutubeDL(ydl_opts) as ydl:
                    ydl.download([search_query])
                
                # Find the created MP3
                downloaded = glob.glob(str(temp_dir / f"{track_id}.mp3"))
                if downloaded:
                    # Rename to something readable for the ZIP
                    safe_name = "".join(c for c in track_name if c.isalnum() or c in " -_()").strip()
                    final_path = temp_dir / f"{safe_name[:60]}.mp3"
                    os.rename(downloaded[0], final_path)
                    track_files.append(final_path)
            except Exception as e:
                print(f"Error downloading track {track_name}: {e}")

        # ZIP it up
        jobs[job_id]["status"] = "zipping"
        jobs[job_id]["progress"] = 95
        
        file_id = str(uuid.uuid4())
        zip_path = DOWNLOAD_DIR / f"{file_id}.zip"
        
        with zipfile.ZipFile(zip_path, 'w') as zipf:
            for f in track_files:
                zipf.write(f, arcname=f.name)

        # Cleanup temp dir
        shutil.rmtree(temp_dir)

        jobs[job_id].update({
            "status": "ready",
            "progress": 100,
            "filepath": str(zip_path),
            "filename": f"{playlist_name}.zip"
        })

    except Exception as e:
        jobs[job_id].update({
            "status": "error",
            "error": str(e)
        })


@app.get("/info")
def get_info(request: Request, url: str):
    """Return basic video info (title, thumbnail, duration)."""
    url = clean_youtube_url(url)
    
    if _is_rate_limited(request.client.host):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Please wait a minute.")

    if not URL_REGEX.match(url):
        raise HTTPException(status_code=400, detail="Invalid video URL.")

    try:
        is_spotify = "spotify.com/track/" in url
        opts = {**BASE_OPTS, "extract_flat": is_spotify}
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            if info is None:
                raise HTTPException(status_code=400, detail="Could not retrieve information for this URL. It might be private, restricted, or age-gated.")

            if is_spotify:
                try:
                    meta = _get_spotify_metadata(url)
                    return {
                        "title": meta["title"],
                        "thumbnail": meta["thumbnail"],
                        "duration": 0, # Duration not available in embed tags easily
                        "uploader": meta["artist"],
                        "filesize": 0,
                        "is_spotify": True
                    }
                except Exception as e:
                   raise HTTPException(status_code=400, detail=f"Could not extract metadata from Spotify: {str(e)}")
            
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
        err_msg = str(e)
        if "Sign in to confirm you're not a bot" in err_msg:
            raise HTTPException(status_code=403, detail="YouTube is blocking this request (bot detection). We are trying to bypass it, but some videos may still be restricted.")
        raise HTTPException(status_code=400, detail=f"Error fetching info: {err_msg}")


@app.get("/formats")
def get_formats(request: Request, url: str, user: User = Depends(get_secure_user)):
    """Return available video formats filtered and sorted."""
    url = clean_youtube_url(url)
    is_pro = user.is_pro

    if _is_rate_limited(request.client.host):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Please wait a minute.")

    if not URL_REGEX.match(url):
        raise HTTPException(status_code=400, detail="Invalid video URL.")

    # Prevent formats endpoint from hanging on Spotify links
    if "spotify.com/" in url:
        return {"title": "Spotify", "formats": [], "is_spotify": True}
        
    try:
        with YoutubeDL(BASE_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)

            if info is None:
                raise HTTPException(status_code=400, detail="Could not extract format details. The video might be private, restricted (e.g. by uploader), or age-gated.")

            formats_raw = info.get("formats", [])
            if not formats_raw:
                raise HTTPException(status_code=400, detail="No downloadable formats found for this URL.")
            
            filtered_map = {} # height -> best_format_object
            
            for f in formats_raw:
                width = f.get("width")
                height = f.get("height")
                ext = f.get("ext", "")
                vcodec = f.get("vcodec", "none")
                acodec = f.get("acodec", "none")
                
                # 1. Basic filtering
                if not width or not height: continue
                if height < 360: continue # Only >= 360p
                if not is_pro and height > 720: continue # Limit free to 720p
                if ext == "mhtml": continue # Exclude mhtml
                
                # 2. Identify if it's a video stream (combined or video-only)
                if vcodec == "none": continue
                
                # 2. Duplicate resolution handling (Prefer MP4 if available)
                current_best = filtered_map.get(height)
                if not current_best:
                    filtered_map[height] = f
                else:
                    # Preference: Combined MP4 > Combined > Video-only MP4 > Video-only
                    def get_score(fmt):
                        score = 0
                        if fmt.get("acodec") != "none": score += 10
                        if fmt.get("ext") == "mp4": score += 5
                        return score
                    
                    if get_score(f) > get_score(current_best):
                        filtered_map[height] = f

            # 3. Format the results
            final_formats = []
            for height, f in filtered_map.items():
                width = f.get("width")
                res_val = f"{height}p"
                if height >= 2160: res_val += " (4K)"
                elif height >= 1440: res_val += " (2K)"
                
                # Check if it needs merging (video-only)
                is_video_only = f.get("acodec") == "none"
                info_suffix = " (Needs Merging)" if is_video_only else ""
                
                final_formats.append({
                    "format_id": f["format_id"],
                    "ext": "mp4" if is_video_only else f.get("ext"),
                    "resolution": f"{width}x{height} ({res_val}){info_suffix}",
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
        err_msg = str(e)
        if "Sign in to confirm you're not a bot" in err_msg:
            raise HTTPException(status_code=403, detail="YouTube is blocking this request (bot detection). Try again in a few minutes or try another video.")
        raise HTTPException(status_code=400, detail=f"Error fetching formats: {err_msg}")

@app.get("/playlist-info")
def get_playlist_info(request: Request, url: str, user: User = Depends(get_secure_user)):
    """Extract tracks from a Spotify playlist using yt-dlp flat-playlist."""
    if _is_rate_limited(request.client.host):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    
    try:
        # We use extract_flat to avoid DRM stream errors and just get metadata
        opts = {**BASE_OPTS, "extract_flat": "in_playlist", "playlist_items": "1-30"}
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            if info is None:
                raise HTTPException(status_code=400, detail="Failed to fetch playlist information.")

            entries = info.get("entries", [])
            tracks_data = []
            for e in entries:
                tracks_data.append({
                    "title": e.get("title", "Unknown Track"),
                    "artist": e.get("uploader", "Unknown Artist"),
                    "selected": True
                })
            
            return {
                "title": info.get("title", "Spotify Playlist"),
                "thumbnail": info.get("thumbnails")[0]["url"] if info.get("thumbnails") else None,
                "tracks": tracks_data
            }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch playlist: {str(e)}")

@app.post("/start-playlist-download")
def start_playlist_download(
    request: Request,
    background_tasks: BackgroundTasks,
    playlist_name: str = Body(..., embed=True),
    tracks: List[str] = Body(..., embed=True),
    user: User = Depends(get_secure_user)
):
    
    if not user.is_pro:
        raise HTTPException(status_code=403, detail="Playlist downloads are only available for PRO accounts. Upgrade to PRO today!")

    if _is_rate_limited(request.client.host):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    if len(tracks) > 30:
        raise HTTPException(status_code=400, detail="Limit 30 tracks per request")

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "preparing",
        "progress": 0,
        "created_at": time.time()
    }
    
    background_tasks.add_task(_playlist_download_task, job_id, playlist_name, tracks)
    return {"job_id": job_id}


@app.post("/start-download")
def start_download(
    request: Request,
    background_tasks: BackgroundTasks,
    url: str = Body(..., embed=True),
    format_id: Optional[str] = Body(None, embed=True),
    ext: Optional[str] = Body(None, embed=True),
    audio_only: bool = Body(False, embed=True),
    user: User = Depends(get_secure_user),
    db: Session = Depends(get_db)
):
    """Kicks off an asynchronous download job."""
    url = clean_youtube_url(url)
    
    if not user.is_pro and user.downloads_today >= 5:
        raise HTTPException(status_code=403, detail="Daily download limit reached for free account (5/day). Upgrade to PRO!")

    if _is_rate_limited(request.client.host):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Please wait a minute.")

    # Increment download count
    user.downloads_today += 1
    db.commit()

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
    
    job_info = jobs[job_id].copy()
    if job_info["status"] == "ready":
        job_info["download_url"] = f"/download-file/{job_id}"
        job_info["status"] = "ready" # Keep status as ready for frontend logic
    
    return job_info


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


@app.get("/user-status")
def get_user_status(user: User = Depends(get_secure_user)):
    return {
        "is_pro": user.is_pro,
        "downloads_today": user.downloads_today,
        "limit": 5 if not user.is_pro else "Unlimited",
        "user_id": user.id
    }

@app.post("/create-checkout-session")
async def create_checkout_session(user: User = Depends(get_secure_user)):
    try:
        checkout_session = stripe.checkout.Session.create(
            line_items=[{
                'price': PRO_PLAN_PRICE_ID,
                'quantity': 1,
            }],
            mode='subscription',
            success_url=f"http://localhost:8000/?success=true&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"http://localhost:8000/?canceled=true",
            client_reference_id=user.id,
        )
        return {"url": checkout_session.url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session.get("client_reference_id")
        if user_id:
            db = SessionLocal()
            user = _get_or_create_user(db, user_id, ip=None)
            user.is_pro = True
            user.stripe_customer_id = session.get("customer")
            db.commit()
            db.close()

    return {"status": "success"}

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


# The startup event is now handled by the lifespan context manager above


if __name__ == "__main__":
    import uvicorn
    # Railway sets the PORT environment variable
    port = int(os.environ.get("PORT", 8000))
    # Pass the app object directly to avoid ModuleNotFoundError on Railway
    # which occurs when project structure differs (Docker vs. local)
    uvicorn.run(app, host="0.0.0.0", port=port)