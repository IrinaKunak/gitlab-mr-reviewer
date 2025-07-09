# FastAPI-based quality service
from fastapi import FastAPI, BackgroundTasks
import gitlab

app = FastAPI()


@app.post("/webhook")
async def handle_gitlab_webhook(request: Request, background_tasks: BackgroundTasks):
    # Read https://docs.gitlab.com/user/project/integrations/webhook_events/#merge-request-events
    # before creating parse_webhook function
    event = parse_webhook(request)

    # Quick response to GitLab
    background_tasks.add_task(process_quality_check, event)
    return {"status": "accepted"}


async def process_quality_check(event):
    # Run quality analysis
    # Use gemini-wrapper for this
    results = await analyze_code_quality(event.project_id, event.ref)

    # Update merge request status
    gl = gitlab.Gitlab(GITLAB_URL, private_token=GITLAB_TOKEN)
    project = gl.projects.get(event.project_id)
    mr = project.mergerequests.get(event.merge_request_iid)

    if results.passed:
        mr.approve()
    else:
        mr.notes.create({'body': f'Quality check failed: {results.summary}'})