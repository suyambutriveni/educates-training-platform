# Generated by Django 3.2.20 on 2023-09-28 01:51

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('workshops', '0006_environment_created_at'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='workshop',
            name='content',
        ),
    ]
