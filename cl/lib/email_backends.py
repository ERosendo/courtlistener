import time
from collections.abc import Sequence

from django.conf import settings
from django.core.mail import (
    EmailMessage,
    EmailMultiAlternatives,
    get_connection,
)
from django.core.mail.backends.base import BaseEmailBackend
from redis import Redis

from cl.lib.redis_utils import make_redis_interface
from cl.users.email_handlers import (
    add_bcc_random,
    enqueue_email,
    is_not_email_banned,
    normalize_addresses,
    store_message,
    under_backoff_waiting_period,
)


def incr_email_temp_counter(r: Redis) -> None:
    """increments the temporary counter and adds a new
    element to the sorted set once it reaches the value of
    the EMAILS_TEMP_COUNTER setting.

    Args:
        r (Redis): The Redis DB connection interface.
    """
    temp_counter = r.get("email:temp_counter")

    if temp_counter:
        if int(temp_counter) + 1 >= settings.EMAIL_MAX_TEMP_COUNTER:
            current_time = time.time_ns()
            pipe = r.pipeline()
            pipe.zadd(
                "email:delivery_attempts", {str(current_time): current_time}
            )
            pipe.set("email:temp_counter", 0)
            pipe.execute()
            return

    pipe = r.pipeline()
    pipe.incr("email:temp_counter")
    pipe.execute()
    return


def get_attempts_in_window(r: Redis) -> int:
    """
    Returns the number of elements stored in the set. This method
    check the current time, and shave off any attempts in the sorted set
    that are outside of our window, then check the cardinality of the
    sorted set

    Args:
        r (Redis): The Redis DB connection interface.

    Returns:
        int: number of elements stored in the set
    """
    current_time = time.time_ns()
    trim_time = current_time - (24 * 60 * 60 * 1_000_000_000)
    pipe = r.pipeline()
    # Removes attempts outside the current window
    pipe.zremrangebyscore("email:delivery_attempts", 0, trim_time)
    # Get number of elements in the set
    pipe.zcard("email:delivery_attempts")
    _, size = pipe.execute()
    return int(size)


def get_email_count(r: Redis) -> int:
    """Returns the number of email sent in the last 24 hours.

    Args:
        r (Redis): The Redis DB connection interface.

    Returns:
        int: number of emails sent in the last 24 hours
    """
    temp_counter = r.get("email:temp_counter")
    previous_attempts = get_attempts_in_window(r)
    if not temp_counter:
        return previous_attempts * settings.EMAIL_MAX_TEMP_COUNTER
    return (
        int(temp_counter) + previous_attempts * settings.EMAIL_MAX_TEMP_COUNTER
    )


def check_emergency_brake(r: Redis) -> None:
    """
    Checks the number of emails sent in the last 24 hours, raises an
    exception if we've reached the threshold.

    Args:
        r (Redis): The Redis DB to connect to as a connection interface

    Raises:
        ValueError: if the counter is bigger than the threshold from the settings.
    """
    current_email_count = get_email_count(r)
    if current_email_count >= settings.EMAIL_EMERGENCY_THRESHOLD:
        raise ValueError(
            "Emergency brake engaged to prevent email quota exhaustion"
        )


class EmailBackend(BaseEmailBackend):
    """This is a custom email backend to handle sending an email with some
    extra functions before the email is sent.

    Is neccesary to set the following settings:
    BASE_BACKEND: The base email backend to use to send emails, in our case:
    django_ses.SESBackend, for testing is used:
    django.core.mail.backends.locmem.EmailBackend

    - Verifies if the recipient's email address is not banned before sending
    or storing the message.
    - Stores a message in DB, generates a unique message_id.
    - Verifies if the recipient's email address is under a backoff event
    waiting period.
    - Compose messages according to available content types
    """

    def send_messages(
        self,
        email_messages: Sequence[EmailMessage | EmailMultiAlternatives],
    ) -> int:
        if not email_messages:
            return 0
        # Open a connection to the BASE_BACKEND set in settings (e.g: SES).
        base_backend = settings.BASE_BACKEND
        connection = get_connection(base_backend)
        connection.open()
        r = make_redis_interface("CACHE")
        # check the emergency brake before entering the loop
        check_emergency_brake(r)
        msg_count = 0
        for email_message in email_messages:
            message = email_message.message()
            original_recipients = normalize_addresses(email_message.to)
            recipient_list = []

            # Verify a recipient's email address is banned.
            for email_address in original_recipients:
                if is_not_email_banned(email_address):
                    recipient_list.append(email_address)

            # If all recipients are banned, the message is discarded
            if not recipient_list:
                continue

            email = email_message

            # Verify if email addresses are under a backoff waiting period
            final_recipient_list = []
            backoff_recipient_list = []
            for email_address in recipient_list:
                if under_backoff_waiting_period(email_address):
                    # If an email address is under a waiting period
                    # add to a backoff recipient list to queue the message
                    backoff_recipient_list.append(email_address)
                else:
                    # If an email address is not under a waiting period
                    # add to the final recipients list
                    final_recipient_list.append(email_address)

            # Store message in DB and obtain the unique
            # message_id to add in headers to identify the message
            stored_id = store_message(email_message)

            if backoff_recipient_list:
                # Enqueue message for recipients under a waiting backoff period
                enqueue_email(backoff_recipient_list, stored_id)

            # Add header with unique message_id to identify message
            email.extra_headers["X-CL-ID"] = stored_id
            # Use base backend connection to send the message
            email.connection = connection

            # Call add_bcc_random function to BCC the message based on the
            # EMAIL_BCC_COPY_RATE set
            email = add_bcc_random(email, settings.EMAIL_BCC_COPY_RATE)

            # If we have recipients to send the message to, we send it.
            if final_recipient_list:
                # Update message with the final recipient list
                email.to = final_recipient_list
                # check the emergency brake before sending an email
                check_emergency_brake(r)
                email.send()
                # update the counters
                incr_email_temp_counter(r)
                msg_count += 1

        # Close base backend connection
        connection.close()
        return msg_count
