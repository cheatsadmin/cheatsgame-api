from django.db import migrations, models


def set_review_status_from_accepted(apps, schema_editor):
    Reviews = apps.get_model("product", "Reviews")
    Reviews.objects.filter(accepted=True).update(status="approved")
    Reviews.objects.filter(accepted=False).update(status="pending")


class Migration(migrations.Migration):

    dependencies = [
        ("product", "0016_attachment_description"),
    ]

    operations = [
        migrations.AddField(
            model_name="reviews",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "PENDING"),
                    ("approved", "APPROVED"),
                    ("rejected", "REJECTED"),
                ],
                db_index=True,
                default="pending",
                max_length=20,
            ),
        ),
        migrations.RunPython(set_review_status_from_accepted, migrations.RunPython.noop),
    ]
