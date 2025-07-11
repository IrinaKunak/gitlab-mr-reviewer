#!/usr/bin/env python3
"""
Test MR creation on instance 2 (lab.catzwolf.ru)
"""

import os
import gitlab
import time
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

gitlab_url = os.getenv("GITLAB_URL_2")
gitlab_token = os.getenv("GITLAB_TOKEN_2")

print(f"Connecting to {gitlab_url}...")

try:
    # Initialize GitLab client with proxy
    gl = gitlab.Gitlab(gitlab_url, private_token=gitlab_token, timeout=30)
    gl.auth()
    print("✅ Connected to GitLab instance 2")

    # Search for the gitlab-mr-reviewer project
    projects = gl.projects.list(search="gitlab-mr-reviewer", get_all=False)
    if not projects:
        print("❌ No gitlab-mr-reviewer project found")
        # List all projects to help find it
        print("\nListing available projects:")
        all_projects = gl.projects.list(per_page=20, get_all=False)
        for p in all_projects:
            print(f"  - {p.path_with_namespace} (ID: {p.id})")
    else:
        project = projects[0]
        print(f"✅ Found project: {project.path_with_namespace} (ID: {project.id})")

        # Create a test branch and MR
        test_branch = f"test-instance2-{int(time.time())}"

        # Get default branch
        default_branch = project.default_branch or "main"
        print(f"   Default branch: {default_branch}")

        # Get latest commit
        commits = project.commits.list(
            ref_name=default_branch, per_page=1, get_all=False
        )
        if commits:
            latest_commit = commits[0]
            print(f"✅ Latest commit: {latest_commit.id[:8]}")

            # Create branch
            branch = project.branches.create(
                {"branch": test_branch, "ref": latest_commit.id}
            )
            print(f"✅ Created branch: {test_branch}")

            # Create a test file with issues
            test_content = """# Test File for Instance 2

def insecure_login(username, password):
    # SECURITY ISSUE: Hardcoded credentials
    if username == "admin" and password == "admin123":
        return True
    
    # SECURITY ISSUE: SQL injection vulnerability
    query = f"SELECT * FROM users WHERE username='{username}' AND password='{password}'"
    # execute_query(query)  # Vulnerable!
    
    return False

def inefficient_sort(items):
    # PERFORMANCE ISSUE: Bubble sort O(n²)
    n = len(items)
    for i in range(n):
        for j in range(0, n-i-1):
            if items[j] > items[j+1]:
                items[j], items[j+1] = items[j+1], items[j]
    return items

# BUG: Division by zero not handled
def calculate_average(numbers):
    return sum(numbers) / len(numbers)
"""

            project.files.create(
                {
                    "file_path": f"security_test_{test_branch}.py",
                    "branch": test_branch,
                    "content": test_content,
                    "commit_message": "Add test file with security issues",
                }
            )
            print("✅ Created test file with issues")

            # Create MR
            mr = project.mergerequests.create(
                {
                    "source_branch": test_branch,
                    "target_branch": default_branch,
                    "title": f"Test MR Instance 2 - {test_branch}",
                    "description": "Test MR for multi-instance support on lab.catzwolf.ru",
                }
            )
            print(f"✅ Created MR: !{mr.iid}")
            print(f"   URL: {mr.web_url}")

except Exception as e:
    print(f"❌ Error: {e}")
    import traceback

    traceback.print_exc()
