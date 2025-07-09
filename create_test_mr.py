#!/usr/bin/env python3

import os
import gitlab
import time
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

GITLAB_URL = os.getenv("GITLAB_URL")
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN")

print(f"Creating test merge request in GitLab...")

try:
    # Initialize GitLab client
    gl = gitlab.Gitlab(GITLAB_URL, private_token=GITLAB_TOKEN)
    gl.auth()
    
    # Get the project (ID 132 is spikerwork/gitlab-mr-reviewer)
    project = gl.projects.get(132)
    print(f"Using project: {project.path_with_namespace}")
    
    # Get default branch
    default_branch = project.default_branch or "main"
    print(f"Default branch: {default_branch}")
    
    # Create a test branch
    test_branch_name = f"test-mr-{int(time.time())}"
    
    # Get the latest commit from default branch
    commits = project.commits.list(ref_name=default_branch, get_all=False, per_page=1)
    if not commits:
        print("❌ No commits found in default branch")
        exit(1)
    
    latest_commit = commits[0]
    print(f"Latest commit: {latest_commit.id[:8]} - {latest_commit.title}")
    
    # Create new branch from latest commit
    branch = project.branches.create({
        'branch': test_branch_name,
        'ref': latest_commit.id
    })
    print(f"✅ Created branch: {test_branch_name}")
    
    # Create a test file in the new branch
    test_file_content = """# Test File for MR Review

def calculate_sum(a, b):
    # This function has some issues for testing
    result = a + b
    print(result)  # Should not print in a utility function
    return str(result)  # Wrong return type

def risky_function(user_input):
    # Security issue: potential code injection
    eval(user_input)
    
def inefficient_search(items, target):
    # Performance issue: O(n) when could be O(1)
    for i in range(len(items)):
        if items[i] == target:
            return i
    return -1

# TODO: Add error handling
# TODO: Add input validation
"""
    
    # Create file in the test branch
    file_path = "test_code_review.py"
    project.files.create({
        'file_path': file_path,
        'branch': test_branch_name,
        'content': test_file_content,
        'commit_message': 'Add test file with code issues for review'
    })
    print(f"✅ Created test file: {file_path}")
    
    # Create merge request
    mr = project.mergerequests.create({
        'source_branch': test_branch_name,
        'target_branch': default_branch,
        'title': f'Test MR for Automated Review - {test_branch_name}',
        'description': '''This is a test merge request to verify the automated code review system.

The test file contains several intentional issues:
- Wrong return types
- Security vulnerabilities
- Performance problems
- Missing error handling

The Gemini AI reviewer should identify these issues.'''
    })
    
    print(f"✅ Created merge request: !{mr.iid}")
    print(f"   Title: {mr.title}")
    print(f"   URL: {mr.web_url}")
    print(f"\n📝 The webhook should trigger automatically and post a review comment.")
    print(f"   Check the MR page for the automated review results.")
    
except gitlab.exceptions.GitlabError as e:
    print(f"❌ GitLab error: {e}")
except Exception as e:
    print(f"❌ Error: {e}")