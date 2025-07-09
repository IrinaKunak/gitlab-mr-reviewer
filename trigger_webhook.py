#!/usr/bin/env python3

import os
import gitlab
import requests
import json
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

GITLAB_URL = os.getenv("GITLAB_URL")
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN")

try:
    # Initialize GitLab client
    gl = gitlab.Gitlab(GITLAB_URL, private_token=GITLAB_TOKEN)
    gl.auth()
    
    # Get the project and MR
    project = gl.projects.get(132)
    mr = project.mergerequests.get(2)  # MR !2
    
    print(f"Triggering webhook for MR !{mr.iid} - {mr.title}")
    
    # Build webhook payload based on real MR data
    webhook_payload = {
        "object_kind": "merge_request",
        "event_type": "merge_request",
        "user": {
            "id": mr.author['id'],
            "name": mr.author['name'],
            "username": mr.author['username'],
            "email": mr.author.get('email', 'test@example.com')
        },
        "project": {
            "id": project.id,
            "name": project.name,
            "description": project.description or "",
            "path_with_namespace": project.path_with_namespace
        },
        "object_attributes": {
            "id": mr.id,
            "iid": mr.iid,
            "title": mr.title,
            "description": mr.description or "",
            "source_branch": mr.source_branch,
            "target_branch": mr.target_branch,
            "state": mr.state,
            "action": "update",  # Simulate update action
            "url": mr.web_url,
            "last_commit": {
                "id": mr.sha,
                "message": "Test commit"
            }
        }
    }
    
    # Send webhook to local server
    url = "http://localhost:5000/webhook"
    headers = {
        "X-Gitlab-Event": "Merge Request Hook",
        "Content-Type": "application/json"
    }
    
    print(f"Sending webhook to {url}")
    response = requests.post(url, json=webhook_payload, headers=headers, timeout=10)
    
    print(f"Response status: {response.status_code}")
    print(f"Response body: {response.json()}")
    
    print("\nCheck server.log for processing details")
    
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()