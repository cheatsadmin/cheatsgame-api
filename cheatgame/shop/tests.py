from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from cheatgame.product.models import DeliveryOption
from cheatgame.product.models import Attachment, AttachmentType, Product, ProductType
from cheatgame.shop.models import (
    Cart,
    CartItem,
    CartItemAttachment,
    DeliveryData,
    DeliverySchedule,
    DeliveryScheduleType,
    DeliverySide,
    DeliveryType,
    Discount,
    DiscountType,
    DiscountValueType,
    Order,
    OrderItem,
    OrderItemAttachment,
    OrderStatus,
    PaymentTransaction,
    PaymentTransactionStatus,
)
from cheatgame.users.models import Address, BaseUser, UserTypes


class MockZarinpalResponse:
    def __init__(self, payload, status_code=200, text=None):
        self.payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload, separators=(",", ":"))

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class CheckoutSchedulingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = BaseUser.objects.create_user(
            phone_number="09122222222",
            firstname="Checkout",
            lastname="User",
            password="StrongPass123!",
        )
        self.user.phone_verified = True
        self.user.save(update_fields=["phone_verified"])
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
        user = kwargs.pop("user", self.user)
        kwargs.setdefault("is_game", True)
        return Order.objects.create(
            user=user,
            total_price=Decimal("1000"),
            total_price_discount=Decimal("1000"),
            **kwargs,
        )

    def create_verified_user(self, phone_number):
        user = BaseUser.objects.create_user(phone_number=phone_number, firstname="Other", lastname="User", password="StrongPass123!")
        user.phone_verified = True
        user.save(update_fields=["phone_verified"])
        return user

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

    def test_book_time_creates_new_delivery_data_when_same_address_slot_has_capacity(self):
        delivery_data = DeliveryData.objects.create(
            type=self.delivery_type,
            schedule=self.schedule,
            address=self.address,
            is_used=True,
        )
        self.create_order(schedule=delivery_data)
        target_order = self.create_order()

        response = self.client.post(
            "/api/shop/book-time/",
            {"type": self.delivery_type.id, "schedule": self.schedule.id, "address": self.address.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotEqual(response.data["id"], delivery_data.id)
        self.assertEqual(DeliveryData.objects.filter(schedule=self.schedule, address=self.address).count(), 2)

        update_response = self.client.put(
            f"/api/shop/order-detail/{target_order.id}/",
            {"schedule": response.data["id"]},
            format="json",
        )

        self.assertEqual(update_response.status_code, status.HTTP_200_OK)
        target_order.refresh_from_db()
        self.assertEqual(target_order.schedule_id, response.data["id"])
        self.assertEqual(DeliveryData.objects.filter(schedule=self.schedule, is_used=True).count(), 1)

    def test_order_detail_update_assigns_schedule_without_consuming_capacity(self):
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
        self.assertFalse(delivery_data.is_used)
        self.assertEqual(DeliveryData.objects.filter(schedule=self.schedule, is_used=True).count(), 0)

    def test_product_order_detail_update_stores_shipping_without_schedule(self):
        order = self.create_order(is_game=False)

        with patch("cheatgame.shop.services.order.is_delivery_schedule_full") as schedule_full:
            response = self.client.put(
                f"/api/shop/order-detail/{order.id}/",
                {
                    "shipping_address": self.address.id,
                    "shipping_method": self.delivery_type.id,
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        schedule_full.assert_not_called()
        order.refresh_from_db()
        self.assertIsNone(order.schedule_id)
        self.assertEqual(order.shipping_address_id, self.address.id)
        self.assertEqual(order.shipping_method_id, self.delivery_type.id)
        self.assertEqual(DeliveryData.objects.count(), 0)

    def test_product_order_detail_update_rejects_delivery_schedule(self):
        order = self.create_order(is_game=False)
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

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("فقط روش ارسال", response.data["error"])
        order.refresh_from_db()
        self.assertIsNone(order.schedule_id)

    def test_order_detail_update_is_idempotent_for_same_schedule(self):
        order = self.create_order()
        delivery_data = DeliveryData.objects.create(
            type=self.delivery_type,
            schedule=self.schedule,
            address=self.address,
        )
        order.schedule = delivery_data
        order.save(update_fields=["schedule"])

        response = self.client.put(
            f"/api/shop/order-detail/{order.id}/",
            {"schedule": delivery_data.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(DeliveryData.objects.filter(schedule=self.schedule, is_used=True).count(), 0)

    def test_order_detail_update_rejects_second_schedule(self):
        order = self.create_order()
        first_delivery_data = DeliveryData.objects.create(
            type=self.delivery_type,
            schedule=self.schedule,
            address=self.address,
            is_used=True,
        )
        order.schedule = first_delivery_data
        order.save(update_fields=["schedule"])
        second_address = Address.objects.create(
            user=self.user,
            province="Tehran",
            city="Tehran",
            postal_code="4234567890",
            address_detail="Second checkout address",
        )
        second_delivery_data = DeliveryData.objects.create(
            type=self.delivery_type,
            schedule=self.schedule,
            address=second_address,
        )

        response = self.client.put(
            f"/api/shop/order-detail/{order.id}/",
            {"schedule": second_delivery_data.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        order.refresh_from_db()
        second_delivery_data.refresh_from_db()
        self.assertEqual(order.schedule_id, first_delivery_data.id)
        self.assertFalse(second_delivery_data.is_used)

    def test_delivery_schedule_list_reports_full_slots(self):
        self.schedule.capacity = 1
        self.schedule.save(update_fields=["capacity"])
        DeliveryData.objects.create(
            type=self.delivery_type,
            schedule=self.schedule,
            address=self.address,
            is_used=True,
        )

        response = self.client.get(
            "/api/shop/delivery-schedule-list/",
            {
                "from_date": self.schedule.start.date().isoformat(),
                "to_date": self.schedule.start.date().isoformat(),
                "type": DeliveryScheduleType.ORDER.value,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["capacity"], 1)
        self.assertEqual(response.data[0]["reserved_count"], 1)
        self.assertEqual(response.data[0]["remaining_capacity"], 0)
        self.assertTrue(response.data[0]["is_full"])

    def test_book_time_rejects_full_schedule(self):
        self.schedule.capacity = 1
        self.schedule.save(update_fields=["capacity"])
        DeliveryData.objects.create(
            type=self.delivery_type,
            schedule=self.schedule,
            address=self.address,
            is_used=True,
        )
        second_address = Address.objects.create(
            user=self.user,
            province="Tehran",
            city="Tehran",
            postal_code="5234567890",
            address_detail="Second checkout address",
        )

        response = self.client.post(
            "/api/shop/book-time/",
            {"type": self.delivery_type.id, "schedule": self.schedule.id, "address": second_address.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(DeliveryData.objects.filter(address=second_address, schedule=self.schedule).exists())

    def test_book_time_rejects_same_address_when_schedule_is_full(self):
        self.schedule.capacity = 1
        self.schedule.save(update_fields=["capacity"])
        DeliveryData.objects.create(
            type=self.delivery_type,
            schedule=self.schedule,
            address=self.address,
            is_used=True,
        )

        response = self.client.post(
            "/api/shop/book-time/",
            {"type": self.delivery_type.id, "schedule": self.schedule.id, "address": self.address.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(DeliveryData.objects.filter(address=self.address, schedule=self.schedule).count(), 1)

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

    def test_book_time_rejects_address_owned_by_another_user_for_any_delivery_side(self):
        other_user = self.create_verified_user("09129990001")
        other_address = Address.objects.create(
            user=other_user,
            province="Tehran",
            city="Tehran",
            postal_code="2234567890",
            address_detail="Other user address",
        )
        issue_delivery_type = DeliveryType.objects.create(
            name="Pickup",
            delivery_type=DeliveryOption.MOTOR,
            side=DeliverySide.RECIEVEFROMUSER,
        )
        start = timezone.now() + timedelta(days=5)
        issue_schedule = DeliverySchedule.objects.create(
            type=DeliveryScheduleType.ISSUE,
            start=start,
            end=start + timedelta(hours=2),
            capacity=2,
        )

        response = self.client.post(
            "/api/shop/book-time/",
            {"type": issue_delivery_type.id, "schedule": issue_schedule.id, "address": other_address.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(DeliveryData.objects.filter(address=other_address, schedule=issue_schedule).exists())

    def test_order_detail_update_rejects_order_owned_by_another_user(self):
        other_user = self.create_verified_user("09129990002")
        other_order = self.create_order(user=other_user)
        delivery_data = DeliveryData.objects.create(
            type=self.delivery_type,
            schedule=self.schedule,
            address=self.address,
        )

        response = self.client.put(
            f"/api/shop/order-detail/{other_order.id}/",
            {"schedule": delivery_data.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        other_order.refresh_from_db()
        delivery_data.refresh_from_db()
        self.assertIsNone(other_order.schedule_id)
        self.assertFalse(delivery_data.is_used)

    def test_order_detail_update_rejects_delivery_data_for_another_users_address(self):
        other_user = self.create_verified_user("09129990003")
        other_address = Address.objects.create(
            user=other_user,
            province="Tehran",
            city="Tehran",
            postal_code="3234567890",
            address_detail="Other user delivery address",
        )
        order = self.create_order()
        delivery_data = DeliveryData.objects.create(
            type=self.delivery_type,
            schedule=self.schedule,
            address=other_address,
        )

        response = self.client.put(
            f"/api/shop/order-detail/{order.id}/",
            {"schedule": delivery_data.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        order.refresh_from_db()
        delivery_data.refresh_from_db()
        self.assertIsNone(order.schedule_id)
        self.assertFalse(delivery_data.is_used)

    def test_customer_order_detail_rejects_order_owned_by_another_user(self):
        other_user = self.create_verified_user("09129990004")
        other_order = self.create_order(user=other_user)

        response = self.client.get(f"/api/shop/get-order-detail/{other_order.id}/")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_customer_order_detail_returns_item_quantity_and_price(self):
        product = Product.objects.create(
            product_type=ProductType.PHYSCIAL,
            title="Detail Product",
            main_image="product/main_images/detail.jpg",
            price=Decimal("1000"),
            off_price=Decimal("900"),
            quantity=5,
            description="product/descriptions/detail.html",
            order_limit=5,
        )
        order = self.create_order(is_game=False)
        OrderItem.objects.create(
            order=order,
            product=product,
            quantity=3,
            price=Decimal("2700"),
        )

        response = self.client.get(f"/api/shop/get-order-detail/{order.id}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["product_data"][0]["quantity"], 3)
        self.assertEqual(response.data["product_data"][0]["price"], "2700")


class RepairScheduleGeneratorTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = BaseUser.objects.create_user(
            phone_number="09127770001",
            firstname="Schedule",
            lastname="Admin",
            password="StrongPass123!",
            user_type=UserTypes.ADMIN,
        )
        self.client.force_authenticate(self.admin)

    def post_generator(self, **payload):
        default_payload = {
            "from_date": "2026-06-18",
            "to_date": "2026-06-18",
            "start_time": "11:30",
            "end_time": "19:00",
            "slot_minutes": 120,
            "capacity": 15,
            "closed_weekdays": [4],
        }
        default_payload.update(payload)
        return self.client.post(
            "/api/shop/repair-delivery-schedule-generator/",
            default_payload,
            format="json",
        )

    def aware_datetime(self, target_date, target_time):
        return timezone.make_aware(
            datetime.combine(target_date, target_time),
            ZoneInfo("Asia/Tehran"),
        )

    def test_generator_creates_repair_slots_and_skips_friday(self):
        response = self.post_generator(
            from_date="2026-06-18",
            to_date="2026-06-19",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["created_count"], 4)
        self.assertEqual(response.data["skipped_duplicate_count"], 0)
        self.assertEqual(response.data["skipped_closed_day_count"], 1)
        self.assertEqual(response.data["partial_slot_count"], 1)
        self.assertFalse(DeliverySchedule.objects.filter(type=DeliveryScheduleType.ORDER).exists())
        self.assertEqual(DeliverySchedule.objects.filter(type=DeliveryScheduleType.ISSUE).count(), 4)
        self.assertEqual(set(DeliverySchedule.objects.values_list("capacity", flat=True)), {15})

    def test_generator_is_idempotent_for_same_repair_slots(self):
        first_response = self.post_generator()
        second_response = self.post_generator()

        self.assertEqual(first_response.status_code, status.HTTP_200_OK, first_response.data)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK, second_response.data)
        self.assertEqual(first_response.data["created_count"], 4)
        self.assertEqual(second_response.data["created_count"], 0)
        self.assertEqual(second_response.data["skipped_duplicate_count"], 4)
        self.assertEqual(DeliverySchedule.objects.filter(type=DeliveryScheduleType.ISSUE).count(), 4)

    def test_generator_skips_overlapping_repair_slots_but_not_product_slots(self):
        target_date = date(2026, 6, 22)
        DeliverySchedule.objects.create(
            type=DeliveryScheduleType.ORDER,
            start=self.aware_datetime(target_date, time(11, 30)),
            end=self.aware_datetime(target_date, time(13, 30)),
            capacity=2,
        )
        DeliverySchedule.objects.create(
            type=DeliveryScheduleType.ISSUE,
            start=self.aware_datetime(target_date, time(13, 30)),
            end=self.aware_datetime(target_date, time(14, 0)),
            capacity=2,
        )

        response = self.post_generator(
            from_date="2026-06-22",
            to_date="2026-06-22",
            start_time="11:30",
            end_time="15:30",
            closed_weekdays=[],
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["created_count"], 1)
        self.assertEqual(response.data["skipped_duplicate_count"], 1)
        self.assertEqual(DeliverySchedule.objects.filter(type=DeliveryScheduleType.ORDER).count(), 1)
        self.assertEqual(DeliverySchedule.objects.filter(type=DeliveryScheduleType.ISSUE).count(), 2)


class CartItemOwnershipTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = self.create_verified_user("09121110001")
        self.other_user = self.create_verified_user("09121110002")
        self.product = Product.objects.create(
            product_type=ProductType.PHYSCIAL,
            title="Ownership Product",
            main_image="product/main_images/test.jpg",
            price=Decimal("1000"),
            off_price=Decimal("900"),
            quantity=5,
            description="product/descriptions/test.html",
            order_limit=5,
        )
        self.other_cart = Cart.objects.create(user=self.other_user)
        self.other_cart_item = CartItem.objects.create(
            cart=self.other_cart,
            product=self.product,
            quantity=1,
            price=Decimal("1000"),
        )
        self.client.force_authenticate(self.user)

    def create_verified_user(self, phone_number):
        user = BaseUser.objects.create_user(phone_number=phone_number, firstname="Cart", lastname="User", password="StrongPass123!")
        user.phone_verified = True
        user.save(update_fields=["phone_verified"])
        return user

    def test_customer_cannot_update_another_users_cart_item(self):
        response = self.client.put(f"/api/shop/udpate-cart-item/{self.other_cart_item.id}/", {"quantity": 2}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.other_cart_item.refresh_from_db()
        self.assertEqual(self.other_cart_item.quantity, 1)

    def test_customer_cannot_delete_another_users_cart_item(self):
        response = self.client.delete(f"/api/shop/udpate-cart-item/{self.other_cart_item.id}/")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(CartItem.objects.filter(id=self.other_cart_item.id).exists())

    def test_cart_item_list_returns_cart_quantity_separate_from_product_stock(self):
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(
            cart=cart,
            product=self.product,
            quantity=2,
            price=Decimal("2000"),
        )

        response = self.client.get("/api/shop/cart-item-list/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["quantity"], 2)
        self.assertEqual(response.data[0]["product"]["quantity"], 5)


class ProductAttachmentWorkflowTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = BaseUser.objects.create_user(
            phone_number="09124440001",
            firstname="Attachment",
            lastname="Customer",
            password="StrongPass123!",
        )
        self.user.phone_verified = True
        self.user.save(update_fields=["phone_verified"])
        self.client.force_authenticate(self.user)
        self.product = self.create_product("Attachment Test Product")
        self.other_product = self.create_product("Other Attachment Product")
        self.free_warranty = Attachment.objects.create(
            product=self.product,
            attachment_type=AttachmentType.GUARANTEE,
            title="گارانتی رایگان",
            price=Decimal("0"),
            is_force_attachment=False,
        )
        self.paid_insurance = Attachment.objects.create(
            product=self.product,
            attachment_type=AttachmentType.INSURANCE,
            title="بیمه ارسال",
            price=Decimal("150"),
            is_force_attachment=False,
        )
        self.other_attachment = Attachment.objects.create(
            product=self.other_product,
            attachment_type=AttachmentType.INSURANCE,
            title="بیمه محصول دیگر",
            price=Decimal("300"),
            is_force_attachment=False,
        )

    def create_product(self, title):
        return Product.objects.create(
            product_type=ProductType.PHYSCIAL,
            title=title,
            main_image="product/main_images/attachment-test.jpg",
            price=Decimal("1000"),
            off_price=Decimal("0"),
            quantity=10,
            description="product/descriptions/attachment-test.html",
            order_limit=5,
        )

    def add_to_cart(self, *, product=None, quantity=1, attachments=None):
        return self.client.post(
            "/api/shop/add-to-cart/",
            {
                "product": (product or self.product).id,
                "quantity": quantity,
                "attachment": [
                    {"attachment": attachment.id} for attachment in (attachments or [])
                ],
            },
            format="json",
        )

    def test_free_attachment_keeps_cart_total_at_product_price(self):
        response = self.add_to_cart(attachments=[self.free_warranty])

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        cart_item = CartItem.objects.get(cart__user=self.user)
        self.assertEqual(cart_item.price, Decimal("1000"))
        self.assertEqual(
            list(CartItemAttachment.objects.filter(cart_item=cart_item).values_list("attachment_id", flat=True)),
            [self.free_warranty.id],
        )

    def test_paid_attachment_is_added_to_cart_total(self):
        response = self.add_to_cart(attachments=[self.paid_insurance])

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        cart_item = CartItem.objects.get(cart__user=self.user)
        self.assertEqual(cart_item.price, Decimal("1150"))

    def test_quantity_two_paid_attachment_total_is_per_unit(self):
        response = self.add_to_cart(quantity=2, attachments=[self.paid_insurance])

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        cart_item = CartItem.objects.get(cart__user=self.user)
        self.assertEqual(cart_item.quantity, 2)
        self.assertEqual(cart_item.price, Decimal("2300"))

        order_response = self.client.post("/api/shop/submit-order/", {}, format="json")

        self.assertEqual(order_response.status_code, status.HTTP_200_OK, order_response.data)
        order = Order.objects.get(user=self.user)
        order_item = OrderItem.objects.get(order=order)
        self.assertEqual(order_item.price, Decimal("2300"))
        self.assertEqual(order.total_price, Decimal("2300"))
        self.assertEqual(order.total_price_discount, Decimal("2300"))

    def test_invalid_attachment_from_another_product_is_rejected(self):
        response = self.add_to_cart(attachments=[self.other_attachment])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("معتبر نیست", response.data["error"])
        self.assertFalse(CartItem.objects.filter(cart__user=self.user).exists())

    def test_warranty_and_insurance_can_be_selected_together(self):
        response = self.add_to_cart(attachments=[self.free_warranty, self.paid_insurance])

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        cart_item = CartItem.objects.get(cart__user=self.user)
        self.assertEqual(cart_item.price, Decimal("1150"))
        self.assertCountEqual(
            CartItemAttachment.objects.filter(cart_item=cart_item).values_list("attachment_id", flat=True),
            [self.free_warranty.id, self.paid_insurance.id],
        )

        order_response = self.client.post("/api/shop/submit-order/", {}, format="json")

        self.assertEqual(order_response.status_code, status.HTTP_200_OK, order_response.data)
        order_item = OrderItem.objects.get(order__user=self.user)
        self.assertCountEqual(
            OrderItemAttachment.objects.filter(order_item=order_item).values_list("attachment_id", flat=True),
            [self.free_warranty.id, self.paid_insurance.id],
        )


@override_settings(PAYMENT_GATEWAY_PROVIDER="fake", PAYMENT_SUCCESS_REDIRECT_URL="http://frontend.test/PaymentSuccess")
class ProductSalePricingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = BaseUser.objects.create_user(
            phone_number="09124440101",
            firstname="Sale",
            lastname="Customer",
            password="StrongPass123!",
        )
        self.user.phone_verified = True
        self.user.save(update_fields=["phone_verified"])
        self.client.force_authenticate(self.user)
        self.insurance_price = Decimal("150")

    def create_product(self, *, price=Decimal("3900"), off_price=Decimal("0"), quantity=10):
        product = Product.objects.create(
            product_type=ProductType.PHYSCIAL,
            title=f"Sale Test Product {Product.objects.count() + 1}",
            main_image="product/main_images/sale-test.jpg",
            price=price,
            off_price=off_price,
            quantity=quantity,
            description="product/descriptions/sale-test.html",
            order_limit=5,
        )
        product.insurance = Attachment.objects.create(
            product=product,
            attachment_type=AttachmentType.INSURANCE,
            title="بیمه تست",
            price=self.insurance_price,
            is_force_attachment=False,
        )
        return product

    def add_to_cart(self, *, product, quantity=1, attachments=None):
        return self.client.post(
            "/api/shop/add-to-cart/",
            {
                "product": product.id,
                "quantity": quantity,
                "attachment": [
                    {"attachment": attachment.id} for attachment in (attachments or [])
                ],
            },
            format="json",
        )

    def submit_order(self):
        response = self.client.post("/api/shop/submit-order/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return Order.objects.get(user=self.user)

    def attach_shipping(self, order):
        address = Address.objects.create(
            user=self.user,
            province="Tehran",
            city="Tehran",
            postal_code=f"{Address.objects.count() + 800:010d}",
            address_detail="Sale pricing shipping address",
        )
        delivery_type = DeliveryType.objects.create(
            name="پیک اختصاصی",
            delivery_type=DeliveryOption.MOTOR,
            side=DeliverySide.SENDTOUSER,
        )
        order.shipping_address = address
        order.shipping_method = delivery_type
        order.save(update_fields=["shipping_address", "shipping_method", "updated_at"])

    def test_no_discount_uses_price(self):
        product = self.create_product(price=Decimal("3900"), off_price=Decimal("0"))

        response = self.add_to_cart(product=product)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        cart_item = CartItem.objects.get(cart__user=self.user)
        self.assertEqual(cart_item.price, Decimal("3900"))

    def test_off_price_zero_uses_price(self):
        product = self.create_product(price=Decimal("3900"), off_price=Decimal("0"))

        self.add_to_cart(product=product)
        order = self.submit_order()

        self.assertEqual(order.total_price, Decimal("3900"))
        self.assertEqual(order.total_price_discount, Decimal("3900"))
        self.assertEqual(order.total_price - order.total_price_discount, Decimal("0"))

    def test_discount_uses_off_price(self):
        product = self.create_product(price=Decimal("3900"), off_price=Decimal("3600"))

        response = self.add_to_cart(product=product)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        cart_item = CartItem.objects.get(cart__user=self.user)
        self.assertEqual(cart_item.price, Decimal("3600"))

        order = self.submit_order()
        order_item = OrderItem.objects.get(order=order)
        self.assertEqual(order_item.price, Decimal("3600"))
        self.assertEqual(order.total_price, Decimal("3900"))
        self.assertEqual(order.total_price_discount, Decimal("3600"))

    def test_discount_plus_paid_attachment_uses_off_price_plus_attachment(self):
        product = self.create_product(price=Decimal("3900"), off_price=Decimal("3600"))

        response = self.add_to_cart(product=product, attachments=[product.insurance])

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        cart_item = CartItem.objects.get(cart__user=self.user)
        self.assertEqual(cart_item.price, Decimal("3750"))

        order = self.submit_order()
        self.assertEqual(order.total_price, Decimal("4050"))
        self.assertEqual(order.total_price_discount, Decimal("3750"))

    def test_quantity_two_discount_attachment_total_and_savings(self):
        product = self.create_product(price=Decimal("3900"), off_price=Decimal("3600"))

        response = self.add_to_cart(product=product, quantity=2, attachments=[product.insurance])

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        cart_item = CartItem.objects.get(cart__user=self.user)
        self.assertEqual(cart_item.price, Decimal("7500"))

        order = self.submit_order()
        order_item = OrderItem.objects.get(order=order)
        self.assertEqual(order_item.price, Decimal("7500"))
        self.assertEqual(order.total_price, Decimal("8100"))
        self.assertEqual(order.total_price_discount, Decimal("7500"))
        self.assertEqual(order.total_price - order.total_price_discount, Decimal("600"))

    def test_payment_amount_equals_final_payable_total(self):
        product = self.create_product(price=Decimal("3900"), off_price=Decimal("3600"), quantity=2)
        response = self.add_to_cart(product=product, quantity=2, attachments=[product.insurance])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order = self.submit_order()
        self.attach_shipping(order)

        payment_response = self.client.post(f"/api/shop/orders/{order.id}/payment/request/", {}, format="json")

        self.assertEqual(payment_response.status_code, status.HTTP_200_OK, payment_response.data)
        transaction_obj = PaymentTransaction.objects.get(order=order)
        self.assertEqual(transaction_obj.amount, Decimal("7500"))
        self.assertEqual(transaction_obj.amount, order.total_price_discount)


@override_settings(PAYMENT_GATEWAY_PROVIDER="fake", PAYMENT_SUCCESS_REDIRECT_URL="http://frontend.test/PaymentSuccess")
class Batch3CheckoutIntegrityTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = self.create_verified_user("09121110011")
        self.other_user = self.create_verified_user("09121110012")
        self.product = Product.objects.create(
            product_type=ProductType.PHYSCIAL,
            title="Batch 3 Product",
            main_image="product/main_images/batch3.jpg",
            price=Decimal("1000"),
            off_price=Decimal("900"),
            quantity=1,
            description="product/descriptions/batch3.html",
            order_limit=5,
        )
        self.client.force_authenticate(self.user)

    def create_verified_user(self, phone_number):
        user = BaseUser.objects.create_user(
            phone_number=phone_number,
            firstname="Batch",
            lastname="User",
            password="StrongPass123!",
        )
        user.phone_verified = True
        user.save(update_fields=["phone_verified"])
        return user

    def create_cart_item(self, user=None, product=None, quantity=1):
        cart = Cart.objects.create(user=user or self.user)
        return CartItem.objects.create(
            cart=cart,
            product=product or self.product,
            quantity=quantity,
            price=Decimal("1000"),
        )

    def create_shipping_address(self, user=None):
        user = user or self.user
        return Address.objects.create(
            user=user,
            province="Tehran",
            city="Tehran",
            postal_code=f"{Address.objects.count() + 100:010d}",
            address_detail="Product checkout shipping address",
        )

    def create_shipping_method(self):
        return DeliveryType.objects.create(
            name="پیک اختصاصی",
            delivery_type=DeliveryOption.MOTOR,
            side=DeliverySide.SENDTOUSER,
        )

    def create_order_with_item(self, product=None, quantity=1, **kwargs):
        product = product or self.product
        with_shipping = kwargs.pop("with_shipping", True)
        if with_shipping and not kwargs.get("is_game", False):
            kwargs.setdefault("shipping_address", self.create_shipping_address())
            kwargs.setdefault("shipping_method", self.create_shipping_method())
        order = Order.objects.create(
            user=self.user,
            total_price=Decimal("1000") * quantity,
            total_price_discount=Decimal("1000") * quantity,
            **kwargs,
        )
        OrderItem.objects.create(
            order=order,
            product=product,
            quantity=quantity,
            price=Decimal("1000") * quantity,
        )
        return order

    def create_coupon(self, **kwargs):
        defaults = {
            "name": "Batch Coupon",
            "code": "BATCH3",
            "type": DiscountType.COUPON.value,
            "value_type": DiscountValueType.AMOUNT.value,
            "valid_from": timezone.now() - timedelta(days=1),
            "valid_until": timezone.now() + timedelta(days=1),
            "is_active": True,
            "min_purchase_amount": Decimal("100"),
            "amount": Decimal("100"),
            "percent": 0,
            "admin_user": self.user,
            "usage_number": 2,
        }
        defaults.update(kwargs)
        return Discount.objects.create(**defaults)

    def create_delivery_data(self, *, schedule=None, user=None, is_used=False, capacity=1):
        user = user or self.user
        address = Address.objects.create(
            user=user,
            province="Tehran",
            city="Tehran",
            postal_code=f"{Address.objects.count() + 1:010d}",
            address_detail="Batch checkout address",
        )
        delivery_type = DeliveryType.objects.create(
            name="Batch Courier",
            delivery_type=DeliveryOption.MOTOR,
            side=DeliverySide.SENDTOUSER,
        )
        if schedule is None:
            start = timezone.now() + timedelta(days=5)
            schedule = DeliverySchedule.objects.create(
                type=DeliveryScheduleType.ORDER,
                start=start,
                end=start + timedelta(hours=2),
                capacity=capacity,
            )
        return DeliveryData.objects.create(
            type=delivery_type,
            schedule=schedule,
            address=address,
            is_used=is_used,
        )

    def request_fake_payment(self, order):
        return self.client.post(f"/api/shop/orders/{order.id}/payment/request/", {}, format="json")

    def callback_fake_payment(self, transaction_obj):
        return self.client.get(
            "/api/payment/callback/fake/",
            {"authority": transaction_obj.gateway_authority, "status": "OK"},
        )

    def test_submit_order_returns_public_tracking_code_without_reserving_pending_stock(self):
        self.create_cart_item(user=self.user)

        response = self.client.post("/api/shop/submit-order/", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order_data = response.data[0]
        self.assertTrue(order_data["public_tracking_code"].startswith("CH-"))
        self.assertNotEqual(order_data["public_tracking_code"], str(order_data["id"]))
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 1)

        self.client.force_authenticate(self.other_user)
        self.create_cart_item(user=self.other_user)
        second_response = self.client.post("/api/shop/submit-order/", {}, format="json")

        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 1)
        self.assertEqual(OrderItem.objects.filter(product=self.product, order__payment_status=OrderStatus.PENDDING.value).count(), 2)

    def test_coupon_validation_returns_explicit_feedback(self):
        response = self.client.post(
            "/api/shop/check-user-discount-code/",
            {"code": "MISSING", "total_price": "1000"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["valid"])
        self.assertFalse(response.data["message"])
        self.assertEqual(response.data["detail"], "کد تخفیف یافت نشد.")

    def test_coupon_code_updates_order_discount_total(self):
        order = self.create_order_with_item()
        coupon = self.create_coupon(amount=Decimal("250"))

        response = self.client.put(
            f"/api/shop/order-detail/{order.id}/",
            {"coupon_code": coupon.code},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.discount_id, coupon.id)
        self.assertEqual(order.total_price_discount, Decimal("750"))
        self.assertEqual(response.data["public_tracking_code"], order.public_tracking_code)

    def test_payment_request_rejects_stale_stock_change_before_gateway(self):
        order = self.create_order_with_item()
        Product.objects.filter(id=self.product.id).update(quantity=0)

        response = self.request_fake_payment(order)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("موجودی", response.data["error"])
        self.assertEqual(PaymentTransaction.objects.count(), 0)

    def test_payment_request_rejects_product_order_without_shipping_data(self):
        order = self.create_order_with_item(with_shipping=False)

        response = self.request_fake_payment(order)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("آدرس ارسال", response.data["error"])
        self.assertEqual(PaymentTransaction.objects.count(), 0)

    def test_successful_product_payment_decrements_stock_without_delivery_slot(self):
        order = self.create_order_with_item()
        response = self.request_fake_payment(order)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        transaction_obj = PaymentTransaction.objects.get()
        self.callback_fake_payment(transaction_obj)

        verify_response = self.client.post(f"/api/shop/payments/{transaction_obj.id}/verify/", {}, format="json")

        self.assertEqual(verify_response.status_code, status.HTTP_200_OK)
        self.product.refresh_from_db()
        order.refresh_from_db()
        transaction_obj.refresh_from_db()
        self.assertEqual(self.product.quantity, 0)
        self.assertIsNone(order.schedule_id)
        self.assertEqual(DeliveryData.objects.count(), 0)
        self.assertEqual(order.payment_status, OrderStatus.PAID.value)
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.PAID)

    def test_payment_request_rejects_unavailable_delivery_slot_before_gateway(self):
        delivery_data = self.create_delivery_data(capacity=1)
        order = self.create_order_with_item(schedule=delivery_data, is_game=True, with_shipping=False)
        self.create_delivery_data(schedule=delivery_data.schedule, is_used=True)

        response = self.request_fake_payment(order)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("ظرفیت زمان ارسال", response.data["error"])
        self.assertEqual(PaymentTransaction.objects.count(), 0)

    def test_verify_payment_fails_if_stock_changes_after_payment_request(self):
        order = self.create_order_with_item()
        response = self.request_fake_payment(order)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        transaction_obj = PaymentTransaction.objects.get()
        Product.objects.filter(id=self.product.id).update(quantity=0)
        self.callback_fake_payment(transaction_obj)

        verify_response = self.client.post(f"/api/shop/payments/{transaction_obj.id}/verify/", {}, format="json")

        self.assertEqual(verify_response.status_code, status.HTTP_200_OK)
        transaction_obj.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.FAILED)
        self.assertEqual(transaction_obj.error_code, "checkout_integrity_failed")
        self.assertEqual(order.payment_status, OrderStatus.FAIDED.value)

    def test_verify_payment_fails_if_delivery_slot_fills_after_payment_request(self):
        delivery_data = self.create_delivery_data(capacity=1)
        order = self.create_order_with_item(schedule=delivery_data, is_game=True, with_shipping=False)
        response = self.request_fake_payment(order)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        transaction_obj = PaymentTransaction.objects.get()
        self.create_delivery_data(schedule=delivery_data.schedule, is_used=True)
        self.callback_fake_payment(transaction_obj)

        verify_response = self.client.post(f"/api/shop/payments/{transaction_obj.id}/verify/", {}, format="json")

        self.assertEqual(verify_response.status_code, status.HTTP_200_OK)
        transaction_obj.refresh_from_db()
        order.refresh_from_db()
        delivery_data.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.FAILED)
        self.assertEqual(transaction_obj.error_code, "checkout_integrity_failed")
        self.assertEqual(order.payment_status, OrderStatus.FAIDED.value)
        self.assertFalse(delivery_data.is_used)
        self.assertEqual(self.product.quantity, 1)

    def test_successful_verify_decrements_stock_once_and_consumes_delivery_slot(self):
        delivery_data = self.create_delivery_data()
        order = self.create_order_with_item(schedule=delivery_data, is_game=True, with_shipping=False)
        response = self.request_fake_payment(order)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        transaction_obj = PaymentTransaction.objects.get()
        self.callback_fake_payment(transaction_obj)

        first_response = self.client.post(f"/api/shop/payments/{transaction_obj.id}/verify/", {}, format="json")
        second_response = self.client.post(f"/api/shop/payments/{transaction_obj.id}/verify/", {}, format="json")

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.product.refresh_from_db()
        order.refresh_from_db()
        transaction_obj.refresh_from_db()
        delivery_data.refresh_from_db()
        self.assertEqual(self.product.quantity, 0)
        self.assertTrue(delivery_data.is_used)
        self.assertEqual(order.payment_status, OrderStatus.PAID.value)
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.PAID)
        self.assertEqual(second_response.data["order_public_tracking_code"], order.public_tracking_code)


class AdminOrderApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.customer = BaseUser.objects.create_user(
            phone_number="09125555555",
            firstname="Order",
            lastname="Customer",
            password="StrongPass123!",
        )
        self.customer.phone_verified = True
        self.customer.save(update_fields=["phone_verified"])
        self.manager = BaseUser.objects.create_user(
            phone_number="09126666666",
            firstname="Order",
            lastname="Manager",
            password="StrongPass123!",
            user_type=UserTypes.MANAGER,
        )

    def create_order(self, **kwargs):
        return Order.objects.create(
            user=self.customer,
            total_price=Decimal("1000"),
            total_price_discount=Decimal("1000"),
            **kwargs,
        )

    def test_manager_can_list_admin_orders(self):
        order = self.create_order(is_game=False)
        game_order = self.create_order(is_game=True)
        self.client.force_authenticate(self.manager)

        response = self.client.get("/api/shop/order-list-admin/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order_ids = [item["id"] for item in response.data]
        self.assertIn(order.id, order_ids)
        self.assertNotIn(game_order.id, order_ids)
        self.assertEqual(response.data[0]["customer"]["phone_number"], self.customer.phone_number)

    def test_manager_can_retrieve_admin_order_detail(self):
        order = self.create_order()
        self.client.force_authenticate(self.manager)

        response = self.client.get(f"/api/shop/get-order-detail-admin/{order.id}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], order.id)
        self.assertEqual(response.data["customer"]["phone_number"], self.customer.phone_number)

    def test_customer_cannot_list_admin_orders(self):
        self.create_order()
        self.client.force_authenticate(self.customer)

        response = self.client.get("/api/shop/order-list-admin/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_cannot_access_sell_report(self):
        response = self.client.get("/api/shop/sell-order-report/")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_customer_cannot_access_sell_report(self):
        self.client.force_authenticate(self.customer)

        response = self.client.get("/api/shop/sell-order-report/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_manager_can_access_sell_report(self):
        self.client.force_authenticate(self.manager)

        response = self.client.get("/api/shop/sell-order-report/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("game_number", response.data)


@override_settings(
    PAYMENT_GATEWAY_PROVIDER="fake",
    PAYMENT_SUCCESS_REDIRECT_URL="http://frontend.test/PaymentSuccess",
)
class FakePaymentFlowTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = BaseUser.objects.create_user(
            phone_number="09123333333",
            firstname="Payment",
            lastname="User",
            password="StrongPass123!",
        )
        self.user.phone_verified = True
        self.user.save(update_fields=["phone_verified"])
        self.client.force_authenticate(self.user)

    def create_shipping_address(self, user):
        return Address.objects.create(
            user=user,
            province="Tehran",
            city="Tehran",
            postal_code=f"{Address.objects.count() + 200:010d}",
            address_detail="Fake payment shipping address",
        )

    def create_shipping_method(self):
        return DeliveryType.objects.create(
            name="پیک اختصاصی",
            delivery_type=DeliveryOption.MOTOR,
            side=DeliverySide.SENDTOUSER,
        )

    def create_order(self, user=None, **kwargs):
        user = user or self.user
        with_shipping = kwargs.pop("with_shipping", True)
        if with_shipping and not kwargs.get("is_game", False):
            kwargs.setdefault("shipping_address", self.create_shipping_address(user))
            kwargs.setdefault("shipping_method", self.create_shipping_method())
        return Order.objects.create(
            user=user,
            total_price=Decimal("1000"),
            total_price_discount=Decimal("1000"),
            **kwargs,
        )

    def create_payment_request(self, order, data=None, origin=None):
        headers = {}
        if origin is not None:
            headers["HTTP_ORIGIN"] = origin
        return self.client.post(
            f"/api/shop/orders/{order.id}/payment/request/",
            data or {},
            format="json",
            **headers,
        )

    def record_callback(self, transaction_obj, callback_status="OK", accept=None):
        headers = {}
        if accept is not None:
            headers["HTTP_ACCEPT"] = accept
        return self.client.get(
            "/api/payment/callback/fake/",
            {"authority": transaction_obj.gateway_authority, "status": callback_status},
            **headers,
        )

    def test_payment_request_creates_pending_fake_transaction(self):
        order = self.create_order()

        response = self.create_payment_request(order)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(PaymentTransaction.objects.count(), 1)
        transaction_obj = PaymentTransaction.objects.get()
        self.assertEqual(transaction_obj.order_id, order.id)
        self.assertEqual(transaction_obj.user_id, self.user.id)
        self.assertEqual(transaction_obj.provider, "fake")
        self.assertEqual(transaction_obj.amount, Decimal("1000"))
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.PENDING)
        self.assertEqual(response.data["status"], PaymentTransactionStatus.PENDING)
        self.assertIn("/api/payment/callback/fake/", response.data["gateway_payment_url"])

    @override_settings(PAYMENT_SUCCESS_REDIRECT_URL="")
    def test_payment_request_can_store_browser_success_redirect_url(self):
        order = self.create_order()

        self.create_payment_request(
            order,
            data={"success_redirect_url": "http://localhost:4174/PaymentSuccess"},
            origin="http://localhost:4174",
        )
        transaction_obj = PaymentTransaction.objects.get()
        response = self.record_callback(transaction_obj)

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(
            response["Location"],
            f"http://localhost:4174/PaymentSuccess?transaction_id={transaction_obj.id}&order_id={order.id}",
        )

    def test_payment_request_rejects_cross_origin_success_redirect_url(self):
        order = self.create_order()

        response = self.create_payment_request(
            order,
            data={"success_redirect_url": "http://other.test/PaymentSuccess"},
            origin="http://frontend.test",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(PaymentTransaction.objects.count(), 0)

    def test_payment_request_rejects_order_owned_by_another_user(self):
        other_user = BaseUser.objects.create_user(
            phone_number="09124444444",
            firstname="Other",
            lastname="User",
            password="StrongPass123!",
        )
        order = self.create_order(user=other_user)

        response = self.create_payment_request(order)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(PaymentTransaction.objects.count(), 0)

    def test_fake_callback_records_payload(self):
        order = self.create_order()
        self.create_payment_request(order)
        transaction_obj = PaymentTransaction.objects.get()

        response = self.record_callback(transaction_obj)

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(
            response["Location"],
            f"http://frontend.test/PaymentSuccess?transaction_id={transaction_obj.id}&order_id={order.id}",
        )
        transaction_obj.refresh_from_db()
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.CALLBACK_RECEIVED)
        self.assertEqual(transaction_obj.callback_payload["authority"], transaction_obj.gateway_authority)
        self.assertEqual(transaction_obj.callback_payload["status"], "OK")

    def test_fake_callback_can_return_json_for_api_clients(self):
        order = self.create_order()
        self.create_payment_request(order)
        transaction_obj = PaymentTransaction.objects.get()

        response = self.record_callback(transaction_obj, accept="application/json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["transaction_id"], transaction_obj.id)
        self.assertEqual(response.data["order_id"], order.id)
        self.assertEqual(response.data["status"], PaymentTransactionStatus.CALLBACK_RECEIVED)

    def test_verify_paid_updates_transaction_and_order(self):
        order = self.create_order()
        self.create_payment_request(order)
        transaction_obj = PaymentTransaction.objects.get()
        self.record_callback(transaction_obj)

        response = self.client.post(f"/api/shop/payments/{transaction_obj.id}/verify/", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        transaction_obj.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.PAID)
        self.assertEqual(transaction_obj.gateway_ref_id, f"FAKE-REF-{transaction_obj.id}")
        self.assertEqual(order.payment_status, OrderStatus.PAID.value)

    def test_duplicate_verify_is_idempotent(self):
        order = self.create_order()
        self.create_payment_request(order)
        transaction_obj = PaymentTransaction.objects.get()
        self.record_callback(transaction_obj)

        first_response = self.client.post(f"/api/shop/payments/{transaction_obj.id}/verify/", {}, format="json")
        second_response = self.client.post(f"/api/shop/payments/{transaction_obj.id}/verify/", {}, format="json")

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        transaction_obj.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.PAID)
        self.assertEqual(second_response.data["gateway_ref_id"], f"FAKE-REF-{transaction_obj.id}")
        self.assertEqual(order.payment_status, OrderStatus.PAID.value)

    def test_failed_verify_does_not_mark_order_paid(self):
        order = self.create_order()
        self.create_payment_request(order)
        transaction_obj = PaymentTransaction.objects.get()
        self.record_callback(transaction_obj, callback_status="NOK")

        response = self.client.post(f"/api/shop/payments/{transaction_obj.id}/verify/", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        transaction_obj.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.FAILED)
        self.assertEqual(order.payment_status, OrderStatus.FAIDED.value)
        self.assertNotEqual(order.payment_status, OrderStatus.PAID.value)


@override_settings(
    PAYMENT_GATEWAY_PROVIDER="zarinpal",
    PAYMENT_SUCCESS_REDIRECT_URL="http://frontend.test/PaymentSuccess",
    PAYMENT_AMOUNT_UNIT="IRT",
    ZARINPAL_MERCHANT_ID="sandbox-merchant-id",
    ZARINPAL_SANDBOX=True,
    ZARINPAL_REQUEST_URL="https://sandbox.test/pg/v4/payment/request.json",
    ZARINPAL_VERIFY_URL="https://sandbox.test/pg/v4/payment/verify.json",
    ZARINPAL_STARTPAY_URL="https://sandbox.test/pg/StartPay/{authority}",
)
class ZarinpalPaymentFlowTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = BaseUser.objects.create_user(
            phone_number="09125555555",
            firstname="Zarinpal",
            lastname="User",
            password="StrongPass123!",
        )
        self.user.phone_verified = True
        self.user.save(update_fields=["phone_verified"])
        self.client.force_authenticate(self.user)

    def create_shipping_address(self, user):
        return Address.objects.create(
            user=user,
            province="Tehran",
            city="Tehran",
            postal_code=f"{Address.objects.count() + 300:010d}",
            address_detail="Zarinpal shipping address",
        )

    def create_shipping_method(self):
        return DeliveryType.objects.create(
            name="پیک اختصاصی",
            delivery_type=DeliveryOption.MOTOR,
            side=DeliverySide.SENDTOUSER,
        )

    def create_order(self, user=None, amount=Decimal("1000"), **kwargs):
        user = user or self.user
        with_shipping = kwargs.pop("with_shipping", True)
        if with_shipping and not kwargs.get("is_game", False):
            kwargs.setdefault("shipping_address", self.create_shipping_address(user))
            kwargs.setdefault("shipping_method", self.create_shipping_method())
        return Order.objects.create(
            user=user,
            total_price=amount,
            total_price_discount=amount,
            **kwargs,
        )

    def create_zarinpal_transaction(
        self,
        *,
        order=None,
        amount=Decimal("1000"),
        authority="AUTH-VERIFY",
        status_value=PaymentTransactionStatus.CALLBACK_RECEIVED,
        callback_status="OK",
    ):
        order = order or self.create_order(amount=amount)
        callback_payload = {}
        if status_value == PaymentTransactionStatus.CALLBACK_RECEIVED:
            callback_payload = {
                "Authority": authority,
                "Status": callback_status,
                "authority": authority,
                "status": callback_status,
                "provider": "zarinpal",
            }
        return PaymentTransaction.objects.create(
            order=order,
            user=order.user,
            provider="zarinpal",
            amount=amount,
            status=status_value,
            gateway_authority=authority,
            gateway_payment_url=f"https://sandbox.test/pg/StartPay/{authority}",
            callback_payload=callback_payload,
            idempotency_key=f"zarinpal:{order.id}:{authority}",
        )

    @patch("cheatgame.shop.payments.providers.requests.post")
    def test_zarinpal_payment_request_creates_pending_transaction(self, mocked_post):
        mocked_post.return_value = MockZarinpalResponse(
            {"data": {"code": 100, "authority": "AUTH-REQUEST"}, "errors": []}
        )
        order = self.create_order()

        response = self.client.post(f"/api/shop/orders/{order.id}/payment/request/", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        transaction_obj = PaymentTransaction.objects.get()
        self.assertEqual(transaction_obj.provider, "zarinpal")
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.PENDING)
        self.assertEqual(transaction_obj.gateway_authority, "AUTH-REQUEST")
        self.assertEqual(transaction_obj.gateway_payment_url, "https://sandbox.test/pg/StartPay/AUTH-REQUEST")
        request_payload = mocked_post.call_args.kwargs["json"]
        self.assertEqual(mocked_post.call_args.kwargs["url"], "https://sandbox.test/pg/v4/payment/request.json")
        self.assertEqual(request_payload["merchant_id"], "sandbox-merchant-id")
        self.assertEqual(request_payload["amount"], 10000)
        self.assertEqual(request_payload["callback_url"], "http://testserver/api/payment/callback/zarinpal/")
        self.assertEqual(transaction_obj.request_payload["gateway_amount"], 10000)
        self.assertEqual(transaction_obj.request_payload["response"]["data"]["authority"], "AUTH-REQUEST")

    @patch("cheatgame.shop.payments.providers.requests.post")
    def test_zarinpal_request_converts_toman_to_rial_when_amount_unit_is_irt(self, mocked_post):
        mocked_post.return_value = MockZarinpalResponse(
            {"data": {"code": 100, "authority": "AUTH-AMOUNT"}, "errors": []}
        )
        order = self.create_order(amount=Decimal("2500"))

        response = self.client.post(f"/api/shop/orders/{order.id}/payment/request/", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        request_payload = mocked_post.call_args.kwargs["json"]
        self.assertEqual(request_payload["amount"], 25000)
        transaction_obj = PaymentTransaction.objects.get()
        self.assertEqual(transaction_obj.amount, Decimal("2500"))
        self.assertEqual(transaction_obj.request_payload["amount_unit"], "IRT")
        self.assertEqual(transaction_obj.request_payload["gateway_amount"], 25000)

    def test_zarinpal_callback_records_authority_and_status_payload(self):
        order = self.create_order()
        transaction_obj = self.create_zarinpal_transaction(
            order=order,
            authority="AUTH-CALLBACK",
            status_value=PaymentTransactionStatus.PENDING,
        )

        response = self.client.get(
            "/api/payment/callback/zarinpal/",
            {"Authority": "AUTH-CALLBACK", "Status": "OK"},
        )

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(
            response["Location"],
            f"http://frontend.test/PaymentSuccess?transaction_id={transaction_obj.id}&order_id={order.id}",
        )
        transaction_obj.refresh_from_db()
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.CALLBACK_RECEIVED)
        self.assertEqual(transaction_obj.callback_payload["Authority"], "AUTH-CALLBACK")
        self.assertEqual(transaction_obj.callback_payload["Status"], "OK")
        self.assertEqual(transaction_obj.callback_payload["authority"], "AUTH-CALLBACK")
        self.assertEqual(transaction_obj.callback_payload["status"], "OK")

    @patch("cheatgame.shop.payments.providers.requests.post")
    def test_zarinpal_verify_success_marks_transaction_and_order_paid(self, mocked_post):
        mocked_post.return_value = MockZarinpalResponse(
            {
                "data": {"code": 100, "ref_id": 987654, "card_hash": "CARD-HASH"},
                "errors": [],
            }
        )
        order = self.create_order()
        transaction_obj = self.create_zarinpal_transaction(order=order, authority="AUTH-SUCCESS")

        response = self.client.post(f"/api/shop/payments/{transaction_obj.id}/verify/", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        verify_payload = mocked_post.call_args.kwargs["json"]
        self.assertEqual(mocked_post.call_args.kwargs["url"], "https://sandbox.test/pg/v4/payment/verify.json")
        self.assertEqual(verify_payload["merchant_id"], "sandbox-merchant-id")
        self.assertEqual(verify_payload["authority"], "AUTH-SUCCESS")
        self.assertEqual(verify_payload["amount"], 10000)
        transaction_obj.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.PAID)
        self.assertEqual(transaction_obj.gateway_ref_id, "987654")
        self.assertEqual(transaction_obj.gateway_trace_no, "CARD-HASH")
        self.assertEqual(transaction_obj.verify_payload["response"]["data"]["code"], 100)
        self.assertEqual(order.payment_status, OrderStatus.PAID.value)

    @patch("cheatgame.shop.payments.providers.requests.post")
    def test_zarinpal_verify_fail_marks_transaction_failed(self, mocked_post):
        mocked_post.return_value = MockZarinpalResponse(
            {
                "data": {},
                "errors": {"code": -51, "message": "Payment was not found."},
            }
        )
        order = self.create_order()
        transaction_obj = self.create_zarinpal_transaction(order=order, authority="AUTH-FAIL")

        response = self.client.post(f"/api/shop/payments/{transaction_obj.id}/verify/", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        transaction_obj.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.FAILED)
        self.assertEqual(transaction_obj.error_code, "-51")
        self.assertEqual(transaction_obj.error_message, "Payment was not found.")
        self.assertEqual(order.payment_status, OrderStatus.FAIDED.value)

    @patch("cheatgame.shop.payments.providers.requests.post")
    def test_zarinpal_duplicate_verify_is_idempotent(self, mocked_post):
        mocked_post.return_value = MockZarinpalResponse(
            {
                "data": {"code": 100, "ref_id": 123456, "card_hash": "CARD-HASH"},
                "errors": [],
            }
        )
        order = self.create_order()
        transaction_obj = self.create_zarinpal_transaction(order=order, authority="AUTH-DUPLICATE")

        first_response = self.client.post(f"/api/shop/payments/{transaction_obj.id}/verify/", {}, format="json")
        second_response = self.client.post(f"/api/shop/payments/{transaction_obj.id}/verify/", {}, format="json")

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertEqual(mocked_post.call_count, 1)
        transaction_obj.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.PAID)
        self.assertEqual(second_response.data["gateway_ref_id"], "123456")
        self.assertEqual(order.payment_status, OrderStatus.PAID.value)
