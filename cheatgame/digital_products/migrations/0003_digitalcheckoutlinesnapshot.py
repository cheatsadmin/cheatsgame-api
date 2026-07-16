from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("digital_products", "0002_digitalcartselection"),
        ("shop", "0019_checkoutline_commerce_authority"),
    ]

    operations = [
        migrations.CreateModel(
            name="DigitalCheckoutLineSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("product_id", models.PositiveBigIntegerField()),
                ("product_name", models.CharField(max_length=200)),
                (
                    "commerce_authority",
                    models.CharField(
                        choices=[
                            ("standard_commerce", "STANDARD_COMMERCE"),
                            ("digital_products", "DIGITAL_PRODUCTS"),
                        ],
                        default="digital_products",
                        max_length=30,
                    ),
                ),
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
                ("fulfillment_method", models.CharField(choices=[("in_store", "IN_STORE"), ("remote", "REMOTE")], max_length=16)),
                ("version_label", models.CharField(max_length=32)),
                ("native_console", models.CharField(choices=[("ps4", "PS4"), ("ps5", "PS5")], max_length=10)),
                (
                    "compatibility_disclosure",
                    models.CharField(
                        choices=[
                            ("native_version_v1", "NATIVE_VERSION_V1"),
                            ("ps4_on_ps5_backward_compatible_v1", "PS4_ON_PS5_BACKWARD_COMPATIBLE_V1"),
                        ],
                        max_length=48,
                    ),
                ),
                (
                    "capacity_disclosure",
                    models.CharField(
                        choices=[
                            ("capacity_1_offline_in_store_v1", "CAPACITY_1_OFFLINE_IN_STORE_V1"),
                            ("capacity_2_online_offline_flexible_v1", "CAPACITY_2_ONLINE_OFFLINE_FLEXIBLE_V1"),
                            ("capacity_3_online_flexible_v1", "CAPACITY_3_ONLINE_FLEXIBLE_V1"),
                        ],
                        max_length=48,
                    ),
                ),
                ("unit_price", models.DecimalField(decimal_places=0, max_digits=15)),
                ("quantity", models.PositiveIntegerField(default=1)),
                ("line_total", models.DecimalField(decimal_places=0, max_digits=16)),
                ("safe_display_metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "checkout_line",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="digital_snapshot",
                        to="shop.checkoutline",
                    ),
                ),
                (
                    "delivered_version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="digital_checkout_snapshots",
                        to="product.deliveredversion",
                    ),
                ),
                (
                    "inventory_pool",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="checkout_snapshots",
                        to="digital_products.inventorypool",
                    ),
                ),
                (
                    "offer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="checkout_snapshots",
                        to="digital_products.digitaloffer",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(check=models.Q(commerce_authority="digital_products"), name="digital_snapshot_authority"),
                    models.CheckConstraint(check=models.Q(customer_console__in=["ps4", "ps5"]), name="digital_checkout_console_valid"),
                    models.CheckConstraint(check=models.Q(capacity__in=["capacity_1", "capacity_2", "capacity_3"]), name="digital_checkout_capacity_valid"),
                    models.CheckConstraint(check=models.Q(fulfillment_method__in=["in_store", "remote"]), name="digital_checkout_method_valid"),
                    models.CheckConstraint(check=models.Q(native_console__in=["ps4", "ps5"]), name="digital_checkout_native_console_valid"),
                    models.CheckConstraint(check=models.Q(compatibility_disclosure__in=["native_version_v1", "ps4_on_ps5_backward_compatible_v1"]), name="digital_checkout_compat_valid"),
                    models.CheckConstraint(check=models.Q(capacity_disclosure__in=["capacity_1_offline_in_store_v1", "capacity_2_online_offline_flexible_v1", "capacity_3_online_flexible_v1"]), name="digital_checkout_capacity_rule_valid"),
                    models.CheckConstraint(check=~models.Q(version_label=""), name="digital_checkout_version_label_nonempty"),
                    models.CheckConstraint(check=models.Q(unit_price__gte=0), name="digital_snapshot_unit_price_gte_zero"),
                    models.CheckConstraint(check=models.Q(quantity=1), name="digital_snapshot_quantity_one"),
                    models.CheckConstraint(check=models.Q(line_total=models.F("unit_price") * models.F("quantity")), name="digital_snapshot_total_equation"),
                ],
            },
        ),
    ]
