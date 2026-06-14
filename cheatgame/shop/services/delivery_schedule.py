from typing import List

from django.db import transaction
from django.db.models import QuerySet

from cheatgame.shop.models import DeliverySchedule, DeliveryType, DeliveryData
from cheatgame.users.models import Address


class DeliverySlotFullError(Exception):
    pass


class DeliveryDataAlreadyUsedError(Exception):
    pass


def create_delivery_schedule(*, delivery_schedule: List[DeliverySchedule]) -> QuerySet[DeliverySchedule]:
    return DeliverySchedule.objects.bulk_create(delivery_schedule)


def update_delivery_schedule(*, id, type: int, start, end, capacity) -> DeliverySchedule:
    delivery_schedule = DeliverySchedule.objects.get(id=id)
    delivery_schedule.type = type
    delivery_schedule.start = start
    delivery_schedule.end = end
    delivery_schedule.capacity = capacity
    delivery_schedule.save()
    return delivery_schedule


def delete_delivery_schedule(*, delivery_schedule_id: id) -> None:
    DeliverySchedule.objects.get(id=delivery_schedule_id).delete()


def get_reserved_delivery_count(*, schedule: DeliverySchedule) -> int:
    return DeliveryData.objects.filter(schedule=schedule, is_used=True).count()


def is_delivery_schedule_full(*, schedule: DeliverySchedule) -> bool:
    return get_reserved_delivery_count(schedule=schedule) >= schedule.capacity


@transaction.atomic
def reserve_delivery_data(*, delivery_data: DeliveryData) -> DeliveryData:
    schedule = DeliverySchedule.objects.select_for_update().get(id=delivery_data.schedule_id)
    delivery_data = (
        DeliveryData.objects.select_for_update()
        .select_related("schedule", "address", "type")
        .get(id=delivery_data.id)
    )

    if delivery_data.is_used:
        raise DeliveryDataAlreadyUsedError()

    if is_delivery_schedule_full(schedule=schedule):
        raise DeliverySlotFullError()

    delivery_data.is_used = True
    delivery_data.save(update_fields=["is_used", "updated_at"])
    return delivery_data


@transaction.atomic
def create_schedule_data(*, type: DeliveryType, schedule: DeliverySchedule, address: Address) -> DeliveryData:
    return DeliveryData.objects.create(type=type, schedule=schedule, address=address)
