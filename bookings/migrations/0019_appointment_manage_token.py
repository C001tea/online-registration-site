import secrets

from django.db import migrations, models


def fill_tokens(apps, schema_editor):
    Appointment = apps.get_model('bookings', 'Appointment')
    for appointment in Appointment.objects.filter(manage_token__isnull=True).only('id').iterator():
        Appointment.objects.filter(pk=appointment.pk).update(manage_token=secrets.token_urlsafe(18))


class Migration(migrations.Migration):

    dependencies = [
        ('bookings', '0018_company_signup'),
    ]

    operations = [
        migrations.AddField(
            model_name='appointment',
            name='manage_token',
            field=models.CharField(blank=True, editable=False, max_length=32, null=True, unique=True),
        ),
        migrations.RunPython(fill_tokens, migrations.RunPython.noop),
    ]
