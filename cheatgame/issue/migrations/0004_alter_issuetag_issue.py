# Generated by Django 4.0.7 on 2024-03-05 05:54

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('issue', '0003_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='issuetag',
            name='issue',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='tags', to='issue.issue'),
        ),
    ]
