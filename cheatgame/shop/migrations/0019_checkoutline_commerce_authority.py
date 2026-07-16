from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("shop", "0018_cartitem_commerce_authority"),
    ]

    operations = [
        migrations.AddField(
            model_name="checkoutline",
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
        migrations.AddConstraint(
            model_name="checkoutline",
            constraint=models.CheckConstraint(
                check=models.Q(commerce_authority__in=["standard_commerce", "digital_products"]),
                name="checkout_line_authority_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="checkoutline",
            constraint=models.CheckConstraint(
                check=(~models.Q(commerce_authority="digital_products") | models.Q(quantity=1)),
                name="checkout_line_digital_quantity_one",
            ),
        ),
        migrations.AddConstraint(
            model_name="checkoutline",
            constraint=models.UniqueConstraint(
                condition=models.Q(source_cart_item_id__isnull=False),
                fields=("checkout", "source_cart_item_id"),
                name="uniq_checkout_source_cart_item",
            ),
        ),
    ]
