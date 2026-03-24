from django.contrib import admin

# Register your models here.

from .models import *
# Register your models here.


admin.site.register(turfBooking)
admin.site.register(bookslot)
admin.site.register(TurfBooked)
admin.site.register(TurfSettings)