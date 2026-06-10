from datetime import timedelta
from decimal import Decimal
import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from cheatgame.product.models import DeliveryOption
from cheatgame.product.models import Product, ProductType
from cheatgame.shop.models import (
    Cart,
    CartItem,
    DeliveryData,
    DeliverySchedule,
    DeliveryScheduleType,
    DeliverySide,
    DeliveryType,
    Order,
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

    def create_order(self, user=None, **kwargs):
        return Order.objects.create(
            user=user or self.user,
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

    def create_order(self, user=None, amount=Decimal("1000"), **kwargs):
        return Order.objects.create(
            user=user or self.user,
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
