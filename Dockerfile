# GitLab MR Reviewer v2 — plain Python image (Node.js/Gemini CLI no longer needed)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# git: repo clone cache; ripgrep + universal-ctags: repo tools' search engine
# and symbol index (both optional at runtime — the code degrades without them)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    ripgrep \
    universal-ctags \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN groupadd -r appuser && useradd -r -g appuser -m appuser

COPY requirements.txt .

RUN python -m venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
RUN pip install --no-cache-dir -r requirements.txt

COPY reviewer/ ./reviewer/
COPY w-server.py gemini-wrapper.sh ./
RUN chmod +x gemini-wrapper.sh

RUN mkdir -p /app/logs /app/cache /app/repos && \
    chown -R appuser:appuser /app /home/appuser

ENV AI_CACHE_DIR=/app/cache \
    AI_LOG_DIR=/app/logs \
    REPO_CACHE_DIR=/app/repos

USER appuser

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/ || exit 1

EXPOSE 5000

CMD ["/app/.venv/bin/python", "-m", "uvicorn", "w-server:app", "--host", "0.0.0.0", "--port", "5000"]
