from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q

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
