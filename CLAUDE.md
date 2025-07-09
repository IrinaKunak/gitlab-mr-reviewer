# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a GitLab Merge Request Reviewer service - a FastAPI-based webhook receiver that performs automated code quality checks on GitLab merge requests using Gemini AI.

## Architecture

The project consists of:
- **w-server.py**: FastAPI webhook server that receives GitLab webhook events and triggers code quality analysis
- **gemini-wrapper.sh**: Bash script that wraps Gemini CLI for code review with caching and rate limiting
- **gemini-wrapper-reference.sh**: Reference implementation that inspired the main wrapper

## Development Commands

### Install Dependencies
```bash
# Use existing virtual environment
source .venv/bin/activate
pip install -r requirements.txt
```

### Run the Server
```bash
source .venv/bin/activate
DEBUG=true uvicorn w-server:app --host 0.0.0.0 --port 5000

# Run with logging to file
DEBUG=true uvicorn w-server:app --host 0.0.0.0 --port 5000 > server.log 2>&1 &
```

### Test the Webhook
```bash
# Test webhook endpoint
python test_webhook.py

# Test GitLab connection
python test_gitlab_connection.py

# Create a test merge request
python create_test_mr.py
```

## Environment Configuration

The application requires a `.env` file with:
- `GITLAB_URL`: GitLab instance URL (e.g., https://lab.smysl.pro)
- `GITLAB_TOKEN`: GitLab private token for API access
- `GEMINI_PROMPT`: Custom prompt for Gemini AI reviews (optional)
- `GEMINI_PROMPT_RU`: Russian language prompt for Gemini AI reviews (optional)
- `REVIEW_LANGUAGE`: Language for reviews - "en" for English, "ru" for Russian (default: en)
- `HTTP_PROXY`: HTTP proxy URL (e.g., http://127.0.0.1:8181) (optional)
- `SOCKS_PROXY`: SOCKS proxy address (e.g., 127.0.0.1:8180) (optional)
- `WEBHOOK_SECRET`: Secret token for webhook verification (optional)

## Key Features

1. **Webhook Processing**: 
   - Handles GitLab merge request events (open, update, reopen)
   - Validates webhook tokens if configured
   - Processes events asynchronously

2. **Code Review Flow**:
   - Receives webhook when MR is created/updated
   - Posts initial comment on MR
   - Fetches MR diff content
   - Calls gemini-wrapper.sh for AI analysis
   - Posts formatted review results as MR comment

3. **Gemini Integration**:
   - Caches responses to avoid duplicate API calls (1-hour TTL)
   - Rate limiting (2 seconds between calls)
   - Handles large diffs (up to 500KB)
   - Timeout protection (60 seconds)
   - Uses gemini-2.5-flash model
   - Calls Gemini CLI with `-p` parameter for prompt input
   - Multi-language support (English/Russian)
   - Language-specific prompts and responses

4. **Network & Proxy Support**:
   - HTTP proxy support for GitLab API connections
   - SOCKS proxy support with PySocks
   - Automatic proxy detection from environment variables
   - Connection debugging and logging

## Webhook Configuration

The webhook endpoint is available at:
- Local: `http://localhost:5000/webhook`
- External: `http://7820.spikerwork.keenetic.pro/webhook`

Configure in GitLab project settings:
- URL: Your webhook endpoint
- Trigger: Merge request events
- Optional: Set secret token

## Language Support

The system supports multiple languages for code reviews:

### English (Default)
```env
REVIEW_LANGUAGE=en
GEMINI_PROMPT="Review this merge request and provide:
1. Code quality assessment
2. Potential bugs or issues
3. Security concerns
4. Performance considerations
5. Best practices violations
6. Suggestions for improvement

Be concise but thorough. Focus on actionable feedback."
```

### Russian
```env
REVIEW_LANGUAGE=ru
GEMINI_PROMPT_RU="Проанализируйте этот запрос на слияние и предоставьте:
1. Оценка качества кода
2. Потенциальные баги или проблемы
3. Проблемы безопасности
4. Вопросы производительности
5. Нарушения лучших практик
6. Предложения по улучшению

Будьте лаконичными, но основательными. Сосредоточьтесь на практических рекомендациях."
```

## Proxy Configuration

For environments requiring proxy connections:

### HTTP Proxy
```env
HTTP_PROXY=http://127.0.0.1:8181
```

### SOCKS Proxy
```env
SOCKS_PROXY=127.0.0.1:8180
```

**Note**: HTTP proxy takes precedence over SOCKS proxy if both are configured.

## Monitoring

Server logs include:
- Webhook receipt confirmations
- MR processing status
- GitLab API interactions
- Proxy connection status
- Language configuration
- Gemini analysis results
- Error details with stack traces
- UTF-8 encoding handling

## Current Implementation Status

✅ Complete:
- FastAPI webhook server
- GitLab webhook parsing
- Async task processing
- GitLab API integration with proxy support (HTTP/SOCKS)
- Gemini wrapper script with correct CLI syntax
- Russian language support (prompts, comments, reviews)
- Multi-language interface (English/Russian)
- UTF-8 encoding handling
- Error handling and logging
- Environment configuration
- Test utilities
- End-to-end webhook processing (verified working)
- AI code reviews posted to GitLab MRs

📝 Future Improvements:
- Add unit tests
- Create Docker deployment
- Add metrics/monitoring
- Support for multiple prompts
- Web UI for configuration
- CI/CD pipeline
- Additional language support (beyond English/Russian)
- Custom review templates
- Performance optimization for large diffs
- Webhook authentication improvements