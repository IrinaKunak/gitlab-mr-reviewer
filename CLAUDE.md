# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a GitLab Merge Request Reviewer service - a FastAPI-based webhook receiver that performs automated code quality checks on GitLab merge requests using Gemini AI.

## Architecture

The project consists of:
- **w-server.py**: FastAPI webhook server (incomplete) that receives GitLab webhook events and triggers code quality analysis
- **gemini-wrapper-reference.sh**: Reference implementation for wrapping Gemini CLI with caching and rate limiting
- **gemini-wrapper.sh**: Empty file, intended for actual implementation

## Development Commands

### Install Dependencies
```bash
pip install -r requirements.txt
```

### Run the Server
```bash
uvicorn w-server:app --reload
```

### Environment Configuration
The application expects a `.env` file with:
- `GITLAB_URL`: GitLab instance URL
- `GITLAB_TOKEN`: GitLab private token for API access

## Key Implementation Notes

1. **Webhook Processing**: The server needs to implement:
   - `parse_webhook()` function to parse GitLab merge request events (see https://docs.gitlab.com/user/project/integrations/webhook_events/#merge-request-events)
   - `analyze_code_quality()` function to integrate with gemini-wrapper for code analysis
   - Proper request handling with FastAPI's Request import

2. **Missing Imports**: The main server file is missing:
   - `from fastapi import Request`
   - Environment variable loading for `GITLAB_URL` and `GITLAB_TOKEN`

3. **Gemini Integration**: The gemini-wrapper-reference.sh provides a complete implementation pattern for:
   - Caching responses to avoid duplicate API calls
   - Rate limiting to respect API quotas
   - File processing with size and count limits
   - Error handling and logging

## Current State

The project is in early development with:
- Skeleton FastAPI server with undefined functions
- Reference implementation for Gemini wrapper
- No tests, CI/CD, or deployment configuration