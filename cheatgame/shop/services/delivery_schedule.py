from datetime import datetime, timedelta
from typing import List, Optional, Sequence
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from cheatgame.shop.models import DeliverySchedule, DeliveryScheduleType, DeliveryType, DeliveryData
from cheatgame.users.models import Address


class DeliverySlotFullError(Exception):
    pass


class DeliveryDataAlreadyUsedError(Exception):
    pass


SHOP_LOCAL_TIMEZONE = ZoneInfo("Asia/Tehran")


def create_delivery_schedule(*, delivery_schedule: List[DeliverySchedule]) -> QuerySet[DeliverySchedule]:
    return DeliverySchedule.objects.bulk_create(delivery_schedule)


@transaction.atomic
def generate_repair_delivery_schedules(
    *,
    from_date,
    to_date,
    start_time,
    end_time,
    slot_minutes: int,
    capacity: int,
    closed_weekdays: Sequence[int],
) -> dict:
    current_day = from_date
    created_schedules: List[DeliverySchedule] = []
    skipped_duplicate_count = 0
    skipped_closed_day_count = 0
    partial_slot_count = 0

    while current_day <= to_date:
        if current_day.weekday() in closed_weekdays:
            skipped_closed_day_count += 1
            current_day += timedelta(days=1)
            continue

        slot_start = timezone.make_aware(
            datetime.combine(current_day, start_time),
            SHOP_LOCAL_TIMEZONE,
        )
        day_end = timezone.make_aware(
            datetime.combine(current_day, end_time),
            SHOP_LOCAL_TIMEZONE,
        )

        while slot_start < day_end:
            natural_slot_end = slot_start + timedelta(minutes=slot_minutes)
            slot_end = min(natural_slot_end, day_end)
            if slot_end < natural_slot_end:
                partial_slot_count += 1

            has_overlap = DeliverySchedule.objects.filter(
                type=DeliveryScheduleType.ISSUE,
                start__lt=slot_end,
                end__gt=slot_start,
            ).exists()

            if has_overlap:
                skipped_duplicate_count += 1
            else:
                created_schedules.append(
                    DeliverySchedule(
                        type=DeliveryScheduleType.ISSUE,
                        start=slot_start,
                        end=slot_end,
                        capacity=capacity,
                    )
                )

            slot_start = slot_end

        current_day += timedelta(days=1)

    created_schedules = DeliverySchedule.objects.bulk_create(created_schedules)
    return {
        "created_schedules": created_schedules,
        "created_count": len(created_schedules),
        "skipped_duplicate_count": skipped_duplicate_count,
        "skipped_closed_day_count": skipped_closed_day_count,
        "partial_slot_count": partial_slot_count,
    }


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
def create_schedule_data(*, type: DeliveryType, schedule: Optional[DeliverySchedule], address: Address) -> DeliveryData:
    return DeliveryData.objects.create(type=type, schedule=schedule, address=address)
