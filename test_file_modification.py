#!/usr/bin/env python3
"""
Test file modification to verify enhanced review context
"""

import os
import gitlab
import time
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

gitlab_url = os.getenv("GITLAB_URL")
gitlab_token = os.getenv("GITLAB_TOKEN")

print(f"Testing file modification on {gitlab_url}...")

try:
    gl = gitlab.Gitlab(gitlab_url, private_token=gitlab_token, timeout=30)
    gl.auth()
    print("✅ Connected to GitLab")

    project = gl.projects.get(132)
    print(f"✅ Using project: {project.path_with_namespace}")

    # Create a test branch
    test_branch = f"test-modification-{int(time.time())}"

    # Get latest commit
    commits = project.commits.list(ref_name="master", per_page=1, get_all=False)
    latest_commit = commits[0]

    # Create branch
    branch = project.branches.create({"branch": test_branch, "ref": latest_commit.id})
    print(f"✅ Created branch: {test_branch}")

    # First, create an initial file with original content
    original_content = """# Configuration Manager
import os
import json

class ConfigManager:
    def __init__(self, config_path="config.json"):
        self.config_path = config_path
        self.config = {}
        self.load_config()
    
    def load_config(self):
        try:
            with open(self.config_path, 'r') as f:
                self.config = json.load(f)
        except FileNotFoundError:
            print("Config file not found, using defaults")
            self.config = self.get_default_config()
    
    def get_default_config(self):
        return {
            "database_host": "localhost",
            "database_port": 5432,
            "debug": False,
            "max_connections": 10
        }
    
    def get(self, key, default=None):
        return self.config.get(key, default)
    
    def set(self, key, value):
        self.config[key] = value
    
    def save(self):
        with open(self.config_path, 'w') as f:
            json.dump(self.config, f, indent=2)

# Usage example
if __name__ == "__main__":
    manager = ConfigManager()
    print(f"Database host: {manager.get('database_host')}")
"""

    file_path = f"config_manager_{test_branch}.py"
    project.files.create(
        {
            "file_path": file_path,
            "branch": test_branch,
            "content": original_content,
            "commit_message": f"Add original config manager - {test_branch}",
        }
    )
    print(f"✅ Created original file: {file_path}")

    # Wait a moment to ensure the file is committed
    time.sleep(2)

    # Now modify the file with issues
    modified_content = """# Configuration Manager - Modified with Issues
import os
import json
import pickle  # SECURITY ISSUE: Using pickle for serialization

class ConfigManager:
    def __init__(self, config_path="config.json"):
        self.config_path = config_path
        self.config = {}
        self.admin_password = "admin123"  # SECURITY: Hardcoded password
        self.load_config()
    
    def load_config(self):
        try:
            with open(self.config_path, 'r') as f:
                self.config = json.load(f)
        except FileNotFoundError:
            print("Config file not found, using defaults")
            self.config = self.get_default_config()
        except Exception as e:
            # BUG: Too broad exception handling
            pass
    
    def get_default_config(self):
        return {
            "database_host": "localhost",
            "database_port": 5432,
            "debug": True,  # ISSUE: Debug enabled by default
            "max_connections": 10,
            "api_key": "sk-1234567890abcdef"  # SECURITY: Hardcoded API key
        }
    
    def get(self, key, default=None):
        return self.config.get(key, default)
    
    def set(self, key, value):
        self.config[key] = value
    
    def save(self):
        with open(self.config_path, 'w') as f:
            json.dump(self.config, f, indent=2)
    
    def execute_command(self, command):
        # SECURITY VULNERABILITY: Command injection
        os.system(command)
    
    def load_from_pickle(self, pickle_data):
        # SECURITY VULNERABILITY: Unsafe deserialization
        return pickle.loads(pickle_data)
    
    def sql_query(self, table, where_clause):
        # SQL INJECTION VULNERABILITY
        query = f"SELECT * FROM {table} WHERE {where_clause}"
        return query

# PERFORMANCE ISSUE: Inefficient loop
def process_large_list(items):
    result = []
    for i in range(len(items)):
        for j in range(len(items)):
            if i != j and items[i] == items[j]:
                result.append(items[i])
    return result

# Usage example
if __name__ == "__main__":
    manager = ConfigManager()
    print(f"Database host: {manager.get('database_host')}")
    # BUG: No error handling for missing keys
    print(f"Missing key: {manager.config['nonexistent_key']}")
"""

    # Update the file
    file = project.files.get(file_path, ref=test_branch)
    file.content = modified_content
    file.save(
        branch=test_branch,
        commit_message=f"Add security vulnerabilities and bugs - {test_branch}",
    )
    print(f"✅ Modified file with issues: {file_path}")

    # Create MR
    mr = project.mergerequests.create(
        {
            "source_branch": test_branch,
            "target_branch": "master",
            "title": f"Enhanced Review Test - File Modification - {test_branch}",
            "description": f"""This MR tests the enhanced review system with file modifications.

## What was changed:
1. **Modified existing file**: `{file_path}`
2. **Added multiple security vulnerabilities**: 
   - Hardcoded credentials
   - Command injection
   - Unsafe deserialization
   - SQL injection
3. **Performance issues**: O(n²) loop
4. **Code quality issues**: Broad exception handling, missing error handling

## Expected AI Review:
The review should include both:
- **Original file content** for context
- **Diff showing changes** 

This tests the enhanced review feature that provides Gemini with full file context.""",
        }
    )

    print(f"✅ Created MR: !{mr.iid}")
    print(f"   URL: {mr.web_url}")
    print("\n📝 This MR tests enhanced file review with:")
    print("   1. Original file content as context")
    print("   2. Diff showing the modifications")
    print("   3. Multiple security and performance issues")

except Exception as e:
    print(f"❌ Error: {e}")
    import traceback

    traceback.print_exc()
