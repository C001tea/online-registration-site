# Панель салона: несколько админов у салона и роли, статус «не пришёл»,
# записи от персонала, выходные мастеров по датам.
# Схема сгенерирована makemigrations локально, RunPython дописан вручную.
# Откат возможен, только пока у каждого салона не больше одного AdminProfile
# (company снова станет OneToOne/UNIQUE) — см. docs/STAFF.md.

import django.db.models.deletion
from django.db import migrations, models


def existing_admins_become_owners(apps, schema_editor):
    # Пока company был OneToOne, у салона мог быть только один админ — он и есть владелец
    AdminProfile = apps.get_model('bookings', 'AdminProfile')
    AdminProfile.objects.filter(company__isnull=False).update(role='owner')


class Migration(migrations.Migration):

    dependencies = [
        ('bookings', '0011_company_main_photo'),
    ]

    operations = [
        migrations.AddField(
            model_name='adminprofile',
            name='role',
            field=models.CharField(choices=[('owner', 'Владелец'), ('admin', 'Администратор')], default='admin', max_length=10),
        ),
        migrations.AddField(
            model_name='appointment',
            name='created_by_staff',
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name='adminprofile',
            name='company',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='admins', to='bookings.company'),
        ),
        migrations.AlterField(
            model_name='appointment',
            name='status',
            field=models.CharField(choices=[('pending', 'Ожидает'), ('confirmed', 'Подтверждена'), ('cancelled', 'Отменена'), ('completed', 'Завершена'), ('no_show', 'Не пришёл')], default='pending', max_length=20),
        ),
        migrations.CreateModel(
            name='MasterDayOff',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField()),
                ('master', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='days_off', to='bookings.master')),
            ],
            options={
                'ordering': ['date'],
                'unique_together': {('master', 'date')},
            },
        ),
        # Данные — после всех изменений схемы
        migrations.RunPython(existing_admins_become_owners, migrations.RunPython.noop),
    ]
