from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("general", "0020_blog_foundation_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="banner",
            name="alt_text",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="banner",
            name="is_active",
            field=models.BooleanField(db_index=True, default=True),
        ),
        migrations.AddField(
            model_name="banner",
            name="sort_order",
            field=models.PositiveIntegerField(db_index=True, default=0),
        ),
        migrations.AddField(
            model_name="slider",
            name="alt_text",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="slider",
            name="is_active",
            field=models.BooleanField(db_index=True, default=True),
        ),
        migrations.AddField(
            model_name="slider",
            name="sort_order",
            field=models.PositiveIntegerField(db_index=True, default=0),
        ),
        migrations.AddField(
            model_name="story",
            name="alt_text",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="story",
            name="is_active",
            field=models.BooleanField(db_index=True, default=True),
        ),
        migrations.AddField(
            model_name="story",
            name="sort_order",
            field=models.PositiveIntegerField(db_index=True, default=0),
        ),
    ]
