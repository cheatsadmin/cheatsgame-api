from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("issue", "0017_repair_status_v1"),
    ]

    operations = [
        migrations.AddField(
            model_name="issue",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="issue",
            name="sort_order",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
