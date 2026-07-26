from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("digital_products", "0011_postgresql_fulfillment_integrity_hardening"),
        ("product", "0020_deliveredversion_product_commerce_authority"),
    ]

    operations = [
        migrations.CreateModel(
            name="DigitalGameReleaseMetadata",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        db_index=True,
                        default=django.utils.timezone.now,
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("release_date", models.DateField(blank=True, null=True)),
                (
                    "upcoming_status",
                    models.CharField(
                        choices=[
                            ("ANNOUNCED", "ANNOUNCED"),
                            ("COMING_SOON", "COMING_SOON"),
                            ("PREORDER_OPEN", "PREORDER_OPEN"),
                            ("RELEASED", "RELEASED"),
                            ("DELAYED", "DELAYED"),
                            ("CANCELLED", "CANCELLED"),
                        ],
                        default="ANNOUNCED",
                        max_length=20,
                    ),
                ),
                ("preorder_enabled", models.BooleanField(default=False)),
                ("preorder_open_at", models.DateTimeField(blank=True, null=True)),
                ("preorder_close_at", models.DateTimeField(blank=True, null=True)),
                (
                    "product",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="digital_release_metadata",
                        to="product.product",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="digitalgamereleasemetadata",
            constraint=models.CheckConstraint(
                check=models.Q(("upcoming_status__in", ["ANNOUNCED", "COMING_SOON", "PREORDER_OPEN", "RELEASED", "DELAYED", "CANCELLED"])),
                name="digital_game_upcoming_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="digitalgamereleasemetadata",
            constraint=models.CheckConstraint(
                check=models.Q(("preorder_enabled", False)),
                name="digital_game_preorder_disabled_v1",
            ),
        ),
        migrations.AddConstraint(
            model_name="digitalgamereleasemetadata",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(("preorder_close_at__isnull", True))
                    | models.Q(("preorder_open_at__isnull", True))
                    | models.Q(("preorder_close_at__gt", models.F("preorder_open_at")))
                ),
                name="digital_game_preorder_window_order",
            ),
        ),
        migrations.AddConstraint(
            model_name="digitalgamereleasemetadata",
            constraint=models.CheckConstraint(
                check=(
                    ~models.Q(("upcoming_status", "PREORDER_OPEN"))
                    | models.Q(("preorder_enabled", True))
                ),
                name="digital_game_preorder_status_requires_enablement",
            ),
        ),
    ]
