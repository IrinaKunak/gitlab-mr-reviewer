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

The application supports multiple GitLab instances and requires a `.env` file:

### Primary GitLab Instance
- `GITLAB_URL`: Primary GitLab instance URL (e.g., https://lab.smysl.pro)
- `GITLAB_TOKEN`: GitLab private token for API access
- `XGITLABTOKEN`: Webhook token for primary instance (used in X-Gitlab-Token header)

### Additional GitLab Instances (up to 10)
- `GITLAB_URL_2`: Second GitLab instance URL (e.g., https://lab.catzwolf.ru)
- `GITLAB_TOKEN_2`: GitLab private token for second instance
- `XGITLABTOKEN_2`: Webhook token for second instance
- ... (continue pattern for _3, _4, etc.)

### Other Configuration
- `GEMINI_PROMPT`: Custom prompt for Gemini AI reviews (optional)
- `GEMINI_PROMPT_RU`: Russian language prompt for Gemini AI reviews (optional)
- `REVIEW_LANGUAGE`: Language for reviews - "en" for English, "ru" for Russian (default: en)
- `HTTP_PROXY`: HTTP proxy URL (e.g., http://127.0.0.1:8181) (optional)
- `SOCKS_PROXY`: SOCKS proxy address (e.g., 127.0.0.1:8180) (optional)
- `TELEGRAM_BOT_TOKEN`: Telegram bot token for notifications (optional)
- `TELEGRAM_CHAT_ID`: Telegram chat ID for notifications (optional)
- `TELEGRAM`: Enable/disable Telegram notifications - "on" or "off" (default: off)
- `REVIEW_FOR_CONFLICT`: Whether to review MRs with conflicts - "true" or "false" (default: false)

## Key Features

1. **Webhook Processing**: 
   - Handles GitLab merge request events (open, update, reopen)
   - Supports multiple GitLab instances via X-Gitlab-Token header matching
   - Automatically detects which GitLab instance to use based on webhook token
   - Processes events asynchronously

2. **Code Review Flow**:
   - Receives webhook when MR is created/updated
   - Detects merge conflicts automatically
   - Sends initial Telegram notification with MR details
   - Posts initial comment on MR (with conflict warning if applicable)
   - Fetches MR diff content AND original file contents for better context
   - Calls gemini-wrapper.sh for AI analysis with full context
   - Posts formatted review results as MR comment
   - Sends Telegram notification with review summary
   - Sends error notifications to Telegram for any failures

3. **Gemini Integration**:
   - Caches responses to avoid duplicate API calls (1-hour TTL)
   - Rate limiting (2 seconds between calls)
   - Handles large review content (up to 1MB with file contents)
   - Timeout protection (60 seconds)
   - Uses gemini-2.5-flash model
   - Calls Gemini CLI with `-p` parameter for prompt input
   - Reviews include both diffs and original file content for better context
   - Multi-language support (English/Russian)
   - Language-specific prompts and responses

4. **Network & Proxy Support**:
   - HTTP proxy support for GitLab API connections
   - SOCKS proxy support with PySocks
   - Automatic proxy detection from environment variables
   - Connection debugging and logging
   - Proxy support for Telegram API calls

5. **Telegram Notifications**:
   - Real-time notifications for new merge requests
   - Conflict detection and warning alerts
   - Project name, author, and branch information
   - Direct links to merge requests
   - Code review summaries (truncated if too long)
   - Multi-language support (English/Russian)
   - Configurable review behavior for conflicted MRs
   - **Error notifications** for:
     - Gemini AI failures
     - GitLab API errors
     - Webhook processing errors
     - Timeout errors
     - General failures

## Webhook Configuration

The webhook endpoint is available at:
- Local: `http://localhost:5000/webhook`
- External: `http://7820.spikerwork.keenetic.pro/webhook`

### Multi-Instance Setup
1. For each GitLab instance, configure webhook in project settings:
   - URL: Your webhook endpoint (same for all instances)
   - Secret Token: Use the corresponding `XGITLABTOKEN` value
   - Trigger: Merge request events

2. Example configuration:
   - Instance 1 (lab.smysl.pro): Use `XGITLABTOKEN` value as secret token
   - Instance 2 (lab.catzwolf.ru): Use `XGITLABTOKEN_2` value as secret token

The system will automatically route webhooks to the correct GitLab instance based on the X-Gitlab-Token header.

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

## Telegram Configuration

For Telegram notifications:

### Basic Setup
```env
TELEGRAM=on
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

### Conflict Handling
```env
REVIEW_FOR_CONFLICT=true   # Enable reviews for MRs with conflicts
REVIEW_FOR_CONFLICT=false  # Skip reviews for conflicted MRs (default)
```

### Features
- **Status Indicators**: ✅ for normal MRs, ⚠️ for conflicts
- **Conflict Warnings**: 🚫 BLOCKED messages when conflicts detected
- **Rich Formatting**: Markdown with project/author/branch details
- **Review Integration**: Includes code review content or summary
- **Proxy Support**: Uses same proxy configuration as GitLab API

## Monitoring

Server logs include:
- Webhook receipt confirmations
- GitLab instance detection and routing
- MR processing status
- GitLab API interactions
- Proxy connection status
- Language configuration
- Telegram notification status (including error notifications)
- Conflict detection results
- Gemini analysis results
- File content fetching status
- Error details with stack traces
- UTF-8 encoding handling

### Error Notification Details
When errors occur, Telegram notifications include:
- Error type (Gemini failure, GitLab API, webhook, timeout, general)
- Error details and description
- Project ID and MR number (if available)
- GitLab instance name
- Timestamp of the error

## Current Implementation Status

✅ Complete:
- FastAPI webhook server
- GitLab webhook parsing
- Async task processing
- **Multi-instance GitLab support** (up to 10 instances)
- GitLab API integration with proxy support (HTTP/SOCKS)
- Gemini wrapper script with correct CLI syntax
- **Enhanced code reviews** with original file content for context
- Russian language support (prompts, comments, reviews)
- Multi-language interface (English/Russian)
- UTF-8 encoding handling
- Error handling and logging
- **Error notifications to Telegram** for all failure scenarios
- Environment configuration
- Test utilities
- End-to-end webhook processing (verified working)
- AI code reviews posted to GitLab MRs
- Telegram notifications with rich formatting
- Merge conflict detection and warnings
- Configurable review behavior for conflicts
- Proxy support for Telegram API calls

📝 Future Improvements:
- Add unit tests
- Add metrics/monitoring
- Support for multiple prompts
- Web UI for configuration
- CI/CD pipeline
- Additional language support (beyond English/Russian)
- Custom review templates
- Performance optimization for large diffs
- Advanced conflict resolution suggestions
- **Docker Swarm/Kubernetes deployment guides**
- **Custom notification templates**
- **Metrics and monitoring dashboard**
- **Backup and restore functionality**