import os
import json
import logging
import subprocess
import tempfile
import requests
from typing import Dict, Any, Optional
from datetime import datetime

from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
import gitlab
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
GITLAB_URL = os.getenv("GITLAB_URL")
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN")
SOCKS_PROXY = os.getenv("SOCKS_PROXY")
HTTP_PROXY = os.getenv("HTTP_PROXY")
GEMINI_PROMPT = os.getenv("GEMINI_PROMPT",
                          "Review this merge request and provide feedback on code quality, potential issues, and suggestions for improvement.")
GEMINI_PROMPT_RU = os.getenv("GEMINI_PROMPT_RU",
                            "Проанализируйте этот запрос на слияние и предоставьте отзыв о качестве кода, потенциальных проблемах и предложения по улучшению.")
REVIEW_LANGUAGE = os.getenv("REVIEW_LANGUAGE", "en")
TELEGRAM_ENABLED = os.getenv("TELEGRAM", "off").lower() == "on"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
REVIEW_FOR_CONFLICT = os.getenv("REVIEW_FOR_CONFLICT", "false").lower() == "true"

# Logging setup
logging.basicConfig(
    level=logging.DEBUG if os.getenv("DEBUG", "").lower() == "true" else logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(
    title="GitLab MR Reviewer",
    description="Webhook server for automated merge request reviews using Gemini AI"
)


# Initialize GitLab client with proxy support
def get_gitlab_client():
    try:
        # Setup session with proxy if configured
        session = None
        if HTTP_PROXY or SOCKS_PROXY:
            import requests
            session = requests.Session()
            
            if HTTP_PROXY:
                logger.debug(f"Using HTTP proxy: {HTTP_PROXY}")
                session.proxies = {
                    'http': HTTP_PROXY,
                    'https': HTTP_PROXY
                }
            elif SOCKS_PROXY:
                logger.debug(f"Using SOCKS proxy: {SOCKS_PROXY}")
                # For SOCKS proxy, we need pysocks
                try:
                    import socks
                    import socket
                    
                    proxy_host, proxy_port = SOCKS_PROXY.split(':')
                    socks.set_default_proxy(socks.SOCKS5, proxy_host, int(proxy_port))
                    socket.socket = socks.socksocket
                    logger.debug(f"SOCKS proxy configured: {proxy_host}:{proxy_port}")
                except ImportError:
                    logger.warning("PySocks not available for SOCKS proxy support")
                except Exception as e:
                    logger.error(f"Failed to configure SOCKS proxy: {e}")
        
        gl = gitlab.Gitlab(GITLAB_URL, private_token=GITLAB_TOKEN, session=session)
        gl.auth()
        return gl
    except Exception as e:
        logger.error(f"Failed to initialize GitLab client: {e}")
        raise


def send_telegram_notification(message: str) -> bool:
    """Send notification to Telegram"""
    if not TELEGRAM_ENABLED or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram notifications disabled or not configured")
        return False
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
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
                proxy_host, proxy_port = SOCKS_PROXY.split(':')
                proxies = {"http": f"socks5://{proxy_host}:{proxy_port}", "https": f"socks5://{proxy_host}:{proxy_port}"}
            except ImportError:
                logger.warning("PySocks not available for Telegram SOCKS proxy")
        
        response = requests.post(url, json=data, proxies=proxies, timeout=10)
        response.raise_for_status()
        
        logger.debug(f"Telegram notification sent successfully")
        return True
        
    except Exception as e:
        logger.error(f"Failed to send Telegram notification: {e}")
        return False


def format_telegram_message(mr_data: Dict[str, Any], project_name: str, has_conflicts: bool = False, review_content: str = None) -> str:
    """Format message for Telegram notification"""
    # Status and emoji based on conflict and review state
    if has_conflicts:
        status_emoji = "⚠️"
        status_text = "MR with CONFLICTS" if REVIEW_LANGUAGE == "en" else "MR С КОНФЛИКТАМИ"
    else:
        status_emoji = "✅"
        status_text = "New MR" if REVIEW_LANGUAGE == "en" else "Новый MR"
    
    # Build message header
    message_parts = [
        f"{status_emoji} **{status_text}**",
        f"**Project:** `{project_name}`",
        f"**Author:** {mr_data['author']}",
        f"**Title:** {mr_data['title']}",
        f"**Branch:** `{mr_data['source_branch']}` → `{mr_data['target_branch']}`",
        f"**Link:** [!{mr_data['mr_iid']}]({mr_data['url']})"
    ]
    
    # Add conflict warning if present
    if has_conflicts:
        conflict_msg = "🚫 **BLOCKED: Merge conflicts must be resolved before merging!**" if REVIEW_LANGUAGE == "en" else "🚫 **ЗАБЛОКИРОВАН: Конфликты слияния должны быть разрешены перед слиянием!**"
        message_parts.append("")
        message_parts.append(conflict_msg)
    
    # Add review content if available and not too long
    if review_content and len(review_content) < 2000:  # Telegram message limit consideration
        review_header = "\n📝 **Code Review:**" if REVIEW_LANGUAGE == "en" else "\n📝 **Обзор кода:**"
        message_parts.append(review_header)
        # Truncate review if too long for Telegram
        truncated_review = review_content[:1500] + "..." if len(review_content) > 1500 else review_content
        message_parts.append(f"```\n{truncated_review}\n```")
    elif review_content:
        review_note = "\n📝 Code review posted to GitLab (too long for Telegram)" if REVIEW_LANGUAGE == "en" else "\n📝 Обзор кода опубликован в GitLab (слишком длинный для Telegram)"
        message_parts.append(review_note)
    
    return "\n".join(message_parts)


def check_merge_conflicts(mr) -> bool:
    """Check if merge request has conflicts"""
    try:
        # Get MR details including merge status
        mr_details = mr.manager.gitlab.http_get(f"/projects/{mr.project_id}/merge_requests/{mr.iid}")
        
        # Check various conflict indicators
        merge_status = mr_details.get('merge_status', '')
        has_conflicts = (
            merge_status == 'cannot_be_merged' or
            merge_status == 'cannot_be_merged_recheck' or
            mr_details.get('has_conflicts', False) or
            mr_details.get('blocking_discussions_resolved', True) == False  # Unresolved discussions can block
        )
        
        logger.debug(f"MR !{mr.iid} merge_status: {merge_status}, has_conflicts: {has_conflicts}")
        return has_conflicts
        
    except Exception as e:
        logger.warning(f"Could not check merge conflicts for MR !{mr.iid}: {e}")
        return False  # Assume no conflicts if we can't check


@app.get("/")
async def root():
    return {"status": "GitLab MR Reviewer is running", "version": "1.0.0"}


@app.post("/webhook")
async def handle_gitlab_webhook(request: Request, background_tasks: BackgroundTasks):
    """Handle GitLab webhook events for merge requests"""
    try:
        # Get webhook headers
        event_type = request.headers.get("X-Gitlab-Event")
        gitlab_token = request.headers.get("X-Gitlab-Token")

        # Verify webhook token if configured
        expected_token = os.getenv("WEBHOOK_SECRET")
        if expected_token and gitlab_token != expected_token:
            logger.warning("Invalid webhook token received")
            raise HTTPException(status_code=401, detail="Invalid webhook token")

        # Parse webhook payload
        payload = await request.json()

        # Only process merge request events
        if event_type != "Merge Request Hook":
            logger.info(f"Ignoring non-MR event: {event_type}")
            return {"status": "ignored", "reason": f"Not a merge request event: {event_type}"}

        # Extract merge request details
        mr_data = parse_merge_request_webhook(payload)

        if not mr_data:
            return {"status": "ignored", "reason": "Invalid or unsupported MR action"}

        # Add background task for code quality check
        logger.info(f"Queuing quality check for MR !{mr_data['mr_iid']} in project {mr_data['project_id']}")
        background_tasks.add_task(process_quality_check, mr_data)

        return {"status": "accepted", "merge_request": mr_data['mr_iid']}

    except json.JSONDecodeError:
        logger.error("Invalid JSON in webhook payload")
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    except Exception as e:
        logger.error(f"Error handling webhook: {e}")
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
            "url": object_attributes["url"],
            "last_commit": object_attributes.get("last_commit", {}).get("id")
        }
    except KeyError as e:
        logger.error(f"Missing required field in webhook payload: {e}")
        return None


async def process_quality_check(mr_data: Dict[str, Any]):
    """Process quality check for a merge request"""
    try:
        logger.info(f"Starting quality check for MR !{mr_data['mr_iid']} in project {mr_data['project_id']}")
        logger.debug(f"MR data: {json.dumps(mr_data, indent=2)}")

        # Get GitLab client
        gl = get_gitlab_client()
        
        # Get project with debug info
        try:
            project = gl.projects.get(mr_data["project_id"])
            logger.info(f"Found project: {project.path_with_namespace}")
        except gitlab.exceptions.GitlabGetError as e:
            logger.error(f"Failed to get project {mr_data['project_id']}: {e}")
            return
            
        # Get MR with debug info
        try:
            mr = project.mergerequests.get(mr_data["mr_iid"])
            logger.info(f"Found MR: !{mr.iid} - {mr.title}")
        except gitlab.exceptions.GitlabGetError as e:
            logger.error(f"Failed to get MR !{mr_data['mr_iid']} in project {project.path_with_namespace}: {e}")
            return
        
        # Check for merge conflicts
        has_conflicts = check_merge_conflicts(mr)
        logger.info(f"MR !{mr.iid} has conflicts: {has_conflicts}")

        # Send initial Telegram notification
        telegram_message = format_telegram_message(mr_data, project.path_with_namespace, has_conflicts)
        send_telegram_notification(telegram_message)
        
        # Skip review if conflicts and REVIEW_FOR_CONFLICT is False
        if has_conflicts and not REVIEW_FOR_CONFLICT:
            conflict_skip_message = {
                'en': '⚠️ Merge request has conflicts. Code review skipped until conflicts are resolved.',
                'ru': '⚠️ Запрос на слияние имеет конфликты. Обзор кода пропущен до разрешения конфликтов.'
            }
            mr.notes.create({
                'body': conflict_skip_message.get(REVIEW_LANGUAGE, conflict_skip_message['en'])
            })
            logger.info(f"Skipped review for MR !{mr_data['mr_iid']} due to conflicts")
            return
        
        # Post initial comment
        if has_conflicts:
            initial_message = {
                'en': '⚠️ 🤖 Starting automated code review with Gemini AI (conflicts detected)...',
                'ru': '⚠️ 🤖 Начинаем автоматический обзор кода с помощью Gemini AI (обнаружены конфликты)...'
            }
        else:
            initial_message = {
                'en': '🤖 Starting automated code review with Gemini AI...',
                'ru': '🤖 Начинаем автоматический обзор кода с помощью Gemini AI...'
            }
        mr.notes.create({
            'body': initial_message.get(REVIEW_LANGUAGE, initial_message['en'])
        })

        # Fetch MR changes
        changes = mr.changes()
        diff_content = extract_diff_content(changes)

        if not diff_content:
            no_changes_message = {
                'en': '⚠️ No code changes found to review.',
                'ru': '⚠️ Не найдено изменений кода для обзора.'
            }
            mr.notes.create({
                'body': no_changes_message.get(REVIEW_LANGUAGE, no_changes_message['en'])
            })
            return

        # Save diff to temporary file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.diff', delete=False) as tmp_file:
            tmp_file.write(f"Merge Request: {mr_data['title']}\n")
            tmp_file.write(f"Author: {mr_data['author']}\n")
            tmp_file.write(f"Source: {mr_data['source_branch']} -> {mr_data['target_branch']}\n\n")
            tmp_file.write(diff_content)
            tmp_file_path = tmp_file.name

        try:
            # Call gemini-wrapper
            logger.info(f"Calling gemini-wrapper for analysis")
            logger.debug(f"Diff file: {tmp_file_path}, size: {os.path.getsize(tmp_file_path)} bytes")
            
            result = subprocess.run(
                ['./gemini-wrapper.sh', tmp_file_path],
                capture_output=True,
                text=True,
                timeout=120,  # 2 minutes timeout
                cwd=os.path.dirname(os.path.abspath(__file__)),
                encoding='utf-8',
                errors='replace'
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
                    mr.notes.create({'body': review_comment})
                    logger.info(f"Posted review for MR !{mr_data['mr_iid']}")
                    
                    # Send Telegram notification with review content
                    if TELEGRAM_ENABLED:
                        telegram_message = format_telegram_message(mr_data, project.path_with_namespace, has_conflicts, result.stdout)
                        send_telegram_notification(telegram_message)
                        
                except Exception as e:
                    logger.error(f"Failed to post review comment: {e}")
                    # Try to post a shorter error message
                    error_msg = {
                        'en': f'❌ Failed to post review comment: {str(e)}',
                        'ru': f'❌ Не удалось опубликовать комментарий с обзором: {str(e)}'
                    }
                    try:
                        mr.notes.create({'body': error_msg.get(REVIEW_LANGUAGE, error_msg['en'])})
                    except Exception:
                        logger.error("Failed to post error message as well")
            else:
                error_msg = f"Gemini analysis failed with exit code {result.returncode}"
                logger.error(error_msg)
                logger.error(f"stderr: {result.stderr}")
                error_message = {
                    'en': f'❌ Code review failed:\n```\n{result.stderr}\n```',
                    'ru': f'❌ Обзор кода не удался:\n```\n{result.stderr}\n```'
                }
                mr.notes.create({
                    'body': error_message.get(REVIEW_LANGUAGE, error_message['en'])
                })

        finally:
            # Clean up temp file
            os.unlink(tmp_file_path)

    except gitlab.exceptions.GitlabError as e:
        logger.error(f"GitLab API error: {e}")
    except subprocess.TimeoutExpired:
        logger.error("Gemini analysis timed out")
        try:
            timeout_message = {
                'en': '⏱️ Code review timed out. The changes might be too large to analyze.',
                'ru': '⏱️ Тайм-аут обзора кода. Возможно, изменения слишком большие для анализа.'
            }
            mr.notes.create({
                'body': timeout_message.get(REVIEW_LANGUAGE, timeout_message['en'])
            })
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Error in quality check: {e}")
        try:
            error_message = {
                'en': f'❌ An error occurred during code review: {str(e)}',
                'ru': f'❌ Произошла ошибка при обзоре кода: {str(e)}'
            }
            mr.notes.create({
                'body': error_message.get(REVIEW_LANGUAGE, error_message['en'])
            })
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


def format_review_comment(gemini_output: str) -> str:
    """Format Gemini output as a merge request comment"""
    # Clean up the output
    cleaned_output = gemini_output.strip()

    # Choose header and footer based on language
    headers = {
        'en': '## 🤖 Automated Code Review',
        'ru': '## 🤖 Автоматический обзор кода'
    }
    
    footers = {
        'en': '*This review was generated automatically by Gemini AI. Please review the feedback and address any issues before merging.*',
        'ru': '*Этот обзор был создан автоматически с помощью Gemini AI. Пожалуйста, изучите отзывы и устраните все проблемы перед слиянием.*'
    }

    # Add header and formatting
    formatted_comment = f"""{headers.get(REVIEW_LANGUAGE, headers['en'])}

{cleaned_output}

---
{footers.get(REVIEW_LANGUAGE, footers['en'])}
"""

    return formatted_comment


@app.on_event("startup")
async def startup_event():
    """Initialize on startup"""
    logger.info("Starting GitLab MR Reviewer")
    logger.info(f"GitLab URL: {GITLAB_URL}")
    logger.info(f"Webhook endpoint: http://7820.spikerwork.keenetic.pro/webhook")
    logger.info(f"Review language: {REVIEW_LANGUAGE}")
    logger.info(f"Telegram notifications: {'enabled' if TELEGRAM_ENABLED else 'disabled'}")
    logger.info(f"Review for conflicts: {'enabled' if REVIEW_FOR_CONFLICT else 'disabled'}")
    
    # Log proxy configuration
    if HTTP_PROXY:
        logger.info(f"HTTP Proxy configured: {HTTP_PROXY}")
    if SOCKS_PROXY:
        logger.info(f"SOCKS Proxy configured: {SOCKS_PROXY}")
    if not HTTP_PROXY and not SOCKS_PROXY:
        logger.info("No proxy configured - using direct connection")

    # Verify GitLab connection
    try:
        gl = get_gitlab_client()
        logger.info("Successfully connected to GitLab")
    except Exception as e:
        logger.error(f"Failed to connect to GitLab: {e}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5000)
