# Generated by Django 4.0.7 on 2024-02-20 21:11

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0010_alter_address_postal_code'),
        ('shop', '0005_alter_deliverytype_side'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='deliverydata',
            unique_together={('address', 'schedule')},
        ),
    ]
