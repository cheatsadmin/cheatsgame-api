from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("product", "0020_deliveredversion_product_commerce_authority"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="InventoryPool",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("sellable_quantity", models.PositiveIntegerField(default=0)),
                (
                    "status",
                    models.CharField(
                        choices=[("enabled", "ENABLED"), ("paused", "PAUSED"), ("archived", "ARCHIVED")],
                        default="paused",
                        max_length=16,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(check=models.Q(sellable_quantity__gte=0), name="inventory_pool_quantity_gte_zero"),
                    models.CheckConstraint(check=models.Q(status__in=["enabled", "paused", "archived"]), name="inventory_pool_status_valid"),
                ]
            },
        ),
        migrations.CreateModel(
            name="PoolStockAdjustment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("delta", models.IntegerField()),
                ("previous_quantity", models.PositiveIntegerField()),
                ("resulting_quantity", models.PositiveIntegerField()),
                (
                    "reason",
                    models.CharField(
                        choices=[
                            ("inventory_received", "INVENTORY_RECEIVED"),
                            ("reconciliation", "RECONCILIATION"),
                            ("operational_correction", "OPERATIONAL_CORRECTION"),
                            ("mark_unavailable", "MARK_UNAVAILABLE"),
                            ("return_to_stock", "RETURN_TO_STOCK"),
                        ],
                        max_length=32,
                    ),
                ),
                ("idempotency_key", models.UUIDField(editable=False, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="digital_stock_adjustments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "inventory_pool",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="stock_adjustments",
                        to="digital_products.inventorypool",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(check=~models.Q(delta=0), name="pool_adjustment_delta_nonzero"),
                    models.CheckConstraint(check=models.Q(previous_quantity__gte=0), name="pool_adjustment_previous_gte_zero"),
                    models.CheckConstraint(check=models.Q(resulting_quantity__gte=0), name="pool_adjustment_result_gte_zero"),
                    models.CheckConstraint(
                        check=models.Q(resulting_quantity=models.F("previous_quantity") + models.F("delta")),
                        name="pool_adjustment_quantity_equation",
                    ),
                    models.CheckConstraint(
                        check=models.Q(
                            reason__in=[
                                "inventory_received",
                                "reconciliation",
                                "operational_correction",
                                "mark_unavailable",
                                "return_to_stock",
                            ]
                        ),
                        name="pool_adjustment_reason_valid",
                    ),
                ]
            },
        ),
        migrations.CreateModel(
            name="DigitalOffer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("customer_console", models.CharField(choices=[("ps4", "PS4"), ("ps5", "PS5")], max_length=10)),
                (
                    "capacity",
                    models.CharField(
                        choices=[
                            ("capacity_1", "CAPACITY_1"),
                            ("capacity_2", "CAPACITY_2"),
                            ("capacity_3", "CAPACITY_3"),
                        ],
                        max_length=16,
                    ),
                ),
                ("price", models.DecimalField(decimal_places=0, max_digits=15)),
                (
                    "sale_state",
                    models.CharField(
                        choices=[
                            ("draft", "DRAFT"),
                            ("active", "ACTIVE"),
                            ("paused", "PAUSED"),
                            ("hidden", "HIDDEN"),
                            ("archived", "ARCHIVED"),
                        ],
                        default="draft",
                        max_length=16,
                    ),
                ),
                (
                    "delivered_version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="digital_offers",
                        to="product.deliveredversion",
                    ),
                ),
                (
                    "inventory_pool",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="digital_offers",
                        to="digital_products.inventorypool",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(check=models.Q(customer_console__in=["ps4", "ps5"]), name="digital_offer_console_valid"),
                    models.CheckConstraint(check=models.Q(capacity__in=["capacity_1", "capacity_2", "capacity_3"]), name="digital_offer_capacity_valid"),
                    models.CheckConstraint(check=models.Q(price__gte=0), name="digital_offer_price_gte_zero"),
                    models.CheckConstraint(check=models.Q(sale_state__in=["draft", "active", "paused", "hidden", "archived"]), name="digital_offer_sale_state_valid"),
                    models.UniqueConstraint(
                        condition=~models.Q(sale_state="archived"),
                        fields=("delivered_version", "customer_console", "capacity"),
                        name="digital_offer_unique_nonarchived",
                    ),
                ]
            },
        ),
    ]
