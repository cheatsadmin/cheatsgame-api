# Generated by Django 4.0.7 on 2024-01-26 15:55

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('product', '0008_alter_product_slug'),
    ]

    operations = [
        migrations.DeleteModel(
            name='DeliveryType',
        ),
    ]
