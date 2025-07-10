#!/usr/bin/env python3
"""
Test script to verify webhook functionality by creating test merge requests
"""

import os
import sys
import json
import logging
import time
from datetime import datetime
import gitlab
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def create_test_mr(instance_name: str, gitlab_url: str, gitlab_token: str, project_path: str) -> bool:
    """Create a test merge request in the specified project"""
    try:
        logger.info(f"Creating test MR in {instance_name}: {project_path}")
        
        # Connect to GitLab
        gl = gitlab.Gitlab(gitlab_url, private_token=gitlab_token)
        gl.auth()
        
        # Get project
        project = gl.projects.get(project_path, lazy=False)
        
        # Create a unique branch name
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        branch_name = f"test-webhook-{timestamp}"
        
        # Create a new branch from main/master
        try:
            # Try to get default branch
            default_branch = project.default_branch or 'main'
            logger.info(f"Using default branch: {default_branch}")
            
            # Create branch
            branch = project.branches.create({
                'branch': branch_name,
                'ref': default_branch
            })
            logger.info(f"Created branch: {branch_name}")
            
            # Create a test file
            test_content = f"""# Test Webhook File
Created at: {datetime.now().isoformat()}
Instance: {instance_name}
Project: {project_path}

## Test Code
```python
def test_webhook():
    print("Testing webhook functionality")
    return True
```

This is a test merge request to verify webhook integration.
"""
            
            # Create/update a test file
            file_path = f"test-webhook-{timestamp}.md"
            project.files.create({
                'file_path': file_path,
                'branch': branch_name,
                'content': test_content,
                'commit_message': f'Add test webhook file for {instance_name}'
            })
            logger.info(f"Created test file: {file_path}")
            
            # Create merge request
            mr_data = {
                'source_branch': branch_name,
                'target_branch': default_branch,
                'title': f'Test Webhook Integration - {instance_name} - {timestamp}',
                'description': f"""## Test Merge Request

This MR tests the webhook integration for {instance_name}.

### What's being tested:
- Webhook triggering on MR creation
- Gemini AI code review
- Telegram notifications
- Multi-instance support

### Expected behavior:
1. Webhook should trigger when this MR is created
2. AI code review should be posted as a comment
3. Telegram notification should be sent
4. Instance information should be included

Created at: {datetime.now().isoformat()}
Instance: {instance_name}
Project: {project_path}
"""
            }
            
            mr = project.mergerequests.create(mr_data)
            logger.info(f"✅ Created MR !{mr.iid}: {mr.title}")
            logger.info(f"   URL: {mr.web_url}")
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to create MR for {project_path}: {e}")
            return False
            
    except Exception as e:
        logger.error(f"Failed to connect to {instance_name}: {e}")
        return False

def main():
    """Main function"""
    logger.info("🚀 Testing Webhook Integration")
    
    test_repos = [
        {
            "instance_name": "primary",
            "gitlab_url": os.getenv("GITLAB_URL"),
            "gitlab_token": os.getenv("GITLAB_TOKEN"),
            "project_path": "spikerwork/test-repo"
        },
        {
            "instance_name": "instance_2",
            "gitlab_url": os.getenv("GITLAB_URL_2"),
            "gitlab_token": os.getenv("GITLAB_TOKEN_2"),
            "project_path": "gitlab-instance-0d55f60d/max-test"
        }
    ]
    
    results = []
    
    for repo in test_repos:
        if all(repo.values()):
            success = create_test_mr(
                repo["instance_name"],
                repo["gitlab_url"],
                repo["gitlab_token"],
                repo["project_path"]
            )
            results.append({
                "instance": repo["instance_name"],
                "project": repo["project_path"],
                "success": success
            })
            
            # Small delay between requests
            time.sleep(2)
        else:
            logger.warning(f"Skipping {repo['instance_name']} - missing configuration")
    
    # Summary
    logger.info("\n📊 TEST RESULTS:")
    logger.info("=" * 50)
    
    for result in results:
        status = "✅ SUCCESS" if result["success"] else "❌ FAILED"
        logger.info(f"{result['instance']} ({result['project']}): {status}")
    
    successful_tests = sum(1 for r in results if r["success"])
    total_tests = len(results)
    
    logger.info(f"\nTotal: {successful_tests}/{total_tests} tests passed")
    
    if successful_tests == total_tests:
        logger.info("🎉 All webhook tests created successfully!")
        logger.info("👀 Check your GitLab projects and Telegram for notifications")
        return 0
    else:
        logger.error("❌ Some tests failed")
        return 1

if __name__ == "__main__":
    sys.exit(main())