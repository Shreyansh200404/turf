from django.db import migrations, models
import django.db.models.deletion


def create_existing_payment_records(apps, schema_editor):
    TurfBooked = apps.get_model('turf', 'TurfBooked')
    TurfBookingPayment = apps.get_model('turf', 'TurfBookingPayment')

    for booking in TurfBooked.objects.filter(
        paid=True,
        is_dummy_payment=False,
    ).exclude(payment_id='').iterator():
        TurfBookingPayment.objects.get_or_create(
            booking_id=booking.pk,
            payment_id=booking.payment_id,
            defaults={'amount': max(booking.amount, 0)},
        )


class Migration(migrations.Migration):

    dependencies = [
        ('turf', '0010_feedback'),
    ]

    operations = [
        migrations.CreateModel(
            name='TurfBookingPayment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('payment_id', models.CharField(max_length=100)),
                ('amount', models.PositiveIntegerField()),
                ('refunded_amount', models.PositiveIntegerField(default=0)),
                ('refund_id', models.CharField(blank=True, max_length=100)),
                ('refund_status', models.CharField(default='not_applicable', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('booking', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='payment_records', to='turf.turfbooked')),
            ],
            options={
                'ordering': ('created_at', 'pk'),
            },
        ),
        migrations.RunPython(create_existing_payment_records, migrations.RunPython.noop),
    ]
