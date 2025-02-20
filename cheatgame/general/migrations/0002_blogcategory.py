# Generated by Django 4.0.7 on 2024-01-19 17:54

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('product', '0005_product_device_model'),
        ('general', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='BlogCategory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('blog', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='general.blog')),
                ('category', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='product.category')),
            ],
        ),
    ]
