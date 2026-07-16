from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("product", "0019_category_name_not_globally_unique"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="commerce_authority",
            field=models.CharField(
                choices=[
                    ("standard_commerce", "STANDARD_COMMERCE"),
                    ("digital_products", "DIGITAL_PRODUCTS"),
                ],
                default="standard_commerce",
                max_length=30,
            ),
        ),
        migrations.CreateModel(
            name="DeliveredVersion",
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
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "native_console",
                    models.CharField(choices=[("ps4", "PS4"), ("ps5", "PS5")], max_length=10),
                ),
                ("is_active", models.BooleanField(default=True)),
                (
                    "product",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="delivered_versions",
                        to="product.product",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="product",
            constraint=models.CheckConstraint(
                check=models.Q(commerce_authority__in=["standard_commerce", "digital_products"]),
                name="product_commerce_authority_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="product",
            constraint=models.CheckConstraint(
                check=(models.Q(commerce_authority="standard_commerce") | models.Q(product_type=2)),
                name="product_digital_authority_game_only",
            ),
        ),
        migrations.AddConstraint(
            model_name="deliveredversion",
            constraint=models.CheckConstraint(
                check=models.Q(native_console__in=["ps4", "ps5"]),
                name="delivered_version_console_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="deliveredversion",
            constraint=models.UniqueConstraint(
                condition=models.Q(is_active=True),
                fields=("product", "native_console"),
                name="unique_active_delivered_version",
            ),
        ),
    ]
