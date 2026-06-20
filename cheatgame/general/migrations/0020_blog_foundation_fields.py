from django.db import migrations, models
from django.utils.text import slugify


def backfill_blog_foundation(apps, schema_editor):
    Blog = apps.get_model("general", "Blog")
    seen_slugs = set()

    for blog in Blog.objects.order_by("id"):
        base_slug = slugify(blog.slug or blog.title, allow_unicode=True) or f"blog-{blog.pk}"
        base_slug = base_slug[:280]
        slug = base_slug
        counter = 2

        while slug in seen_slugs or Blog.objects.exclude(pk=blog.pk).filter(slug=slug).exists():
            suffix = f"-{counter}"
            slug = f"{base_slug[:300 - len(suffix)]}{suffix}"
            counter += 1

        seen_slugs.add(slug)
        blog.slug = slug
        blog.status = "PUBLISHED"
        blog.seo_title = blog.seo_title or ""
        blog.meta_description = blog.meta_description or ""
        blog.save(update_fields=["slug", "status", "seo_title", "meta_description"])


class Migration(migrations.Migration):

    dependencies = [
        ("general", "0019_delete_contactformsubject"),
    ]

    operations = [
        migrations.AddField(
            model_name="blog",
            name="status",
            field=models.CharField(
                choices=[("DRAFT", "پیش نویس"), ("PUBLISHED", "منتشر شده")],
                db_index=True,
                default="DRAFT",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="blog",
            name="seo_title",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="blog",
            name="meta_description",
            field=models.TextField(blank=True, default="", max_length=320),
        ),
        migrations.RunPython(backfill_blog_foundation, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="blog",
            name="slug",
            field=models.SlugField(allow_unicode=True, db_index=True, max_length=300, unique=True),
        ),
    ]
