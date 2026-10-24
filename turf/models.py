from django.db import models
from django.conf import settings

# Create your models here.


class turfBooking(models.Model):
    time_slot = models.CharField(max_length=12)
    isBooked = models.BooleanField(default=False)
    days = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return self.time_slot


class bookslot(models.Model):
    week = models.TextField(default='[[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]]')


class TurfBooked(models.Model):
    name = models.CharField(max_length=200)
    email = models.CharField(max_length=200)
    turf = models.ForeignKey(
        'Turf',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='bookings',
    )
    turf_name = models.CharField(max_length=200, blank=True, default='')
    amount = models.IntegerField()
    selected_date = models.CharField(max_length=200)
    current_date = models.CharField(max_length=200)
    booking_time = models.CharField(max_length=200, default="")
    slots = models.TextField(default="")
    
    payment_id = models.CharField(max_length=100)
    razorpay_order_id = models.CharField(max_length=100, blank=True)
    payment_method = models.CharField(max_length=50, default="credit_card")
    paid = models.BooleanField(default=False)
    is_dummy_payment = models.BooleanField(default=True)
    cancelled = models.BooleanField(default=False)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    refund_id = models.CharField(max_length=100, blank=True)
    refund_status = models.CharField(max_length=20, default="not_applicable")


class TurfSlotReservation(models.Model):
    turf = models.ForeignKey(
        'Turf',
        on_delete=models.CASCADE,
        related_name='slot_reservations',
    )
    selected_date = models.CharField(max_length=10)
    slot = models.CharField(max_length=12)
    reservation_id = models.CharField(max_length=32, db_index=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('turf', 'selected_date', 'slot'),
                name='unique_turf_date_slot_reservation',
            ),
        ]


class TurfBookingPayment(models.Model):
    booking = models.ForeignKey(
        TurfBooked,
        on_delete=models.CASCADE,
        related_name='payment_records',
    )
    payment_id = models.CharField(max_length=100)
    amount = models.PositiveIntegerField()
    refunded_amount = models.PositiveIntegerField(default=0)
    refund_id = models.CharField(max_length=100, blank=True)
    refund_status = models.CharField(max_length=20, default='not_applicable')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('created_at', 'pk')

    def __str__(self):
        return f'{self.payment_id} for booking {self.booking_id}'


class Turf(models.Model):
    name = models.CharField(max_length=200)
    image = models.ImageField(upload_to='turfs/', blank=True)
    slot_price = models.PositiveIntegerField(default=500)
    description = models.TextField(blank=True)
    address = models.TextField(blank=True)
    contact_phone = models.CharField(max_length=20, blank=True)
    contact_email = models.EmailField(blank=True)
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)

    def __str__(self):
        return self.name


class TurfSettings(models.Model):
    name = models.CharField(max_length=200, default="The SK Sports Turf")
    slot_price = models.IntegerField(default=500)
    description = models.TextField(default="The best sports turf in town")
    address = models.TextField(default="")
    contact_phone = models.CharField(max_length=20, default="")
    contact_email = models.EmailField(default="")
    
    def __str__(self):
        return self.name


class Feedback(models.Model):
    STATUS_NEW = 'new'
    STATUS_REVIEWED = 'reviewed'
    STATUS_RESOLVED = 'resolved'
    STATUS_CHOICES = (
        (STATUS_NEW, 'New'),
        (STATUS_REVIEWED, 'Reviewed'),
        (STATUS_RESOLVED, 'Resolved'),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='feedback_entries',
    )
    rating = models.PositiveSmallIntegerField(
        choices=tuple((value, f'{value} star{"s" if value != 1 else ""}') for value in range(1, 6)),
    )
    message = models.TextField(max_length=2000)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_NEW)
    admin_response = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at', '-pk')

    def __str__(self):
        return f'Feedback from {self.user} ({self.rating}/5)'
