from django.db import migrations, models
import django.db.models.deletion


def create_default_turf_and_link_bookings(apps, schema_editor):
    Turf = apps.get_model('turf', 'Turf')
    TurfBooked = apps.get_model('turf', 'TurfBooked')
    TurfSettings = apps.get_model('turf', 'TurfSettings')

    settings = TurfSettings.objects.order_by('pk').first()
    defaults = {
        'name': settings.name if settings else 'The SK Sports Turf',
        'slot_price': settings.slot_price if settings and settings.slot_price > 0 else 500,
        'description': settings.description if settings else '',
        'address': settings.address if settings else '',
        'contact_phone': settings.contact_phone if settings else '',
        'contact_email': settings.contact_email if settings else '',
        'is_active': True,
        'is_default': True,
    }
    turf = Turf.objects.order_by('pk').first()
    if turf is None:
        turf = Turf.objects.create(**defaults)
    else:
        turf.is_default = True
        turf.save(update_fields=['is_default'])

    for booking in TurfBooked.objects.filter(turf__isnull=True).iterator():
        booking.turf_id = turf.pk
        booking.turf_name = turf.name
        booking.save(update_fields=['turf', 'turf_name'])


class Migration(migrations.Migration):

    dependencies = [
        ('turf', '0008_turfbooked_razorpay_order_id'),
    ]

    operations = [
        migrations.CreateModel(
            name='Turf',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200)),
                ('slot_price', models.PositiveIntegerField(default=500)),
                ('description', models.TextField(blank=True)),
                ('address', models.TextField(blank=True)),
                ('contact_phone', models.CharField(blank=True, max_length=20)),
                ('contact_email', models.EmailField(blank=True, max_length=254)),
                ('is_active', models.BooleanField(default=True)),
                ('is_default', models.BooleanField(default=False)),
            ],
        ),
        migrations.AddField(
            model_name='turfbooked',
            name='turf',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='bookings',
                to='turf.turf',
            ),
        ),
        migrations.AddField(
            model_name='turfbooked',
            name='turf_name',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
        migrations.RunPython(create_default_turf_and_link_bookings, migrations.RunPython.noop),
    ]
