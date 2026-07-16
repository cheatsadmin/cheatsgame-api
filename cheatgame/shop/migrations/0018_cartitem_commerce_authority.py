from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("shop", "0017_checkoutshippingsnapshot_is_pricing_finalized"),
    ]

    operations = [
        migrations.AddField(
            model_name="cartitem",
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
            model_name="cartitem",
            constraint=models.CheckConstraint(
                check=models.Q(commerce_authority__in=["standard_commerce", "digital_products"]),
                name="cart_item_authority_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="cartitem",
            constraint=models.CheckConstraint(
                check=(~models.Q(commerce_authority="digital_products") | models.Q(quantity=1)),
                name="cart_item_digital_quantity_one",
            ),
        ),
    ]
