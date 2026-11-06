from django.db import migrations, models


class Migration(migrations.Migration):
    """Выбор напоминания и письма клиенту. Записям, созданным до этой версии, писем не шлём:
    поля добавляются со значением True, затем default меняется на False для новых записей."""

    dependencies = [
        ('bookings', '0020_master_service_terms'),
    ]

    operations = [
        migrations.AddField(
            model_name='appointment',
            name='email_confirm_sent',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='appointment',
            name='email_reminder_sent',
            field=models.BooleanField(default=True),
        ),
        migrations.AlterField(
            model_name='appointment',
            name='email_confirm_sent',
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name='appointment',
            name='email_reminder_sent',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='appointment',
            name='reminder_minutes',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
