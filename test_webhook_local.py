#!/usr/bin/env python3
"""Test webhook locally"""

import requests
import json
import os
from dotenv import load_dotenv

load_dotenv()

# Test payload
payload = {
    "object_attributes": {
        "id": 123,
        "iid": 1,
        "action": "open",
        "source_branch": "feature-branch",
        "target_branch": "main",
        "title": "Test MR",
        "description": "Test merge request",
        "url": "https://lab.smysl.pro/test/project/-/mergerequests/1",
        "last_commit": {
            "id": "abc123"
        }
    },
    "project": {
        "id": 123,
        "path_with_namespace": "test/project"
    },
    "user": {
        "username": "testuser"
    }
}

# Test with primary instance token
token = os.getenv('XGITLABTOKEN')
print(f"Testing with primary token: {token}")

# Also test with secondary instance token
token2 = os.getenv('XGITLABTOKEN_2')
print(f"Secondary token: {token2}")

headers = {
    'Content-Type': 'application/json',
    'X-Gitlab-Event': 'Merge Request Hook',
    'X-Gitlab-Token': token
}

try:
    response = requests.post('http://localhost:5000/webhook', 
                           json=payload, 
                           headers=headers)
    print(f"Status: {response.status_code}")
    print(f"Response: {response.text}")
    
    if response.status_code == 200:
        print("✅ Webhook test successful!")
    else:
        print("❌ Webhook test failed")
        
except Exception as e:
    print(f"Error: {e}")