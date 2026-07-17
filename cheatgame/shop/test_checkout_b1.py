from datetime import timedelta
from decimal import Decimal
from io import StringIO
from threading import Barrier, Thread
from unittest import mock
from uuid import uuid4

from django.core.management import call_command
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from cheatgame.product.models import Attachment, AttachmentType, DeliveryOption, Product, ProductStatus, ProductType
from cheatgame.product.services.product import ProductDeleteProtectedError, delete_product
from cheatgame.shop.models import (
    Cart,
    CartItem,
    CartItemAttachment,
    CartState,
    Checkout,
    CheckoutLine,
    CheckoutStatus,
    CommerceEvent,
    CommerceEventType,
    DeliverySchedule,
    DeliveryScheduleType,
    DeliverySide,
    DeliveryType,
    PaymentTransaction,
    PaymentTransactionStatus,
    Order,
    StockReservationState,
)
from cheatgame.shop.services.checkout import (
    CheckoutServiceError,
    cancel_checkout,
    create_or_reuse_checkout,
    select_checkout_address,
    select_checkout_schedule,
    select_checkout_shipping,
)
from cheatgame.shop.services.cart import CartMutationLocked, update_cart_item
from cheatgame.shop.services.order import submit_order
from cheatgame.shop.services.pricing import product_line_payable_total, selected_attachment_unit_total
from cheatgame.users.models import Address, BaseUser


class CheckoutB1Fixture:
    def make_user(self, phone="09120000100"):
        user = BaseUser.objects.create_user(
            phone_number=phone,
            firstname="Checkout",
            lastname="Tester",
            password="StrongPass123!",
        )
        user.phone_verified = True
        user.save(update_fields=["phone_verified", "updated_at"])
        return user

    def make_product(self, **overrides):
        values = {
            "product_type": ProductType.PHYSCIAL,
            "title": "Checkout product",
            "main_image": "tests/product.png",
            "price": Decimal("1000"),
            "off_price": Decimal("900"),
            "quantity": 10,
            "description": "tests/product.html",
            "order_limit": 5,
        }
        values.update(overrides)
        return Product.objects.create(**values)

    def make_cart(self, user, product=None, quantity=2, with_attachment=True):
        cart = Cart.objects.create(user=user)
        product = product or self.make_product()
        item = CartItem.objects.create(cart=cart, product=product, quantity=quantity, price=0)
        attachment = None
        if with_attachment:
            attachment = Attachment.objects.create(
                product=product,
                attachment_type=AttachmentType.GUARANTEE,
                title="Warranty",
                price=Decimal("100"),
                is_force_attachment=True,
            )
            CartItemAttachment.objects.create(cart_item=item, attachment=attachment)
        attachments = [attachment] if attachment is not None else []
        item.price = product_line_payable_total(
            product=product,
            attachment_total=selected_attachment_unit_total(attachments=attachments, product=product),
            quantity=quantity,
        )
        item.save(update_fields=["price", "updated_at"])
        return cart, item, attachment


@override_settings(COMMERCE_CHECKOUT_V2_ENABLED=True)
class CheckoutB1ServiceTests(CheckoutB1Fixture, TestCase):
    def setUp(self):
        self.user = self.make_user()
        self.cart, self.item, self.attachment = self.make_cart(self.user)

    def create(self, checkout_uuid=None):
        return create_or_reuse_checkout(user=self.user, client_checkout_uuid=checkout_uuid or uuid4())

    def test_creates_authoritative_snapshots_and_locks_cart_without_payment(self):
        result = self.create()
        self.assertTrue(result.created)
        self.cart.refresh_from_db()
        self.assertEqual(self.cart.state, CartState.LOCKED)
        self.assertEqual(self.cart.active_checkout_id, result.checkout.id)
        line = result.checkout.lines.get()
        self.assertEqual(line.unit_original_price, Decimal("1100"))
        self.assertEqual(line.unit_payable_price, Decimal("1000"))
        self.assertEqual(line.line_payable_total, Decimal("2000"))
        self.assertEqual(line.line_payable_total, self.item.price)
        self.assertEqual(line.attachments.get().total_price, Decimal("200"))
        self.assertFalse(PaymentTransaction.objects.filter(checkout=result.checkout).exists())
        reservation = result.checkout.stock_reservations.get()
        self.assertEqual(reservation.product_id, self.item.product_id)
        self.assertEqual(reservation.quantity, self.item.quantity)
        self.assertEqual(reservation.state, StockReservationState.ACTIVE)
        self.assertEqual(
            list(result.checkout.events.values_list("event_type", flat=True)),
            [
                CommerceEventType.CHECKOUT_DRAFT_CREATED,
                CommerceEventType.STOCK_RESERVATION_CREATED,
                CommerceEventType.CART_LOCKED,
            ],
        )

    def test_same_uuid_and_fingerprint_reuses_once(self):
        checkout_uuid = uuid4()
        first = self.create(checkout_uuid)
        second = self.create(checkout_uuid)
        third = self.create(checkout_uuid)
        self.assertFalse(second.created)
        self.assertEqual(first.checkout.id, second.checkout.id)
        self.assertEqual(first.checkout.id, third.checkout.id)
        self.assertEqual(
            CommerceEvent.objects.filter(
                checkout=first.checkout, event_type=CommerceEventType.CHECKOUT_DRAFT_REUSED
            ).count(),
            1,
        )

    def test_existing_active_hold_reduces_standard_availability(self):
        first = self.create().checkout
        product = self.item.product
        product.quantity = 3
        product.save(update_fields=("quantity", "updated_at"))
        other = self.make_user("09120000109")
        self.make_cart(other, product=product, quantity=2, with_attachment=False)
        with self.assertRaises(CheckoutServiceError) as raised:
            create_or_reuse_checkout(user=other, client_checkout_uuid=uuid4())
        self.assertEqual(raised.exception.code, "CART_INVALID")
        self.assertEqual(first.stock_reservations.get().state, StockReservationState.ACTIVE)

    def test_same_uuid_with_changed_content_conflicts(self):
        checkout_uuid = uuid4()
        self.create(checkout_uuid)
        CartItem.objects.filter(id=self.item.id).update(quantity=3)
        with self.assertRaises(CheckoutServiceError) as raised:
            self.create(checkout_uuid)
        self.assertEqual(raised.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_different_uuid_on_locked_cart_returns_resume_data(self):
        first = self.create()
        with self.assertRaises(CheckoutServiceError) as raised:
            self.create()
        self.assertEqual(raised.exception.code, "CART_LOCKED")
        self.assertEqual(raised.exception.details["public_id"], str(first.checkout.public_id))

    def test_empty_invalid_product_and_invalid_attachment_are_rejected(self):
        CartItem.objects.all().delete()
        with self.assertRaises(CheckoutServiceError) as empty:
            self.create()
        self.assertEqual(empty.exception.code, "CART_EMPTY")

        self.item = CartItem.objects.create(cart=self.cart, product=self.make_product(status=ProductStatus.HIDDEN), quantity=1, price=0)
        with self.assertRaises(CheckoutServiceError) as inactive:
            self.create()
        self.assertEqual(inactive.exception.code, "CART_INVALID")

        CartItem.objects.all().delete()
        product = self.make_product(title="Attachment owner")
        other_product = self.make_product(title="Wrong attachment owner")
        self.item = CartItem.objects.create(cart=self.cart, product=product, quantity=1, price=0)
        wrong_attachment = Attachment.objects.create(
            product=other_product,
            attachment_type=AttachmentType.GUARANTEE,
            title="Wrong warranty",
            price=100,
        )
        CartItemAttachment.objects.create(cart_item=self.item, attachment=wrong_attachment)
        with self.assertRaises(CheckoutServiceError) as invalid_attachment:
            self.create()
        self.assertEqual(invalid_attachment.exception.code, "CART_INVALID")

    def test_snapshot_does_not_change_with_source_models(self):
        checkout = self.create().checkout
        line = checkout.lines.get()
        attachment_snapshot = line.attachments.get()
        Product.objects.filter(id=self.item.product_id).update(title="Changed", price=9999, off_price=8888)
        Attachment.objects.filter(id=self.attachment.id).update(title="Changed warranty", price=777)
        line.refresh_from_db()
        attachment_snapshot.refresh_from_db()
        self.assertEqual(line.product_name, "Checkout product")
        self.assertEqual(line.unit_payable_price, Decimal("1000"))
        self.assertEqual(attachment_snapshot.name, "Warranty")
        self.assertEqual(attachment_snapshot.unit_price, Decimal("100"))

    def test_shipping_source_edits_do_not_change_snapshot(self):
        checkout = self.create().checkout
        address = Address.objects.create(
            user=self.user, province="Tehran", city="Tehran", postal_code="1234567890", address_detail="Original"
        )
        method = DeliveryType.objects.create(
            name="Original shipping", delivery_type=DeliveryOption.MOTOR, side=DeliverySide.SENDTOUSER
        )
        schedule = DeliverySchedule.objects.create(
            type=DeliveryScheduleType.ORDER,
            start=timezone.now() + timedelta(days=5),
            end=timezone.now() + timedelta(days=5, hours=2),
            capacity=5,
        )
        select_checkout_address(user=self.user, public_id=checkout.public_id, address_id=address.id)
        select_checkout_shipping(user=self.user, public_id=checkout.public_id, delivery_method_id=method.id)
        select_checkout_schedule(user=self.user, public_id=checkout.public_id, schedule_id=schedule.id)
        snapshot = checkout.shipping_snapshot
        Address.objects.filter(id=address.id).update(address_detail="Changed", city="Changed")
        DeliveryType.objects.filter(id=method.id).update(name="Changed shipping")
        DeliverySchedule.objects.filter(id=schedule.id).update(
            start=schedule.start + timedelta(days=1), end=schedule.end + timedelta(days=1)
        )
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.full_address, "Original")
        self.assertEqual(snapshot.city, "Tehran")
        self.assertEqual(snapshot.delivery_method_name, "Original shipping")
        self.assertEqual(snapshot.schedule_start, schedule.start)
        self.assertEqual(snapshot.schedule_end, schedule.end)

    def test_failed_event_write_rolls_back_checkout_and_cart_lock(self):
        with mock.patch(
            "cheatgame.shop.services.checkout.append_commerce_event",
            side_effect=RuntimeError("event storage failed"),
        ):
            with self.assertRaises(RuntimeError):
                self.create()
        self.cart.refresh_from_db()
        self.assertEqual(self.cart.state, CartState.OPEN)
        self.assertIsNone(self.cart.active_checkout_id)
        self.assertFalse(Checkout.objects.exists())
        self.assertFalse(CheckoutLine.objects.exists())
        self.assertFalse(CommerceEvent.objects.exists())

    def test_address_shipping_and_schedule_are_owned_validated_and_recoverable(self):
        checkout = self.create().checkout
        address = Address.objects.create(
            user=self.user, province="Tehran", city="Tehran", postal_code="1234567890", address_detail="Test address"
        )
        select_checkout_address(user=self.user, public_id=checkout.public_id, address_id=address.id)
        repair_method = DeliveryType.objects.create(
            name="Repair pickup", delivery_type=DeliveryOption.MOTOR, side=DeliverySide.RECIEVEFROMUSER
        )
        with self.assertRaises(CheckoutServiceError) as invalid:
            select_checkout_shipping(user=self.user, public_id=checkout.public_id, delivery_method_id=repair_method.id)
        self.assertEqual(invalid.exception.code, "SHIPPING_METHOD_INVALID")
        shop_method = DeliveryType.objects.create(
            name="Shop courier", delivery_type=DeliveryOption.MOTOR, side=DeliverySide.SENDTOUSER
        )
        select_checkout_shipping(user=self.user, public_id=checkout.public_id, delivery_method_id=shop_method.id)
        schedule = DeliverySchedule.objects.create(
            type=DeliveryScheduleType.ORDER,
            start=timezone.now() + timedelta(days=5),
            end=timezone.now() + timedelta(days=5, hours=2),
            capacity=5,
        )
        select_checkout_schedule(user=self.user, public_id=checkout.public_id, schedule_id=schedule.id)
        checkout.refresh_from_db()
        snapshot = checkout.shipping_snapshot
        self.assertEqual(snapshot.address_id, address.id)
        self.assertEqual(snapshot.delivery_method_id, shop_method.id)
        self.assertEqual(snapshot.schedule_id, schedule.id)
        self.assertFalse(snapshot.is_pricing_finalized)
        self.assertEqual(snapshot.delivery_cost, 0)

        version = checkout.version
        event_count = checkout.events.count()
        select_checkout_address(user=self.user, public_id=checkout.public_id, address_id=address.id)
        select_checkout_shipping(user=self.user, public_id=checkout.public_id, delivery_method_id=shop_method.id)
        select_checkout_schedule(user=self.user, public_id=checkout.public_id, schedule_id=schedule.id)
        checkout.refresh_from_db()
        snapshot.refresh_from_db()
        self.assertEqual(checkout.version, version)
        self.assertEqual(checkout.events.count(), event_count)
        self.assertEqual(snapshot.delivery_method_id, shop_method.id)
        self.assertEqual(snapshot.schedule_id, schedule.id)

    def test_address_ownership_is_enforced(self):
        checkout = self.create().checkout
        other = self.make_user("09120000101")
        address = Address.objects.create(
            user=other, province="Qom", city="Qom", postal_code="1234567891", address_detail="Other"
        )
        with self.assertRaises(CheckoutServiceError) as raised:
            select_checkout_address(user=self.user, public_id=checkout.public_id, address_id=address.id)
        self.assertEqual(raised.exception.code, "ADDRESS_NOT_FOUND")

    def test_shipping_requires_address_and_schedule_requires_shipping(self):
        checkout = self.create().checkout
        method = DeliveryType.objects.create(
            name="Shop courier", delivery_type=DeliveryOption.MOTOR, side=DeliverySide.SENDTOUSER
        )
        with self.assertRaises(CheckoutServiceError) as no_address:
            select_checkout_shipping(user=self.user, public_id=checkout.public_id, delivery_method_id=method.id)
        self.assertEqual(no_address.exception.code, "ADDRESS_REQUIRED")
        with self.assertRaises(CheckoutServiceError) as no_shipping:
            select_checkout_schedule(user=self.user, public_id=checkout.public_id, schedule_id=999999)
        self.assertEqual(no_shipping.exception.code, "SHIPPING_METHOD_REQUIRED")

    def test_repair_schedule_is_rejected(self):
        checkout = self.create().checkout
        address = Address.objects.create(
            user=self.user, province="Tehran", city="Tehran", postal_code="1234567890", address_detail="Test"
        )
        method = DeliveryType.objects.create(
            name="Shop courier", delivery_type=DeliveryOption.MOTOR, side=DeliverySide.SENDTOUSER
        )
        select_checkout_address(user=self.user, public_id=checkout.public_id, address_id=address.id)
        select_checkout_shipping(user=self.user, public_id=checkout.public_id, delivery_method_id=method.id)
        schedule = DeliverySchedule.objects.create(
            type=DeliveryScheduleType.ISSUE,
            start=timezone.now() + timedelta(days=5),
            end=timezone.now() + timedelta(days=5, hours=2),
            capacity=5,
        )
        with self.assertRaises(CheckoutServiceError) as raised:
            select_checkout_schedule(user=self.user, public_id=checkout.public_id, schedule_id=schedule.id)
        self.assertEqual(raised.exception.code, "SCHEDULE_INVALID")

    def test_full_schedule_is_rejected_without_consuming_capacity(self):
        checkout = self.create().checkout
        address = Address.objects.create(
            user=self.user, province="Tehran", city="Tehran", postal_code="1234567890", address_detail="Test"
        )
        method = DeliveryType.objects.create(
            name="Shop courier", delivery_type=DeliveryOption.MOTOR, side=DeliverySide.SENDTOUSER
        )
        select_checkout_address(user=self.user, public_id=checkout.public_id, address_id=address.id)
        select_checkout_shipping(user=self.user, public_id=checkout.public_id, delivery_method_id=method.id)
        schedule = DeliverySchedule.objects.create(
            type=DeliveryScheduleType.ORDER,
            start=timezone.now() + timedelta(days=5),
            end=timezone.now() + timedelta(days=5, hours=2),
            capacity=0,
        )
        with self.assertRaises(CheckoutServiceError) as raised:
            select_checkout_schedule(user=self.user, public_id=checkout.public_id, schedule_id=schedule.id)
        self.assertEqual(raised.exception.code, "SCHEDULE_INVALID")
        self.assertFalse(checkout.shipping_snapshot.schedule_id)

    def test_cancel_is_idempotent_unlocks_cart_and_preserves_items(self):
        checkout = self.create().checkout
        cancel_checkout(user=self.user, public_id=checkout.public_id)
        cancel_checkout(user=self.user, public_id=checkout.public_id)
        checkout.refresh_from_db()
        self.cart.refresh_from_db()
        self.assertEqual(checkout.status, CheckoutStatus.CANCELED)
        self.assertEqual(self.cart.state, CartState.OPEN)
        self.assertTrue(CartItem.objects.filter(id=self.item.id).exists())
        self.assertEqual(
            CommerceEvent.objects.filter(checkout=checkout, event_type=CommerceEventType.CHECKOUT_CANCELED).count(), 1
        )
        self.assertEqual(
            CommerceEvent.objects.filter(checkout=checkout, event_type=CommerceEventType.CART_UNLOCKED).count(), 1
        )
        self.assertEqual(
            list(checkout.events.order_by("created_at", "id").values_list("event_type", flat=True))[-2:],
            [CommerceEventType.CHECKOUT_CANCELED, CommerceEventType.CART_UNLOCKED],
        )

    def test_cancel_rejects_uncertain_payment(self):
        checkout = self.create().checkout
        order = Order.objects.create(user=self.user, checkout=checkout, total_price=2000, total_price_discount=2000)
        PaymentTransaction.objects.create(
            order=order,
            checkout=checkout,
            user=self.user,
            amount=2000,
            status=PaymentTransactionStatus.PENDING,
            idempotency_key=f"b1:{uuid4()}",
        )
        with self.assertRaises(CheckoutServiceError) as raised:
            cancel_checkout(user=self.user, public_id=checkout.public_id)
        self.assertEqual(raised.exception.code, "CHECKOUT_NOT_CANCELABLE")
        self.cart.refresh_from_db()
        self.assertEqual(self.cart.state, CartState.LOCKED)

    def test_expiry_dry_run_and_apply_preserve_cart_items(self):
        checkout = self.create().checkout
        Checkout.objects.filter(id=checkout.id).update(expires_at=timezone.now() - timedelta(seconds=1))
        output = StringIO()
        call_command("expire_checkouts", stdout=output)
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, CheckoutStatus.CHECKOUT_DRAFT)
        call_command("expire_checkouts", "--apply", stdout=output)
        checkout.refresh_from_db()
        self.cart.refresh_from_db()
        self.assertEqual(checkout.status, CheckoutStatus.EXPIRED)
        self.assertEqual(self.cart.state, CartState.OPEN)
        self.assertTrue(CartItem.objects.filter(id=self.item.id).exists())
        event_count = checkout.events.count()
        call_command("expire_checkouts", "--apply", stdout=output)
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, CheckoutStatus.EXPIRED)
        self.assertEqual(checkout.events.count(), event_count)
        self.assertNotIn(self.user.phone_number, output.getvalue())
        self.assertNotIn(self.user.firstname, output.getvalue())

    def test_selection_expiry_never_exceeds_maximum_lifetime(self):
        checkout = self.create().checkout
        maximum = timezone.now() + timedelta(seconds=5)
        Checkout.objects.filter(id=checkout.id).update(maximum_expires_at=maximum)
        address = Address.objects.create(
            user=self.user, province="Tehran", city="Tehran", postal_code="1234567890", address_detail="Test"
        )
        select_checkout_address(user=self.user, public_id=checkout.public_id, address_id=address.id)
        checkout.refresh_from_db()
        self.assertLessEqual(checkout.expires_at, maximum)


class CheckoutB1ApiTests(CheckoutB1Fixture, TestCase):
    def setUp(self):
        self.user = self.make_user()
        self.cart, self.item, self.attachment = self.make_cart(self.user)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_feature_flag_disabled_returns_503_and_legacy_add_still_works(self):
        response = self.client.post(reverse("api:checkout-v2-create"), {"checkout_uuid": str(uuid4())}, format="json")
        self.assertEqual(response.status_code, 503)
        second = self.make_product(title="Second product")
        response = self.client.post(
            reverse("api:add_to_cart"), {"product": second.id, "quantity": 1, "attachment": []}, format="json"
        )
        self.assertEqual(response.status_code, 200)

    def test_feature_flag_disabled_preserves_legacy_update_delete_and_submit_without_v2_artifacts(self):
        update = self.client.put(reverse("api:cart-item-detail", args=[self.item.id]), {"quantity": 3}, format="json")
        self.assertEqual(update.status_code, 200)
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 3)

        second = self.make_product(title="Delete product")
        second_item = CartItem.objects.create(cart=self.cart, product=second, quantity=1, price=second.price)
        delete = self.client.delete(reverse("api:cart-item-detail", args=[second_item.id]))
        self.assertEqual(delete.status_code, 200)
        self.assertFalse(CartItem.objects.filter(id=second_item.id).exists())

        submit = self.client.post(reverse("api:submit-order"), {}, format="json")
        self.assertEqual(submit.status_code, 200)
        self.cart.refresh_from_db()
        self.assertEqual(self.cart.state, CartState.OPEN)
        self.assertFalse(Checkout.objects.exists())
        self.assertFalse(CheckoutLine.objects.exists())
        self.assertFalse(CommerceEvent.objects.exists())

    def test_all_v2_endpoints_return_stable_disabled_error(self):
        public_id = uuid4()
        requests = (
            self.client.post(reverse("api:checkout-v2-create"), {"checkout_uuid": str(uuid4())}, format="json"),
            self.client.get(reverse("api:checkout-v2-active")),
            self.client.get(reverse("api:checkout-v2-detail", args=[public_id])),
            self.client.patch(reverse("api:checkout-v2-address", args=[public_id]), {"address_id": 1}, format="json"),
            self.client.patch(
                reverse("api:checkout-v2-shipping", args=[public_id]), {"delivery_method_id": 1}, format="json"
            ),
            self.client.patch(reverse("api:checkout-v2-schedule", args=[public_id]), {"schedule_id": 1}, format="json"),
            self.client.post(reverse("api:checkout-v2-cancel", args=[public_id]), {}, format="json"),
        )
        for response in requests:
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.data["code"], "CHECKOUT_V2_DISABLED")

    @override_settings(COMMERCE_CHECKOUT_V2_ENABLED=True)
    def test_create_active_detail_and_ownership(self):
        response = self.client.post(reverse("api:checkout-v2-create"), {"checkout_uuid": str(uuid4())}, format="json")
        self.assertEqual(response.status_code, 201)
        public_id = response.data["public_id"]
        self.assertEqual(self.client.get(reverse("api:checkout-v2-active")).status_code, 200)
        self.assertEqual(self.client.get(reverse("api:checkout-v2-detail", args=[public_id])).status_code, 200)
        other = self.make_user("09120000102")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(reverse("api:checkout-v2-detail", args=[public_id])).status_code, 404)

    @override_settings(COMMERCE_CHECKOUT_V2_ENABLED=True)
    def test_foreign_checkout_mutations_return_same_safe_404_and_anonymous_is_rejected(self):
        owner = self.make_user("09120000103")
        self.make_cart(owner)
        checkout = create_or_reuse_checkout(user=owner, client_checkout_uuid=uuid4()).checkout
        address = Address.objects.create(
            user=self.user, province="Tehran", city="Tehran", postal_code="1234567890", address_detail="Test"
        )
        method = DeliveryType.objects.create(
            name="Shipping", delivery_type=DeliveryOption.MOTOR, side=DeliverySide.SENDTOUSER
        )
        schedule = DeliverySchedule.objects.create(
            type=DeliveryScheduleType.ORDER,
            start=timezone.now() + timedelta(days=5),
            end=timezone.now() + timedelta(days=5, hours=2),
            capacity=5,
        )
        responses = (
            self.client.get(reverse("api:checkout-v2-detail", args=[checkout.public_id])),
            self.client.patch(
                reverse("api:checkout-v2-address", args=[checkout.public_id]), {"address_id": address.id}, format="json"
            ),
            self.client.patch(
                reverse("api:checkout-v2-shipping", args=[checkout.public_id]),
                {"delivery_method_id": method.id},
                format="json",
            ),
            self.client.patch(
                reverse("api:checkout-v2-schedule", args=[checkout.public_id]), {"schedule_id": schedule.id}, format="json"
            ),
            self.client.post(reverse("api:checkout-v2-cancel", args=[checkout.public_id]), {}, format="json"),
        )
        for response in responses:
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.data["code"], "CHECKOUT_NOT_FOUND")

        self.client.force_authenticate(user=None)
        anonymous_requests = (
            self.client.post(reverse("api:checkout-v2-create"), {"checkout_uuid": str(uuid4())}, format="json"),
            self.client.get(reverse("api:checkout-v2-active")),
            self.client.get(reverse("api:checkout-v2-detail", args=[checkout.public_id])),
            self.client.patch(
                reverse("api:checkout-v2-address", args=[checkout.public_id]), {"address_id": address.id}, format="json"
            ),
            self.client.patch(
                reverse("api:checkout-v2-shipping", args=[checkout.public_id]),
                {"delivery_method_id": method.id},
                format="json",
            ),
            self.client.patch(
                reverse("api:checkout-v2-schedule", args=[checkout.public_id]), {"schedule_id": schedule.id}, format="json"
            ),
            self.client.post(reverse("api:checkout-v2-cancel", args=[checkout.public_id]), {}, format="json"),
        )
        for response in anonymous_requests:
            self.assertIn(response.status_code, (401, 403))

    @override_settings(COMMERCE_CHECKOUT_V2_ENABLED=True)
    def test_api_idempotency_conflict_and_cart_locked_responses_are_safe(self):
        checkout_uuid = uuid4()
        public_id_attempt = uuid4()
        created = self.client.post(
            reverse("api:checkout-v2-create"),
            {"checkout_uuid": str(checkout_uuid), "public_id": str(public_id_attempt)},
            format="json",
        )
        self.assertEqual(created.status_code, 201)
        self.assertNotEqual(created.data["public_id"], str(public_id_attempt))
        checkout = Checkout.objects.get(public_id=created.data["public_id"])
        original_snapshot = list(checkout.lines.values("product_name", "quantity", "line_payable_total"))

        CartItem.objects.filter(id=self.item.id).update(quantity=3)
        conflict = self.client.post(
            reverse("api:checkout-v2-create"), {"checkout_uuid": str(checkout_uuid)}, format="json"
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.data["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(list(checkout.lines.values("product_name", "quantity", "line_payable_total")), original_snapshot)

        locked = self.client.post(
            reverse("api:checkout-v2-create"), {"checkout_uuid": str(uuid4())}, format="json"
        )
        self.assertEqual(locked.status_code, 409)
        self.assertEqual(locked.data["code"], "CART_LOCKED")
        self.assertEqual(set(locked.data["details"]), {"public_id", "status", "resume_route"})
        self.assertNotIn("cart_fingerprint", locked.data)

    @override_settings(COMMERCE_CHECKOUT_V2_ENABLED=True)
    def test_locked_cart_rejects_add_update_delete_and_legacy_submit(self):
        checkout = create_or_reuse_checkout(user=self.user, client_checkout_uuid=uuid4()).checkout
        second = self.make_product(title="Second product")
        add = self.client.post(reverse("api:add_to_cart"), {"product": second.id, "quantity": 1, "attachment": []}, format="json")
        update = self.client.put(reverse("api:cart-item-detail", args=[self.item.id]), {"quantity": 3}, format="json")
        delete = self.client.delete(reverse("api:cart-item-detail", args=[self.item.id]))
        submit = self.client.post(reverse("api:submit-order"), {}, format="json")
        self.assertEqual([add.status_code, update.status_code, delete.status_code, submit.status_code], [409, 409, 409, 409])
        self.assertEqual(CheckoutLine.objects.filter(checkout=checkout).count(), 1)
        self.assertTrue(CartItem.objects.filter(id=self.item.id, quantity=2).exists())

    @override_settings(COMMERCE_CHECKOUT_V2_ENABLED=True)
    def test_direct_legacy_submit_and_product_delete_cannot_bypass_cart_lock(self):
        create_or_reuse_checkout(user=self.user, client_checkout_uuid=uuid4())
        cart_items = CartItem.objects.filter(cart=self.cart)
        with self.assertRaises(CartMutationLocked):
            submit_order(user=self.user, total_price=0, product=list(cart_items), game=[], cart_items=cart_items)
        with self.assertRaises(ProductDeleteProtectedError):
            delete_product(product_id=self.item.product_id)
        self.assertTrue(CartItem.objects.filter(id=self.item.id).exists())

    @override_settings(COMMERCE_CHECKOUT_V2_ENABLED=True)
    def test_active_response_contains_full_recovery_state(self):
        checkout = create_or_reuse_checkout(user=self.user, client_checkout_uuid=uuid4()).checkout
        address = Address.objects.create(
            user=self.user, province="Tehran", city="Tehran", postal_code="1234567890", address_detail="Test"
        )
        method = DeliveryType.objects.create(
            name="Shop courier", delivery_type=DeliveryOption.MOTOR, side=DeliverySide.SENDTOUSER
        )
        select_checkout_address(user=self.user, public_id=checkout.public_id, address_id=address.id)
        select_checkout_shipping(user=self.user, public_id=checkout.public_id, delivery_method_id=method.id)
        response = self.client.get(reverse("api:checkout-v2-active"))
        detail = self.client.get(reverse("api:checkout-v2-detail", args=[checkout.public_id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(response.data["shipping"]["address_id"], address.id)
        self.assertEqual(response.data["shipping"]["delivery_method_id"], method.id)
        self.assertEqual(response.data["lines"][0]["attachments"][0]["name"], "Warranty")
        self.assertFalse(response.data["totals"]["is_pricing_finalized"])
        self.assertFalse(response.data["payment_eligible"])
        self.assertEqual(response.data["payment_ineligible_reason"], "SHIPPING_PRICING_NOT_FINALIZED")
        self.assertEqual(response.data["totals"], detail.data["totals"])
        self.assertEqual(response.data["totals"]["items_payable"], str(self.item.price))
        self.assertFalse(response.data["shipping"]["is_pricing_finalized"])


@override_settings(COMMERCE_CHECKOUT_V2_ENABLED=True)
class CheckoutB1ConcurrencyTests(CheckoutB1Fixture, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL is required for row-lock concurrency tests.")
        self.user = self.make_user()
        self.cart, self.item, self.attachment = self.make_cart(self.user)

    def _run_pair(self, uuids):
        barrier = Barrier(2)
        outcomes = []

        def worker(checkout_uuid):
            close_old_connections()
            try:
                user = BaseUser.objects.get(id=self.user.id)
                barrier.wait()
                result = create_or_reuse_checkout(user=user, client_checkout_uuid=checkout_uuid)
                outcomes.append(("ok", result.checkout.id, result.created))
            except CheckoutServiceError as error:
                outcomes.append((error.code, error.details.get("public_id"), False))
            finally:
                close_old_connections()

        threads = [Thread(target=worker, args=(value,)) for value in uuids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        return outcomes

    def test_concurrent_same_uuid_creates_one_checkout_and_one_snapshot_set(self):
        checkout_uuid = uuid4()
        outcomes = self._run_pair([checkout_uuid, checkout_uuid])
        self.assertEqual(len(outcomes), 2)
        self.assertEqual({value[0] for value in outcomes}, {"ok"})
        self.assertEqual(sorted(value[2] for value in outcomes), [False, True])
        self.assertEqual(Checkout.objects.count(), 1)
        self.assertEqual(CheckoutLine.objects.count(), 1)

    def test_concurrent_different_uuid_creates_one_and_returns_cart_locked(self):
        outcomes = self._run_pair([uuid4(), uuid4()])
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(sorted(value[0] for value in outcomes), ["CART_LOCKED", "ok"])
        self.assertEqual(Checkout.objects.count(), 1)
        self.assertEqual(CheckoutLine.objects.count(), 1)

    def test_concurrent_carts_cannot_over_reserve_same_product(self):
        self.item.product.quantity = 2
        self.item.product.save(update_fields=("quantity", "updated_at"))
        other = self.make_user("09120000110")
        self.make_cart(other, product=self.item.product, quantity=2, with_attachment=False)
        barrier = Barrier(2)
        outcomes = []

        def worker(user_id):
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                create_or_reuse_checkout(
                    user=BaseUser.objects.get(pk=user_id), client_checkout_uuid=uuid4()
                )
            except CheckoutServiceError as error:
                outcomes.append(error.code)
            else:
                outcomes.append("ok")
            finally:
                close_old_connections()

        threads = [Thread(target=worker, args=(self.user.pk,)), Thread(target=worker, args=(other.pk,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads), outcomes)
        self.assertCountEqual(outcomes, ["ok", "CART_INVALID"])
        self.assertEqual(Checkout.objects.count(), 1)

    def test_cancel_and_cart_mutation_race_preserves_consistent_ownership(self):
        checkout = create_or_reuse_checkout(user=self.user, client_checkout_uuid=uuid4()).checkout
        barrier = Barrier(2)
        outcomes = []

        def cancel_worker():
            close_old_connections()
            try:
                barrier.wait()
                cancel_checkout(user=BaseUser.objects.get(id=self.user.id), public_id=checkout.public_id)
                outcomes.append("canceled")
            finally:
                close_old_connections()

        def mutate_worker():
            close_old_connections()
            try:
                barrier.wait()
                update_cart_item(cart_item=CartItem.objects.get(id=self.item.id), quantity=3)
                outcomes.append("mutated")
            except CartMutationLocked:
                outcomes.append("locked")
            finally:
                close_old_connections()

        threads = [Thread(target=cancel_worker), Thread(target=mutate_worker)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        checkout.refresh_from_db()
        self.cart.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(checkout.status, CheckoutStatus.CANCELED)
        self.assertEqual(self.cart.state, CartState.OPEN)
        self.assertIsNone(self.cart.active_checkout_id)
        self.assertIn(self.item.quantity, (2, 3))
        self.assertIn("canceled", outcomes)

    def test_expiry_and_new_create_race_leaves_one_authoritative_state(self):
        checkout = create_or_reuse_checkout(user=self.user, client_checkout_uuid=uuid4()).checkout
        Checkout.objects.filter(id=checkout.id).update(expires_at=timezone.now() - timedelta(seconds=1))
        barrier = Barrier(2)
        outcomes = []

        def expire_worker():
            close_old_connections()
            try:
                barrier.wait()
                call_command("expire_checkouts", "--apply", stdout=StringIO())
                outcomes.append("expired")
            finally:
                close_old_connections()

        def create_worker():
            close_old_connections()
            try:
                barrier.wait()
                result = create_or_reuse_checkout(
                    user=BaseUser.objects.get(id=self.user.id), client_checkout_uuid=uuid4()
                )
                outcomes.append("created" if result.created else "reused")
            except CheckoutServiceError as error:
                outcomes.append(error.code)
            finally:
                close_old_connections()

        threads = [Thread(target=expire_worker), Thread(target=create_worker)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.cart.refresh_from_db()
        active = Checkout.objects.filter(cart=self.cart, status__in=Checkout.ACTIVE_STATUSES)
        self.assertLessEqual(active.count(), 1)
        if active.exists():
            self.assertEqual(self.cart.active_checkout_id, active.get().id)
            self.assertEqual(self.cart.state, CartState.LOCKED)
        else:
            self.assertIsNone(self.cart.active_checkout_id)
            self.assertEqual(self.cart.state, CartState.OPEN)

    def test_reuse_and_cancel_race_finishes_canceled_with_open_cart(self):
        checkout_uuid = uuid4()
        checkout = create_or_reuse_checkout(user=self.user, client_checkout_uuid=checkout_uuid).checkout
        barrier = Barrier(2)
        outcomes = []

        def reuse_worker():
            close_old_connections()
            try:
                barrier.wait()
                result = create_or_reuse_checkout(
                    user=BaseUser.objects.get(id=self.user.id), client_checkout_uuid=checkout_uuid
                )
                outcomes.append(("reused", result.checkout.status))
            finally:
                close_old_connections()

        def cancel_worker():
            close_old_connections()
            try:
                barrier.wait()
                cancel_checkout(user=BaseUser.objects.get(id=self.user.id), public_id=checkout.public_id)
                outcomes.append(("canceled", None))
            finally:
                close_old_connections()

        threads = [Thread(target=reuse_worker), Thread(target=cancel_worker)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        checkout.refresh_from_db()
        self.cart.refresh_from_db()
        self.assertEqual(checkout.status, CheckoutStatus.CANCELED)
        self.assertEqual(self.cart.state, CartState.OPEN)
        self.assertIsNone(self.cart.active_checkout_id)
        self.assertEqual({item[0] for item in outcomes}, {"reused", "canceled"})

    def test_address_update_and_cancel_race_cannot_reopen_or_reown_cart(self):
        checkout = create_or_reuse_checkout(user=self.user, client_checkout_uuid=uuid4()).checkout
        address = Address.objects.create(
            user=self.user, province="Tehran", city="Tehran", postal_code="1234567890", address_detail="Test"
        )
        barrier = Barrier(2)
        outcomes = []

        def address_worker():
            close_old_connections()
            try:
                barrier.wait()
                select_checkout_address(
                    user=BaseUser.objects.get(id=self.user.id),
                    public_id=checkout.public_id,
                    address_id=address.id,
                )
                outcomes.append("address_selected")
            except CheckoutServiceError as error:
                outcomes.append(error.code)
            finally:
                close_old_connections()

        def cancel_worker():
            close_old_connections()
            try:
                barrier.wait()
                cancel_checkout(user=BaseUser.objects.get(id=self.user.id), public_id=checkout.public_id)
                outcomes.append("canceled")
            finally:
                close_old_connections()

        threads = [Thread(target=address_worker), Thread(target=cancel_worker)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        checkout.refresh_from_db()
        self.cart.refresh_from_db()
        self.assertEqual(checkout.status, CheckoutStatus.CANCELED)
        self.assertEqual(self.cart.state, CartState.OPEN)
        self.assertIsNone(self.cart.active_checkout_id)
        self.assertIn("canceled", outcomes)
        self.assertTrue(set(outcomes).issubset({"address_selected", "CHECKOUT_NOT_EDITABLE", "canceled"}))

    def test_shipping_update_and_cancel_race_cannot_reopen_or_reown_cart(self):
        checkout = create_or_reuse_checkout(user=self.user, client_checkout_uuid=uuid4()).checkout
        address = Address.objects.create(
            user=self.user, province="Tehran", city="Tehran", postal_code="1234567890", address_detail="Test"
        )
        method = DeliveryType.objects.create(
            name="Shipping", delivery_type=DeliveryOption.MOTOR, side=DeliverySide.SENDTOUSER
        )
        select_checkout_address(user=self.user, public_id=checkout.public_id, address_id=address.id)
        barrier = Barrier(2)
        outcomes = []

        def shipping_worker():
            close_old_connections()
            try:
                barrier.wait()
                select_checkout_shipping(
                    user=BaseUser.objects.get(id=self.user.id),
                    public_id=checkout.public_id,
                    delivery_method_id=method.id,
                )
                outcomes.append("shipping_selected")
            except CheckoutServiceError as error:
                outcomes.append(error.code)
            finally:
                close_old_connections()

        def cancel_worker():
            close_old_connections()
            try:
                barrier.wait()
                cancel_checkout(user=BaseUser.objects.get(id=self.user.id), public_id=checkout.public_id)
                outcomes.append("canceled")
            finally:
                close_old_connections()

        threads = [Thread(target=shipping_worker), Thread(target=cancel_worker)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        checkout.refresh_from_db()
        self.cart.refresh_from_db()
        self.assertEqual(checkout.status, CheckoutStatus.CANCELED)
        self.assertEqual(self.cart.state, CartState.OPEN)
        self.assertIsNone(self.cart.active_checkout_id)
        self.assertIn("canceled", outcomes)
        self.assertTrue(set(outcomes).issubset({"shipping_selected", "CHECKOUT_NOT_EDITABLE", "canceled"}))
