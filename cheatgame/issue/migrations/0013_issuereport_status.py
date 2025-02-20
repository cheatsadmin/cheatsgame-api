# Generated by Django 4.0.7 on 2024-05-31 21:50

import cheatgame.issue.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('issue', '0012_remove_tag_issue'),
    ]

    operations = [
        migrations.AddField(
            model_name='issuereport',
            name='status',
            field=models.PositiveSmallIntegerField(choices=[(1, 'DURING'), (2, 'DONE'), (3, 'CANCELED')], default=cheatgame.issue.models.IssueReportStatus['DURING']),
        ),
    ]
