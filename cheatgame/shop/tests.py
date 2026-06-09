from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from cheatgame.product.models import DeliveryOption
from cheatgame.shop.models import DeliveryData, DeliverySchedule, DeliveryScheduleType, DeliverySide, DeliveryType, Order
from cheatgame.users.models import Address, BaseUser


class CheckoutSchedulingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = BaseUser.objects.create_user(
            phone_number="09122222222",
            firstname="Checkout",
            lastname="User",
            password="StrongPass123!",
        )
        self.client.force_authenticate(self.user)
        self.address = Address.objects.create(
            user=self.user,
            province="Tehran",
            city="Tehran",
            postal_code="1234567890",
            address_detail="Checkout test address",
        )
        self.delivery_type = DeliveryType.objects.create(
            name="Courier",
            delivery_type=DeliveryOption.MOTOR,
            side=DeliverySide.SENDTOUSER,
        )
        start = timezone.now() + timedelta(days=5)
        self.schedule = DeliverySchedule.objects.create(
            type=DeliveryScheduleType.ORDER,
            start=start,
            end=start + timedelta(hours=2),
            capacity=2,
        )

    def create_order(self, **kwargs):
        return Order.objects.create(
            user=self.user,
            total_price=Decimal("1000"),
            total_price_discount=Decimal("1000"),
            **kwargs,
        )

    def test_book_time_reuses_existing_unassigned_delivery_data(self):
        delivery_data = DeliveryData.objects.create(
            type=self.delivery_type,
            schedule=self.schedule,
            address=self.address,
        )

        response = self.client.post(
            "/api/shop/book-time/",
            {"type": self.delivery_type.id, "schedule": self.schedule.id, "address": self.address.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], delivery_data.id)
        self.assertEqual(DeliveryData.objects.count(), 1)
        self.schedule.refresh_from_db()
        self.assertEqual(self.schedule.capacity, 2)

    def test_book_time_rejects_existing_delivery_data_attached_to_order(self):
        delivery_data = DeliveryData.objects.create(
            type=self.delivery_type,
            schedule=self.schedule,
            address=self.address,
            is_used=False,
        )
        self.create_order(schedule=delivery_data)

        response = self.client.post(
            "/api/shop/book-time/",
            {"type": self.delivery_type.id, "schedule": self.schedule.id, "address": self.address.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(DeliveryData.objects.count(), 1)
        self.schedule.refresh_from_db()
        self.assertEqual(self.schedule.capacity, 2)

    def test_order_detail_update_assigns_schedule_and_marks_it_used(self):
        order = self.create_order()
        delivery_data = DeliveryData.objects.create(
            type=self.delivery_type,
            schedule=self.schedule,
            address=self.address,
        )

        response = self.client.put(
            f"/api/shop/order-detail/{order.id}/",
            {"schedule": delivery_data.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        delivery_data.refresh_from_db()
        self.assertEqual(order.schedule_id, delivery_data.id)
        self.assertTrue(delivery_data.is_used)

    def test_order_detail_rejects_delivery_data_attached_to_another_order(self):
        delivery_data = DeliveryData.objects.create(
            type=self.delivery_type,
            schedule=self.schedule,
            address=self.address,
            is_used=False,
        )
        self.create_order(schedule=delivery_data)
        target_order = self.create_order()

        response = self.client.put(
            f"/api/shop/order-detail/{target_order.id}/",
            {"schedule": delivery_data.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        target_order.refresh_from_db()
        self.assertIsNone(target_order.schedule_id)
