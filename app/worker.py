"""Worker entry point. Run with: celery -A app.worker worker"""

from app.core.worker import celery_app
from app.wiring import configure_runtime

configure_runtime()

__all__ = ["celery_app"]
