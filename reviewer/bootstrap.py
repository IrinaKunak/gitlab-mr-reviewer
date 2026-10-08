"""Composition root: the only place that reads the environment and wires objects.

    load_config()          .env -> environment -> validated Settings
    configure_logging()    root logger (was a side effect of importing server)
    build_services(cfg)    the object graph, every dependency passed explicitly
    create_app()           FastAPI over it (`app` below is for uvicorn import paths)

Tests call `build_services(cfg, ai=..., telegram=..., ...)` with fakes for any
edge they want to replace — nothing is monkeypatched at module level.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv

from . import __version__, logging_setup, state_layout
from .adapters.gitlab import GitLabVcs
from .adapters.knowledge import ReviewBridge
from .adapters.notify import CompositeNotifier
from .adapters.notify.telegram import TelegramClient, TelegramFormatter, TelegramNotifier
from .adapters.storage import Database
from .adapters.storage.jobs import JobStore
from .adapters.storage.legacy_import import import_legacy
from .ai_client import AIClient
from .application.answer_note import AnswerNote
from .application.jobs import JobRunner
from .application.ports import KnowledgeSource, Notifier, VcsPort
from .application.review_mr import ReviewMergeRequest
from .application.stages import Translator
from .config import (
    Settings,
    deprecated_env_vars_in_use,
    load_settings,
    masked_dump,
    retired_env_vars_in_use,
)
from .dialogue_budget import DialogueBudget
from .domain.events import SystemAlert
from .domain.models import InstanceRef
from .openrouter_models import OpenRouterCatalog
from .overrides import ModelOverrides
from .prompts import Prompts
from .repo_cache import CacheWorkspace, RepoCache
from .review_state import ReviewStateStore
from .server import ReviewQueue
from .server import create_app as _create_app
from .usage import Pricing, UsageLog

logger = logging.getLogger(__name__)

def load_config(dotenv: bool = True) -> Settings:
    """The mounted .env (never overriding real env vars) + config.yaml, validated.
    A bad value raises SystemExit with a message naming the variable."""
    if dotenv:
        load_dotenv()
    return load_settings()


def configure_logging(cfg: Settings) -> None:
    logging_setup.configure(cfg)


@dataclass
class Services:
    """The running service's object graph plus its lifecycle."""
    settings: Settings
    notifier: Notifier
    bridge: KnowledgeSource  # ReviewBridge, or a scripted fake
    ai: Any  # AIClient, or a scripted fake
    review_mr: ReviewMergeRequest
    answer_note: AnswerNote
    queue: ReviewQueue
    overrides: ModelOverrides
    catalog: OpenRouterCatalog
    pricing: Pricing
    usage_log: UsageLog
    review_state: ReviewStateStore
    db: Database
    # instance -> its VCS client; the startup check learns each one's bot username,
    # so note webhooks from the bot itself are dropped at the door
    vcs_for: Callable[[InstanceRef], VcsPort]
    _verify_task: asyncio.Task | None = None

    async def start(self) -> None:
        cfg = self.settings
        logger.info("GitLab MR Reviewer v%s starting", __version__)
        logger.info("Instances: %s", [i.name for i in cfg.gitlab.routes.values()])
        stages = cfg.pipeline.stages
        logger.info("Flags: investigator=%s bridge=%s tester_report=%s "
                    "review_repo_tools=%s dialogue=%s provider=%s",
                    stages.investigator, cfg.bridge.enabled, stages.tester_report,
                    stages.review_repo_tools, stages.dialogue, cfg.llm.provider)
        if cfg.llm.provider == "openrouter" and not cfg.llm.openrouter.token:
            logger.error("AI_PROVIDER=openrouter but OPENROUTER_API_TOKEN is empty")
        for message in retired_env_vars_in_use() + deprecated_env_vars_in_use(cfg):
            logger.warning("%s", message)
        if cfg.network.proxy_url:
            logger.info("Proxy: %s", cfg.network.proxy_url)
        state_layout.migrate(cfg)  # before anything reads overrides/review state
        import_legacy(self.db, cfg.storage.state_dir, cfg.storage.log_dir)
        await self.queue.start()
        await self.bridge.start()
        self._verify_task = asyncio.create_task(self.verify_instances())

    async def stop(self) -> None:
        if self._verify_task is not None:
            self._verify_task.cancel()
        await self.bridge.stop()
        await self.queue.stop()
        self.db.close()

    async def verify_instances(self) -> None:
        """Startup connectivity check (non-fatal, v1 behavior): the one
        authentication per instance; the pipeline retries it lazily on failure."""
        for instance in self.settings.gitlab.routes.values():
            try:
                bot = await self.vcs_for(instance).connect()
                logger.info("GitLab instance OK: %s (%s), bot=%s", instance.name,
                            instance.url, bot or "?")
            except Exception as exc:  # noqa: BLE001
                logger.error("GitLab instance %s connection failed: %s", instance.name, exc)
                await self.notifier.notify(SystemAlert(
                    "gitlab_api_error", f"Startup connection failed: {exc}",
                    instance=instance.name))


def _telegram_notifier(cfg: Settings, transport: TelegramClient | None) -> Notifier:
    tg = cfg.notify.telegram
    return TelegramNotifier(
        tg, transport or TelegramClient(tg.token, proxy_url=cfg.network.proxy_url),
        TelegramFormatter(cfg.pipeline.language),
        # the bridge chat gets the tester report from the KnowledgeSource
        exclude_document_chats=(cfg.bridge.chat_id,) if cfg.bridge.chat_id else ())


# notification channel registry: notify.channels -> constructor(cfg, telegram transport)
NOTIFIERS: dict[str, Callable[[Settings, TelegramClient | None], Notifier]] = {
    "telegram": _telegram_notifier,
}


def build_notifier(cfg: Settings, telegram: TelegramClient | None = None) -> Notifier:
    channels = []
    for name in cfg.notify.channels:
        if name not in NOTIFIERS:
            raise SystemExit(f"notification channel {name!r} is not implemented yet "
                             f"(NOTIFY_CHANNELS / notify.channels); available: "
                             f"{sorted(NOTIFIERS)}")
        channels.append(NOTIFIERS[name](cfg, telegram))
    return CompositeNotifier(channels)


def build_services(cfg: Settings, *, telegram: TelegramClient | None = None,
                   notifier: Notifier | None = None,
                   ai: Any = None, bridge: Any = None, repo_cache: Any = None,
                   vcs_for: Callable[[InstanceRef], VcsPort] | None = None,
                   catalog: OpenRouterCatalog | None = None,
                   clock: Callable[[], float] = time.monotonic,
                   workers: int | None = None) -> Services:
    """Build the object graph from settings, in dependency order. Keyword
    arguments replace one edge (tests pass fakes; `telegram` is the Telegram
    channel's transport, `notifier` replaces all channels); everything else is real."""
    state_dir = cfg.storage.state_dir
    notifier = notifier or build_notifier(cfg, telegram)
    lang = cfg.pipeline.language

    async def alert(kind: str, details: str) -> None:
        await notifier.notify(SystemAlert(kind, details, language=lang))

    catalog = catalog or OpenRouterCatalog(state_dir, proxy_url=cfg.network.proxy_url)
    pricing = Pricing(cfg.llm.price_table(), catalog)
    db = Database.in_dir(state_dir)
    overrides = ModelOverrides(db, cfg)
    ai = ai or AIClient(cfg, overrides=overrides, alert=alert)
    # the bridge's own bot (default: the notification bot) — independent of notify.*
    bridge = bridge or ReviewBridge(cfg.bridge, TelegramClient(
        cfg.bridge.bot_token or cfg.notify.telegram.token, proxy_url=cfg.network.proxy_url))
    repo_cache = repo_cache or RepoCache(cfg.repo_cache, git_proxy=cfg.network.git_proxy)
    if vcs_for is None:
        clients: dict[str, VcsPort] = {
            i.name: GitLabVcs(i, proxies=cfg.network.requests_proxies)
            for i in cfg.gitlab.routes.values()}
        vcs_for = lambda instance: clients[instance.name]  # noqa: E731
    review_state = ReviewStateStore(db)
    usage_log = UsageLog(cfg.storage.log_dir, db)
    workspace = CacheWorkspace(repo_cache)
    templates = Prompts(cfg.pipeline.prompts_dir or None)
    translator = Translator(ai, cfg.pipeline.language, templates)
    review_mr = ReviewMergeRequest(
        cfg, ai=ai, notifier=notifier, knowledge=bridge, workspace=workspace,
        review_state=review_state, usage_log=usage_log, vcs=vcs_for,
        translator=translator, pricing=pricing, templates=templates)
    answer_note = AnswerNote(
        cfg, ai=ai, workspace=workspace, usage_log=usage_log, vcs=vcs_for,
        translator=translator, pricing=pricing, templates=templates,
        budget=DialogueBudget(db, lambda: cfg.pipeline.dialogue_max_replies_per_mr))
    instances = {i.name: i for i in cfg.gitlab.routes.values()}
    queue = ReviewQueue(cfg.server.workers if workers is None else workers,
                        cfg.dedupe.ttl, cfg.dedupe.burst_seconds,
                        runner=JobRunner(review_mr, answer_note), clock=clock,
                        store=JobStore(db, instances,
                                       max_attempts=cfg.server.job_max_attempts),
                        shutdown_timeout=cfg.server.shutdown_timeout)
    return Services(settings=cfg, notifier=notifier, bridge=bridge, ai=ai,
                    review_mr=review_mr, answer_note=answer_note,
                    queue=queue, overrides=overrides, catalog=catalog, pricing=pricing,
                    usage_log=usage_log, review_state=review_state, db=db, vcs_for=vcs_for)


def _services_from_env() -> Services:
    cfg = load_config()
    configure_logging(cfg)
    return build_services(cfg)


def create_app(cfg: Settings | None = None):
    """FastAPI app. With `cfg` the graph is built now; without it the lifespan
    loads the config at startup (import paths like `uvicorn w-server:app`)."""
    if cfg is None:
        return _create_app(factory=_services_from_env)
    configure_logging(cfg)
    return _create_app(build_services(cfg))


# for `uvicorn reviewer.bootstrap:app` / the w-server.py shim: building it reads
# nothing — the config is loaded when the app starts
app = create_app()


def main_print_config() -> None:
    """`python -m reviewer.config`: effective config (secrets masked) or the errors."""
    cfg = load_config()
    print(json.dumps(masked_dump(cfg), indent=2, ensure_ascii=False))
    for message in retired_env_vars_in_use() + deprecated_env_vars_in_use(cfg):
        print("WARNING:", message)
