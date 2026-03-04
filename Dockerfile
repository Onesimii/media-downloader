FROM python:3.11-slim

# Install ffmpeg (required for merging video+audio and audio extraction)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source
COPY backend/ .

# Copy frontend so FastAPI StaticFiles can serve it
COPY frontend/ ./frontend/

# Create downloads directory
RUN mkdir -p downloads

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]