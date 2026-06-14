from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("shop", "0011_paymenttransaction"),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name="deliverydata",
            unique_together=set(),
        ),
    ]
