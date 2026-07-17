import re
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from cheatgame.common.models import BaseModel
from cheatgame.product.models import DeliveredVersion, NativeConsole, ProductCommerceAuthority


class InventoryPoolStatus(models.TextChoices):
    ENABLED = "enabled", "ENABLED"
    PAUSED = "paused", "PAUSED"
    ARCHIVED = "archived", "ARCHIVED"


class DigitalOfferCapacity(models.TextChoices):
    CAPACITY_1 = "capacity_1", "CAPACITY_1"
    CAPACITY_2 = "capacity_2", "CAPACITY_2"
    CAPACITY_3 = "capacity_3", "CAPACITY_3"


class DigitalOfferSaleState(models.TextChoices):
    DRAFT = "draft", "DRAFT"
    ACTIVE = "active", "ACTIVE"
    PAUSED = "paused", "PAUSED"
    HIDDEN = "hidden", "HIDDEN"
    ARCHIVED = "archived", "ARCHIVED"


class PoolStockAdjustmentReason(models.TextChoices):
    INVENTORY_RECEIVED = "inventory_received", "INVENTORY_RECEIVED"
    RECONCILIATION = "reconciliation", "RECONCILIATION"
    OPERATIONAL_CORRECTION = "operational_correction", "OPERATIONAL_CORRECTION"
    MARK_UNAVAILABLE = "mark_unavailable", "MARK_UNAVAILABLE"
    RETURN_TO_STOCK = "return_to_stock", "RETURN_TO_STOCK"


class DigitalCartFulfillmentMethod(models.TextChoices):
    IN_STORE = "in_store", "IN_STORE"
    REMOTE = "remote", "REMOTE"


class CompatibilityDisclosure(models.TextChoices):
    NATIVE_VERSION_V1 = "native_version_v1", "NATIVE_VERSION_V1"
    PS4_ON_PS5_BACKWARD_COMPATIBLE_V1 = (
        "ps4_on_ps5_backward_compatible_v1",
        "PS4_ON_PS5_BACKWARD_COMPATIBLE_V1",
    )


class CapacityDisclosure(models.TextChoices):
    CAPACITY_1_OFFLINE_IN_STORE_V1 = (
        "capacity_1_offline_in_store_v1",
        "CAPACITY_1_OFFLINE_IN_STORE_V1",
    )
    CAPACITY_2_ONLINE_OFFLINE_FLEXIBLE_V1 = (
        "capacity_2_online_offline_flexible_v1",
        "CAPACITY_2_ONLINE_OFFLINE_FLEXIBLE_V1",
    )
    CAPACITY_3_ONLINE_FLEXIBLE_V1 = (
        "capacity_3_online_flexible_v1",
        "CAPACITY_3_ONLINE_FLEXIBLE_V1",
    )


class DigitalInventoryReservationState(models.TextChoices):
    ACTIVE = "active", "ACTIVE"
    PAYMENT_HOLD = "payment_hold", "PAYMENT_HOLD"
    HELD_FOR_REVIEW = "held_for_review", "HELD_FOR_REVIEW"
    CONSUMED = "consumed", "CONSUMED"
    RELEASED = "released", "RELEASED"
    EXPIRED = "expired", "EXPIRED"


class DigitalFulfillmentStatus(models.TextChoices):
    QUEUED = "queued", "QUEUED"
    WAITING_CUSTOMER = "waiting_customer", "WAITING_CUSTOMER"
    READY_FOR_STAFF = "ready_for_staff", "READY_FOR_STAFF"
    IN_PROGRESS = "in_progress", "IN_PROGRESS"
    WAITING_CONFIRMATION = "waiting_confirmation", "WAITING_CONFIRMATION"
    COMPLETED = "completed", "COMPLETED"
    EXCEPTION = "exception", "EXCEPTION"


DIGITAL_FULFILLMENT_ALLOWED_TRANSITIONS = {
    DigitalFulfillmentStatus.QUEUED: {
        DigitalFulfillmentStatus.WAITING_CUSTOMER,
        DigitalFulfillmentStatus.EXCEPTION,
    },
    DigitalFulfillmentStatus.WAITING_CUSTOMER: {
        DigitalFulfillmentStatus.READY_FOR_STAFF,
        DigitalFulfillmentStatus.IN_PROGRESS,
        DigitalFulfillmentStatus.EXCEPTION,
    },
    DigitalFulfillmentStatus.READY_FOR_STAFF: {
        DigitalFulfillmentStatus.IN_PROGRESS,
        DigitalFulfillmentStatus.EXCEPTION,
    },
    DigitalFulfillmentStatus.IN_PROGRESS: {
        DigitalFulfillmentStatus.WAITING_CONFIRMATION,
        DigitalFulfillmentStatus.COMPLETED,
        DigitalFulfillmentStatus.EXCEPTION,
    },
    DigitalFulfillmentStatus.WAITING_CONFIRMATION: {
        DigitalFulfillmentStatus.COMPLETED,
        DigitalFulfillmentStatus.EXCEPTION,
    },
    DigitalFulfillmentStatus.EXCEPTION: {
        DigitalFulfillmentStatus.QUEUED,
        DigitalFulfillmentStatus.WAITING_CUSTOMER,
        DigitalFulfillmentStatus.READY_FOR_STAFF,
    },
    DigitalFulfillmentStatus.COMPLETED: set(),
}


class DigitalFulfillmentWaitingReason(models.TextChoices):
    CONTACT_REQUIRED = "contact_required", "CONTACT_REQUIRED"
    METHOD_CONFIRMATION_REQUIRED = "method_confirmation_required", "METHOD_CONFIRMATION_REQUIRED"
    APPOINTMENT_REQUIRED = "appointment_required", "APPOINTMENT_REQUIRED"
    WAITING_FOR_CONSOLE = "waiting_for_console", "WAITING_FOR_CONSOLE"
    REMOTE_CUSTOMER_ACTION_REQUIRED = "remote_customer_action_required", "REMOTE_CUSTOMER_ACTION_REQUIRED"
    CUSTOMER_CONFIRMATION_REQUIRED = "customer_confirmation_required", "CUSTOMER_CONFIRMATION_REQUIRED"
    ADDITIONAL_INFORMATION_REQUIRED = "additional_information_required", "ADDITIONAL_INFORMATION_REQUIRED"


class FulfillmentActivityType(models.TextChoices):
    STATUS_CHANGED = "status_changed", "STATUS_CHANGED"
    OPERATOR_ASSIGNED = "operator_assigned", "OPERATOR_ASSIGNED"
    CUSTOMER_CONTACT_ATTEMPTED = "customer_contact_attempted", "CUSTOMER_CONTACT_ATTEMPTED"
    CUSTOMER_CONTACTED = "customer_contacted", "CUSTOMER_CONTACTED"
    METHOD_CHANGED = "method_changed", "METHOD_CHANGED"
    CONSOLE_RECEIVED = "console_received", "CONSOLE_RECEIVED"
    WORK_STARTED = "work_started", "WORK_STARTED"
    INSTALLATION_PERFORMED = "installation_performed", "INSTALLATION_PERFORMED"
    REMOTE_HANDLING_PERFORMED = "remote_handling_performed", "REMOTE_HANDLING_PERFORMED"
    CUSTOMER_ACTION_REQUESTED = "customer_action_requested", "CUSTOMER_ACTION_REQUESTED"
    CUSTOMER_CONFIRMED = "customer_confirmed", "CUSTOMER_CONFIRMED"
    STAFF_VERIFIED = "staff_verified", "STAFF_VERIFIED"
    FAILURE_RECORDED = "failure_recorded", "FAILURE_RECORDED"
    RETRY_STARTED = "retry_started", "RETRY_STARTED"
    NOTE_ADDED = "note_added", "NOTE_ADDED"
    BONUS_RECORDED = "bonus_recorded", "BONUS_RECORDED"
    PROVISIONED = "provisioned", "PROVISIONED"


class FulfillmentActivityActorType(models.TextChoices):
    CUSTOMER = "customer", "CUSTOMER"
    STAFF = "staff", "STAFF"
    SYSTEM = "system", "SYSTEM"


class FulfillmentActorAuthority(models.TextChoices):
    SYSTEM = "system", "SYSTEM"
    CUSTOMER_OWNER = "customer_owner", "CUSTOMER_OWNER"
    ASSIGNED_OPERATOR = "assigned_operator", "ASSIGNED_OPERATOR"
    UNASSIGNED_STAFF = "unassigned_staff", "UNASSIGNED_STAFF"
    ADMIN_OVERRIDE = "admin_override", "ADMIN_OVERRIDE"


class FulfillmentActivityVisibility(models.TextChoices):
    CUSTOMER_SAFE = "customer_safe", "CUSTOMER_SAFE"
    INTERNAL = "internal", "INTERNAL"


class DigitalEntitlementStatus(models.TextChoices):
    PENDING_FULFILLMENT = "pending_fulfillment", "PENDING_FULFILLMENT"
    ACTIVE = "active", "ACTIVE"


class InstalledGameClassification(models.TextChoices):
    PURCHASED = "purchased", "PURCHASED"
    BONUS = "bonus", "BONUS"


class InstalledGameCompletionSource(models.TextChoices):
    STAFF_INSTALLED = "staff_installed", "STAFF_INSTALLED"
    CUSTOMER_CONFIRMED = "customer_confirmed", "CUSTOMER_CONFIRMED"
    STAFF_VERIFIED_REMOTE = "staff_verified_remote", "STAFF_VERIFIED_REMOTE"


class InstalledGameRecordState(models.TextChoices):
    RECORDED = "recorded", "RECORDED"
    REMOVED = "removed", "REMOVED"


_FULFILLMENT_SECRET_PATTERN = re.compile(
    r"password|passcode|credential|otp|2fa|recovery\s*code|account\s*(email|login)|"
    r"login\s*(email|url)|cookie|token|secret",
    re.IGNORECASE,
)


def validate_fulfillment_safe_text(value):
    if _FULFILLMENT_SECRET_PATTERN.search(str(value or "")):
        raise ValidationError("Credential-like material is prohibited.")


class InventoryPool(BaseModel):
    """Authoritative total sellable Digital stock; Batch A has no held quantity."""

    sellable_quantity = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=16,
        choices=InventoryPoolStatus.choices,
        default=InventoryPoolStatus.PAUSED,
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=Q(sellable_quantity__gte=0),
                name="inventory_pool_quantity_gte_zero",
            ),
            models.CheckConstraint(
                check=Q(status__in=InventoryPoolStatus.values),
                name="inventory_pool_status_valid",
            ),
        ]


class DigitalOffer(BaseModel):
    delivered_version = models.ForeignKey(
        DeliveredVersion,
        on_delete=models.PROTECT,
        related_name="digital_offers",
    )
    customer_console = models.CharField(max_length=10, choices=NativeConsole.choices)
    capacity = models.CharField(max_length=16, choices=DigitalOfferCapacity.choices)
    price = models.DecimalField(max_digits=15, decimal_places=0)
    inventory_pool = models.ForeignKey(
        InventoryPool,
        on_delete=models.PROTECT,
        related_name="digital_offers",
    )
    sale_state = models.CharField(
        max_length=16,
        choices=DigitalOfferSaleState.choices,
        default=DigitalOfferSaleState.DRAFT,
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=Q(customer_console__in=NativeConsole.values),
                name="digital_offer_console_valid",
            ),
            models.CheckConstraint(
                check=Q(capacity__in=DigitalOfferCapacity.values),
                name="digital_offer_capacity_valid",
            ),
            models.CheckConstraint(check=Q(price__gte=0), name="digital_offer_price_gte_zero"),
            models.CheckConstraint(
                check=Q(sale_state__in=DigitalOfferSaleState.values),
                name="digital_offer_sale_state_valid",
            ),
            models.UniqueConstraint(
                fields=("delivered_version", "customer_console", "capacity"),
                condition=~Q(sale_state=DigitalOfferSaleState.ARCHIVED),
                name="digital_offer_unique_nonarchived",
            ),
        ]

    @property
    def game(self):
        return self.delivered_version.product

    def clean(self):
        super().clean()
        if not self.delivered_version_id:
            return
        if not self.delivered_version.is_active and self.sale_state == DigitalOfferSaleState.ACTIVE:
            raise ValidationError({"delivered_version": "An active Offer requires an active version."})
        if self.customer_console == NativeConsole.PS4 and self.delivered_version.native_console != NativeConsole.PS4:
            raise ValidationError({"delivered_version": "A PS4 customer requires a PS4 delivered version."})
        if (
            self.sale_state == DigitalOfferSaleState.ACTIVE
            and self.delivered_version.product.commerce_authority
            != ProductCommerceAuthority.DIGITAL_PRODUCTS
        ):
            raise ValidationError({"sale_state": "Active Offers require DIGITAL_PRODUCTS authority."})
        if self.inventory_pool_id:
            incompatible = DigitalOffer.objects.filter(inventory_pool_id=self.inventory_pool_id).exclude(pk=self.pk).exclude(
                delivered_version_id=self.delivered_version_id,
                capacity=self.capacity,
            )
            if incompatible.exists():
                raise ValidationError({"inventory_pool": "Shared Pools require the same version and capacity."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PoolStockAdjustment(models.Model):
    inventory_pool = models.ForeignKey(
        InventoryPool,
        on_delete=models.PROTECT,
        related_name="stock_adjustments",
    )
    delta = models.IntegerField()
    previous_quantity = models.PositiveIntegerField()
    resulting_quantity = models.PositiveIntegerField()
    reason = models.CharField(max_length=32, choices=PoolStockAdjustmentReason.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="digital_stock_adjustments",
    )
    idempotency_key = models.UUIDField(unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        constraints = [
            models.CheckConstraint(check=~Q(delta=0), name="pool_adjustment_delta_nonzero"),
            models.CheckConstraint(check=Q(previous_quantity__gte=0), name="pool_adjustment_previous_gte_zero"),
            models.CheckConstraint(check=Q(resulting_quantity__gte=0), name="pool_adjustment_result_gte_zero"),
            models.CheckConstraint(
                check=Q(resulting_quantity=F("previous_quantity") + F("delta")),
                name="pool_adjustment_quantity_equation",
            ),
            models.CheckConstraint(
                check=Q(reason__in=PoolStockAdjustmentReason.values),
                name="pool_adjustment_reason_valid",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Pool stock adjustments are append-only.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Pool stock adjustments are append-only.")


class DigitalCartSelection(BaseModel):
    cart_item = models.OneToOneField(
        "shop.CartItem",
        on_delete=models.CASCADE,
        related_name="digital_selection",
    )
    offer = models.ForeignKey(
        DigitalOffer,
        on_delete=models.PROTECT,
        related_name="cart_selections",
    )
    fulfillment_method = models.CharField(
        max_length=16,
        choices=DigitalCartFulfillmentMethod.choices,
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=Q(fulfillment_method__in=DigitalCartFulfillmentMethod.values),
                name="digital_cart_method_valid",
            ),
        ]

    def clean(self):
        super().clean()
        if not self.cart_item_id or not self.offer_id:
            return
        from cheatgame.shop.models import CartItemAttachment

        if self.cart_item.commerce_authority != ProductCommerceAuthority.DIGITAL_PRODUCTS:
            raise ValidationError({"cart_item": "Digital selections require DIGITAL_PRODUCTS authority."})
        if self.cart_item.product_id != self.offer.delivered_version.product_id:
            raise ValidationError({"offer": "Offer product does not match the CartItem product."})
        if self.cart_item.quantity != 1:
            raise ValidationError({"cart_item": "Digital CartItem quantity must be one."})
        if self.offer.delivered_version.product.commerce_authority != ProductCommerceAuthority.DIGITAL_PRODUCTS:
            raise ValidationError({"offer": "Offer Product is not enabled for Digital Products."})
        if self.offer.sale_state != DigitalOfferSaleState.ACTIVE:
            raise ValidationError({"offer": "Digital Offer must be active."})
        if not self.offer.delivered_version.is_active:
            raise ValidationError({"offer": "Delivered Version must be active."})
        if self.offer.inventory_pool.status == InventoryPoolStatus.ARCHIVED:
            raise ValidationError({"offer": "Archived Inventory Pools are not selectable."})
        if CartItemAttachment.objects.filter(cart_item_id=self.cart_item_id).exists():
            raise ValidationError({"cart_item": "Digital CartItems cannot use Standard attachments."})
        if (
            self.offer.capacity == DigitalOfferCapacity.CAPACITY_1
            and self.fulfillment_method != DigitalCartFulfillmentMethod.IN_STORE
        ):
            raise ValidationError({"fulfillment_method": "Capacity 1 requires in-store fulfillment."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class DigitalCheckoutLineSnapshot(models.Model):
    checkout_line = models.OneToOneField(
        "shop.CheckoutLine",
        on_delete=models.CASCADE,
        related_name="digital_snapshot",
    )
    offer = models.ForeignKey(DigitalOffer, on_delete=models.PROTECT, related_name="checkout_snapshots")
    inventory_pool = models.ForeignKey(
        InventoryPool,
        on_delete=models.PROTECT,
        related_name="checkout_snapshots",
    )
    delivered_version = models.ForeignKey(
        DeliveredVersion,
        on_delete=models.PROTECT,
        related_name="digital_checkout_snapshots",
    )
    product_id = models.PositiveBigIntegerField()
    product_name = models.CharField(max_length=200)
    commerce_authority = models.CharField(
        max_length=30,
        choices=ProductCommerceAuthority.choices,
        default=ProductCommerceAuthority.DIGITAL_PRODUCTS,
    )
    customer_console = models.CharField(max_length=10, choices=NativeConsole.choices)
    capacity = models.CharField(max_length=16, choices=DigitalOfferCapacity.choices)
    fulfillment_method = models.CharField(max_length=16, choices=DigitalCartFulfillmentMethod.choices)
    version_label = models.CharField(max_length=32)
    native_console = models.CharField(max_length=10, choices=NativeConsole.choices)
    compatibility_disclosure = models.CharField(max_length=48, choices=CompatibilityDisclosure.choices)
    capacity_disclosure = models.CharField(max_length=48, choices=CapacityDisclosure.choices)
    unit_price = models.DecimalField(max_digits=15, decimal_places=0)
    quantity = models.PositiveIntegerField(default=1)
    line_total = models.DecimalField(max_digits=16, decimal_places=0)
    safe_display_metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        constraints = [
            models.CheckConstraint(check=Q(commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS), name="digital_snapshot_authority"),
            models.CheckConstraint(check=Q(customer_console__in=NativeConsole.values), name="digital_checkout_console_valid"),
            models.CheckConstraint(check=Q(capacity__in=DigitalOfferCapacity.values), name="digital_checkout_capacity_valid"),
            models.CheckConstraint(check=Q(fulfillment_method__in=DigitalCartFulfillmentMethod.values), name="digital_checkout_method_valid"),
            models.CheckConstraint(check=Q(native_console__in=NativeConsole.values), name="digital_checkout_native_console_valid"),
            models.CheckConstraint(check=Q(compatibility_disclosure__in=CompatibilityDisclosure.values), name="digital_checkout_compat_valid"),
            models.CheckConstraint(check=Q(capacity_disclosure__in=CapacityDisclosure.values), name="digital_checkout_capacity_rule_valid"),
            models.CheckConstraint(check=~Q(version_label=""), name="digital_checkout_version_label_nonempty"),
            models.CheckConstraint(check=Q(unit_price__gte=0), name="digital_snapshot_unit_price_gte_zero"),
            models.CheckConstraint(check=Q(quantity=1), name="digital_snapshot_quantity_one"),
            models.CheckConstraint(check=Q(line_total=F("unit_price") * F("quantity")), name="digital_snapshot_total_equation"),
        ]

    def clean(self):
        super().clean()
        if not self.checkout_line_id or not self.offer_id:
            return
        if self.checkout_line.commerce_authority != ProductCommerceAuthority.DIGITAL_PRODUCTS:
            raise ValidationError({"checkout_line": "Digital snapshot requires DIGITAL_PRODUCTS authority."})
        if self.checkout_line.product_id != self.product_id or self.product_id != self.offer.delivered_version.product_id:
            raise ValidationError({"product_id": "Snapshot Product identity is inconsistent."})
        if self.checkout_line.quantity != self.quantity or self.quantity != 1:
            raise ValidationError({"quantity": "Digital snapshot quantity must match its CheckoutLine."})
        if self.checkout_line.unit_payable_price != self.unit_price or self.checkout_line.line_payable_total != self.line_total:
            raise ValidationError({"unit_price": "Snapshot pricing must match its CheckoutLine."})
        if self.checkout_line.attachments.exists():
            raise ValidationError({"checkout_line": "Digital CheckoutLines cannot use Standard attachments."})
        if self.inventory_pool_id != self.offer.inventory_pool_id:
            raise ValidationError({"inventory_pool": "Snapshot Pool must match the Offer Pool."})
        if self.delivered_version_id != self.offer.delivered_version_id:
            raise ValidationError({"delivered_version": "Snapshot version must match the Offer version."})
        if (self.customer_console, self.capacity) != (self.offer.customer_console, self.offer.capacity):
            raise ValidationError("Snapshot console and capacity must match the Offer.")
        if self.native_console != self.delivered_version.native_console:
            raise ValidationError({"native_console": "Native console must match the Delivered Version."})
        if any(
            fragment in str(key).lower()
            for key in self.safe_display_metadata
            for fragment in ("password", "secret", "token", "credential", "pool", "stock")
        ):
            raise ValidationError({"safe_display_metadata": "Unsafe metadata key."})

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Digital Checkout snapshots are immutable after creation.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Digital Checkout snapshots are immutable after creation.")


class DigitalInventoryReservation(BaseModel):
    checkout = models.ForeignKey(
        "shop.Checkout",
        on_delete=models.PROTECT,
        related_name="digital_inventory_reservations",
    )
    checkout_line = models.OneToOneField(
        "shop.CheckoutLine",
        on_delete=models.PROTECT,
        related_name="digital_inventory_reservation",
    )
    order = models.ForeignKey(
        "shop.Order",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="digital_inventory_reservations",
    )
    inventory_pool = models.ForeignKey(
        InventoryPool,
        on_delete=models.PROTECT,
        related_name="reservations",
    )
    quantity = models.PositiveIntegerField(default=1)
    state = models.CharField(
        max_length=24,
        choices=DigitalInventoryReservationState.choices,
        default=DigitalInventoryReservationState.ACTIVE,
    )
    expires_at = models.DateTimeField()
    state_changed_at = models.DateTimeField(default=timezone.now)
    idempotency_key = models.UUIDField(unique=True, editable=False)
    resolution_reason = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=("state", "expires_at"), name="digital_res_state_expires"),
            models.Index(fields=("inventory_pool", "state"), name="digital_res_pool_state"),
        ]
        constraints = [
            models.CheckConstraint(check=Q(quantity=1), name="digital_res_quantity_one"),
            models.CheckConstraint(check=Q(state__in=DigitalInventoryReservationState.values), name="digital_res_state_valid"),
            models.CheckConstraint(check=Q(expires_at__gt=F("created_at")), name="digital_res_expiry_after_created"),
        ]

    def clean(self):
        super().clean()
        if not self.checkout_line_id:
            return
        if self.checkout_line.checkout_id != self.checkout_id:
            raise ValidationError({"checkout_line": "Reservation line must belong to Checkout."})
        try:
            snapshot = self.checkout_line.digital_snapshot
        except DigitalCheckoutLineSnapshot.DoesNotExist as exc:
            raise ValidationError({"checkout_line": "Digital reservation requires a snapshot."}) from exc
        if snapshot.inventory_pool_id != self.inventory_pool_id:
            raise ValidationError({"inventory_pool": "Reservation Pool must match the snapshot."})
        if self.expires_at != self.checkout.expires_at:
            raise ValidationError({"expires_at": "Reservation expiry must match Checkout expiry."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class DigitalFulfillmentItem(BaseModel):
    """Mutable operational execution; commercial identity lives only on obligation."""

    public_id = models.UUIDField(default=uuid4, unique=True, editable=False)
    obligation = models.OneToOneField(
        "financial_core.DigitalFulfillmentObligation",
        on_delete=models.PROTECT,
        related_name="execution",
    )
    status = models.CharField(max_length=24, choices=DigitalFulfillmentStatus.choices, default=DigitalFulfillmentStatus.QUEUED)
    waiting_reason = models.CharField(max_length=40, choices=DigitalFulfillmentWaitingReason.choices, null=True, blank=True)
    assigned_operator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assigned_digital_fulfillments",
    )
    current_fulfillment_method = models.CharField(max_length=16, choices=DigitalCartFulfillmentMethod.choices)
    appointment = models.ForeignKey(
        "shop.DeliveryData", on_delete=models.PROTECT, null=True, blank=True,
        related_name="digital_fulfillment_items",
    )
    internal_reference = models.CharField(max_length=100, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=("status", "waiting_reason"), name="digital_fulfill_queue"),
            models.Index(fields=("assigned_operator", "status"), name="digital_fulfill_operator"),
        ]
        constraints = [
            models.CheckConstraint(check=Q(status__in=DigitalFulfillmentStatus.values), name="digital_fulfill_status_valid"),
            models.CheckConstraint(check=Q(current_fulfillment_method__in=DigitalCartFulfillmentMethod.values), name="digital_fulfill_method_valid"),
            models.CheckConstraint(
                check=Q(waiting_reason__isnull=True) | Q(waiting_reason__in=DigitalFulfillmentWaitingReason.values),
                name="digital_fulfill_wait_reason_valid",
            ),
            models.CheckConstraint(
                check=(Q(status=DigitalFulfillmentStatus.COMPLETED, completed_at__isnull=False)
                       | (~Q(status=DigitalFulfillmentStatus.COMPLETED) & Q(completed_at__isnull=True))),
                name="digital_fulfill_completed_time",
            ),
        ]

    def clean(self):
        super().clean()
        if not self.obligation_id:
            return
        snapshot = self.obligation.checkout_line.digital_snapshot
        if self._state.adding and self.current_fulfillment_method != self.obligation.fulfillment_method:
            raise ValidationError({"current_fulfillment_method": "Initial method must match the immutable obligation."})
        if snapshot.capacity == DigitalOfferCapacity.CAPACITY_1 and self.current_fulfillment_method != DigitalCartFulfillmentMethod.IN_STORE:
            raise ValidationError({"current_fulfillment_method": "Capacity 1 requires in-store fulfillment."})
        if self.status == DigitalFulfillmentStatus.COMPLETED and self.completed_at is None:
            raise ValidationError({"completed_at": "Completed fulfillment requires a timestamp."})
        if self.status != DigitalFulfillmentStatus.COMPLETED and self.completed_at is not None:
            raise ValidationError({"completed_at": "Only completed fulfillment may have a timestamp."})
        validate_fulfillment_safe_text(self.internal_reference)

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values(
                "public_id", "obligation_id", "status", "completed_at", "started_at",
                "current_fulfillment_method",
            ).first()
            if original and (original["public_id"] != self.public_id or original["obligation_id"] != self.obligation_id):
                raise ValidationError("Fulfillment commercial ownership is immutable.")
            if original and original["status"] == DigitalFulfillmentStatus.COMPLETED:
                if self.status != DigitalFulfillmentStatus.COMPLETED or self.completed_at != original["completed_at"]:
                    raise ValidationError("Completed fulfillment is permanent.")
            if original and original["started_at"] is not None and self.started_at != original["started_at"]:
                raise ValidationError("Work-start time is immutable once recorded.")
            if original and original["status"] != self.status:
                if self.status not in DIGITAL_FULFILLMENT_ALLOWED_TRANSITIONS[original["status"]]:
                    raise ValidationError("Illegal Digital fulfillment status transition.")
            if original and original["current_fulfillment_method"] != self.current_fulfillment_method:
                if original["status"] not in (
                    DigitalFulfillmentStatus.QUEUED,
                    DigitalFulfillmentStatus.WAITING_CUSTOMER,
                ):
                    raise ValidationError("Fulfillment method cannot change after operational evidence begins.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Finalized fulfillment execution cannot be deleted.")


class FulfillmentActivity(models.Model):
    fulfillment_item = models.ForeignKey(DigitalFulfillmentItem, on_delete=models.PROTECT, related_name="activities")
    activity_type = models.CharField(max_length=40, choices=FulfillmentActivityType.choices)
    actor_type = models.CharField(max_length=16, choices=FulfillmentActivityActorType.choices)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="digital_fulfillment_activities")
    actor_authority = models.CharField(max_length=24, choices=FulfillmentActorAuthority.choices)
    visibility = models.CharField(max_length=16, choices=FulfillmentActivityVisibility.choices)
    previous_status = models.CharField(max_length=24, choices=DigitalFulfillmentStatus.choices, null=True, blank=True)
    new_status = models.CharField(max_length=24, choices=DigitalFulfillmentStatus.choices, null=True, blank=True)
    waiting_reason = models.CharField(max_length=40, choices=DigitalFulfillmentWaitingReason.choices, null=True, blank=True)
    note = models.CharField(max_length=1000, blank=True)
    idempotency_key = models.UUIDField(null=True, blank=True, editable=False)
    request_fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=("fulfillment_item", "created_at"), name="digital_activity_timeline")]
        constraints = [
            models.CheckConstraint(check=Q(activity_type__in=FulfillmentActivityType.values), name="digital_activity_type_valid"),
            models.CheckConstraint(check=Q(actor_type__in=FulfillmentActivityActorType.values), name="digital_activity_actor_type_valid"),
            models.CheckConstraint(check=Q(actor_authority__in=FulfillmentActorAuthority.values), name="digital_activity_authority_valid"),
            models.CheckConstraint(check=Q(visibility__in=FulfillmentActivityVisibility.values), name="digital_activity_visibility_valid"),
            models.CheckConstraint(
                check=(Q(activity_type=FulfillmentActivityType.STATUS_CHANGED, new_status__isnull=False)
                       | (~Q(activity_type=FulfillmentActivityType.STATUS_CHANGED) & Q(previous_status__isnull=True, new_status__isnull=True))),
                name="digital_activity_status_fields",
            ),
            models.CheckConstraint(
                check=Q(previous_status__isnull=True) | Q(new_status__isnull=True) | ~Q(previous_status=F("new_status")),
                name="digital_activity_status_changed",
            ),
            models.CheckConstraint(
                check=(Q(actor_type=FulfillmentActivityActorType.SYSTEM, actor__isnull=True)
                       | Q(actor_type__in=(FulfillmentActivityActorType.CUSTOMER, FulfillmentActivityActorType.STAFF), actor__isnull=False)),
                name="digital_activity_actor_presence",
            ),
            models.UniqueConstraint(fields=("idempotency_key",), condition=Q(idempotency_key__isnull=False), name="digital_activity_idempotency_unique"),
            models.UniqueConstraint(
                fields=("fulfillment_item",), condition=Q(activity_type=FulfillmentActivityType.PROVISIONED),
                name="digital_one_provisioned_activity",
            ),
            models.CheckConstraint(check=~Q(request_fingerprint=""), name="digital_activity_fingerprint_nonempty"),
        ]

    def clean(self):
        super().clean()
        if self.actor_type == FulfillmentActivityActorType.SYSTEM:
            if self.actor_id or self.actor_authority != FulfillmentActorAuthority.SYSTEM:
                raise ValidationError({"actor_authority": "System activity requires bounded system authority."})
        elif self.actor_type == FulfillmentActivityActorType.CUSTOMER and self.actor_id:
            if (
                self.actor_id != self.fulfillment_item.obligation.order.user_id
                or self.actor.user_type != 1
                or not self.actor.is_active
                or self.actor_authority != FulfillmentActorAuthority.CUSTOMER_OWNER
            ):
                raise ValidationError({"actor": "Customer actor must be the active obligation owner."})
        elif self.actor_type == FulfillmentActivityActorType.STAFF and self.actor_id:
            if self.actor.user_type not in (2, 3) or not self.actor.is_active:
                raise ValidationError({"actor": "Staff activity requires active authorized staff."})
            assigned_id = self.fulfillment_item.assigned_operator_id
            valid_authority = (
                self.actor_authority == FulfillmentActorAuthority.ASSIGNED_OPERATOR and assigned_id == self.actor_id
            ) or (
                self.actor_authority == FulfillmentActorAuthority.UNASSIGNED_STAFF and assigned_id is None
            ) or (
                self.actor_authority == FulfillmentActorAuthority.ADMIN_OVERRIDE
                and self.actor.user_type == 2 and assigned_id is not None and assigned_id != self.actor_id
            )
            if not valid_authority:
                raise ValidationError({"actor_authority": "Staff authority does not match the execution assignment."})
        if self.activity_type == FulfillmentActivityType.STATUS_CHANGED and not self.new_status:
            raise ValidationError({"new_status": "Status activity requires its new state."})
        if self.activity_type != FulfillmentActivityType.STATUS_CHANGED and (self.previous_status or self.new_status):
            raise ValidationError("Only status activities may contain status fields.")
        validate_fulfillment_safe_text(self.note)

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Fulfillment activities are append-only.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Fulfillment activities are append-only.")


class Entitlement(BaseModel):
    obligation = models.OneToOneField("financial_core.DigitalFulfillmentObligation", on_delete=models.PROTECT, related_name="entitlement")
    fulfillment_item = models.OneToOneField(DigitalFulfillmentItem, on_delete=models.PROTECT, related_name="entitlement")
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="digital_entitlements")
    status = models.CharField(max_length=24, choices=DigitalEntitlementStatus.choices, default=DigitalEntitlementStatus.PENDING_FULFILLMENT)
    activated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=("customer", "status"), name="digital_entitlement_customer")]
        constraints = [
            models.CheckConstraint(check=Q(status__in=DigitalEntitlementStatus.values), name="digital_entitlement_status_valid"),
            models.CheckConstraint(
                check=(Q(status=DigitalEntitlementStatus.PENDING_FULFILLMENT, activated_at__isnull=True)
                       | Q(status=DigitalEntitlementStatus.ACTIVE, activated_at__isnull=False)),
                name="digital_entitlement_lifecycle_fields",
            ),
        ]

    def clean(self):
        super().clean()
        if not all((self.obligation_id, self.fulfillment_item_id, self.customer_id)):
            return
        if self.fulfillment_item.obligation_id != self.obligation_id:
            raise ValidationError({"fulfillment_item": "Entitlement and execution must share one obligation."})
        if self.customer_id != self.obligation.order.user_id:
            raise ValidationError({"customer": "Entitlement customer must own the obligation Order."})
        if self.status == DigitalEntitlementStatus.ACTIVE and self.fulfillment_item.status != DigitalFulfillmentStatus.COMPLETED:
            raise ValidationError({"status": "Entitlement activation requires completed fulfillment."})

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values(
                "obligation_id", "fulfillment_item_id", "customer_id", "status", "activated_at",
            ).first()
            if original and any(original[k] != getattr(self, k) for k in ("obligation_id", "fulfillment_item_id", "customer_id")):
                raise ValidationError("Entitlement commercial ownership is immutable.")
            if original and original["status"] == DigitalEntitlementStatus.ACTIVE:
                if self.status != DigitalEntitlementStatus.ACTIVE or self.activated_at != original["activated_at"]:
                    raise ValidationError("Active entitlement is permanent.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Permanent ownership records cannot be deleted.")


class InstalledGameRecord(models.Model):
    fulfillment_item = models.ForeignKey(DigitalFulfillmentItem, on_delete=models.PROTECT, related_name="installed_games")
    game = models.ForeignKey("product.Product", on_delete=models.PROTECT, null=True, blank=True, related_name="digital_installation_records")
    delivered_version = models.ForeignKey(DeliveredVersion, on_delete=models.PROTECT, null=True, blank=True, related_name="installation_records")
    classification = models.CharField(max_length=16, choices=InstalledGameClassification.choices)
    completion_source = models.CharField(max_length=24, choices=InstalledGameCompletionSource.choices)
    operator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="digital_installed_game_records")
    actor_authority = models.CharField(max_length=24, choices=FulfillmentActorAuthority.choices)
    installed_at = models.DateTimeField(default=timezone.now)
    fallback_title = models.CharField(max_length=200, blank=True)
    state = models.CharField(max_length=16, choices=InstalledGameRecordState.choices, default=InstalledGameRecordState.RECORDED)
    corrects = models.OneToOneField("self", on_delete=models.PROTECT, null=True, blank=True, related_name="superseded_by")
    correction_reason = models.CharField(max_length=500, blank=True)
    idempotency_key = models.UUIDField(unique=True, editable=False)
    request_fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=("fulfillment_item", "created_at"), name="digital_installed_timeline")]
        constraints = [
            models.CheckConstraint(check=Q(classification__in=InstalledGameClassification.values), name="digital_installed_class_valid"),
            models.CheckConstraint(check=Q(completion_source__in=InstalledGameCompletionSource.values), name="digital_installed_source_valid"),
            models.CheckConstraint(check=Q(state__in=InstalledGameRecordState.values), name="digital_installed_state_valid"),
            models.CheckConstraint(check=Q(game__isnull=False) | ~Q(fallback_title=""), name="digital_installed_identity_required"),
            models.CheckConstraint(check=~Q(classification=InstalledGameClassification.PURCHASED) | Q(game__isnull=False, delivered_version__isnull=False), name="digital_installed_purchased_identity"),
            models.CheckConstraint(check=Q(completion_source=InstalledGameCompletionSource.CUSTOMER_CONFIRMED) | Q(operator__isnull=False), name="digital_installed_staff_operator"),
            models.CheckConstraint(
                check=(Q(corrects__isnull=True, correction_reason="")
                       | Q(corrects__isnull=False) & ~Q(correction_reason="")),
                name="digital_installed_correction_fields",
            ),
            models.CheckConstraint(check=~Q(request_fingerprint=""), name="digital_installed_fingerprint_nonempty"),
            models.CheckConstraint(check=Q(actor_authority__in=FulfillmentActorAuthority.values), name="digital_installed_authority_valid"),
        ]

    def clean(self):
        super().clean()
        if self.classification == InstalledGameClassification.PURCHASED and self.fulfillment_item_id:
            snapshot = self.fulfillment_item.obligation.checkout_line.digital_snapshot
            if self.game_id != snapshot.product_id or self.delivered_version_id != snapshot.delivered_version_id:
                raise ValidationError("Purchased installation identity must match the immutable snapshot.")
        if self.classification == InstalledGameClassification.BONUS and self.game_id and self.delivered_version_id:
            if self.delivered_version.product_id != self.game_id:
                raise ValidationError("Bonus version must belong to its catalog game.")
        if self.operator_id and self.operator.user_type not in (2, 3):
            raise ValidationError({"operator": "Installation evidence requires authorized staff."})
        if self.operator_id:
            if not self.operator.is_active:
                raise ValidationError({"operator": "Installation evidence requires active staff."})
            assigned_id = self.fulfillment_item.assigned_operator_id
            valid_authority = (
                self.actor_authority == FulfillmentActorAuthority.ASSIGNED_OPERATOR and assigned_id == self.operator_id
            ) or (
                self.actor_authority == FulfillmentActorAuthority.UNASSIGNED_STAFF and assigned_id is None
            ) or (
                self.actor_authority == FulfillmentActorAuthority.ADMIN_OVERRIDE
                and self.operator.user_type == 2 and assigned_id is not None and assigned_id != self.operator_id
            )
            if not valid_authority:
                raise ValidationError({"actor_authority": "Evidence authority does not match the execution assignment."})
        if self.corrects_id:
            if self.corrects_id == self.pk:
                raise ValidationError({"corrects": "Evidence cannot supersede itself."})
            if self.corrects.fulfillment_item_id != self.fulfillment_item_id:
                raise ValidationError({"corrects": "Evidence correction must remain in one execution."})
            if self.corrects.classification != self.classification:
                raise ValidationError({"corrects": "Evidence correction must preserve classification."})
            if self.corrects.state != InstalledGameRecordState.RECORDED:
                raise ValidationError({"corrects": "Removed evidence is terminal and cannot be superseded."})
            if type(self).objects.filter(corrects_id=self.corrects_id).exclude(pk=self.pk).exists():
                raise ValidationError({"corrects": "Evidence correction must reference the current unsuperseded record."})
        validate_fulfillment_safe_text(self.fallback_title)
        validate_fulfillment_safe_text(self.correction_reason)

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Installation evidence is append-only.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Installation evidence is append-only.")
