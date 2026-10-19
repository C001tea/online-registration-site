# Создана на продакшен-сервере (makemigrations) и уже применена там;
# перенесена в репозиторий без изменений, чтобы история миграций совпадала

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bookings', '0010_adminprofile_company_master_user_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='company',
            name='main_photo',
            field=models.ImageField(blank=True, null=True, upload_to='main_photos'),
        ),
    ]
