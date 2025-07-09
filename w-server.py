import os
import json
import logging
import subprocess
import tempfile
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
GEMINI_PROMPT = os.getenv("GEMINI_PROMPT",
                          "Review this merge request and provide feedback on code quality, potential issues, and suggestions for improvement.")
GEMINI_PROMPT_RU = os.getenv("GEMINI_PROMPT_RU",
                            "Проанализируйте этот запрос на слияние и предоставьте отзыв о качестве кода, потенциальных проблемах и предложения по улучшению.")
REVIEW_LANGUAGE = os.getenv("REVIEW_LANGUAGE", "en")

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


# Initialize GitLab client
def get_gitlab_client():
    try:
        gl = gitlab.Gitlab(GITLAB_URL, private_token=GITLAB_TOKEN)
        gl.auth()
        return gl
    except Exception as e:
        logger.error(f"Failed to initialize GitLab client: {e}")
        raise


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

        # Post initial comment
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
                cwd=os.path.dirname(os.path.abspath(__file__))
            )
            
            logger.debug(f"Gemini wrapper exit code: {result.returncode}")
            logger.debug(f"Gemini wrapper stdout: {result.stdout[:500]}...")
            logger.debug(f"Gemini wrapper stderr: {result.stderr}")
            
            if result.returncode == 0:
                # Post review results
                review_comment = format_review_comment(result.stdout)
                mr.notes.create({'body': review_comment})
                logger.info(f"Posted review for MR !{mr_data['mr_iid']}")
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

    # Verify GitLab connection
    try:
        gl = get_gitlab_client()
        logger.info("Successfully connected to GitLab")
    except Exception as e:
        logger.error(f"Failed to connect to GitLab: {e}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5000)
