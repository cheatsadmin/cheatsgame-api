# Generated by Django 4.0.7 on 2024-02-16 13:46

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('general', '0011_commonquestion'),
    ]

    operations = [
        migrations.RenameField(
            model_name='commonquestion',
            old_name='question_type',
            new_name='question_locaiton',
        ),
    ]
