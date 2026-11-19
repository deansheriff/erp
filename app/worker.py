"""Worker entry point. Run with: celery -A app.worker worker (and celery -A app.worker beat)"""

from app.core.worker import celery_app

# Task modules register their tasks and beat schedules when imported. Celery's
# autodiscovery only looks for app.tasks, so each package's tasks are listed here.
from app.files import tasks as files_tasks  # noqa: F401
from app.modules.hr import tasks as hr_tasks  # noqa: F401
from app.wiring import configure_runtime

configure_runtime()

__all__ = ["celery_app"]
