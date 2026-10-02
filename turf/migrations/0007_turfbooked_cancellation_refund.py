from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('turf', '0006_delete_time'),
    ]

    operations = [
        migrations.AddField(
            model_name='turfbooked',
            name='cancelled',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='turfbooked',
            name='cancelled_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='turfbooked',
            name='refund_id',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name='turfbooked',
            name='refund_status',
            field=models.CharField(default='not_applicable', max_length=20),
        ),
    ]
