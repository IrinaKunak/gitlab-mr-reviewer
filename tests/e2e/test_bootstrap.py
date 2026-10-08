"""Composition root: independent object graphs, nothing built at import."""

from __future__ import annotations

from tests.factories import (
    make_services,
    make_settings,
    review_job,
)


def test_composition_root_builds_independent_graphs(tmp_path):
    # #3: config and singletons were created at import, so a second instance
    # with another configuration was impossible and tests patched globals.
    # Now importing builds nothing, and two graphs coexist without sharing state.
    from reviewer import bootstrap
    assert bootstrap.app.state.services is None  # config loads at startup, not import

    ru = make_services(make_settings(tmp_path / "a", pipeline__language="ru"))
    en = make_services(make_settings(tmp_path / "b", pipeline__language="en"))
    assert ru.review_mr is not en.review_mr and ru.queue is not en.queue
    ru_tg, en_tg = (svc.notifier.channels[0].formatter for svc in (ru, en))
    assert ru_tg.language == "ru" and en_tg.language == "en"
    job = review_job(last_commit="abc")
    ru.review_state.set_last_sha(*job.ref.key, "abc")
    assert en.review_state.get_last_sha(*job.ref.key) is None  # separate state dirs
    assert ru.queue.submit(job) and en.queue.submit(job)       # separate dedupe
