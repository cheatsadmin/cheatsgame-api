from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("digital_products", "0001_initial"),
        ("shop", "0018_cartitem_commerce_authority"),
    ]

    operations = [
        migrations.CreateModel(
            name="DigitalCartSelection",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "fulfillment_method",
                    models.CharField(choices=[("in_store", "IN_STORE"), ("remote", "REMOTE")], max_length=16),
                ),
                (
                    "cart_item",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="digital_selection",
                        to="shop.cartitem",
                    ),
                ),
                (
                    "offer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="cart_selections",
                        to="digital_products.digitaloffer",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        check=models.Q(fulfillment_method__in=["in_store", "remote"]),
                        name="digital_cart_method_valid",
                    ),
                ],
            },
        ),
    ]
