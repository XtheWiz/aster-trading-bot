# Use official lightweight Python image
FROM python:3.10-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Set work directory
WORKDIR /app

# Install system dependencies (needed for pandas/numpy compilation if no wheel found)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Copy the rest of the application
COPY . .

# Create volume mount point for SQLite (persistence)
# Railway Volume should be mounted here via dashboard config
VOLUME /app/data

# Initial command (can be overridden by Railway Start Command)
# Using python -u for unbuffered output to see logs immediately
CMD ["python", "-u", "grid_bot.py"]
