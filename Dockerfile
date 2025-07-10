# GitLab MR Reviewer Docker Image
FROM node:20-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# Install system dependencies including Python
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    curl \
    git \
    build-essential \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Gemini CLI via npm
RUN npm install -g @google/gemini-cli

# Create app directory
WORKDIR /app

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Copy requirements first for better caching
COPY requirements.txt .

# Create and activate virtual environment
RUN python3 -m venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Install Python dependencies in virtual environment
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create necessary directories including Gemini CLI config directory
RUN mkdir -p /app/logs /app/cache /home/appuser/.gemini && \
    chown -R appuser:appuser /app /home/appuser/.gemini

# Make scripts executable
RUN chmod +x gemini-wrapper.sh

# Set up logging directory
ENV GEMINI_CACHE_DIR=/app/cache
ENV GEMINI_LOG_DIR=/app/logs

# Switch to non-root user
USER appuser

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/ || exit 1

# Expose port
EXPOSE 5000

# Default command
CMD ["/app/.venv/bin/python", "-m", "uvicorn", "w-server:app", "--host", "0.0.0.0", "--port", "5000"]