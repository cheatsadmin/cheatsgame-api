from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [("product", "0020_deliveredversion_product_commerce_authority")]

    operations = [
        migrations.CreateModel(
            name="ProductSlugHistory",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        db_index=True,
                        default=django.utils.timezone.now,
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "slug",
                    models.SlugField(
                        allow_unicode=True,
                        max_length=120,
                        unique=True,
                    ),
                ),
                (
                    "product",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="slug_history",
                        to="product.product",
                    ),
                ),
            ],
            options={"ordering": ("pk",)},
        ),
    ]
