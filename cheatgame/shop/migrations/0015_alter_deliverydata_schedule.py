from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("shop", "0014_order_shipping_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="deliverydata",
            name="schedule",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="shop.deliveryschedule",
            ),
        ),
    ]
