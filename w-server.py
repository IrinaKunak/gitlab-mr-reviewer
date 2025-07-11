import os
import json
import logging
import subprocess
import tempfile
import requests
from typing import Dict, Any, Optional
from datetime import datetime

from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from contextlib import asynccontextmanager
import gitlab
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
SOCKS_PROXY = os.getenv("SOCKS_PROXY")
HTTP_PROXY = os.getenv("HTTP_PROXY")
GEMINI_PROMPT = os.getenv(
    "GEMINI_PROMPT",
    "Review this merge request and provide feedback on code quality, potential issues, and suggestions for improvement.",
)
GEMINI_PROMPT_RU = os.getenv(
    "GEMINI_PROMPT_RU",
    "Проанализируйте этот запрос на слияние и предоставьте отзыв о качестве кода, потенциальных проблемах и "
    "предложения по улучшению.",
)
REVIEW_LANGUAGE = os.getenv("REVIEW_LANGUAGE", "en")
TELEGRAM_ENABLED = os.getenv("TELEGRAM", "off").lower() == "on"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# Support multiple Telegram channels
TELEGRAM_CHAT_IDS = []
if os.getenv("TELEGRAM_CHAT_ID"):
    TELEGRAM_CHAT_IDS.append(os.getenv("TELEGRAM_CHAT_ID"))
for idx in range(1, 11):  # Support up to 10 additional channels
    chat_id = os.getenv(f"TELEGRAM_CHAT_ID_{idx}")
    if chat_id:
        TELEGRAM_CHAT_IDS.append(chat_id)

REVIEW_FOR_CONFLICT = os.getenv("REVIEW_FOR_CONFLICT", "false").lower() == "true"

# Multi-instance GitLab configuration
GITLAB_INSTANCES = {}


# Load all GitLab instances from environment
def load_gitlab_instances():
    """Load GitLab instances configuration from environment variables"""
    instances = {}

    # Load primary instance
    if os.getenv("GITLAB_URL") and os.getenv("GITLAB_TOKEN"):
        webhook_token = os.getenv("XGITLABTOKEN")
        if webhook_token:
            instances[webhook_token] = {
                "url": os.getenv("GITLAB_URL"),
                "token": os.getenv("GITLAB_TOKEN"),
                "name": "primary",
            }

    # Load additional instances (up to 10)
    for idx in range(2, 11):
        url_key = f"GITLAB_URL_{idx}"
        token_key = f"GITLAB_TOKEN_{idx}"
        webhook_key = f"XGITLABTOKEN_{idx}"

        if os.getenv(url_key) and os.getenv(token_key) and os.getenv(webhook_key):
            instances[os.getenv(webhook_key)] = {
                "url": os.getenv(url_key),
                "token": os.getenv(token_key),
                "name": f"instance_{idx}",
            }

    return instances


GITLAB_INSTANCES = load_gitlab_instances()

# Logging setup
logging.basicConfig(
    level=logging.DEBUG if os.getenv("DEBUG", "").lower() == "true" else logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting GitLab MR Reviewer")
    logger.info(f"Webhook endpoint: {os.getenv('WEBHOOK_ENDPOINT', 'https://r.smysl.pro/webhook')}")
    logger.info(f"Review language: {REVIEW_LANGUAGE}")
    if TELEGRAM_ENABLED:
        logger.info(
            f"Telegram notifications: enabled for {len(TELEGRAM_CHAT_IDS)} channel(s)"
        )
        for channel_idx, telegram_chat_id in enumerate(TELEGRAM_CHAT_IDS):
            logger.info(f"  - Channel {channel_idx + 1}: {telegram_chat_id}")
    else:
        logger.info("Telegram notifications: disabled")
    logger.info(
        f"Review for conflicts: {'enabled' if REVIEW_FOR_CONFLICT else 'disabled'}"
    )

    # Log proxy configuration
    if HTTP_PROXY:
        logger.info(f"HTTP Proxy configured: {HTTP_PROXY}")
    if SOCKS_PROXY:
        logger.info(f"SOCKS Proxy configured: {SOCKS_PROXY}")
    if not HTTP_PROXY and not SOCKS_PROXY:
        logger.info("No proxy configured - using direct connection")

    # Log configured GitLab instances
    if GITLAB_INSTANCES:
        logger.info("Configured GitLab instances:")
        for webhook_token, config in GITLAB_INSTANCES.items():
            logger.info(
                f"  - {config['name']}: {config['url']} (webhook token: {webhook_token[:10]}...)"
            )
    else:
        logger.warning("No GitLab instances configured!")

    # Verify GitLab connections
    for webhook_token, config in GITLAB_INSTANCES.items():
        try:
            test_client = get_gitlab_client(config)
            logger.info(
                f"Successfully connected to GitLab instance {config['name']} ({config['url']})"
            )
        except Exception as e:
            logger.error(f"Failed to connect to GitLab instance {config['name']}: {e}")
            send_error_notification(
                "gitlab_api_error",
                f"Failed to connect to {config['url']}: {str(e)}",
                {"gitlab_instance": config["name"]},
            )
    yield
    # Shutdown
    logger.info("Shutting down GitLab MR Reviewer")


# Initialize FastAPI app
app = FastAPI(
    title="GitLab MR Reviewer",
    description="Webhook server for automated merge request reviews using Gemini AI",
    lifespan=lifespan,
)


# Initialize GitLab client with proxy support
def get_gitlab_client(gitlab_config: Dict[str, str]):
    """Get GitLab client for a specific instance configuration"""
    try:
        # Setup session with proxy if configured
        session = None
        if HTTP_PROXY or SOCKS_PROXY:
            session = requests.Session()

            if HTTP_PROXY:
                logger.debug(f"Using HTTP proxy: {HTTP_PROXY}")
                session.proxies = {"http": HTTP_PROXY, "https": HTTP_PROXY}
            elif SOCKS_PROXY:
                logger.debug(f"Using SOCKS proxy: {SOCKS_PROXY}")
                # For SOCKS proxy, we need pysocks
                try:
                    import socks
                    import socket

                    proxy_host, proxy_port = SOCKS_PROXY.split(":")
                    socks.set_default_proxy(socks.SOCKS5, proxy_host, int(proxy_port))
                    socket.socket = socks.socksocket
                    logger.debug(f"SOCKS proxy configured: {proxy_host}:{proxy_port}")
                except ImportError:
                    socks = None
                    socket = None
                    logger.warning("PySocks not available for SOCKS proxy support")
                except Exception as e:
                    logger.error(f"Failed to configure SOCKS proxy: {e}")

        gl = gitlab.Gitlab(
            gitlab_config["url"], private_token=gitlab_config["token"], session=session
        )
        gl.auth()
        return gl
    except Exception as e:
        logger.error(
            f"Failed to initialize GitLab client for {gitlab_config.get('name', 'unknown')}: {e}"
        )
        raise


def send_telegram_notification(message: str, is_error: bool = False) -> bool:
    """Send notification to all configured Telegram channels"""
    if not TELEGRAM_ENABLED or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        logger.debug("Telegram notifications disabled or not configured")
        return False

    success_count = 0

    for telegram_chat_id in TELEGRAM_CHAT_IDS:
        try:
            # Add error prefix if it's an error notification
            formatted_message = message
            if is_error:
                error_prefix = (
                    "🚨 **ERROR** 🚨\n"
                    if REVIEW_LANGUAGE == "en"
                    else "🚨 **ОШИБКА** 🚨\n"
                )
                formatted_message = error_prefix + message

            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            data = {
                "chat_id": telegram_chat_id,
                "text": formatted_message,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }

            # Use proxy if configured
            proxies = None
            if HTTP_PROXY:
                proxies = {"http": HTTP_PROXY, "https": HTTP_PROXY}
            elif SOCKS_PROXY:
                # For SOCKS proxy with requests, we need to use requests[socks]
                try:
                    import socks
                    import urllib3.contrib.socks

                    proxy_host, proxy_port = SOCKS_PROXY.split(":")
                    proxies = {
                        "http": f"socks5://{proxy_host}:{proxy_port}",
                        "https": f"socks5://{proxy_host}:{proxy_port}",
                    }
                except ImportError:
                    socks = None
                    logger.warning("PySocks not available for Telegram SOCKS proxy")

            response = requests.post(url, json=data, proxies=proxies, timeout=10)
            response.raise_for_status()

            logger.debug(f"Telegram notification sent successfully to chat {telegram_chat_id}")
            success_count += 1

        except Exception as e:
            logger.error(f"Failed to send Telegram notification to chat {telegram_chat_id}: {e}")

    return success_count > 0


def send_error_notification(
    error_type: str, error_details: str, context: Dict[str, Any] = None
) -> bool:
    """Send error notification to Telegram"""
    if not TELEGRAM_ENABLED:
        return False

    # Format error message
    if REVIEW_LANGUAGE == "ru":
        error_messages = {
            "gemini_failure": "Ошибка Gemini AI",
            "gitlab_api_error": "Ошибка GitLab API",
            "webhook_error": "Ошибка обработки webhook",
            "timeout": "Превышено время ожидания",
            "general": "Общая ошибка",
        }
    else:
        error_messages = {
            "gemini_failure": "Gemini AI Error",
            "gitlab_api_error": "GitLab API Error",
            "webhook_error": "Webhook Processing Error",
            "timeout": "Timeout Error",
            "general": "General Error",
        }

    error_title = error_messages.get(error_type, error_messages["general"])

    message_parts = [f"**{error_title}**", f"**Details:** {error_details}"]

    if context:
        if "project_id" in context:
            message_parts.append(f"**Project ID:** {context['project_id']}")
        if "mr_iid" in context:
            message_parts.append(f"**MR:** !{context['mr_iid']}")
        if "gitlab_instance" in context:
            message_parts.append(f"**Instance:** {context['gitlab_instance']}")

    message_parts.append(f"**Time:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    message = "\n".join(message_parts)
    return send_telegram_notification(message, is_error=True)


def format_telegram_message(
    mr_data: Dict[str, Any],
    project_name: str,
    has_conflicts: bool = False,
    review_content: str = None,
    gitlab_instance: str = None,
) -> str:
    """Format message for Telegram notification"""
    # Status and emoji based on conflict and review state
    if has_conflicts:
        status_emoji = "⚠️"
        status_text = (
            "MR with CONFLICTS" if REVIEW_LANGUAGE == "en" else "MR С КОНФЛИКТАМИ"
        )
    else:
        status_emoji = "✅"
        status_text = "New MR" if REVIEW_LANGUAGE == "en" else "Новый MR"

    # Get instance info
    instance_info = ""
    if gitlab_instance:
        instance_parts = gitlab_instance.split("://")
        if len(instance_parts) > 1:
            instance_domain = instance_parts[1].rstrip("/")
        else:
            instance_domain = gitlab_instance
        instance_info = f" from `{instance_domain}`"

    # Build message header
    message_parts = [
        f"{status_emoji} **{status_text}{instance_info}**",
        f"**Project:** `{project_name}`",
        f"**Author:** {mr_data['author']}",
        f"**Title:** {mr_data['title']}",
        f"**Branch:** `{mr_data['source_branch']}` → `{mr_data['target_branch']}`",
        f"**Link:** [!{mr_data['mr_iid']}]({mr_data['url']})",
    ]

    # Add conflict warning if present
    if has_conflicts:
        conflict_msg = (
            "🚫 **BLOCKED: Merge conflicts must be resolved before merging!**"
            if REVIEW_LANGUAGE == "en"
            else "🚫 **ЗАБЛОКИРОВАН: Конфликты слияния должны быть разрешены перед слиянием!**"
        )
        message_parts.append("")
        message_parts.append(conflict_msg)

    # Add review content if available and not too long
    if (
        review_content and len(review_content) < 2000
    ):  # Telegram message limit consideration
        review_header = (
            "\n📝 **Code Review:**"
            if REVIEW_LANGUAGE == "en"
            else "\n📝 **Обзор кода:**"
        )
        message_parts.append(review_header)
        # Truncate review if too long for Telegram
        truncated_review = (
            review_content[:1500] + "..."
            if len(review_content) > 1500
            else review_content
        )
        message_parts.append(f"```\n{truncated_review}\n```")
    elif review_content:
        review_note = (
            "\n📝 Code review posted to GitLab (too long for Telegram)"
            if REVIEW_LANGUAGE == "en"
            else "\n📝 Обзор кода опубликован в GitLab (слишком длинный для Telegram)"
        )
        message_parts.append(review_note)

    return "\n".join(message_parts)


def check_merge_conflicts(mr) -> bool:
    """Check if merge request has conflicts"""
    try:
        # Get MR details including merge status
        mr_details = mr.manager.gitlab.http_get(
            f"/projects/{mr.project_id}/merge_requests/{mr.iid}"
        )

        # Check various conflict indicators
        merge_status = mr_details.get("merge_status", "")
        has_conflicts = (
            merge_status == "cannot_be_merged"
            or merge_status == "cannot_be_merged_recheck"
            or mr_details.get("has_conflicts", False)
            or mr_details.get("blocking_discussions_resolved", True) is False  # Unresolved discussions can block
        )

        logger.debug(
            f"MR !{mr.iid} merge_status: {merge_status}, has_conflicts: {has_conflicts}"
        )
        return has_conflicts

    except Exception as e:
        logger.warning(f"Could not check merge conflicts for MR !{mr.iid}: {e}")
        return False  # Assume no conflicts if we can't check


@app.get("/")
async def root():
    return {"status": "GitLab MR Reviewer is running", "version": "1.0.2"}


@app.post("/webhook")
async def handle_gitlab_webhook(request: Request, background_tasks: BackgroundTasks):
    """Handle GitLab webhook events for merge requests"""
    try:
        # Get webhook headers
        event_type = request.headers.get("X-Gitlab-Event")
        gitlab_token = request.headers.get("X-Gitlab-Token")

        # Find matching GitLab instance by webhook token
        gitlab_config = None
        if gitlab_token and gitlab_token in GITLAB_INSTANCES:
            gitlab_config = GITLAB_INSTANCES[gitlab_token]
            logger.info(
                f"Matched webhook token to GitLab instance: {gitlab_config['name']} ({gitlab_config['url']})"
            )
        else:
            logger.warning(
                f"No GitLab instance found for webhook token: {gitlab_token}"
            )
            # Send error notification
            send_error_notification(
                "webhook_error",
                f"Unknown webhook token received: {gitlab_token[:10]}...",
                {"event_type": event_type},
            )
            raise HTTPException(status_code=401, detail="Invalid webhook token")

        # Parse webhook payload
        payload = await request.json()

        # Only process merge request events
        if event_type != "Merge Request Hook":
            logger.info(f"Ignoring non-MR event: {event_type}")
            return {
                "status": "ignored",
                "reason": f"Not a merge request event: {event_type}",
            }

        # Extract merge request details
        mr_data = parse_merge_request_webhook(payload)

        if not mr_data:
            return {"status": "ignored", "reason": "Invalid or unsupported MR action"}

        # Add GitLab instance config to mr_data
        mr_data["gitlab_config"] = gitlab_config

        # Add background task for code quality check
        logger.info(
            f"Queuing quality check for MR !{mr_data['mr_iid']} in project "
            f"{mr_data['project_id']} on {gitlab_config['name']}"
        )
        background_tasks.add_task(process_quality_check, mr_data)

        return {
            "status": "accepted",
            "merge_request": mr_data["mr_iid"],
            "instance": gitlab_config["name"],
        }

    except json.JSONDecodeError:
        logger.error("Invalid JSON in webhook payload")
        send_error_notification("webhook_error", "Invalid JSON in webhook payload")
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error handling webhook: {e}")
        send_error_notification("webhook_error", str(e), {"event_type": event_type if 'event_type' in locals() and event_type else 'unknown'})
        raise HTTPException(status_code=500, detail=str(e))


def parse_merge_request_webhook(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Parse GitLab merge request webhook payload"""
    try:
        # Only process open/update actions
        action = payload.get("object_attributes", {}).get("action")
        if action not in ["open", "update", "reopen"]:
            logger.info(f"Ignoring MR action: {action}")
            return None

        object_attributes = payload["object_attributes"]
        project = payload["project"]

        # Construct correct GitLab URL format (using merge_requests with underscores)
        gitlab_url = object_attributes.get("url", "")
        if "/-/mergerequests/" in gitlab_url:
            # Fix the URL format to use merge_requests instead of mergerequests
            gitlab_url = gitlab_url.replace("/-/mergerequests/", "/-/merge_requests/")

        return {
            "project_id": project["id"],
            "project_path": project["path_with_namespace"],
            "mr_iid": object_attributes["iid"],
            "mr_id": object_attributes["id"],
            "source_branch": object_attributes["source_branch"],
            "target_branch": object_attributes["target_branch"],
            "title": object_attributes["title"],
            "description": object_attributes.get("description", ""),
            "author": payload["user"]["username"],
            "action": action,
            "url": gitlab_url,
            "last_commit": object_attributes.get("last_commit", {}).get("id"),
        }
    except KeyError as e:
        logger.error(f"Missing required field in webhook payload: {e}")
        return None


async def process_quality_check(mr_data: Dict[str, Any]):
    """Process quality check for a merge request"""
    try:
        gitlab_config = mr_data.get("gitlab_config")
        if not gitlab_config:
            logger.error("No GitLab configuration found in mr_data")
            return

        logger.info(
            f"Starting quality check for MR !{mr_data['mr_iid']} in project "
            f"{mr_data['project_id']} on {gitlab_config['name']}"
        )
        logger.debug(
            f"MR data: {json.dumps({k: v for k, v in mr_data.items() if k != 'gitlab_config'}, indent=2)}"
        )

        # Get GitLab client for the specific instance
        gl = get_gitlab_client(gitlab_config)

        # Get project with debug info
        try:
            project = gl.projects.get(mr_data["project_id"])
            logger.info(f"Found project: {project.path_with_namespace}")
        except gitlab.exceptions.GitlabGetError as e:
            logger.error(f"Failed to get project {mr_data['project_id']}: {e}")
            send_error_notification(
                "gitlab_api_error",
                f"Failed to get project {mr_data['project_id']}: {str(e)}",
                {
                    "project_id": mr_data["project_id"],
                    "gitlab_instance": gitlab_config["name"],
                },
            )
            return

        # Get MR with debug info
        try:
            mr = project.mergerequests.get(mr_data["mr_iid"])
            logger.info(f"Found MR: !{mr.iid} - {mr.title}")
        except gitlab.exceptions.GitlabGetError as e:
            logger.error(
                f"Failed to get MR !{mr_data['mr_iid']} in project {project.path_with_namespace}: {e}"
            )
            send_error_notification(
                "gitlab_api_error",
                f"Failed to get MR !{mr_data['mr_iid']}: {str(e)}",
                {
                    "project_id": mr_data["project_id"],
                    "mr_iid": mr_data["mr_iid"],
                    "gitlab_instance": gitlab_config["name"],
                },
            )
            return

        # Check for merge conflicts
        has_conflicts = check_merge_conflicts(mr)
        logger.info(f"MR !{mr.iid} has conflicts: {has_conflicts}")

        # Send initial Telegram notification
        telegram_message = format_telegram_message(
            mr_data,
            project.path_with_namespace,
            has_conflicts,
            gitlab_instance=gitlab_config["url"],
        )
        send_telegram_notification(telegram_message)

        # Skip review if conflicts and REVIEW_FOR_CONFLICT is False
        if has_conflicts and not REVIEW_FOR_CONFLICT:
            conflict_skip_message = {
                "en": "⚠️ Merge request has conflicts. Code review skipped until conflicts are resolved.",
                "ru": "⚠️ Запрос на слияние имеет конфликты. Обзор кода пропущен до разрешения конфликтов.",
            }
            mr.notes.create(
                {
                    "body": conflict_skip_message.get(
                        REVIEW_LANGUAGE, conflict_skip_message["en"]
                    )
                }
            )
            logger.info(f"Skipped review for MR !{mr_data['mr_iid']} due to conflicts")
            return

        # Post initial comment
        if has_conflicts:
            initial_message = {
                "en": "⚠️ 🤖 Starting automated code review with Gemini AI (conflicts detected)...",
                "ru": "⚠️ 🤖 Начинаем автоматический обзор кода с помощью Gemini AI (обнаружены конфликты)...",
            }
        else:
            initial_message = {
                "en": "🤖 Starting automated code review with Gemini AI...",
                "ru": "🤖 Начинаем автоматический обзор кода с помощью Gemini AI...",
            }
        mr.notes.create(
            {"body": initial_message.get(REVIEW_LANGUAGE, initial_message["en"])}
        )

        # Fetch MR changes
        changes = mr.changes()
        # Extract diff content with file contents for better context
        review_content = extract_review_content(project, mr, changes, gitlab_config)

        if not review_content:
            no_changes_message = {
                "en": "⚠️ No code changes found to review.",
                "ru": "⚠️ Не найдено изменений кода для обзора.",
            }
            mr.notes.create(
                {
                    "body": no_changes_message.get(
                        REVIEW_LANGUAGE, no_changes_message["en"]
                    )
                }
            )
            return

        # Save review content to temporary file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        ) as tmp_file:
            tmp_file.write(f"Merge Request: {mr_data['title']}\n")
            tmp_file.write(f"Author: {mr_data['author']}\n")
            tmp_file.write(
                f"Source: {mr_data['source_branch']} -> {mr_data['target_branch']}\n\n"
            )
            tmp_file.write(review_content)
            tmp_file_path = tmp_file.name

        try:
            # Call gemini-wrapper
            logger.info("Calling gemini-wrapper for analysis")
            logger.debug(
                f"Diff file: {tmp_file_path}, size: {os.path.getsize(tmp_file_path)} bytes"
            )

            result = subprocess.run(
                ["./gemini-wrapper.sh", tmp_file_path],
                capture_output=True,
                text=True,
                timeout=120,  # 2 minutes timeout
                cwd=os.path.dirname(os.path.abspath(__file__)),
                encoding="utf-8",
                errors="replace",
            )

            logger.debug(f"Gemini wrapper exit code: {result.returncode}")
            logger.debug(f"Gemini wrapper stdout: {result.stdout[:500]}...")
            logger.debug(f"Gemini wrapper stderr: {result.stderr}")

            if result.returncode == 0:
                # Post review results
                logger.debug("Starting to format review comment...")
                review_comment = format_review_comment(result.stdout)
                logger.debug(f"Formatted comment length: {len(review_comment)}")
                logger.debug(f"Formatted comment preview: {review_comment[:200]}...")

                logger.debug("Posting review comment to GitLab...")
                try:
                    mr.notes.create({"body": review_comment})
                    logger.info(f"Posted review for MR !{mr_data['mr_iid']}")

                    # Send Telegram notification with review content
                    if TELEGRAM_ENABLED:
                        telegram_message = format_telegram_message(
                            mr_data,
                            project.path_with_namespace,
                            has_conflicts,
                            result.stdout,
                            gitlab_config["url"],
                        )
                        send_telegram_notification(telegram_message)

                except Exception as e:
                    logger.error(f"Failed to post review comment: {e}")
                    send_error_notification(
                        "gitlab_api_error",
                        f"Failed to post review comment: {str(e)}",
                        {
                            "project_id": mr_data["project_id"],
                            "mr_iid": mr_data["mr_iid"],
                            "gitlab_instance": gitlab_config["name"],
                        },
                    )
                    # Try to post a shorter error message
                    error_msg = {
                        "en": f"❌ Failed to post review comment: {str(e)}",
                        "ru": f"❌ Не удалось опубликовать комментарий с обзором: {str(e)}",
                    }
                    try:
                        mr.notes.create(
                            {"body": error_msg.get(REVIEW_LANGUAGE, error_msg["en"])}
                        )
                    except Exception:
                        logger.error("Failed to post error message as well")
            else:
                error_msg = f"Gemini analysis failed with exit code {result.returncode}"
                logger.error(error_msg)
                logger.error(f"stderr: {result.stderr}")
                send_error_notification(
                    "gemini_failure",
                    f"Exit code {result.returncode}: {result.stderr[:200]}...",
                    {
                        "project_id": mr_data["project_id"],
                        "mr_iid": mr_data["mr_iid"],
                        "gitlab_instance": gitlab_config["name"],
                    },
                )
                error_message = {
                    "en": f"❌ Code review failed:\n```\n{result.stderr}\n```",
                    "ru": f"❌ Обзор кода не удался:\n```\n{result.stderr}\n```",
                }
                mr.notes.create(
                    {"body": error_message.get(REVIEW_LANGUAGE, error_message["en"])}
                )

        finally:
            # Clean up temp file
            os.unlink(tmp_file_path)

    except gitlab.exceptions.GitlabError as e:
        logger.error(f"GitLab API error: {e}")
        send_error_notification(
            "gitlab_api_error",
            str(e),
            {
                "project_id": mr_data.get("project_id"),
                "mr_iid": mr_data.get("mr_iid"),
                "gitlab_instance": gitlab_config.get("name")
                if gitlab_config
                else "unknown",
            },
        )
    except subprocess.TimeoutExpired:
        logger.error("Gemini analysis timed out")
        send_error_notification(
            "timeout",
            "Gemini analysis exceeded timeout limit",
            {
                "project_id": mr_data.get("project_id"),
                "mr_iid": mr_data.get("mr_iid"),
                "gitlab_instance": gitlab_config.get("name")
                if gitlab_config
                else "unknown",
            },
        )
        try:
            timeout_message = {
                "en": "⏱️ Code review timed out. The changes might be too large to analyze.",
                "ru": "⏱️ Тайм-аут обзора кода. Возможно, изменения слишком большие для анализа.",
            }
            mr.notes.create(
                {"body": timeout_message.get(REVIEW_LANGUAGE, timeout_message["en"])}
            )
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Error in quality check: {e}")
        send_error_notification(
            "general",
            str(e),
            {
                "project_id": mr_data.get("project_id"),
                "mr_iid": mr_data.get("mr_iid"),
                "gitlab_instance": gitlab_config.get("name")
                if gitlab_config
                else "unknown",
            },
        )
        try:
            error_message = {
                "en": f"❌ An error occurred during code review: {str(e)}",
                "ru": f"❌ Произошла ошибка при обзоре кода: {str(e)}",
            }
            mr.notes.create(
                {"body": error_message.get(REVIEW_LANGUAGE, error_message["en"])}
            )
        except Exception:
            pass


def extract_diff_content(changes: Dict[str, Any]) -> str:
    """Extract diff content from MR changes"""
    diff_parts = []

    for change in changes.get("changes", []):
        file_path = change.get("new_path", change.get("old_path", "unknown"))
        diff = change.get("diff", "")

        if diff:
            diff_parts.append(f"\n--- {file_path} ---\n{diff}")

    return "\n".join(diff_parts)


def extract_review_content(
    project, mr, changes: Dict[str, Any], gitlab_config: Dict[str, str] = None
) -> str:
    """Extract review content including diffs and original files for context"""
    review_parts = []
    file_count = 0

    for change in changes.get("changes", []):
        file_path = change.get("new_path", change.get("old_path", "unknown"))
        diff = change.get("diff", "")

        if not diff:
            continue

        file_count += 1
        review_parts.append(
            f"\n{'=' * 80}\nFILE #{file_count}: {file_path}\n{'=' * 80}"
        )

        # Check if file was deleted
        if change.get("deleted_file"):
            review_parts.append("\n[FILE DELETED]\n")
            review_parts.append("\n--- DIFF ---\n")
            review_parts.append(diff)
            continue

        # Try to get the current file content from the source branch
        try:
            # For new files, only show the diff
            if change.get("new_file"):
                review_parts.append("\n[NEW FILE]\n")
                review_parts.append("\n--- DIFF ---\n")
                review_parts.append(diff)
            else:
                # Get file content from source branch for context
                try:
                    file_content = project.files.get(file_path, ref=mr.source_branch)
                    decoded_content = file_content.decode().decode(
                        "utf-8", errors="replace"
                    )

                    # Limit file content to reasonable size (first 200 lines)
                    content_lines = decoded_content.split("\n")
                    if len(content_lines) > 200:
                        truncated_content = "\n".join(content_lines[:200])
                        review_parts.append(
                            f"\n--- CURRENT FILE CONTENT (first 200 lines of {len(content_lines)} total) ---\n"
                        )
                        review_parts.append(truncated_content)
                        review_parts.append("\n... [truncated] ...\n")
                    else:
                        review_parts.append("\n--- CURRENT FILE CONTENT ---\n")
                        review_parts.append(decoded_content)
                except Exception as e:
                    logger.debug(f"Could not fetch file content for {file_path}: {e}")
                    review_parts.append(
                        f"\n--- CURRENT FILE CONTENT ---\n[Unable to fetch: {str(e)}]\n"
                    )

                # Add the diff
                review_parts.append("\n--- DIFF ---\n")
                review_parts.append(diff)

        except Exception as e:
            logger.warning(f"Error processing file {file_path}: {e}")
            # Fall back to just the diff
            review_parts.append("\n--- DIFF ---\n")
            review_parts.append(diff)

    return "\n".join(review_parts)


def format_review_comment(gemini_output: str) -> str:
    """Format Gemini output as a merge request comment"""
    # Clean up the output
    cleaned_output = gemini_output.strip()

    # Choose header and footer based on language
    headers = {
        "en": "## 🤖 Automated Code Review",
        "ru": "## 🤖 Автоматический обзор кода",
    }

    footers = {
        "en": "*This review was generated automatically by Gemini AI. "
        "Please review the feedback and address any issues before merging.*",
        "ru": "*Этот обзор был создан автоматически с помощью Gemini AI. "
        "Пожалуйста, изучите отзывы и устраните все проблемы перед слиянием.*",
    }

    # Add header and formatting
    formatted_comment = f"""{headers.get(REVIEW_LANGUAGE, headers["en"])}

{cleaned_output}

---
{footers.get(REVIEW_LANGUAGE, footers["en"])}
"""

    return formatted_comment


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5000)
