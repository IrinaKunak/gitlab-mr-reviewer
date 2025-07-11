#!/usr/bin/env python3

import requests

# Webhook payload for a test merge request event
webhook_payload = {
    "object_kind": "merge_request",
    "event_type": "merge_request",
    "user": {
        "id": 1,
        "name": "Test User",
        "username": "testuser",
        "email": "test@example.com",
    },
    "project": {
        "id": 132,
        "name": "gitlab-mr-reviewer",
        "description": "Test project",
        "path_with_namespace": "spikerwork/gitlab-mr-reviewer",
    },
    "object_attributes": {
        "id": 999,
        "iid": 1,
        "title": "Test MR: Add new feature",
        "description": "This is a test merge request",
        "source_branch": "feature/test",
        "target_branch": "main",
        "state": "opened",
        "action": "open",
        "url": "https://lab.smysl.pro/spikerwork/gitlab-mr-reviewer/-/merge_requests/1",
        "last_commit": {"id": "abc123", "message": "Test commit"},
    },
}

# Test local webhook endpoint
url = "http://localhost:5000/webhook"
headers = {"X-Gitlab-Event": "Merge Request Hook", "Content-Type": "application/json"}

print(f"Testing webhook endpoint at {url}")
print(
    f"Payload: MR !{webhook_payload['object_attributes']['iid']} - {webhook_payload['object_attributes']['title']}"
)

try:
    response = requests.post(url, json=webhook_payload, headers=headers, timeout=5)
    print(f"\nResponse status: {response.status_code}")
    try:
        print(f"Response body: {response.json()}")
    except:
        print(f"Response text: {response.text}")
except requests.exceptions.ConnectionError as e:
    print(f"\n❌ Could not connect to server: {e}")
    print("Make sure the FastAPI server is running on port 5000")
except requests.exceptions.RequestException as e:
    print(f"\n❌ Request error: {e}")
except Exception as e:
    print(f"\n❌ Error: {e}")
