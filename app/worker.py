"""Worker entry point. Run with: celery -A app.worker worker (and celery -A app.worker beat)"""

from app.core.worker import celery_app

# Task modules register their tasks and beat schedules when imported. Celery's
# autodiscovery only looks for app.tasks, so each package's tasks are listed here.
from app.files import tasks as files_tasks  # noqa: F401
from app.modules.chat import tasks as chat_tasks  # noqa: F401
from app.modules.finance import tasks as finance_tasks  # noqa: F401
from app.modules.fleet import tasks as fleet_tasks  # noqa: F401
from app.modules.hr import tasks as hr_tasks  # noqa: F401
from app.modules.inventory import tasks as inventory_tasks  # noqa: F401
from app.modules.leave import tasks as leave_tasks  # noqa: F401
from app.modules.projects import tasks as projects_tasks  # noqa: F401
from app.modules.support import tasks as support_tasks  # noqa: F401
from app.wiring import configure_runtime

configure_runtime()

__all__ = ["celery_app"]
