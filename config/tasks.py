from time import sleep
from celery import shared_task


@shared_task
def notify_customers(message):
    # This legacy task must never echo caller-controlled customer content.
    del message
    sleep(10)
