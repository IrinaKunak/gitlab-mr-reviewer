# GitLab MR Reviewer 🚀

A comprehensive GitLab Merge Request reviewer service that provides automated code quality analysis using Gemini AI, with multi-instance support, enhanced notifications, and Docker deployment.

## ✨ Features

- 🔗 **Multi-Instance GitLab Support** - Connect up to 10 GitLab instances
- 🤖 **AI-Powered Code Reviews** - Enhanced reviews with original file context
- 📱 **Multi-Channel Telegram Notifications** - Support for multiple Telegram channels
- 🐳 **Docker Ready** - Complete containerization with docker-compose
- 🔍 **Debug Logging** - Detailed Gemini request/response tracking
- 🌐 **Proxy Support** - HTTP/SOCKS proxy compatibility
- 🔒 **Security Focus** - Identifies security vulnerabilities and best practices
- 🌍 **Multi-Language** - English and Russian support

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

Configure webhooks in each GitLab instance:

1. Go to Project Settings > Webhooks
2. URL: `http://your-server:5000/webhook`
3. Secret Token: Use the corresponding `XGITLABTOKEN` value
4. Triggers: ✅ Merge request events

## 📊 Features Overview

### Multi-Instance Support
- Automatically detects GitLab instance by webhook token
- Support for up to 10 different GitLab instances
- Instance information included in notifications

### Enhanced Code Reviews
- Reviews include both diffs AND original file content
- Better context for AI analysis
- Handles up to 1MB of review content
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
- **Security**: Non-root user execution
- **Health Checks**: Built-in container health monitoring
- **Volumes**: Persistent logs and cache storage
- **Auto-reload**: Environment changes require rebuild

## 🔍 Monitoring

### Logs
- **Application**: Standard uvicorn/FastAPI logs
- **Gemini Debug**: `/app/logs/gemini-debug.log` (when `GEMINI_DEBUG=true`)
- **Docker**: `docker logs gitlab-mr-reviewer`

### Health Check
```bash
curl http://localhost:5000/
# Response: {"status":"GitLab MR Reviewer is running","version":"1.0.0"}
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
```

### Debug Mode
```bash
# Enable debug logging
export DEBUG=true
export GEMINI_DEBUG=true

# Check debug logs
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

## 📞 Support

For issues and questions:
- Check the logs in `/app/logs/`
- Review the configuration in CLAUDE.md
- Create an issue in the repository

---

**Made with ❤️ for better code reviews**