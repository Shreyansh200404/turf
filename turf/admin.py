from django.contrib import admin
from django.utils.html import format_html

# Register your models here.

from .models import (
    Feedback,
    Turf,
    TurfBooked,
    TurfBookingPayment,
    TurfSettings,
    bookslot,
    turfBooking,
)
# Register your models here.


admin.site.register(turfBooking)
admin.site.register(bookslot)
@admin.register(Turf)
class TurfAdmin(admin.ModelAdmin):
    list_display = ('name', 'image_preview', 'slot_price', 'is_active', 'is_default')
    list_filter = ('is_active',)
    search_fields = ('name', 'address')
    readonly_fields = ('is_default', 'image_preview')

    @admin.display(description='Image')
    def image_preview(self, obj):
        if not obj or not obj.image:
            return 'No image'
        return format_html('<img src="{}" alt="{}" style="max-height: 60px;">', obj.image.url, obj.name)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(TurfBooked)
class TurfBookedAdmin(admin.ModelAdmin):
    list_display = ('id', 'turf_name', 'name', 'selected_date', 'amount', 'paid', 'cancelled')
    list_filter = ('paid', 'cancelled', 'turf')
    search_fields = ('name', 'email', 'turf_name', 'payment_id')


admin.site.register(TurfSettings)
admin.site.register(TurfBookingPayment)


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'rating', 'status', 'created_at')
    list_filter = ('status', 'rating', 'created_at')
    search_fields = ('user__username', 'user__email', 'message', 'admin_response')
    readonly_fields = ('user', 'rating', 'message', 'created_at', 'updated_at')