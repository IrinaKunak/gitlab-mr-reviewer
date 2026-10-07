#!/usr/bin/env python3
"""
Test script for multi-instance GitLab webhook configuration
"""

import os
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
WEBHOOK_URL = os.getenv("WEBHOOK_ENDPOINT", "http://localhost:5000/webhook")


def test_webhook_instance(instance_number=None):
    """Test webhook for a specific GitLab instance"""

    # Get the webhook token for the instance
    if instance_number is None:
        token_key = "XGITLABTOKEN"
        instance_name = "primary"
        gitlab_url = os.getenv("GITLAB_URL", "https://lab.smysl.pro")
    else:
        token_key = f"XGITLABTOKEN_{instance_number}"
        instance_name = f"instance_{instance_number}"
        gitlab_url = os.getenv(
            f"GITLAB_URL_{instance_number}",
            f"https://example{instance_number}.gitlab.com",
        )

    webhook_token = os.getenv(token_key)

    if not webhook_token:
        print(f"❌ No webhook token found for {instance_name} ({token_key})")
        return

    print(f"\n🔧 Testing {instance_name} (URL: {gitlab_url})")
    print(f"   Using token: {webhook_token[:10]}...")

    # Create a test MR webhook payload
    payload = {
        "object_kind": "merge_request",
        "event_type": "merge_request",
        "user": {"username": "test_user", "name": "Test User"},
        "project": {
            "id": 12345,
            "name": "test-project",
            "path_with_namespace": f"test-group/test-project-{instance_name}",
            "web_url": f"{gitlab_url}/test-group/test-project",
        },
        "object_attributes": {
            "id": 1,
            "iid": 1,
            "title": f"Test MR for {instance_name}",
            "description": "This is a test merge request",
            "source_branch": "feature/test",
            "target_branch": "main",
            "action": "open",
            "url": f"{gitlab_url}/test-group/test-project/-/merge_requests/1",
            "last_commit": {"id": "abc123def456"},
        },
    }

    # Send the webhook
    headers = {
        "Content-Type": "application/json",
        "X-Gitlab-Event": "Merge Request Hook",
        "X-Gitlab-Token": webhook_token,
    }

    try:
        response = requests.post(WEBHOOK_URL, json=payload, headers=headers, timeout=10)

        if response.status_code == 200:
            print(f"✅ Success! Response: {response.json()}")
        else:
            print(f"❌ Failed with status {response.status_code}")
            print(f"   Response: {response.text}")

    except requests.exceptions.RequestException as e:
        print(f"❌ Request failed: {e}")


def main():
    """Test all configured GitLab instances"""
    print("🚀 GitLab Multi-Instance Webhook Tester")
    print(f"📍 Webhook URL: {WEBHOOK_URL}")
    print("=" * 50)

    # Test primary instance
    test_webhook_instance()

    # Test additional instances (up to 10)
    for i in range(2, 11):
        if os.getenv(f"GITLAB_URL_{i}") and os.getenv(f"XGITLABTOKEN_{i}"):
            test_webhook_instance(i)

    print("\n✨ Testing complete!")
    print("\nNote: Check the server logs and Telegram for notifications.")
    print("The webhook processor runs asynchronously, so the actual MR processing")
    print("will happen in the background after the webhook returns success.")


if __name__ == "__main__":
    main()
