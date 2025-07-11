#!/usr/bin/env python3

import os
import gitlab
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

GITLAB_URL = os.getenv("GITLAB_URL")
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN")

print(f"Testing connection to GitLab at {GITLAB_URL}")

try:
    # Initialize GitLab client
    gl = gitlab.Gitlab(GITLAB_URL, private_token=GITLAB_TOKEN)
    gl.auth()

    print("✅ Successfully authenticated with GitLab")

    # Get current user info
    user = gl.user
    print(f"Authenticated as: {user.username} ({user.name})")

    # List accessible projects
    projects = gl.projects.list(owned=True, get_all=False, per_page=5)
    print("\nAccessible projects (showing first 5):")
    for project in projects:
        print(f"  - {project.path_with_namespace} (ID: {project.id})")

except gitlab.exceptions.GitlabAuthenticationError:
    print("❌ Authentication failed. Check your GITLAB_TOKEN")
except Exception as e:
    print(f"❌ Error: {e}")
