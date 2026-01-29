"""
Celery configuration for CFDI Verifier.
"""
import os
from celery import Celery
from dotenv import load_dotenv

load_dotenv()

# Redis URL from environment or default
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "cfdi_verifier",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["tasks"]
)

celery_app.conf.update(
    # Limit concurrency to prevent resource exhaustion
    worker_concurrency=3,

    # Prefetch only 1 task per worker (important for long-running tasks)
    worker_prefetch_multiplier=1,

    # Task settings
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,

    # Result expiration (24 hours)
    result_expires=86400,

    # Task time limits
    task_soft_time_limit=120,  # 2 minutes soft limit
    task_time_limit=180,       # 3 minutes hard limit

    # Retry settings
    task_acks_late=True,
    task_reject_on_worker_lost=True,
)
