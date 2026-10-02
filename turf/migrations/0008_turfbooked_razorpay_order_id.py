from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('turf', '0007_turfbooked_cancellation_refund'),
    ]

    operations = [
        migrations.AddField(
            model_name='turfbooked',
            name='razorpay_order_id',
            field=models.CharField(blank=True, max_length=100),
        ),
    ]
