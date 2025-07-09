#!/usr/bin/env python3

import os
import gitlab
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

GITLAB_URL = os.getenv("GITLAB_URL")
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN")

try:
    # Initialize GitLab client
    gl = gitlab.Gitlab(GITLAB_URL, private_token=GITLAB_TOKEN)
    gl.auth()
    
    # Get the project (ID 132 is spikerwork/gitlab-mr-reviewer)
    project = gl.projects.get(132)
    print(f"Project: {project.path_with_namespace}")
    
    # List open merge requests
    mrs = project.mergerequests.list(state='opened', get_all=False)
    
    if mrs:
        print(f"\nOpen merge requests:")
        for mr in mrs:
            print(f"  !{mr.iid} - {mr.title}")
            print(f"    Source: {mr.source_branch} -> {mr.target_branch}")
            print(f"    Author: {mr.author['username']}")
            print(f"    URL: {mr.web_url}")
    else:
        print("\nNo open merge requests found.")
        
except Exception as e:
    print(f"Error: {e}")