from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("shop", "0016_checkout_checkoutline_cart_lock_reason_and_more")]

    operations = [
        migrations.AddField(
            model_name="checkoutshippingsnapshot",
            name="is_pricing_finalized",
            field=models.BooleanField(default=False),
        ),
    ]
