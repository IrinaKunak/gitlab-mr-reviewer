"""JobContext logging (stage 19): every record of a job carries its id, so two
reviews running in parallel workers can be told apart in one log."""

from __future__ import annotations

import asyncio
import io
import logging

from reviewer import usage
from reviewer.logging_setup import LOG_FORMAT, JobContextFilter, current_job, job_context
from tests.factories import dialogue_job, review_job


def _capture() -> tuple[logging.Logger, io.StringIO, logging.Handler]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(JobContextFilter())
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    log = logging.getLogger("reviewer.test_jobs")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    return log, stream, handler


def test_parallel_jobs_log_with_their_own_ids():
    log, stream, handler = _capture()

    async def work(job, steps):
        with job_context(job):
            for step in range(steps):
                log.info("step %d", step)
                await asyncio.sleep(0)  # interleave with the other job

    async def both():
        await asyncio.gather(
            work(review_job(job_id="aaaa1111", project_path="g/app", mr_iid=7), 3),
            work(dialogue_job(job_id="bbbb2222", project_path="g/lib", mr_iid=9), 3))

    try:
        asyncio.run(both())
        log.info("between jobs")
    finally:
        log.removeHandler(handler)

    lines = stream.getvalue().splitlines()
    a = [line for line in lines if "[aaaa1111 primary g/app!7 review]" in line]
    b = [line for line in lines if "[bbbb2222 primary g/lib!9 dialogue]" in line]
    assert len(a) == 3 and len(b) == 3
    assert lines[0].endswith("step 0") and lines[1].endswith("step 0")  # interleaved
    assert lines[-1].endswith(" - INFO - between jobs")  # outside a job: no label


def test_job_context_carries_the_usage_tracker():
    tracker = usage.UsageTracker()
    assert current_job() is None
    with job_context(review_job(job_id="x"), usage=tracker):
        assert current_job().job_id == "x"
        usage.record(tier="fast", model="claude-haiku-4-5", provider="gateway",
                     input_tokens=3, output_tokens=1)
    assert current_job() is None
    assert tracker.total_input == 3


def test_configure_installs_the_filter(monkeypatch):
    from reviewer.logging_setup import configure
    from tests.factories import make_settings

    root = logging.getLogger()
    handler = logging.StreamHandler(io.StringIO())
    monkeypatch.setattr(root, "handlers", [handler])
    configure(make_settings())
    assert any(isinstance(f, JobContextFilter) for f in handler.filters)
    configure(make_settings())  # idempotent: one filter
    assert sum(isinstance(f, JobContextFilter) for f in handler.filters) == 1
