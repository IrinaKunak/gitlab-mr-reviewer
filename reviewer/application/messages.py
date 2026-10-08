"""User-facing MR note texts (ru/en)."""

from __future__ import annotations

CONFLICT_SKIP_MSG = {
    "en": "⚠️ Merge request has conflicts. Code review skipped until conflicts are resolved.",
    "ru": "⚠️ Запрос на слияние имеет конфликты. Обзор кода пропущен до разрешения конфликтов.",
}
INITIAL_MSG = {
    "en": "🤖 Starting automated code review...",
    "ru": "🤖 Начинаем автоматический обзор кода...",
}
INITIAL_MSG_CONFLICT = {
    "en": "⚠️ 🤖 Starting automated code review (conflicts detected)...",
    "ru": "⚠️ 🤖 Начинаем автоматический обзор кода (обнаружены конфликты)...",
}
NO_CHANGES_MSG = {
    "en": "⚠️ No code changes found to review.",
    "ru": "⚠️ Не найдено изменений кода для обзора.",
}
TOO_LARGE_MSG = {
    "en": "⚠️ The merge request is too large to analyze. Please break it into smaller changes.",
    "ru": "⚠️ Запрос на слияние слишком большой для анализа. Пожалуйста, разбейте его на меньшие изменения.",
}
TIMEOUT_MSG = {
    "en": "⏱️ Code review timed out. The changes might be too large to analyze.",
    "ru": "⏱️ Тайм-аут обзора кода. Возможно, изменения слишком большие для анализа.",
}
# MR comments are visible to every project member: error paths post only a
# neutral line with the job id — exception text (internal URLs, provider
# errors, disk paths) goes to the log and the internal Telegram alert only
GENERAL_ERROR_MSG = {
    "en": "❌ Code review was not completed, job id: {job_id}",
    "ru": "❌ Ревью не выполнено, id задачи: {job_id}",
}
POST_FAILED_MSG = {
    "en": "❌ Failed to post the review comment, job id: {job_id}",
    "ru": "❌ Не удалось опубликовать комментарий с обзором, id задачи: {job_id}",
}
TESTER_REPORT_COMMENT = {
    "en": "## 🧪 Tester Report\n\nA verification guide for this MR is attached: {link}",
    "ru": "## 🧪 Отчёт для тестировщика\n\nИнструкция по проверке этого MR во вложении: {link}",
}


def msg(table: dict[str, str], lang: str, **kwargs: object) -> str:
    template = table.get(lang, table["en"])
    return template.format(**kwargs) if kwargs else template
