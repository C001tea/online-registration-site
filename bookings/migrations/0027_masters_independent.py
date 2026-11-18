from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bookings', '0026_appearance'),
    ]

    operations = [
        migrations.AddField(
            model_name='company',
            name='masters_independent',
            field=models.BooleanField(default=False),
        ),
    ]
