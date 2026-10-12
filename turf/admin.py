from django.contrib import admin

# Register your models here.

from .models import Turf, TurfBooked, TurfSettings, bookslot, turfBooking
# Register your models here.


admin.site.register(turfBooking)
admin.site.register(bookslot)
@admin.register(Turf)
class TurfAdmin(admin.ModelAdmin):
    list_display = ('name', 'slot_price', 'is_active', 'is_default')
    list_filter = ('is_active',)
    search_fields = ('name', 'address')
    readonly_fields = ('is_default',)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(TurfBooked)
class TurfBookedAdmin(admin.ModelAdmin):
    list_display = ('id', 'turf_name', 'name', 'selected_date', 'amount', 'paid', 'cancelled')
    list_filter = ('paid', 'cancelled', 'turf')
    search_fields = ('name', 'email', 'turf_name', 'payment_id')


admin.site.register(TurfSettings)