FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Set workspace directory
WORKDIR /app

# Install system dependencies (curl for healthchecks if needed, and git/build-essential if packages require compiling)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy only requirements first to leverage Docker cache layers
COPY requirements.txt .

# Install Python packages
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application files
COPY . .

# Run the Telegram bot daemon
CMD ["python", "bot.py"]
