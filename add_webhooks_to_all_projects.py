#!/usr/bin/env python3
"""
Script to add webhook integration for merge request events to all projects
in configured GitLab instances.
"""

import logging
import os
import sys
import time
from typing import Any

import gitlab
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
WEBHOOK_ENDPOINT = os.getenv("WEBHOOK_ENDPOINT", "https://r.smysl.pro/webhook")
SOCKS_PROXY = os.getenv("SOCKS_PROXY")
HTTP_PROXY = os.getenv("HTTP_PROXY")

# Setup logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def load_gitlab_instances() -> dict[str, dict[str, str]]:
    """Load GitLab instances configuration from environment variables"""
    instances = {}

    # Load primary instance
    if os.getenv("GITLAB_URL") and os.getenv("GITLAB_TOKEN"):
        webhook_token = os.getenv("XGITLABTOKEN")
        if webhook_token:
            instances["primary"] = {
                "url": os.getenv("GITLAB_URL"),
                "token": os.getenv("GITLAB_TOKEN"),
                "webhook_token": webhook_token,
                "name": "primary",
            }

    # Load additional instances (up to 10)
    for i in range(2, 11):
        url_key = f"GITLAB_URL_{i}"
        token_key = f"GITLAB_TOKEN_{i}"
        webhook_key = f"XGITLABTOKEN_{i}"

        if os.getenv(url_key) and os.getenv(token_key) and os.getenv(webhook_key):
            instances[f"instance_{i}"] = {
                "url": os.getenv(url_key),
                "token": os.getenv(token_key),
                "webhook_token": os.getenv(webhook_key),
                "name": f"instance_{i}",
            }

    return instances


def get_gitlab_client(instance_config: dict[str, str]) -> gitlab.Gitlab:
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
                try:
                    import socket

                    import socks

                    proxy_host, proxy_port = SOCKS_PROXY.split(":")
                    socks.set_default_proxy(socks.SOCKS5, proxy_host, int(proxy_port))
                    socket.socket = socks.socksocket
                    logger.debug(f"SOCKS proxy configured: {proxy_host}:{proxy_port}")
                except ImportError:
                    logger.warning("PySocks not available for SOCKS proxy support")
                except Exception as e:
                    logger.error(f"Failed to configure SOCKS proxy: {e}")

        gl = gitlab.Gitlab(
            instance_config["url"],
            private_token=instance_config["token"],
            session=session,
        )
        gl.auth()
        return gl
    except Exception as e:
        logger.error(
            f"Failed to initialize GitLab client for {instance_config['name']}: {e}"
        )
        raise


def get_all_projects(gl: gitlab.Gitlab) -> list[Any]:
    """Get all projects from GitLab instance"""
    try:
        # Get all projects (including ones user is a member of)
        projects = gl.projects.list(all=True, membership=True)
        logger.info(f"Found {len(projects)} projects")
        return projects
    except Exception as e:
        logger.error(f"Failed to get projects: {e}")
        raise


def check_existing_webhook(project, webhook_url: str) -> Any | None:
    """Check if webhook already exists for the project"""
    try:
        hooks = project.hooks.list()
        for hook in hooks:
            if hook.url == webhook_url:
                return hook
        return None
    except Exception as e:
        logger.debug(
            f"Failed to check existing webhooks for project {project.path_with_namespace}: {e}"
        )
        return None


def ensure_hook_events(project, hook) -> bool:
    """Bring an existing hook up to the current event set (MR + note events).

    note_events powers the MR dialogue feature (the bot answering replies in
    discussion threads) — hooks created before 2026-07-31 have it off.
    Returns True when the hook was modified."""
    changed = False
    if not getattr(hook, "merge_requests_events", False):
        hook.merge_requests_events = True
        changed = True
    if not getattr(hook, "note_events", False):
        hook.note_events = True
        changed = True
    if changed:
        hook.save()
        logger.info(
            f"🔁 Updated webhook events for {project.path_with_namespace} "
            f"(note_events enabled)"
        )
    return changed


def add_webhook_to_project(project, webhook_url: str, webhook_token: str) -> bool:
    """Add webhook to a specific project"""
    try:
        # Check if webhook already exists
        existing_hook = check_existing_webhook(project, webhook_url)
        if existing_hook:
            ensure_hook_events(project, existing_hook)
            logger.info(
                f"Webhook already exists for {project.path_with_namespace} (ID: {existing_hook.id})"
            )
            return True

        # Create webhook
        hook_data = {
            "url": webhook_url,
            "merge_requests_events": True,
            "push_events": False,
            "issues_events": False,
            "confidential_issues_events": False,
            "tag_push_events": False,
            "note_events": True,  # MR dialogue: the bot answers thread replies
            "job_events": False,
            "pipeline_events": False,
            "wiki_page_events": False,
            "deployment_events": False,
            "releases_events": False,
            "subgroup_events": False,
            "enable_ssl_verification": True,
            "token": webhook_token,
        }

        hook = project.hooks.create(hook_data)
        logger.info(
            f"✅ Added webhook to {project.path_with_namespace} (ID: {hook.id})"
        )
        return True

    except gitlab.exceptions.GitlabCreateError as e:
        logger.error(
            f"❌ Failed to create webhook for {project.path_with_namespace}: {e}"
        )
        return False
    except Exception as e:
        logger.error(f"❌ Error adding webhook to {project.path_with_namespace}: {e}")
        return False


def test_webhook_endpoint() -> bool:
    """Test if webhook endpoint is reachable"""
    try:
        # Use proxy if configured
        proxies = None
        if HTTP_PROXY:
            proxies = {"http": HTTP_PROXY, "https": HTTP_PROXY}
        elif SOCKS_PROXY:
            try:
                import socks
                import urllib3.contrib.socks

                proxy_host, proxy_port = SOCKS_PROXY.split(":")
                proxies = {
                    "http": f"socks5://{proxy_host}:{proxy_port}",
                    "https": f"socks5://{proxy_host}:{proxy_port}",
                }
            except ImportError:
                logger.warning("PySocks not available for webhook endpoint test")

        response = requests.get(
            WEBHOOK_ENDPOINT.replace("/webhook", "/"), proxies=proxies, timeout=10
        )
        if response.status_code == 200:
            logger.info(f"✅ Webhook endpoint is reachable: {WEBHOOK_ENDPOINT}")
            return True
        else:
            logger.warning(
                f"⚠️ Webhook endpoint returned status {response.status_code}: {WEBHOOK_ENDPOINT}"
            )
            return False
    except Exception as e:
        logger.error(f"❌ Failed to test webhook endpoint {WEBHOOK_ENDPOINT}: {e}")
        return False


def process_gitlab_instance(
    instance_name: str, instance_config: dict[str, str], dry_run: bool = False
) -> dict[str, Any]:
    """Process all projects in a GitLab instance"""
    results = {
        "instance": instance_name,
        "url": instance_config["url"],
        "total_projects": 0,
        "successful_webhooks": 0,
        "failed_webhooks": 0,
        "existing_webhooks": 0,
        "errors": [],
    }

    try:
        logger.info(
            f"\n🔗 Processing GitLab instance: {instance_name} ({instance_config['url']})"
        )

        # Get GitLab client
        gl = get_gitlab_client(instance_config)

        # Get all projects
        projects = get_all_projects(gl)
        results["total_projects"] = len(projects)

        if not projects:
            logger.warning(f"No projects found in {instance_name}")
            return results

        # Process each project
        for project in projects:
            try:
                logger.info(f"Processing project: {project.path_with_namespace}")

                if dry_run:
                    logger.info(
                        f"[DRY RUN] Would add webhook to {project.path_with_namespace}"
                    )
                    results["successful_webhooks"] += 1
                    continue

                # Check if webhook already exists (and refresh its event set —
                # pre-2026-07-31 hooks lack note_events for the MR dialogue)
                existing_hook = check_existing_webhook(project, WEBHOOK_ENDPOINT)
                if existing_hook:
                    try:
                        ensure_hook_events(project, existing_hook)
                    except Exception as e:
                        logger.error(
                            f"Failed to update webhook events for "
                            f"{project.path_with_namespace}: {e}"
                        )
                    results["existing_webhooks"] += 1
                    continue

                # Add webhook
                if add_webhook_to_project(
                    project, WEBHOOK_ENDPOINT, instance_config["webhook_token"]
                ):
                    results["successful_webhooks"] += 1
                    # Small delay to avoid rate limiting
                    time.sleep(0.5)
                else:
                    results["failed_webhooks"] += 1

            except Exception as e:
                error_msg = (
                    f"Error processing project {project.path_with_namespace}: {str(e)}"
                )
                logger.error(error_msg)
                results["errors"].append(error_msg)
                results["failed_webhooks"] += 1

    except Exception as e:
        error_msg = f"Error processing GitLab instance {instance_name}: {str(e)}"
        logger.error(error_msg)
        results["errors"].append(error_msg)

    return results


def main():
    """Main function"""
    import argparse

    parser = argparse.ArgumentParser(
        description="Add webhooks to all projects in GitLab instances"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without actually doing it",
    )
    parser.add_argument(
        "--test-endpoint",
        action="store_true",
        help="Test webhook endpoint reachability",
    )
    parser.add_argument(
        "--instance", help="Process only specific instance (primary, instance_2, etc.)"
    )
    args = parser.parse_args()

    logger.info("🚀 GitLab Webhook Integration Script")
    logger.info(f"Webhook endpoint: {WEBHOOK_ENDPOINT}")

    if args.test_endpoint:
        logger.info("\n🔍 Testing webhook endpoint...")
        if not test_webhook_endpoint():
            logger.error("❌ Webhook endpoint test failed. Please check your server.")
            return 1

    # Load GitLab instances
    instances = load_gitlab_instances()

    if not instances:
        logger.error("❌ No GitLab instances configured in environment variables")
        return 1

    logger.info(f"\n📋 Found {len(instances)} configured GitLab instance(s):")
    for name, config in instances.items():
        logger.info(f"  - {name}: {config['url']}")

    if args.dry_run:
        logger.info("\n🧪 DRY RUN MODE - No actual changes will be made")

    # Process instances
    all_results = []

    for instance_name, instance_config in instances.items():
        if args.instance and instance_name != args.instance:
            logger.info(f"Skipping instance {instance_name} (not specified)")
            continue

        results = process_gitlab_instance(instance_name, instance_config, args.dry_run)
        all_results.append(results)

    # Print summary
    logger.info("\n📊 SUMMARY")
    logger.info("=" * 50)

    total_projects = 0
    total_successful = 0
    total_failed = 0
    total_existing = 0

    for results in all_results:
        logger.info(f"\n{results['instance']} ({results['url']}):")
        logger.info(f"  Total projects: {results['total_projects']}")
        logger.info(f"  Successful webhooks: {results['successful_webhooks']}")
        logger.info(f"  Failed webhooks: {results['failed_webhooks']}")
        logger.info(f"  Existing webhooks: {results['existing_webhooks']}")

        if results["errors"]:
            logger.info(f"  Errors: {len(results['errors'])}")
            for error in results["errors"]:
                logger.error(f"    - {error}")

        total_projects += results["total_projects"]
        total_successful += results["successful_webhooks"]
        total_failed += results["failed_webhooks"]
        total_existing += results["existing_webhooks"]

    logger.info("\n🎯 TOTAL ACROSS ALL INSTANCES:")
    logger.info(f"  Total projects: {total_projects}")
    logger.info(f"  Successful webhooks: {total_successful}")
    logger.info(f"  Failed webhooks: {total_failed}")
    logger.info(f"  Existing webhooks: {total_existing}")

    if total_failed > 0:
        logger.warning(f"⚠️ {total_failed} webhooks failed to be created")
        return 1

    logger.info("✅ All webhooks processed successfully!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
