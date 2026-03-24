from django.db import models

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
    amount = models.IntegerField()
    selected_date = models.CharField(max_length=200)
    current_date = models.CharField(max_length=200)
    booking_time = models.CharField(max_length=200, default="")
    slots = models.TextField(default="")
    
    payment_id = models.CharField(max_length=100)
    payment_method = models.CharField(max_length=50, default="credit_card")
    paid = models.BooleanField(default=False)
    is_dummy_payment = models.BooleanField(default=True)

class TurfSettings(models.Model):
    name = models.CharField(max_length=200, default="The SK Sports Turf")
    slot_price = models.IntegerField(default=500)
    description = models.TextField(default="The best sports turf in town")
    address = models.TextField(default="")
    contact_phone = models.CharField(max_length=20, default="")
    contact_email = models.EmailField(default="")
    
    def __str__(self):
        return self.name
