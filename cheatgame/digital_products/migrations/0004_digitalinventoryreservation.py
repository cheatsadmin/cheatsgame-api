from django.db import migrations, models
import django.db.models.deletion
import django.db.models.expressions
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("digital_products", "0003_digitalcheckoutlinesnapshot"),
        ("shop", "0019_checkoutline_commerce_authority"),
    ]

    operations = [
        migrations.CreateModel(
            name="DigitalInventoryReservation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("quantity", models.PositiveIntegerField(default=1)),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("active", "ACTIVE"),
                            ("held_for_review", "HELD_FOR_REVIEW"),
                            ("consumed", "CONSUMED"),
                            ("released", "RELEASED"),
                            ("expired", "EXPIRED"),
                        ],
                        default="active",
                        max_length=24,
                    ),
                ),
                ("expires_at", models.DateTimeField()),
                ("state_changed_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("idempotency_key", models.UUIDField(editable=False, unique=True)),
                ("resolution_reason", models.CharField(blank=True, max_length=64, null=True)),
                (
                    "checkout",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="digital_inventory_reservations",
                        to="shop.checkout",
                    ),
                ),
                (
                    "checkout_line",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="digital_inventory_reservation",
                        to="shop.checkoutline",
                    ),
                ),
                (
                    "inventory_pool",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reservations",
                        to="digital_products.inventorypool",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="digitalinventoryreservation",
            index=models.Index(fields=["state", "expires_at"], name="digital_res_state_expires"),
        ),
        migrations.AddIndex(
            model_name="digitalinventoryreservation",
            index=models.Index(fields=["inventory_pool", "state"], name="digital_res_pool_state"),
        ),
        migrations.AddConstraint(
            model_name="digitalinventoryreservation",
            constraint=models.CheckConstraint(check=models.Q(quantity=1), name="digital_res_quantity_one"),
        ),
        migrations.AddConstraint(
            model_name="digitalinventoryreservation",
            constraint=models.CheckConstraint(
                check=models.Q(state__in=["active", "held_for_review", "consumed", "released", "expired"]),
                name="digital_res_state_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="digitalinventoryreservation",
            constraint=models.CheckConstraint(
                check=models.Q(expires_at__gt=django.db.models.expressions.F("created_at")),
                name="digital_res_expiry_after_created",
            ),
        ),
    ]
