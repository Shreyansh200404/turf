from django.db import migrations

def create_default_turf_settings(apps, schema_editor):
    # We can't import the model directly as it may be a newer
    # version than this migration expects. We use the historical version.
    TurfSettings = apps.get_model('turf', 'TurfSettings')
    
    # Create default TurfSettings if it doesn't exist
    if not TurfSettings.objects.exists():
        TurfSettings.objects.create(
            name="The SK Sports Turf",
            slot_price=500,
            description="The best sports turf in town with world-class facilities.",
            address="123 Sports Avenue, Mumbai, Maharashtra, 400101",
            contact_phone="+91 9876543210",
            contact_email="info@sksportsturf.com"
        )

class Migration(migrations.Migration):

    dependencies = [
        ('turf', '0003_turfsettings'),
    ]

    operations = [
        migrations.RunPython(create_default_turf_settings),
    ] 