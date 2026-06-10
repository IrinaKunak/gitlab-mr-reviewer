# GitLab MR Reviewer 🚀

A comprehensive GitLab Merge Request reviewer service that provides automated code quality analysis, with multi-instance support, enhanced notifications, and Docker deployment.

> **v2 (this branch):** reviews are powered by **Claude via Cloudflare AI Gateway** with an
> **OpenRouter fallback** (the Gemini CLI wrapper is retired; `AI_PROVIDER=gemini` remains as a
> rollback hatch). New feature-flagged stages: Haiku triage → Sonnet review → Opus investigator
> (whole-repo analysis + AIManager Q&A via the Review Bridge) → Russian tester reports delivered
> to the MR and the bridge chat. Design doc: `plans/2026-06-11-v2-architecture.md`.
> The webhook contract, env names, port and endpoints are unchanged from v1.

## ✨ Features

- 🔗 **Multi-Instance GitLab Support** - Connect up to 10 GitLab instances
- 🤖 **AI-Powered Code Reviews** - Enhanced reviews with original file context
- 📱 **Multi-Channel Telegram Notifications** - Support for multiple Telegram channels
- 🐳 **Docker Ready** - Complete containerization with docker-compose
- 🔍 **Debug Logging** - Detailed Gemini request/response tracking
- 🌐 **Proxy Support** - HTTP/SOCKS proxy compatibility
- 🔒 **Security Focus** - Identifies security vulnerabilities and best practices
- 🌍 **Multi-Language** - English and Russian support
- 🔧 **Bulk Webhook Management** - Automated webhook setup for all projects
- 🧪 **Testing Utilities** - Comprehensive webhook and integration testing
- ⚡ **Production Ready** - Fully tested and optimized for production use
- 🔄 **URL Fix** - Automatic correction of GitLab URL formats

## 🚀 Quick Start

### Docker Deployment (Recommended)

```bash
# Clone the repository
git clone <your-repo-url>
cd gitlab-mr-reviewer

# Configure environment
cp .env.example .env
# Edit .env with your GitLab and Telegram settings

# Deploy with Docker Compose
docker-compose up -d

# Or build and run manually
docker build -t gitlab-mr-reviewer .
docker run -p 5000:5000 -d gitlab-mr-reviewer
```

### Manual Installation

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install Gemini CLI
npm install -g @google/gemini-cli

# Start the server
DEBUG=true uvicorn w-server:app --host 0.0.0.0 --port 5000
```

## ⚙️ Configuration

### Environment Variables

```bash
# Primary GitLab Instance
GITLAB_URL=https://gitlab.example.com
GITLAB_TOKEN=your_gitlab_token
XGITLABTOKEN=your_webhook_secret

# Additional GitLab Instances (up to 10)
GITLAB_URL_2=https://gitlab2.example.com
GITLAB_TOKEN_2=your_second_gitlab_token
XGITLABTOKEN_2=your_second_webhook_secret

# Telegram Configuration
TELEGRAM=on
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_primary_chat_id
TELEGRAM_CHAT_ID_1=additional_chat_id_1
TELEGRAM_CHAT_ID_2=additional_chat_id_2

# Gemini Configuration
GEMINI_API_KEY=your_gemini_api_key
GEMINI_DEBUG=false

# Review Settings
REVIEW_LANGUAGE=en  # or 'ru' for Russian
REVIEW_FOR_CONFLICT=false

# Proxy Settings (optional)
HTTP_PROXY=http://proxy.example.com:8080
SOCKS_PROXY=proxy.example.com:1080
```

## 🔧 Webhook Setup

### Manual Setup
Configure webhooks in each GitLab instance:

1. Go to Project Settings > Webhooks
2. URL: `https://r.smysl.pro/webhook`
3. Secret Token: Use the corresponding `XGITLABTOKEN` value
4. Triggers: ✅ Merge request events

### Automated Bulk Setup
Use the bulk webhook management script:

```bash
# Preview what webhooks would be added
python add_webhooks_to_all_projects.py --dry-run

# Add webhooks to all projects in all instances
python add_webhooks_to_all_projects.py

# Add webhooks to specific instance only
python add_webhooks_to_all_projects.py --instance primary

# Test webhook endpoint connectivity
python add_webhooks_to_all_projects.py --test-endpoint
```

**Features:**
- ✅ Adds webhooks to all projects across multiple GitLab instances
- ✅ Detects and skips existing webhooks
- ✅ Supports proxy configurations
- ✅ Provides detailed progress and error reporting
- ✅ Includes dry-run mode for safe testing

## 📊 Features Overview

### Multi-Instance Support
- Automatically detects GitLab instance by webhook token
- Support for up to 10 different GitLab instances
- Instance information included in notifications

### Enhanced Code Reviews
- Reviews include both diffs AND original file content
- Better context for AI analysis
- Handles large diffs without size limits (uses stdin piping)
- Identifies security vulnerabilities, performance issues, and code quality problems

### Multiple Telegram Channels
- Notify up to 10 different Telegram channels
- Instance-specific notifications
- Rich formatting with project details and direct links

### Debug Logging
- Detailed Gemini request/response logging
- Separate debug log files
- Configurable debug levels

## 🐳 Docker Features

- **Base Image**: Node.js 20 with Python 3.11
- **Security**: Non-root user execution with proper permissions
- **Health Checks**: Built-in container health monitoring
- **Volumes**: Persistent logs and cache storage
- **Auto-reload**: Environment changes require rebuild
- **Gemini CLI**: Properly configured with permission fixes
- **Permission Management**: Automated creation of required directories

## 🔍 Monitoring

### Logs
- **Application**: Standard uvicorn/FastAPI logs
- **Gemini Debug**: `/app/logs/gemini-debug.log` (when `GEMINI_DEBUG=true`)
- **Docker**: `docker logs gitlab-mr-reviewer-test`
- **Cache**: `/app/cache/` directory for Gemini response caching

### Health Check
```bash
curl http://localhost:5000/
# Response: {"status":"GitLab MR Reviewer is running","version":"1.0.2"}

# Docker container health
docker ps | grep gitlab-mr-reviewer
# Should show "healthy" status
```

## 🛠️ Development

### Testing
```bash
# Test webhook functionality
python test_multi_instance_webhook.py

# Create test MRs
python create_test_mr_multi.py

# Test Docker features
python test_docker_features.py

# Add webhooks to all projects (bulk setup)
python add_webhooks_to_all_projects.py --dry-run
python add_webhooks_to_all_projects.py

# Test webhook integration end-to-end
python test_webhooks.py
```

### Debug Mode
```bash
# Enable debug logging
export DEBUG=true
export GEMINI_DEBUG=true

# Check debug logs (Docker)
docker exec gitlab-mr-reviewer-test tail -f /app/logs/gemini-debug.log

# Check debug logs (local)
tail -f logs/gemini-debug.log
```

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## 🔧 Troubleshooting

### Common Issues

#### Gemini CLI Permission Errors
If you see `EACCES: permission denied, mkdir '/home/appuser/.gemini'`:
```bash
# This is fixed in the latest Docker image
docker pull gitlab-mr-reviewer:latest
docker stop gitlab-mr-reviewer-test
docker rm gitlab-mr-reviewer-test
docker run -d -p 5000:5000 --name gitlab-mr-reviewer-test gitlab-mr-reviewer
```

#### Large Diff Handling
If you see `Argument list too long` error in Gemini wrapper:
- This has been fixed in the latest version
- The script now uses stdin piping instead of command-line arguments
- No size limit for merge request diffs

#### Container Health Issues
```bash
# Check container status
docker ps | grep gitlab-mr-reviewer
docker logs gitlab-mr-reviewer-test

# Test Gemini CLI inside container
docker exec gitlab-mr-reviewer-test gemini -p "test"
```

#### Webhook Not Working
```bash
# Verify webhook endpoint
curl -X POST -H "Content-Type: application/json" \
  -H "X-Gitlab-Event: Merge Request Hook" \
  -H "X-Gitlab-Token: your_webhook_token" \
  -d '{"test": "data"}' \
  https://r.smysl.pro/webhook

# Test with actual merge requests
python test_webhooks.py

# Check webhook configuration
python add_webhooks_to_all_projects.py --dry-run
```

#### Permission Issues
```bash
# If bulk webhook setup fails with permission errors
# Ensure your GitLab tokens have:
# - Maintainer/Owner role on projects
# - API access enabled
# - Webhook creation permissions
```

## 📞 Support

For issues and questions:
- Check the logs in `/app/logs/` (Docker) or `logs/` (local)
- Review the configuration in CLAUDE.md
- Verify Docker container health: `docker ps | grep gitlab-mr-reviewer`
- Test Gemini CLI: `docker exec <container> gemini -p "test"`
- Run webhook tests: `python test_webhooks.py`
- Check webhook setup: `python add_webhooks_to_all_projects.py --dry-run`
- Create an issue in the repository

## 🧪 Testing

The project includes comprehensive testing utilities and has been fully tested in production:

### Test Results ✅
- **Server Health**: All endpoints responding correctly
- **Multi-Instance Support**: Successfully tested with 133 projects (primary) + 245 projects (secondary)
- **Webhook Processing**: Verified with actual GitLab merge requests
- **Telegram Notifications**: Confirmed delivery to all configured channels
- **Gemini Integration**: AI code reviews working with caching and rate limiting
- **Docker Deployment**: Container health checks and proper permission handling
- **URL Correction**: Automatic fix for GitLab URL formats (`/mergerequests/` → `/merge_requests/`)
- **PyCharm Integration**: All IDE warnings and highlights resolved

### Test Repositories
- **Primary Instance**: `spikerwork/test-repo` (https://lab.smysl.pro)
- **Secondary Instance**: `gitlab-instance-0d55f60d/max-test` (https://lab.catzwolf.ru)

### Test Workflow
1. Run `python test_webhooks.py` to create test merge requests
2. Check GitLab projects for AI code review comments
3. Verify Telegram notifications are received
4. Confirm multi-instance routing works correctly
5. Test webhook endpoint with `curl` or Python scripts

### Webhook Management
- **Bulk Setup**: `python add_webhooks_to_all_projects.py`
- **Test Connectivity**: `python add_webhooks_to_all_projects.py --test-endpoint`
- **Dry Run**: `python add_webhooks_to_all_projects.py --dry-run`
- **Instance Specific**: `python add_webhooks_to_all_projects.py --instance primary`
- **Local Testing**: `python test_webhook_local.py` for direct API testing

---

**Made with ❤️ for better code reviews**