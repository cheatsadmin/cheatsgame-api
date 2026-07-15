from enum import IntEnum
from secrets import token_hex
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from cheatgame.common.models import BaseModel
from cheatgame.product.models import DeliveryOption


class OrderStatus(IntEnum):
    PENDDING = 1
    FAIDED = 2
    PAID = 3
    CANCELD = 4

    @classmethod
    def choices(cls):
        return [(key.value, key.name) for key in cls]


class PaymentTransactionStatus(models.TextChoices):
    CREATED = "created", "CREATED"
    PENDING = "pending", "PENDING"
    CALLBACK_RECEIVED = "callback_received", "CALLBACK_RECEIVED"
    VERIFYING = "verifying", "VERIFYING"
    PAID = "paid", "PAID"
    FAILED = "failed", "FAILED"
    REQUIRES_MANUAL_REVIEW = "requires_manual_review", "REQUIRES_MANUAL_REVIEW"


class CheckoutStatus(models.TextChoices):
    CHECKOUT_DRAFT = "checkout_draft", "CHECKOUT_DRAFT"
    PENDING_PAYMENT = "pending_payment", "PENDING_PAYMENT"
    PAID = "paid", "PAID"
    REQUIRES_MANUAL_REVIEW = "requires_manual_review", "REQUIRES_MANUAL_REVIEW"
    CANCELED = "canceled", "CANCELED"
    EXPIRED = "expired", "EXPIRED"


ACTIVE_CHECKOUT_STATUSES = (
    CheckoutStatus.CHECKOUT_DRAFT,
    CheckoutStatus.PENDING_PAYMENT,
    CheckoutStatus.REQUIRES_MANUAL_REVIEW,
)


class ManualReviewReason(models.TextChoices):
    AMOUNT_MISMATCH = "amount_mismatch", "AMOUNT_MISMATCH"
    STOCK_CONFLICT = "stock_conflict", "STOCK_CONFLICT"
    DELIVERY_CONFLICT = "delivery_conflict", "DELIVERY_CONFLICT"
    DISCOUNT_CONFLICT = "discount_conflict", "DISCOUNT_CONFLICT"
    PROVIDER_STATE_UNCLEAR = "provider_state_unclear", "PROVIDER_STATE_UNCLEAR"
    FINALIZATION_ERROR = "finalization_error", "FINALIZATION_ERROR"
    LATE_PAYMENT_AFTER_EXPIRY = "late_payment_after_expiry", "LATE_PAYMENT_AFTER_EXPIRY"
    UNKNOWN = "unknown", "UNKNOWN"


class CartState(models.TextChoices):
    OPEN = "open", "OPEN"
    LOCKED = "locked", "LOCKED"


class CartLockReason(models.TextChoices):
    CHECKOUT_IN_PROGRESS = "checkout_in_progress", "CHECKOUT_IN_PROGRESS"
    PAYMENT_IN_PROGRESS = "payment_in_progress", "PAYMENT_IN_PROGRESS"
    MANUAL_REVIEW = "manual_review", "MANUAL_REVIEW"
    ADMIN = "admin", "ADMIN"


class FulfillmentStatus(models.TextChoices):
    NOT_STARTED = "not_started", "NOT_STARTED"
    PROCESSING = "processing", "PROCESSING"
    SENDING = "sending", "SENDING"
    DELIVERED = "delivered", "DELIVERED"
    CANCELED = "canceled", "CANCELED"


class StockReservationState(models.TextChoices):
    ACTIVE = "active", "ACTIVE"
    CONSUMED = "consumed", "CONSUMED"
    RELEASED = "released", "RELEASED"


class CommerceActorType(models.TextChoices):
    CUSTOMER = "customer", "CUSTOMER"
    SYSTEM = "system", "SYSTEM"
    GATEWAY = "gateway", "GATEWAY"
    ADMIN = "admin", "ADMIN"
    SUPPORT = "support", "SUPPORT"


class CommerceEventType(models.TextChoices):
    CHECKOUT_DRAFT_CREATED = "checkout_draft_created", "CHECKOUT_DRAFT_CREATED"
    CHECKOUT_DRAFT_REUSED = "checkout_draft_reused", "CHECKOUT_DRAFT_REUSED"
    CART_LOCKED = "cart_locked", "CART_LOCKED"
    CART_UNLOCKED = "cart_unlocked", "CART_UNLOCKED"
    ADDRESS_SELECTED = "address_selected", "ADDRESS_SELECTED"
    SHIPPING_SELECTED = "shipping_selected", "SHIPPING_SELECTED"
    SCHEDULE_SELECTED = "schedule_selected", "SCHEDULE_SELECTED"
    PAYMENT_REQUESTED = "payment_requested", "PAYMENT_REQUESTED"
    GATEWAY_CALLBACK_RECEIVED = "gateway_callback_received", "GATEWAY_CALLBACK_RECEIVED"
    PAYMENT_VERIFICATION_STARTED = "payment_verification_started", "PAYMENT_VERIFICATION_STARTED"
    PAYMENT_VERIFIED = "payment_verified", "PAYMENT_VERIFIED"
    PAYMENT_FAILED = "payment_failed", "PAYMENT_FAILED"
    STOCK_RESERVATION_CREATED = "stock_reservation_created", "STOCK_RESERVATION_CREATED"
    STOCK_RESERVATION_RELEASED = "stock_reservation_released", "STOCK_RESERVATION_RELEASED"
    STOCK_RESERVATION_CONSUMED = "stock_reservation_consumed", "STOCK_RESERVATION_CONSUMED"
    FULFILLMENT_FINALIZATION_SUCCEEDED = (
        "fulfillment_finalization_succeeded",
        "FULFILLMENT_FINALIZATION_SUCCEEDED",
    )
    MANUAL_REVIEW_REQUIRED = "manual_review_required", "MANUAL_REVIEW_REQUIRED"
    CHECKOUT_EXPIRED = "checkout_expired", "CHECKOUT_EXPIRED"
    CHECKOUT_CANCELED = "checkout_canceled", "CHECKOUT_CANCELED"


class OrderUserStatus(IntEnum):
    NOTCOMPLETED = 1
    NOTSEEN = 2
    RECEIVED = 3
    SENDING = 4
    CANCLED = 5
    FINISHED = 6

    @classmethod
    def choices(cls):
        return [(key.value, key.name) for key in cls]


class DiscountType(IntEnum):
    DIRECT = 1
    COUPON = 2

    @classmethod
    def choices(cls):
        return [(key.value, key.name) for key in cls]


class DeliveryScheduleType(IntEnum):
    ISSUE = 1
    ORDER = 2

    @classmethod
    def choices(cls):
        return [(key.value, key.name) for key in cls]


class DiscountValueType(IntEnum):
    PERCENT = 1
    AMOUNT = 2

    @classmethod
    def choices(cls):
        return [(key.value, key.name) for key in cls]


class DeliverySide(IntEnum):
    RECIEVEFROMUSER = 1
    SENDTOUSER = 2

    @classmethod
    def choices(cls):
        return [(key.value, key.name) for key in cls]


class Checkout(BaseModel):
    ACTIVE_STATUSES = ACTIVE_CHECKOUT_STATUSES

    public_id = models.UUIDField(default=uuid4, unique=True, editable=False)
    user = models.ForeignKey("users.BaseUser", on_delete=models.PROTECT, related_name="checkouts")
    # A Cart can have many historical Checkouts, but only one active Checkout.
    cart = models.ForeignKey("Cart", on_delete=models.SET_NULL, null=True, blank=True, related_name="checkouts")
    client_checkout_uuid = models.UUIDField()
    cart_fingerprint = models.CharField(max_length=64, db_index=True)
    status = models.CharField(
        max_length=32,
        choices=CheckoutStatus.choices,
        default=CheckoutStatus.CHECKOUT_DRAFT,
        db_index=True,
    )
    expires_at = models.DateTimeField()
    maximum_expires_at = models.DateTimeField()
    locked_at = models.DateTimeField()
    paid_at = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)
    manual_review_reason = models.CharField(
        max_length=40,
        choices=ManualReviewReason.choices,
        null=True,
        blank=True,
    )
    manual_review_message = models.TextField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        indexes = [
            models.Index(fields=["user", "status", "-created_at"], name="shop_co_user_stat_created"),
            models.Index(fields=["status", "expires_at"], name="shop_co_status_expires"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "client_checkout_uuid"],
                name="uniq_checkout_user_client_uuid",
            ),
            models.UniqueConstraint(
                fields=["cart"],
                condition=Q(cart__isnull=False, status__in=ACTIVE_CHECKOUT_STATUSES),
                name="uniq_active_checkout_per_cart",
            ),
        ]

    @property
    def is_active(self):
        return self.status in self.ACTIVE_STATUSES

    def save(self, *args, **kwargs):
        if self.pk:
            original_public_id = type(self).objects.filter(pk=self.pk).values_list("public_id", flat=True).first()
            if original_public_id is not None and original_public_id != self.public_id:
                raise ValidationError({"public_id": "Checkout public_id is immutable."})
        return super().save(*args, **kwargs)


class Cart(BaseModel):
    user = models.OneToOneField("users.BaseUser", on_delete=models.CASCADE)
    state = models.CharField(max_length=16, choices=CartState.choices, default=CartState.OPEN, db_index=True)
    lock_reason = models.CharField(max_length=32, choices=CartLockReason.choices, null=True, blank=True)
    active_checkout = models.ForeignKey(
        "Checkout",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="locked_carts",
    )
    locked_at = models.DateTimeField(null=True, blank=True)
    lock_version = models.PositiveIntegerField(default=0)


class CartItem(BaseModel):
    product = models.ForeignKey("product.Product", on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=1)
    price = models.DecimalField(max_digits=16, decimal_places=0)
    cart = models.ForeignKey("Cart", on_delete=models.CASCADE)


class CartItemAttachment(BaseModel):
    cart_item = models.ForeignKey("CartItem", on_delete=models.CASCADE)
    attachment = models.ForeignKey("product.Attachment", on_delete=models.PROTECT)


class Order(BaseModel):
    user = models.ForeignKey("users.BaseUser", on_delete=models.CASCADE)
    public_tracking_code = models.CharField(max_length=16, unique=True, null=True, blank=True, editable=False)
    discount = models.ForeignKey("Discount", on_delete=models.SET_NULL, null=True, blank=True)
    payment_status = models.IntegerField(choices=OrderStatus.choices(), default=OrderStatus.PENDDING)
    user_status = models.IntegerField(choices=OrderUserStatus.choices(), default=OrderUserStatus.NOTCOMPLETED)
    total_price = models.DecimalField(max_digits=16, decimal_places=0)
    total_price_discount = models.DecimalField(max_digits=16, decimal_places=0)
    schedule = models.ForeignKey("DeliveryData", on_delete=models.PROTECT, null=True, blank=True)
    shipping_address = models.ForeignKey(
        "users.Address",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="shipping_orders",
    )
    shipping_method = models.ForeignKey(
        "shop.DeliveryType",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="shipping_orders",
    )
    is_game = models.BooleanField(default=False)
    checkout = models.ForeignKey(
        "Checkout",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="orders",
    )
    fulfillment_status = models.CharField(
        max_length=20,
        choices=FulfillmentStatus.choices,
        default=FulfillmentStatus.NOT_STARTED,
        db_index=True,
    )

    @classmethod
    def generate_public_tracking_code(cls) -> str:
        for _ in range(10):
            code = f"CH-{token_hex(5).upper()}"
            if not cls.objects.filter(public_tracking_code=code).exists():
                return code
        return f"CH-{token_hex(6).upper()}"

    def save(self, *args, **kwargs):
        if not self.public_tracking_code:
            self.public_tracking_code = self.generate_public_tracking_code()
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = [*update_fields, "public_tracking_code"]
        super().save(*args, **kwargs)


class PaymentTransaction(BaseModel):
    order = models.ForeignKey("Order", on_delete=models.PROTECT, related_name="payment_transactions")
    user = models.ForeignKey("users.BaseUser", on_delete=models.PROTECT, related_name="payment_transactions")
    provider = models.CharField(max_length=50, default="fake", db_index=True)
    amount = models.DecimalField(max_digits=16, decimal_places=0)
    status = models.CharField(
        max_length=32,
        choices=PaymentTransactionStatus.choices,
        default=PaymentTransactionStatus.CREATED,
        db_index=True,
    )
    gateway_authority = models.CharField(max_length=128, null=True, blank=True)
    gateway_ref_id = models.CharField(max_length=128, null=True, blank=True)
    gateway_trace_no = models.CharField(max_length=128, null=True, blank=True)
    gateway_payment_url = models.URLField(max_length=500, blank=True)
    request_payload = models.JSONField(default=dict, blank=True)
    callback_payload = models.JSONField(default=dict, blank=True)
    verify_payload = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(blank=True)
    idempotency_key = models.CharField(max_length=128, unique=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    checkout = models.ForeignKey(
        "Checkout",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payment_transactions",
    )
    verification_claim_token = models.UUIDField(null=True, blank=True)
    verification_claimed_at = models.DateTimeField(null=True, blank=True)
    result_token_hash = models.CharField(max_length=128, null=True, blank=True, db_index=True)
    provider_reported_amount = models.DecimalField(max_digits=16, decimal_places=0, null=True, blank=True)
    manual_review_reason = models.CharField(
        max_length=40,
        choices=ManualReviewReason.choices,
        null=True,
        blank=True,
    )
    manual_review_message = models.TextField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["order", "status"]),
            models.Index(fields=["user", "status"]),
            models.Index(fields=["provider", "gateway_authority"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "gateway_authority"],
                condition=models.Q(gateway_authority__isnull=False),
                name="unique_payment_provider_authority",
            ),
            models.UniqueConstraint(
                fields=["provider", "gateway_ref_id"],
                condition=models.Q(gateway_ref_id__isnull=False),
                name="unique_payment_provider_ref_id",
            ),
        ]


class OrderItem(BaseModel):
    product = models.ForeignKey("product.Product", on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=1)
    price = models.DecimalField(max_digits=16, decimal_places=0)
    order = models.ForeignKey("Order", on_delete=models.CASCADE, related_name="order_items")


class OrderItemAttachment(BaseModel):
    order_item = models.ForeignKey("OrderItem", on_delete=models.CASCADE)
    attachment = models.ForeignKey("product.Attachment", on_delete=models.PROTECT)


class ImmutableSnapshotMixin(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Commerce snapshots are immutable after creation.")
        self.full_clean()
        return super().save(*args, **kwargs)


class CheckoutLine(ImmutableSnapshotMixin, models.Model):
    checkout = models.ForeignKey("Checkout", on_delete=models.CASCADE, related_name="lines")
    source_cart_item_id = models.PositiveBigIntegerField(null=True, blank=True)
    product_id = models.PositiveBigIntegerField(null=True, blank=True)
    product_name = models.CharField(max_length=200)
    product_sku = models.CharField(max_length=100, null=True, blank=True)
    product_type = models.IntegerField()
    variation_id = models.PositiveBigIntegerField(null=True, blank=True)
    variation_name = models.CharField(max_length=200, null=True, blank=True)
    unit_original_price = models.DecimalField(max_digits=16, decimal_places=0)
    unit_payable_price = models.DecimalField(max_digits=16, decimal_places=0)
    quantity = models.PositiveIntegerField()
    line_original_total = models.DecimalField(max_digits=16, decimal_places=0)
    line_payable_total = models.DecimalField(max_digits=16, decimal_places=0)
    snapshot = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["checkout", "product_id"], name="shop_line_checkout_product")]
        constraints = [
            models.CheckConstraint(check=Q(quantity__gt=0), name="checkout_line_quantity_gt_zero"),
            models.CheckConstraint(check=Q(unit_original_price__gte=0), name="checkout_line_original_gte_zero"),
            models.CheckConstraint(check=Q(unit_payable_price__gte=0), name="checkout_line_payable_gte_zero"),
            models.CheckConstraint(check=Q(line_original_total__gte=0), name="checkout_line_total_orig_gte_zero"),
            models.CheckConstraint(check=Q(line_payable_total__gte=0), name="checkout_line_total_pay_gte_zero"),
        ]

    def clean(self):
        errors = {}
        if self.quantity and self.unit_original_price * self.quantity != self.line_original_total:
            errors["line_original_total"] = "Original total must equal unit price multiplied by quantity."
        if self.quantity and self.unit_payable_price * self.quantity != self.line_payable_total:
            errors["line_payable_total"] = "Payable total must equal unit price multiplied by quantity."
        if errors:
            raise ValidationError(errors)


class CheckoutLineAttachment(ImmutableSnapshotMixin, models.Model):
    checkout_line = models.ForeignKey("CheckoutLine", on_delete=models.CASCADE, related_name="attachments")
    attachment_id = models.PositiveBigIntegerField(null=True, blank=True)
    attachment_type = models.IntegerField()
    name = models.CharField(max_length=200)
    unit_price = models.DecimalField(max_digits=16, decimal_places=0)
    quantity_basis = models.PositiveIntegerField(default=1)
    total_price = models.DecimalField(max_digits=16, decimal_places=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(check=Q(quantity_basis__gt=0), name="checkout_attachment_qty_gt_zero"),
            models.CheckConstraint(check=Q(unit_price__gte=0), name="checkout_attachment_unit_gte_zero"),
            models.CheckConstraint(check=Q(total_price__gte=0), name="checkout_attachment_total_gte_zero"),
        ]

    def clean(self):
        if self.quantity_basis and self.unit_price * self.quantity_basis != self.total_price:
            raise ValidationError({"total_price": "Total must equal unit price multiplied by quantity basis."})


class CheckoutShippingSnapshot(BaseModel):
    checkout = models.OneToOneField("Checkout", on_delete=models.CASCADE, related_name="shipping_snapshot")
    address_id = models.PositiveBigIntegerField(null=True, blank=True)
    recipient_name = models.CharField(max_length=200, blank=True)
    recipient_phone = models.CharField(max_length=32, blank=True)
    province = models.CharField(max_length=100, blank=True)
    city = models.CharField(max_length=200, blank=True)
    full_address = models.TextField(blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    delivery_method_id = models.PositiveBigIntegerField(null=True, blank=True)
    delivery_method_name = models.CharField(max_length=200, blank=True)
    delivery_cost = models.DecimalField(max_digits=16, decimal_places=0, default=0)
    is_pricing_finalized = models.BooleanField(default=False)
    schedule_id = models.PositiveBigIntegerField(null=True, blank=True)
    schedule_start = models.DateTimeField(null=True, blank=True)
    schedule_end = models.DateTimeField(null=True, blank=True)
    snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(check=Q(delivery_cost__gte=0), name="checkout_shipping_cost_gte_zero")
        ]


class StockReservation(BaseModel):
    checkout = models.ForeignKey("Checkout", on_delete=models.CASCADE, related_name="stock_reservations")
    product = models.ForeignKey("product.Product", on_delete=models.PROTECT, related_name="stock_reservations")
    quantity = models.PositiveIntegerField()
    expires_at = models.DateTimeField()
    state = models.CharField(
        max_length=16,
        choices=StockReservationState.choices,
        default=StockReservationState.ACTIVE,
        db_index=True,
    )

    class Meta:
        indexes = [models.Index(fields=["state", "expires_at"], name="shop_res_state_expires")]
        constraints = [
            models.CheckConstraint(check=Q(quantity__gt=0), name="stock_reservation_quantity_gt_zero"),
            models.UniqueConstraint(
                fields=["checkout", "product"],
                condition=Q(state=StockReservationState.ACTIVE),
                name="uniq_active_reservation_product",
            ),
        ]


class CommerceEvent(models.Model):
    checkout = models.ForeignKey("Checkout", on_delete=models.PROTECT, related_name="events")
    order = models.ForeignKey("Order", on_delete=models.PROTECT, null=True, blank=True, related_name="commerce_events")
    payment_transaction = models.ForeignKey(
        "PaymentTransaction",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="commerce_events",
    )
    event_type = models.CharField(max_length=64, choices=CommerceEventType.choices, db_index=True)
    actor_type = models.CharField(max_length=16, choices=CommerceActorType.choices)
    actor_id = models.PositiveBigIntegerField(null=True, blank=True)
    idempotency_reference = models.CharField(max_length=128, null=True, blank=True)
    request_id = models.CharField(max_length=128, null=True, blank=True)
    correlation_id = models.CharField(max_length=128, null=True, blank=True)
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["checkout", "created_at"], name="shop_event_checkout_created"),
            models.Index(fields=["order", "created_at"], name="shop_event_order_created"),
            models.Index(fields=["payment_transaction", "created_at"], name="shop_event_payment_created"),
            models.Index(fields=["event_type", "created_at"], name="shop_event_type_created"),
            models.Index(fields=["idempotency_reference"], name="shop_event_idempotency"),
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Commerce events are append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Commerce events are append-only.")


class Discount(BaseModel):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=100, unique=True)
    type = models.IntegerField(choices=DiscountType.choices())
    value_type = models.IntegerField(choices=DiscountValueType.choices())
    valid_from = models.DateTimeField()
    valid_until = models.DateTimeField()
    is_active = models.BooleanField()
    min_purchase_amount = models.DecimalField(max_digits=16, decimal_places=0)
    amount = models.DecimalField(max_digits=16, decimal_places=0)
    percent = models.PositiveIntegerField()
    admin_user = models.ForeignKey("users.BaseUser", on_delete=models.PROTECT)
    usage_number = models.PositiveIntegerField(default=1)


class UserDiscount(BaseModel):
    discount = models.ForeignKey("Discount", on_delete=models.CASCADE)
    user = models.ForeignKey("users.BaseUser", on_delete=models.CASCADE)
    is_used = models.BooleanField(default=False)

    class Meta:
        unique_together = ("discount", "user")


class DeliverySchedule(BaseModel):
    type = models.IntegerField(choices=DeliveryScheduleType.choices())
    start = models.DateTimeField()
    end = models.DateTimeField()
    capacity = models.PositiveIntegerField()

    def __str__(self):
        return f"{self.start}"


class DeliveryType(BaseModel):
    name = models.CharField(max_length=200)
    delivery_type = models.IntegerField(
        choices=DeliveryOption.choices(),
        default=DeliveryOption.MOTOR,
    )

    side = models.IntegerField(choices=DeliverySide.choices())

    def __str(self):
        return self.name


class DeliveryData(BaseModel):
    type = models.ForeignKey("DeliveryType", on_delete=models.PROTECT)
    schedule = models.ForeignKey("DeliverySchedule", on_delete=models.PROTECT, null=True, blank=True)
    address = models.ForeignKey("users.Address", on_delete=models.PROTECT, null=True, blank=True)
    is_used = models.BooleanField(default=False)
