import logging
from urllib.parse import urljoin

import requests
from botocore import exceptions as botocore_exception
from celery import Task
from django.conf import settings

from cl.celery_init import app
from cl.users.models import STATUS_TYPES, FailedEmail

logger = logging.getLogger(__name__)


def abort_or_retry(task, exc):
    """Abort a task if we've run out of retries. Else, retry it."""
    if task.request.retries == task.max_retries:
        return
    else:
        raise task.retry(exc=exc)


@app.task(
    bind=True, max_retries=5, interval_start=5 * 60, interval_step=5 * 60
)
def update_moosend_subscription(self: Task, email: str, action: str) -> None:
    """Subscribe or unsubscribe email address to moosend mailing list.

    Perform update if email address is already registered.

    :param self: The celery task
    :param email: The user's email address
    :param action: Action to perfom on moosend
    :return: None
    """
    allowed_actions = ["subscribe", "unsubscribe"]
    assert action in allowed_actions, f"'{action}' is not an allowed action."
    params = {"apikey": settings.MOOSEND_API_KEY}

    if action == "subscribe":
        path = f"/v3/subscribers/{settings.MOOSEND_DEFAULT_LIST_ID}/subscribe.json"
    else:
        path = f"/v3/subscribers/{settings.MOOSEND_DEFAULT_LIST_ID}/unsubscribe.json"

    try:
        r = requests.post(
            url=urljoin(settings.MOOSEND_API_URL, path),
            params=params,
            json={
                "Email": email,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        abort_or_retry(self, exc)
        return

    j = r.json()
    code = j.get("Code")

    if code == 0:
        logger.info(
            "Successfully completed '%s' action on '%s' in moosend.",
            action,
            email,
        )
    else:
        error = j.get("Error", "Unknown error")
        logger.warning(
            "Did not complete '%s' action on '%s' in moosend: '%s'",
            action,
            email,
            error,
        )


@app.task(bind=True, max_retries=3, interval_start=5 * 60)
def send_failed_email(
    self: Task,
    failed_pk: int,
) -> int:
    """Task to retry failed email messages"""

    failed_email = FailedEmail.objects.get(pk=failed_pk)
    if failed_email.status != STATUS_TYPES.SUCCESSFUL:
        # Only execute this task if it has not been previously processed.
        failed_email.status = STATUS_TYPES.IN_PROGRESS
        failed_email.save()
        # Compose email from stored message.
        email = failed_email.stored_email.convert_to_email_multipart()
        try:
            email.send()
        except (
            botocore_exception.HTTPClientError,
            botocore_exception.ConnectionError,
        ) as exc:
            # In case of error when sending e.g: SES downtime, retry the task.
            raise self.retry(exc=exc)
        failed_email.status = STATUS_TYPES.SUCCESSFUL
        failed_email.save()
