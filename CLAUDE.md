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

### Docker Deployment (Recommended)
```bash
# Build and run Docker container
docker build -t gitlab-mr-reviewer .
docker run -d -p 5000:5000 --name gitlab-mr-reviewer-test gitlab-mr-reviewer

# Using docker-compose
docker-compose up -d

# Check container status
docker ps | grep gitlab-mr-reviewer
docker logs gitlab-mr-reviewer-test
```

### Local Development
```bash
# Use existing virtual environment
source .venv/bin/activate
pip install -r requirements.txt

# Install Gemini CLI (requires Node.js 20+)
npm install -g @google/gemini-cli

# Run the server
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

# Test Docker features
python test_docker_features.py

# Add webhooks to all projects in GitLab instances
python add_webhooks_to_all_projects.py --dry-run  # Preview changes
python add_webhooks_to_all_projects.py  # Add webhooks to all instances

# Test webhook functionality with test MRs
python test_webhooks.py

# Test webhook endpoint directly
python test_webhook_local.py

# Run all tests to verify functionality
python -m py_compile w-server.py  # Syntax check
DEBUG=true python w-server.py     # Local server test
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
- `GEMINI_API_KEY`: Gemini API key for authentication (required)
- `GEMINI_DEBUG`: Enable debug logging for Gemini wrapper - "true" or "false" (default: false)
- `GEMINI_PROMPT`: Custom prompt for Gemini AI reviews (optional)
- `GEMINI_PROMPT_RU`: Russian language prompt for Gemini AI reviews (optional)
- `REVIEW_LANGUAGE`: Language for reviews - "en" for English, "ru" for Russian (default: en)
- `HTTP_PROXY`: HTTP proxy URL (e.g., http://192.168.193.10:8181) (optional)
- `SOCKS_PROXY`: SOCKS proxy address (e.g., 192.168.193.10:8180) (optional)
- `TELEGRAM_BOT_TOKEN`: Telegram bot token for notifications (optional)
- `TELEGRAM_CHAT_ID`: Primary Telegram chat ID for notifications (optional)
- `TELEGRAM_CHAT_ID_1` through `TELEGRAM_CHAT_ID_10`: Additional Telegram channels (optional)
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
- External: `https://r.smysl.pro/webhook`

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

### Multiple Telegram Channels
```env
TELEGRAM_CHAT_ID=-1234567890    # Primary channel
TELEGRAM_CHAT_ID_1=123456789    # Additional channel 1
TELEGRAM_CHAT_ID_2=987654321    # Additional channel 2
# ... up to TELEGRAM_CHAT_ID_10
```

### Features
- **Status Indicators**: ✅ for normal MRs, ⚠️ for conflicts
- **Conflict Warnings**: 🚫 BLOCKED messages when conflicts detected
- **Rich Formatting**: Markdown with project/author/branch details
- **Review Integration**: Includes code review content or summary
- **Instance Information**: Shows which GitLab instance the MR is from
- **Multi-Channel Support**: Notify up to 10 different Telegram channels
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

## Docker Deployment

### Container Architecture
- **Base Image**: Node.js 20 slim with Python 3.11
- **User**: Non-root `appuser` with proper permissions
- **Directories**: 
  - `/app/` - Application code and virtual environment
  - `/app/logs/` - Debug and application logs
  - `/app/cache/` - Gemini response cache
  - `/home/appuser/.gemini/` - Gemini CLI configuration

### Key Docker Features
- **Permission Management**: Automated creation of required directories with proper ownership
- **Gemini CLI Integration**: NPM-installed Gemini CLI with permission fixes
- **Health Checks**: Built-in container health monitoring
- **Virtual Environment**: Isolated Python environment to avoid system conflicts
- **Security**: Non-root user execution

### Troubleshooting Docker Issues

#### Permission Errors
```bash
# If you see Gemini CLI permission errors, rebuild with latest image
docker build -t gitlab-mr-reviewer .
docker stop gitlab-mr-reviewer-test
docker rm gitlab-mr-reviewer-test
docker run -d -p 5000:5000 --name gitlab-mr-reviewer-test gitlab-mr-reviewer
```

#### Container Health
```bash
# Check container status
docker ps | grep gitlab-mr-reviewer
docker logs gitlab-mr-reviewer-test

# Test Gemini CLI inside container
docker exec gitlab-mr-reviewer-test gemini -p "test prompt"

# Check directory permissions
docker exec gitlab-mr-reviewer-test ls -la /home/appuser/.gemini/
docker exec gitlab-mr-reviewer-test ls -la /app/logs/
```

#### Environment Variables
```bash
# Verify configuration
docker exec gitlab-mr-reviewer-test cat /app/.env
```

## Current Implementation Status

✅ Complete:
- FastAPI webhook server with modern lifespan event handlers
- GitLab webhook parsing with URL format correction
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
- **Docker deployment** with Node.js 20 + Python virtual environment
- **Multiple Telegram channels** support (up to 10 channels)
- **Gemini debug logging** with separate log files
- **GitLab instance information** in Telegram notifications
- **Permission fixes** for Docker container Gemini CLI access
- **Caching system** for Gemini responses
- **Health checks** and container monitoring
- **Bulk webhook management** for adding webhooks to all projects across instances
- **Webhook testing utilities** for verifying integration functionality
- **Production testing** - Fully tested with 378 total projects across 2 GitLab instances
- **Code quality improvements** - All PyCharm warnings and highlights resolved
- **URL format correction** - Automatic fix for GitLab merge request URLs
- **Local testing tools** - Direct API testing capabilities

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
- **Automated testing pipeline**
- **Container orchestration examples**
- **Scalability improvements**

## Webhook Management

### Bulk Webhook Setup

The project includes a comprehensive script to add webhooks to all projects across multiple GitLab instances:

```bash
# Add webhooks to all projects in all instances
python add_webhooks_to_all_projects.py

# Preview what would be done (dry run)
python add_webhooks_to_all_projects.py --dry-run

# Add webhooks to specific instance only
python add_webhooks_to_all_projects.py --instance primary
python add_webhooks_to_all_projects.py --instance instance_2

# Test webhook endpoint connectivity
python add_webhooks_to_all_projects.py --test-endpoint
```

### Webhook Script Features
- **Multi-Instance Support**: Automatically configures webhooks for all GitLab instances
- **Duplicate Detection**: Checks for existing webhooks to avoid duplicates
- **Proxy Support**: Uses configured HTTP/SOCKS proxy settings
- **Error Handling**: Detailed logging and error reporting
- **Progress Tracking**: Real-time progress with success/failure counts
- **Dry Run Mode**: Preview changes without making actual modifications
- **Instance Filtering**: Target specific GitLab instances

### Testing Webhook Integration

Use the webhook test script to verify functionality:

```bash
# Create test merge requests in configured test repositories
python test_webhooks.py
```

This script:
- Creates test branches with sample code
- Opens merge requests in test repositories
- Triggers webhook processing
- Allows verification of:
  - Webhook reception
  - AI code reviews
  - Telegram notifications
  - Multi-instance routing

### Webhook Configuration Requirements

For each GitLab instance:
1. **Project Access**: Token must have sufficient permissions to:
   - List all projects
   - Create webhooks
   - Read project details

2. **Webhook Settings**:
   - URL: `https://r.smysl.pro/webhook`
   - Secret Token: Corresponding `XGITLABTOKEN` value
   - Triggers: Merge request events only
   - SSL Verification: Enabled

3. **Permission Requirements**:
   - **Maintainer** or **Owner** role on projects
   - **Developer** role minimum for webhook creation
   - **API access** enabled for the token

### Test Repositories

The following test repositories are configured for webhook testing:
- **Primary Instance**: `spikerwork/test-repo` (https://lab.smysl.pro)
- **Secondary Instance**: `gitlab-instance-0d55f60d/max-test` (https://lab.catzwolf.ru)

Both repositories have webhooks configured and can be used to test the complete workflow.

## Production Testing Status

✅ **Fully Tested and Production Ready**:
- **Server Health**: All endpoints responding correctly (`http://localhost:5000/` returns status)
- **Multi-Instance Support**: Successfully tested with 133 projects (primary) + 245 projects (secondary)
- **Webhook Processing**: Verified with actual GitLab merge requests
- **Telegram Notifications**: Confirmed delivery to all configured channels
- **Gemini Integration**: AI code reviews working with caching and rate limiting
- **Docker Deployment**: Container health checks and proper permission handling
- **URL Correction**: Automatic fix for GitLab URL formats (`/mergerequests/` → `/merge_requests/`)
- **PyCharm Integration**: All IDE warnings and highlights resolved
- **Code Quality**: All syntax errors fixed, modern FastAPI patterns implemented
- **Error Handling**: Comprehensive error notifications to Telegram
- **Proxy Support**: HTTP/SOCKS proxy functionality verified
- **Bulk Operations**: Webhook management script tested with 378 total projects