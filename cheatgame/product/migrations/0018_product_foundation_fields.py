from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("product", "0017_reviews_status"),
    ]

    operations = [
        migrations.AlterField(
            model_name="product",
            name="slug",
            field=models.SlugField(
                allow_unicode=True,
                blank=True,
                db_index=True,
                max_length=120,
                unique=True,
            ),
        ),
        migrations.AddField(
            model_name="product",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "DRAFT"),
                    ("published", "PUBLISHED"),
                    ("hidden", "HIDDEN"),
                ],
                db_index=True,
                default="published",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="product",
            name="seo_title",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="product",
            name="meta_description",
            field=models.CharField(blank=True, default="", max_length=300),
        ),
    ]
