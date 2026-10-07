#!/usr/bin/env python3
"""
Create test merge requests for multiple GitLab instances with file modifications
"""

import os
import gitlab
import time
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


def create_test_mr(instance_number=None):
    """Create a test MR for a specific GitLab instance"""

    # Get configuration for the instance
    if instance_number is None:
        gitlab_url = os.getenv("GITLAB_URL")
        gitlab_token = os.getenv("GITLAB_TOKEN")
        instance_name = "primary"
        project_id = 132  # spikerwork/gitlab-mr-reviewer on lab.smysl.pro
    else:
        gitlab_url = os.getenv(f"GITLAB_URL_{instance_number}")
        gitlab_token = os.getenv(f"GITLAB_TOKEN_{instance_number}")
        instance_name = f"instance_{instance_number}"
        # For lab.catzwolf.ru, we'll need to find the project ID
        project_id = None

    if not gitlab_url or not gitlab_token:
        print(f"❌ Configuration missing for {instance_name}")
        return False

    print(f"\n🔧 Creating test MR on {instance_name} ({gitlab_url})")

    try:
        # Initialize GitLab client
        gl = gitlab.Gitlab(gitlab_url, private_token=gitlab_token)
        gl.auth()
        print("✅ Connected to GitLab")

        # If project_id is not set, try to find the project
        if project_id is None:
            # Search for the gitlab-mr-reviewer project
            projects = gl.projects.list(search="gitlab-mr-reviewer")
            if projects:
                project = projects[0]
                project_id = project.id
                print(
                    f"✅ Found project: {project.path_with_namespace} (ID: {project_id})"
                )
            else:
                print("❌ Could not find gitlab-mr-reviewer project")
                return False
        else:
            project = gl.projects.get(project_id)
            print(f"✅ Using project: {project.path_with_namespace}")

        # Get default branch
        default_branch = project.default_branch or "main"
        print(f"   Default branch: {default_branch}")

        # Create a test branch
        test_branch_name = f"test-mr-{int(time.time())}"

        # Get the latest commit from default branch
        commits = project.commits.list(
            ref_name=default_branch, get_all=False, per_page=1
        )
        if not commits:
            print("❌ No commits found in default branch")
            return False

        latest_commit = commits[0]
        print(f"   Latest commit: {latest_commit.id[:8]} - {latest_commit.title}")

        # Create new branch from latest commit
        branch = project.branches.create(
            {"branch": test_branch_name, "ref": latest_commit.id}
        )
        print(f"✅ Created branch: {test_branch_name}")

        # First, create an initial file that we'll modify later
        initial_content = """# Code Review Test File

class Calculator:
    def __init__(self):
        self.result = 0
    
    def add(self, a, b):
        return a + b
    
    def subtract(self, a, b):
        return a - b
    
    def multiply(self, a, b):
        return a * b
    
    def divide(self, a, b):
        if b == 0:
            return None
        return a / b

def process_data(data):
    result = []
    for item in data:
        if item > 0:
            result.append(item * 2)
    return result

# Simple implementation
print("Calculator loaded")
"""

        # Create initial file
        file_path = f"calculator_{instance_name}.py"
        project.files.create(
            {
                "file_path": file_path,
                "branch": test_branch_name,
                "content": initial_content,
                "commit_message": f"Add initial calculator file for {instance_name}",
            }
        )
        print(f"✅ Created initial file: {file_path}")

        # Now modify the file to create a diff
        modified_content = """# Code Review Test File - Modified Version

import os
import sys

class Calculator:
    def __init__(self):
        self.result = 0
        self.history = []  # Track calculation history
    
    def add(self, a, b):
        # Added type checking
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            raise TypeError("Arguments must be numbers")
        result = a + b
        self.history.append(f"add({a}, {b}) = {result}")
        return result
    
    def subtract(self, a, b):
        return a - b
    
    def multiply(self, a, b):
        return a * b
    
    def divide(self, a, b):
        # Improved error handling
        if b == 0:
            raise ValueError("Cannot divide by zero")
        return a / b
    
    def dangerous_eval(self, expression):
        # SECURITY ISSUE: Using eval is dangerous!
        return eval(expression)

def process_data(data):
    # Performance issue: unnecessary list comprehension
    result = []
    for item in data:
        if item > 0:
            result.append(item * 2)
    
    # This could be: return [item * 2 for item in data if item > 0]
    return result

def sql_query(user_input):
    # SQL INJECTION VULNERABILITY
    query = f"SELECT * FROM users WHERE name = '{user_input}'"
    # Should use parameterized queries instead
    return query

# TODO: Add unit tests
# TODO: Add proper logging instead of print
print("Calculator loaded with new features")

# Unused imports (code smell)
if False:
    import json
    import requests
"""

        # Get the file and update it
        file = project.files.get(file_path, ref=test_branch_name)
        file.content = modified_content
        file.save(
            branch=test_branch_name,
            commit_message=f"Update calculator with issues for review - {instance_name}",
        )
        print(f"✅ Modified file: {file_path}")

        # Create another new file to test new file detection
        new_file_content = """# Configuration file with issues

API_KEY = "hardcoded-secret-key-12345"  # Security issue: hardcoded secrets
DATABASE_PASSWORD = "admin123"  # Another hardcoded credential

def get_config():
    config = {
        "debug": True,  # Should be False in production
        "api_key": API_KEY,
        "db_pass": DATABASE_PASSWORD
    }
    return config

# Missing error handling
def load_config_file(path):
    with open(path) as f:
        return f.read()
"""

        config_file_path = f"config_{instance_name}.py"
        project.files.create(
            {
                "file_path": config_file_path,
                "branch": test_branch_name,
                "content": new_file_content,
                "commit_message": f"Add configuration file with security issues - {instance_name}",
            }
        )
        print(f"✅ Created new file: {config_file_path}")

        # Create merge request
        mr = project.mergerequests.create(
            {
                "source_branch": test_branch_name,
                "target_branch": default_branch,
                "title": f"Test MR for Multi-Instance Review - {instance_name} - {test_branch_name}",
                "description": f"""This is a test merge request for the multi-instance GitLab reviewer on {instance_name}.

## Changes in this MR:
1. Modified `{file_path}` with various code issues
2. Added `{config_file_path}` with security vulnerabilities

## Expected Issues to be Found:
- **Security Issues**: eval() usage, SQL injection, hardcoded credentials
- **Performance Issues**: Inefficient list processing
- **Code Quality**: Missing error handling, unused imports
- **Best Practices**: Direct print statements, missing tests

The Gemini AI reviewer should identify these issues and provide recommendations.

Instance: **{gitlab_url}**""",
            }
        )

        print(f"✅ Created merge request: !{mr.iid}")
        print(f"   Title: {mr.title}")
        print(f"   URL: {mr.web_url}")
        print("\n📝 The webhook should trigger automatically.")
        print("   Check Telegram and the MR page for review results.")

        return True

    except gitlab.exceptions.GitlabError as e:
        print(f"❌ GitLab error: {e}")
        return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False


def main():
    """Create test MRs on all configured instances"""
    print("🚀 Multi-Instance GitLab MR Test Creator")
    print("=" * 50)

    success_count = 0

    # Test primary instance
    if create_test_mr():
        success_count += 1

    # Test additional instances
    for i in range(2, 11):
        if os.getenv(f"GITLAB_URL_{i}"):
            if create_test_mr(i):
                success_count += 1

    print("\n" + "=" * 50)
    print(f"✨ Created {success_count} test merge request(s)")
    print("\nNext steps:")
    print("1. Check your Telegram for notifications")
    print("2. Wait for the AI reviews to be posted")
    print("3. Check the MR pages for detailed feedback")


if __name__ == "__main__":
    main()
