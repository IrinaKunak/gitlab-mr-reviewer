#!/usr/bin/env python3
"""
Test script for Docker-deployed GitLab MR Reviewer with new features
"""

import os
import gitlab
import time
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

gitlab_url = os.getenv("GITLAB_URL")
gitlab_token = os.getenv("GITLAB_TOKEN")

print(f"Testing Docker deployment features on {gitlab_url}...")

try:
    gl = gitlab.Gitlab(gitlab_url, private_token=gitlab_token, timeout=30)
    gl.auth()
    print("✅ Connected to GitLab")
    
    project = gl.projects.get(132)
    print(f"✅ Using project: {project.path_with_namespace}")
    
    # Create a test branch
    test_branch = f"test-docker-features-{int(time.time())}"
    
    # Get latest commit
    commits = project.commits.list(ref_name="master", per_page=1, get_all=False)
    latest_commit = commits[0]
    
    # Create branch
    branch = project.branches.create({
        'branch': test_branch,
        'ref': latest_commit.id
    })
    print(f"✅ Created branch: {test_branch}")
    
    # Create a test file with multiple issues for comprehensive testing
    test_content = """# Docker Feature Test File

import os
import subprocess
import json

class DatabaseManager:
    def __init__(self):
        # SECURITY ISSUE: Hardcoded database credentials
        self.db_host = "localhost"
        self.db_user = "admin"
        self.db_password = "admin123"  # Hardcoded password!
        self.api_key = "sk-proj-abcd1234567890"  # Hardcoded API key!
    
    def connect(self):
        # SECURITY VULNERABILITY: SQL injection possible
        connection_string = f"host={self.db_host} user={self.db_user} password={self.db_password}"
        return connection_string
    
    def execute_query(self, user_query):
        # CRITICAL SECURITY FLAW: Direct command execution
        os.system(f"mysql -e '{user_query}'")  # Command injection vulnerability!
    
    def unsafe_deserialize(self, data):
        # SECURITY ISSUE: Unsafe deserialization
        import pickle
        return pickle.loads(data)  # Dangerous!

# PERFORMANCE ISSUE: Inefficient algorithm O(n²)
def find_duplicates(items):
    duplicates = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if items[i] == items[j] and items[i] not in duplicates:
                duplicates.append(items[i])
    return duplicates

# BUG: Missing error handling
def divide_numbers(a, b):
    return a / b  # Division by zero not handled!

# CODE QUALITY ISSUE: Poor exception handling
def read_config_file(filename):
    try:
        with open(filename, 'r') as f:
            return json.load(f)
    except:  # Too broad exception handling
        pass  # Silent failure

# POTENTIAL BUG: Mutable default argument
def add_item(item, items_list=[]):
    items_list.append(item)
    return items_list

# Testing the new Docker features:
# 1. GitLab instance info in Telegram notifications
# 2. Multiple Telegram channels support  
# 3. Enhanced file context reviews
# 4. Gemini debug logging
print("This file tests all new Docker deployment features!")
"""
    
    file_path = f"docker_test_{test_branch}.py"
    project.files.create({
        'file_path': file_path,
        'branch': test_branch,
        'content': test_content,
        'commit_message': f'Add Docker feature test file - {test_branch}'
    })
    print(f"✅ Created test file: {file_path}")
    
    # Create MR
    mr = project.mergerequests.create({
        'source_branch': test_branch,
        'target_branch': 'master',
        'title': f'🐳 Docker Feature Test - {test_branch}',
        'description': f'''# Docker Deployment Feature Test

This MR tests the new features implemented in the Docker deployment:

## 🆕 New Features Being Tested:
1. **GitLab Instance Info in Telegram** - Should show "from lab.smysl.pro"
2. **Multiple Telegram Channels** - Should notify all configured channels
3. **Enhanced File Reviews** - Should include original file content + diff
4. **Gemini Debug Logging** - Should create detailed debug logs

## 🐛 Issues in Code (for AI to find):
- **Security**: Hardcoded credentials, SQL injection, command injection, unsafe deserialization
- **Performance**: O(n²) algorithm in find_duplicates
- **Bugs**: Division by zero, mutable default arguments
- **Code Quality**: Broad exception handling, silent failures

## 🧪 Expected Behavior:
- Webhook should be received by Docker container
- Both Telegram channels should get notifications with instance info
- Gemini should provide comprehensive review with file context
- Debug logs should capture request/response details

**Container**: Running in Docker on port 5000
**Instance**: {gitlab_url}'''
    })
    
    print(f"✅ Created MR: !{mr.iid}")
    print(f"   URL: {mr.web_url}")
    print(f"\n🐳 Docker Feature Test Summary:")
    print(f"   📦 Container: Running on port 5000")
    print(f"   🔗 Instance: {gitlab_url}")  
    print(f"   📱 Telegram: Should notify multiple channels")
    print(f"   🧠 Gemini: Debug logging enabled")
    print(f"   📄 Review: Enhanced with file context")
    
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()