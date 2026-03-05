FROM python:3.11-slim

# Install ffmpeg (required for merging video+audio and audio extraction)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source (includes cookies.txt if present)
COPY backend/ .

# Copy frontend so FastAPI can serve it
COPY frontend/ ./frontend/

# Create downloads directory
RUN mkdir -p downloads

# Run the application via Python to correctly handle the PORT environment variable
CMD ["python", "main.py"]