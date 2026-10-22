from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('turf', '0011_turfbookingpayment'),
    ]

    operations = [
        migrations.CreateModel(
            name='TurfSlotReservation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('selected_date', models.CharField(max_length=10)),
                ('slot', models.CharField(max_length=12)),
                ('reservation_id', models.CharField(db_index=True, max_length=32)),
                ('expires_at', models.DateTimeField(db_index=True)),
                ('turf', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='slot_reservations', to='turf.turf')),
            ],
        ),
        migrations.AddConstraint(
            model_name='turfslotreservation',
            constraint=models.UniqueConstraint(
                fields=('turf', 'selected_date', 'slot'),
                name='unique_turf_date_slot_reservation',
            ),
        ),
    ]
