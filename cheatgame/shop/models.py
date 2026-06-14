from enum import IntEnum
from secrets import token_hex

from django.db import models

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
    PAID = "paid", "PAID"
    FAILED = "failed", "FAILED"


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


class Cart(BaseModel):
    user = models.OneToOneField("users.BaseUser", on_delete=models.CASCADE)


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
    schedule = models.ForeignKey("DeliverySchedule", on_delete=models.PROTECT)
    address = models.ForeignKey("users.Address", on_delete=models.PROTECT, null=True, blank=True)
    is_used = models.BooleanField(default=False)
