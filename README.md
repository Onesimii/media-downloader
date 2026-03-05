# Media Downloader

A powerful and user-friendly web application for downloading media from various platforms using FastAPI and `yt-dlp`.

## Features
- **Wide Platform Support**: Download videos and audio from YouTube, Spotify, and more.
- **FastAPI Backend**: High-performance asynchronous API.
- **Modern Frontend**: Responsive and intuitive user interface.
- **Docker Ready**: Easy deployment using Docker and Docker Compose.

## Prerequisites
- Python 3.10+
- Docker (optional, for containerized deployment)

## Local Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/Onesimii/media-downloader.git
   cd media-downloader
   ```

2. **Create a virtual environment**:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows use `venv\Scripts\activate`
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Run the backend**:
   ```bash
   uvicorn backend.main:app --reload
   ```

5. **Access the frontend**:
   Open `frontend/index.html` in your browser.

## Docker Deployment

Build and run the application using Docker Compose:
```bash
docker-compose up --build
```
The application will be available at `http://localhost:8000`.

## Project Structure
- `backend/`: FastAPI application and business logic.
- `frontend/`: HTML/JS frontend.
- `downloads/`: Default directory for downloaded media.
