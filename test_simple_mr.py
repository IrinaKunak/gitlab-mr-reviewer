#!/usr/bin/env python3
"""
Simple test to create a MR on the primary instance
"""

import os
import gitlab
import time
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

gitlab_url = os.getenv("GITLAB_URL")
gitlab_token = os.getenv("GITLAB_TOKEN")

print(f"Connecting to {gitlab_url}...")

try:
    # Initialize GitLab client with explicit timeout
    gl = gitlab.Gitlab(gitlab_url, private_token=gitlab_token, timeout=30)
    gl.auth()
    print("✅ Connected to GitLab")

    # Get project 132
    project = gl.projects.get(132)
    print(f"✅ Found project: {project.path_with_namespace}")

    # Create a simple test branch and MR
    test_branch = f"test-simple-{int(time.time())}"

    # Get latest commit
    commits = project.commits.list(ref_name="master", per_page=1)
    if commits:
        latest_commit = commits[0]
        print(f"✅ Latest commit: {latest_commit.id[:8]}")

        # Create branch
        branch = project.branches.create(
            {"branch": test_branch, "ref": latest_commit.id}
        )
        print(f"✅ Created branch: {test_branch}")

        # Create a simple file
        project.files.create(
            {
                "file_path": "test_simple.py",
                "branch": test_branch,
                "content": '# Test file\nprint("Hello")\n',
                "commit_message": "Add test file",
            }
        )
        print("✅ Created test file")

        # Create MR
        mr = project.mergerequests.create(
            {
                "source_branch": test_branch,
                "target_branch": "master",
                "title": f"Simple Test MR - {test_branch}",
            }
        )
        print(f"✅ Created MR: !{mr.iid}")
        print(f"   URL: {mr.web_url}")

except Exception as e:
    print(f"❌ Error: {e}")
    import traceback

    traceback.print_exc()
