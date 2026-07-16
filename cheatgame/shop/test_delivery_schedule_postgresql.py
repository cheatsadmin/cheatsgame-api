from datetime import timedelta
from threading import Barrier, Thread
from unittest import skipUnless

from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from cheatgame.product.models import DeliveryOption
from cheatgame.shop.models import (
    DeliveryData,
    DeliverySchedule,
    DeliveryScheduleType,
    DeliverySide,
    DeliveryType,
)
from cheatgame.shop.services.delivery_schedule import (
    DeliverySlotFullError,
    reserve_delivery_data,
)


@skipUnless(connection.vendor == "postgresql", "PostgreSQL row-lock validation only")
class DeliverySchedulePostgreSQLLockingTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.delivery_type = DeliveryType.objects.create(
            name="Repair pickup",
            delivery_type=DeliveryOption.MOTOR,
            side=DeliverySide.RECIEVEFROMUSER,
        )
        start = timezone.now()
        self.schedule = DeliverySchedule.objects.create(
            type=DeliveryScheduleType.ISSUE,
            start=start,
            end=start + timedelta(hours=1),
            capacity=1,
        )

    def create_delivery_data(self):
        return DeliveryData.objects.create(
            type=self.delivery_type,
            schedule=self.schedule,
            address=None,
        )

    def test_nullable_related_rows_lock_only_delivery_data(self):
        delivery_data = self.create_delivery_data()

        with CaptureQueriesContext(connection) as queries:
            reserved = reserve_delivery_data(delivery_data=delivery_data)

        locking_sql = next(
            query["sql"]
            for query in queries.captured_queries
            if 'FROM "shop_deliverydata"' in query["sql"] and "FOR UPDATE" in query["sql"]
        )
        self.assertIn('FOR UPDATE OF "shop_deliverydata"', locking_sql)
        self.assertTrue(reserved.is_used)

    def test_concurrent_reservations_serialize_on_schedule_capacity(self):
        delivery_data_ids = [self.create_delivery_data().id for _ in range(2)]
        barrier = Barrier(2)
        outcomes = []

        def worker(delivery_data_id):
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                reserve_delivery_data(
                    delivery_data=DeliveryData.objects.get(id=delivery_data_id),
                )
            except DeliverySlotFullError:
                outcomes.append("full")
            except Exception as exc:  # Preserve unexpected thread failures for the assertion.
                outcomes.append(f"unexpected:{type(exc).__name__}:{exc}")
            else:
                outcomes.append("reserved")
            finally:
                close_old_connections()

        threads = [
            Thread(target=worker, args=(delivery_data_id,))
            for delivery_data_id in delivery_data_ids
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads), outcomes)
        self.assertCountEqual(outcomes, ["reserved", "full"])
        self.assertEqual(
            DeliveryData.objects.filter(schedule=self.schedule, is_used=True).count(),
            1,
        )
