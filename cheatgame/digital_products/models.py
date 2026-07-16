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
