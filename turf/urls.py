from django.urls import path, include
from . import views
from django.contrib.auth import views as auth_views





urlpatterns = [
    path("", views.index, name="index"),
    path("book_now/", views.book_now, name="book_now"),
    path("turf_details/", views.turf_details, name="turf_details"),
    path("slot_details/", views.slot_details, name="slot_details"),
    path("login/", views.login, name="login"),
    path("signup/", views.signup, name="signup"),
    path("logout/", views.logout, name="logout"),
    path('turfBilling/', views.turfBilling, name='turfBilling'),
    path('orderHistory/', views.orderHistory, name="orderHistory"),
    path('allBookings/', views.allBookings, name="allBookings"),
    path('delete_booking/<int:id>/', views.delete_booking, name="delete_booking"),
    path('success/', views.success, name='success'),
    path('check_slot_status/', views.check_slot_status, name='check_slot_status'),
    
    # API endpoints for form validation
    path('check_username/', views.check_username, name='check_username'),
    path('check_email/', views.check_email, name='check_email'),
    
    # Owner dashboard routes
    path('owner/login/', views.owner_login, name='owner_login'),
    path('owner/dashboard/', views.owner_dashboard, name='owner_dashboard'),
    path('owner/bookings/', views.owner_bookings, name='owner_bookings'),
    path('owner/payments/', views.owner_payments, name='owner_payments'),
    path('owner/settings/', views.owner_settings, name='owner_settings'),
    path('owner/analytics/', views.owner_analytics, name='owner_analytics'),

    # Password Reset URLs
    path('password_reset/', auth_views.PasswordResetView.as_view(
        template_name='password_reset.html',
        email_template_name='email.html',
        subject_template_name='email.txt'
    ), name='password_reset'),
    path('password_reset/done/', auth_views.PasswordResetDoneView.as_view(
        template_name='password_reset_done.html'
    ), name='password_reset_done'),
    path('reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(
        template_name='password_reset_confirm.html'
    ), name='password_reset_confirm'),
    path('reset/done/', auth_views.PasswordResetCompleteView.as_view(
        template_name='password_reset_complete.html'
    ), name='password_reset_complete'),
]