from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("general", "0021_homepage_cms_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="slider",
            name="hero_eyebrow",
            field=models.CharField(blank=True, max_length=120, null=True),
        ),
        migrations.AddField(
            model_name="slider",
            name="hero_headline",
            field=models.CharField(blank=True, max_length=220, null=True),
        ),
        migrations.AddField(
            model_name="slider",
            name="hero_highlight",
            field=models.CharField(blank=True, max_length=120, null=True),
        ),
        migrations.AddField(
            model_name="slider",
            name="hero_subtitle",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="slider",
            name="hero_primary_label",
            field=models.CharField(blank=True, max_length=120, null=True),
        ),
        migrations.AddField(
            model_name="slider",
            name="hero_primary_link",
            field=models.CharField(blank=True, max_length=300, null=True),
        ),
        migrations.AddField(
            model_name="slider",
            name="hero_secondary_label",
            field=models.CharField(blank=True, max_length=120, null=True),
        ),
        migrations.AddField(
            model_name="slider",
            name="hero_secondary_link",
            field=models.CharField(blank=True, max_length=300, null=True),
        ),
        migrations.AddField(
            model_name="slider",
            name="hero_artwork_image",
            field=models.FileField(blank=True, null=True, upload_to=""),
        ),
    ]
