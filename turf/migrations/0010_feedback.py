from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('turf', '0009_turf_multi_booking'),
    ]

    operations = [
        migrations.CreateModel(
            name='Feedback',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('rating', models.PositiveSmallIntegerField(choices=[(1, '1 star'), (2, '2 stars'), (3, '3 stars'), (4, '4 stars'), (5, '5 stars')])),
                ('message', models.TextField(max_length=2000)),
                ('status', models.CharField(choices=[('new', 'New'), ('reviewed', 'Reviewed'), ('resolved', 'Resolved')], default='new', max_length=20)),
                ('admin_response', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='feedback_entries', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ('-created_at', '-pk'),
            },
        ),
    ]
