from django.shortcuts import render, redirect, HttpResponse
from django.contrib.auth.models import User, auth
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from .models import *
from datetime import date, datetime, timedelta
import json
from django.db.models import Q, Sum, Count

from django.template.loader import render_to_string
from django.conf import settings
from django.core.mail import send_mail, EmailMessage
from django.http import JsonResponse
from django.template.defaulttags import register
try:
    from pytz import timezone
except ImportError:
    # If pytz is not available, create a dummy timezone function
    def timezone(zone=None):
        return None

# Try to import razorpay, but don't fail if not available
try:
    import razorpay
except ImportError:
    razorpay = None

from django.views.decorators.csrf import csrf_exempt

# Custom template filters
@register.filter
def get_item(lst, index):
    try:
        if isinstance(lst, list):
            return lst[index]
        return 0
    except (IndexError, TypeError):
        return 0

@register.filter
def get_range(value):
    return range(value)

@register.filter
def divide(value, arg):
    try:
        # Convert value and arg to float, handling potential list inputs
        if isinstance(value, list):
            value = float(value[0] if value else 0)
        else:
            value = float(value)
            
        if isinstance(arg, list):
            arg = float(arg[0] if arg else 1)
        else:
            arg = float(arg)
            
        if arg == 0:
            return 0
        return value / arg
    except (ValueError, ZeroDivisionError, TypeError, IndexError):
        return 0

@register.filter
def sum_amount(bookings):
    total = 0
    for booking in bookings:
        total += booking.amount
    return total

@register.filter
def avg_amount(bookings):
    count = len(bookings)
    if count == 0:
        return 0
    return sum_amount(bookings) / count

def index(request):
    # Ensure there's always a TurfSettings entry
    if not TurfSettings.objects.exists():
        TurfSettings.objects.create()
    
    turf_settings = TurfSettings.objects.first()
    
    if request.user.is_authenticated:
        username = request.user.username
        return render(request, 'mainpage_index.html', {'username': username, 'turf': turf_settings})
    return render(request, 'mainpage_index.html', {'turf': turf_settings})


def initialize_time_slots():
    # List of all possible time slots
    time_slots = [
        '6-7 am', '7-8 am', '8-9 am', '9-10 am', '10-11 am', '11-12 pm',
        '12-1 pm', '1-2 pm', '2-3 pm', '3-4 pm', '4-5 pm', '5-6 pm',
        '6-7 pm', '7-8 pm', '8-9 pm', '9-10 pm', '10-11 pm', '11-12 am',
        '12-1 am'
    ]
    
    # Create entries for each time slot if they don't exist
    for slot in time_slots:
        turfBooking.objects.get_or_create(
            time_slot=slot,
            defaults={
                'isBooked': False,
                'days': ''
            }
        )

def book_now(request):
    # Clean up stale pending bookings
    clean_pending_bookings()
    
    # Initialize time slots if they don't exist
    initialize_time_slots()
    
    turf_settings = TurfSettings.objects.first()
    if request.user.is_authenticated:
        username = request.user.username
        return render(request, 'booking_index.html', {'username': username, 'turf': turf_settings})
    return render(request, 'booking_index.html', {'turf': turf_settings})


def turf_details(request):
    # Clean up stale pending bookings
    clean_pending_bookings()
    
    currentDate = date.today().strftime("%Y-%m-%d")
    endDate = (date.today() + timedelta(days=6)).strftime("%Y-%m-%d")
    turf_settings = TurfSettings.objects.first()
    return render(request, 'turfblog.html', {'currentDate': currentDate, 'endDate': endDate, 'turf': turf_settings})


def login(request):
    if request.method == 'POST':
        username = request.POST['username']
        password = request.POST['password']

        user = auth.authenticate(username=username, password=password)

        if user is not None:
            auth.login(request, user)
            if user.is_staff:
                return redirect('owner_dashboard')
            return redirect('book_now')
        else:
            messages.info(request, 'Invalid Credentials')
            return redirect('login')
    else:
        return render(request, 'signIn.html')


def owner_login(request):
    if request.method == 'POST':
        username = request.POST['username']
        password = request.POST['password']

        user = auth.authenticate(username=username, password=password)

        if user is not None and user.is_staff:
            auth.login(request, user)
            return redirect('owner_dashboard')
        else:
            messages.error(request, 'Invalid credentials or insufficient permissions. Owner access only.')
            return redirect('owner_login')
    else:
        if request.user.is_authenticated and request.user.is_staff:
            return redirect('owner_dashboard')
        return render(request, 'owner_login.html')


def signup(request):
    if request.method == 'POST':
        username = request.POST.get('username', '')
        email = request.POST.get('emailid', '')
        password = request.POST.get('password', '')
        password_confirm = request.POST.get('password_confirm', '')
        captcha = request.POST.get('captcha', '')
        hidden_captcha = request.POST.get('hidden_captcha', '')

        # Store form data to return if validation fails
        form_data = {
            'username': username,
            'email': email
        }

        # Validate captcha
        if captcha != hidden_captcha:
            messages.error(request, 'Invalid captcha. Please try again.')
            return render(request, 'signUp.html', {'form_data': form_data})

        # Validate password match
        if password != password_confirm:
            messages.error(request, 'Passwords do not match. Please try again.')
            return render(request, 'signUp.html', {'form_data': form_data})

        # Validate password requirements
        if len(password) < 8:
            messages.error(request, 'Password must be at least 8 characters long. Please try again.')
            return render(request, 'signUp.html', {'form_data': form_data})
        if not any(c.isupper() for c in password):
            messages.error(request, 'Password must contain at least one uppercase letter. Please try again.')
            return render(request, 'signUp.html', {'form_data': form_data})
        if not any(c.islower() for c in password):
            messages.error(request, 'Password must contain at least one lowercase letter. Please try again.')
            return render(request, 'signUp.html', {'form_data': form_data})
        if not any(c in '!@#$%^&*()_+-=[]{}|;:,.<>?' for c in password):
            messages.error(request, 'Password must contain at least one special character. Please try again.')
            return render(request, 'signUp.html', {'form_data': form_data})

        # Check if username already exists
        if User.objects.filter(username=username).exists():
            messages.error(request, f'Username "{username}" is already taken. Please try a different username.')
            return render(request, 'signUp.html', {'form_data': form_data})
        
        # Check if email already exists
        if User.objects.filter(email=email).exists():
            messages.error(request, f'Email "{email}" is already registered. Please use a different email or try logging in.')
            return render(request, 'signUp.html', {'form_data': form_data})
        
        # Create user if all validations pass
        try:
            user = User.objects.create_user(username=username, password=password, email=email)
            user.save()
            messages.success(request, 'Account created successfully! Please login.')
            return redirect('login')
        except Exception as e:
            messages.error(request, f'Error creating account: {str(e)}. Please try again.')
            return render(request, 'signUp.html', {'form_data': form_data})
    else:
        return render(request, 'signUp.html')

def logout(request):
    auth.logout(request)
    return redirect('/')

# Owner dashboard views
@login_required(login_url='owner_login')
def owner_dashboard(request):
    if not request.user.is_staff:
        return redirect('book_now')
    
    # First, check and clean up any records with payment_method='pending'
    # This is just a safety check to ensure no pending records exist in the database
    pending_records = TurfBooked.objects.filter(payment_method='pending')
    if pending_records.exists():
        pending_records.delete()
        
    total_bookings = TurfBooked.objects.count()
    paid_bookings = TurfBooked.objects.filter(paid=True).count()
    total_revenue = TurfBooked.objects.filter(paid=True).aggregate(Sum('amount'))['amount__sum'] or 0
    
    # For recent activity
    recent_bookings = TurfBooked.objects.order_by('-id')[:10]
    
    # Get analytics data
    today = date.today().strftime("%Y-%m-%d")
    week_start = (date.today() - timedelta(days=date.today().weekday())).strftime("%Y-%m-%d")
    month_start = date.today().replace(day=1).strftime("%Y-%m-%d")
    year_start = date.today().replace(month=1, day=1).strftime("%Y-%m-%d")
    
    # Daily income
    daily_bookings = TurfBooked.objects.filter(selected_date=today, paid=True)
    daily_income = daily_bookings.aggregate(Sum('amount'))['amount__sum'] or 0
    daily_count = daily_bookings.count()
    
    # Weekly income
    weekly_bookings = TurfBooked.objects.filter(selected_date__gte=week_start, paid=True)
    weekly_income = weekly_bookings.aggregate(Sum('amount'))['amount__sum'] or 0
    weekly_count = weekly_bookings.count()
    
    # Monthly income
    monthly_bookings = TurfBooked.objects.filter(selected_date__gte=month_start, paid=True)
    monthly_income = monthly_bookings.aggregate(Sum('amount'))['amount__sum'] or 0
    monthly_count = monthly_bookings.count()
    
    # Yearly income
    yearly_bookings = TurfBooked.objects.filter(selected_date__gte=year_start, paid=True)
    yearly_income = yearly_bookings.aggregate(Sum('amount'))['amount__sum'] or 0
    yearly_count = yearly_bookings.count()
    
    context = {
        'daily_income': daily_income,
        'daily_count': daily_count,
        'weekly_income': weekly_income,
        'weekly_count': weekly_count,
        'monthly_income': monthly_income,
        'monthly_count': monthly_count,
        'yearly_income': yearly_income,
        'yearly_count': yearly_count,
        'recent_bookings': recent_bookings
    }
    
    return render(request, 'owner_dashboard.html', context)

@login_required(login_url='owner_login')
def owner_bookings(request):
    # Fetch all bookings
    bookings = TurfBooked.objects.order_by('-id')
    return render(request, 'owner_bookings.html', {'bookings': bookings})

@login_required(login_url='owner_login')
def owner_payments(request):
    if not request.user.is_staff:
        return redirect('book_now')
    
    # Show all paid bookings
    payments = TurfBooked.objects.filter(paid=True).order_by('-id')
    return render(request, 'owner_payments.html', {'payments': payments})

@login_required(login_url='owner_login')
def owner_settings(request):
    if not request.user.is_staff:
        return redirect('book_now')
    
    turf_settings = TurfSettings.objects.first()
    
    if request.method == 'POST':
        turf_settings.name = request.POST.get('name')
        turf_settings.slot_price = request.POST.get('slot_price')
        turf_settings.description = request.POST.get('description')
        turf_settings.address = request.POST.get('address')
        turf_settings.contact_phone = request.POST.get('contact_phone')
        turf_settings.contact_email = request.POST.get('contact_email')
        turf_settings.save()
        messages.success(request, 'Settings updated successfully!')
        return redirect('owner_settings')
    
    return render(request, 'owner_settings.html', {'turf_settings': turf_settings})

@login_required(login_url='owner_login')
def owner_analytics(request):
    if not request.user.is_staff:
        return redirect('book_now')
    
    # Get date range from request or default to last 7 days
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')
    view_type = request.GET.get('view_type', 'daily') # daily, weekly, monthly, yearly
    
    if start_date_str and end_date_str:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    else:
        end_date = date.today()
        start_date = end_date - timedelta(days=6)
    
    # Generate date range
    date_range = []
    current_date = start_date
    while current_date <= end_date:
        date_range.append(current_date.strftime('%Y-%m-%d'))
        current_date += timedelta(days=1)
    
    # Get bookings data
    bookings_data = []
    revenue_data = []
    avg_revenue_data = []
    
    for day in date_range:
        day_bookings = TurfBooked.objects.filter(selected_date=day, paid=True)
        bookings_count = day_bookings.count()
        revenue = day_bookings.aggregate(Sum('amount'))['amount__sum'] or 0
        
        # Calculate average revenue per booking
        avg_revenue = revenue / bookings_count if bookings_count > 0 else 0
        
        bookings_data.append(bookings_count)
        revenue_data.append(revenue)
        avg_revenue_data.append(avg_revenue)
    
    # Make sure there are no pending payment records in the system
    pending_records = TurfBooked.objects.filter(payment_method='pending')
    if pending_records.exists():
        pending_records.delete()
    
    # Get all paid bookings for analytics
    all_bookings = TurfBooked.objects.filter(
        selected_date__gte=start_date.strftime('%Y-%m-%d'),
        selected_date__lte=end_date.strftime('%Y-%m-%d'),
        paid=True
    )
    
    # Payment method distribution
    payment_methods = all_bookings.values('payment_method').annotate(
        count=Count('payment_method'),
        total=Sum('amount')
    ).order_by('-count')
    
    payment_method_labels = []
    payment_method_counts = []
    payment_method_amounts = []
    
    for method in payment_methods:
        method_name = method['payment_method']
        if method_name == 'credit_card':
            method_name = 'Credit/Debit Card'
        elif method_name == 'upi':
            method_name = 'UPI'
        elif method_name == 'net_banking':
            method_name = 'Net Banking'
        elif method_name == 'wallet':
            method_name = 'Wallet'
            
        payment_method_labels.append(method_name)
        payment_method_counts.append(method['count'])
        payment_method_amounts.append(method['total'])
    
    # Get time-based analytics (daily, weekly, monthly, yearly)
    time_based_data = {
        'labels': [],
        'bookings': [],
        'revenue': []
    }
    
    if view_type == 'daily':
        # Already have the daily data
        time_based_data['labels'] = date_range
        time_based_data['bookings'] = bookings_data
        time_based_data['revenue'] = revenue_data
    elif view_type == 'weekly':
        # Group by week
        start_week = start_date - timedelta(days=start_date.weekday())
        end_week = end_date + timedelta(days=(6-end_date.weekday()))
        current_week = start_week
        
        while current_week <= end_week:
            week_end = current_week + timedelta(days=6)
            week_label = f"{current_week.strftime('%d %b')} - {week_end.strftime('%d %b %Y')}"
            
            week_bookings = TurfBooked.objects.filter(
                selected_date__gte=current_week.strftime('%Y-%m-%d'),
                selected_date__lte=week_end.strftime('%Y-%m-%d'),
                paid=True
            )
            
            bookings_count = week_bookings.count()
            revenue = week_bookings.aggregate(Sum('amount'))['amount__sum'] or 0
            
            time_based_data['labels'].append(week_label)
            time_based_data['bookings'].append(bookings_count)
            time_based_data['revenue'].append(revenue)
            
            current_week += timedelta(days=7)
    elif view_type == 'monthly':
        # Group by month
        months = {}
        for booking in all_bookings:
            booking_date = datetime.strptime(booking.selected_date, '%Y-%m-%d').date()
            month_key = booking_date.strftime('%Y-%m')
            month_label = booking_date.strftime('%b %Y')
            
            if month_key not in months:
                months[month_key] = {
                    'label': month_label,
                    'count': 0,
                    'revenue': 0
                }
            
            months[month_key]['count'] += 1
            months[month_key]['revenue'] += booking.amount
        
        # Sort months and add to time_based_data
        for month_key in sorted(months.keys()):
            time_based_data['labels'].append(months[month_key]['label'])
            time_based_data['bookings'].append(months[month_key]['count'])
            time_based_data['revenue'].append(months[month_key]['revenue'])
    elif view_type == 'yearly':
        # Group by year
        years = {}
        for booking in all_bookings:
            booking_date = datetime.strptime(booking.selected_date, '%Y-%m-%d').date()
            year_key = booking_date.strftime('%Y')
            
            if year_key not in years:
                years[year_key] = {
                    'label': year_key,
                    'count': 0,
                    'revenue': 0
                }
            
            years[year_key]['count'] += 1
            years[year_key]['revenue'] += booking.amount
        
        # Sort years and add to time_based_data
        for year_key in sorted(years.keys()):
            time_based_data['labels'].append(years[year_key]['label'])
            time_based_data['bookings'].append(years[year_key]['count'])
            time_based_data['revenue'].append(years[year_key]['revenue'])
    
    # Time slot distribution (which time slots are most popular)
    time_slot_popularity = {}
    
    for booking in all_bookings:
        slots_str = booking.slots.replace('[', '').replace(']', '').replace("'", '').replace('"', '')
        booking_slots = [slot.strip() for slot in slots_str.split(',') if slot.strip()]
        
        for slot in booking_slots:
            if slot not in time_slot_popularity:
                time_slot_popularity[slot] = 0
            time_slot_popularity[slot] += 1
    
    # Sort time slots by popularity
    sorted_time_slots = sorted(time_slot_popularity.items(), key=lambda x: x[1], reverse=True)
    time_slot_labels = [slot[0] for slot in sorted_time_slots[:10]]  # Top 10 slots
    time_slot_counts = [slot[1] for slot in sorted_time_slots[:10]]
    
    context = {
        'date_range': date_range,
        'bookings_data': bookings_data,
        'revenue_data': revenue_data,
        'avg_revenue_data': avg_revenue_data,
        'start_date': start_date.strftime('%Y-%m-%d'),
        'end_date': end_date.strftime('%Y-%m-%d'),
        'view_type': view_type,
        # Payment method chart data
        'payment_method_labels': json.dumps(payment_method_labels),
        'payment_method_counts': json.dumps(payment_method_counts),
        'payment_method_amounts': json.dumps(payment_method_amounts),
        # Time-based data
        'time_labels': json.dumps(time_based_data['labels']),
        'time_bookings': json.dumps(time_based_data['bookings']),
        'time_revenue': json.dumps(time_based_data['revenue']),
        # Time slot popularity
        'time_slot_labels': json.dumps(time_slot_labels),
        'time_slot_counts': json.dumps(time_slot_counts)
    }
    
    return render(request, 'owner_analytics.html', context)

@login_required(login_url='login')
def slot_details(request):
    # Clean up stale pending bookings
    clean_pending_bookings()
    
    if request.method == 'POST':
        currentDate = date.today().strftime("%Y-%m-%d")
        currentTime = datetime.now()  # Get current time
        selectedDate = request.POST['date']
        choosenDay = datetime.strptime(selectedDate, "%Y-%m-%d").strftime("%A")
        
        # Initialize time slots if they don't exist
        initialize_time_slots()
        
        # Get or create the week matrix
        weekMatrix, created = bookslot.objects.get_or_create(
            id=1,
            defaults={
                'week': '[[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]]'
            }
        )
        
        weekMatrixData = eval(weekMatrix.week)
        day_index = {
            "Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
            "Friday": 4, "Saturday": 5, "Sunday": 6
        }
        day_idx = day_index.get(choosenDay, 0)
        current_day_data = weekMatrixData[day_idx]
        
        # Collect all booked slots from multiple sources
        booked_slots = []
        
        # 1. Check TurfBooked model for confirmed bookings
        existing_bookings = TurfBooked.objects.filter(selected_date=selectedDate, paid=True)
        for booking in existing_bookings:
            slots_str = booking.slots.replace('[', '').replace(']', '').replace("'", '').replace('"', '')
            booking_slots = [slot.strip() for slot in slots_str.split(',') if slot.strip()]
            for slot in booking_slots:
                if slot not in booked_slots:
                    booked_slots.append(slot)
                    
                    # Update turfBooking model to reflect this booking
                    slot_booking, created = turfBooking.objects.get_or_create(
                        time_slot=slot,
                        defaults={
                            'isBooked': True,
                            'days': selectedDate
                        }
                    )
                    if not created:
                        slot_booking.isBooked = True
                        slot_booking.days = selectedDate
                        slot_booking.save()
        
        # 2. Also check turfBooking model for any additional booked slots
        booked_slot_records = turfBooking.objects.filter(days=selectedDate, isBooked=True)
        for record in booked_slot_records:
            if record.time_slot not in booked_slots:
                booked_slots.append(record.time_slot)
        
        # Get turf price from settings
        turf_settings = TurfSettings.objects.first()
        slot_price = turf_settings.slot_price if turf_settings else 500
        
        # Time slots with their start hours in 24-hour format
        time_slots = [
            {'id': 1, 'time': '6-7 am', 'hour': 6},
            {'id': 2, 'time': '7-8 am', 'hour': 7},
            {'id': 3, 'time': '8-9 am', 'hour': 8},
            {'id': 4, 'time': '9-10 am', 'hour': 9},
            {'id': 5, 'time': '10-11 am', 'hour': 10},
            {'id': 6, 'time': '11-12 pm', 'hour': 11},
            {'id': 7, 'time': '12-1 pm', 'hour': 12},
            {'id': 8, 'time': '1-2 pm', 'hour': 13},
            {'id': 9, 'time': '2-3 pm', 'hour': 14},
            {'id': 10, 'time': '3-4 pm', 'hour': 15},
            {'id': 11, 'time': '4-5 pm', 'hour': 16},
            {'id': 12, 'time': '5-6 pm', 'hour': 17},
            {'id': 13, 'time': '6-7 pm', 'hour': 18},
            {'id': 14, 'time': '7-8 pm', 'hour': 19},
            {'id': 15, 'time': '8-9 pm', 'hour': 20},
            {'id': 16, 'time': '9-10 pm', 'hour': 21},
            {'id': 17, 'time': '10-11 pm', 'hour': 22},
            {'id': 18, 'time': '11-12 am', 'hour': 23},
            {'id': 19, 'time': '12-1 am', 'hour': 0},
        ]
        
        # Handle expired and booked slots
        current_hour = currentTime.hour
        current_minute = currentTime.minute
        
        # Convert selected date to datetime for comparison
        selected_date_obj = datetime.strptime(selectedDate, "%Y-%m-%d").date()
        current_date_obj = datetime.strptime(currentDate, "%Y-%m-%d").date()
        
        for slot in time_slots:
            if slot['time'] in booked_slots:
                slot['booked'] = True
                slot['expired'] = False
            else:
                slot['booked'] = False
                
                # For past dates, all slots are expired
                if selected_date_obj < current_date_obj:
                    slot['expired'] = True
                # For future dates, no slots are expired
                elif selected_date_obj > current_date_obj:
                    slot['expired'] = False
                # For current date, check time
                else:
                    slot_hour = slot['hour']
                    
                    # Special handling for 12-1 am slot (hour 0)
                    if slot_hour == 0:
                        # 12-1 am is for the next day, so it's never expired on the current day
                        slot['expired'] = False
                    else:
                        # For current hour, check if we're more than 15 minutes into the hour
                        if slot_hour == current_hour:
                            slot['expired'] = current_minute > 15
                        else:
                            slot['expired'] = slot_hour < current_hour
        
        # Mark slots as booked if they are expired
        for slot in time_slots:
            if slot['expired']:
                slot['booked'] = True
        
        context = {
            'time_slots': time_slots,
            'selectedDate': selectedDate,
            'currentDate': currentDate,
            'slot_price': slot_price,
        }
        
        return render(request, 'slotbooking.html', context)
    
    return redirect('home')


def turfBilling(request):
    if request.method == 'POST':
        total_amount = request.POST.get('total_amount')
        username = request.POST.get('username')
        email = request.POST.get('email')
        selected_date = request.POST.get('selected_date')
        current_date = request.POST.get('current_date')
        slots = request.POST.getlist('slots')
        booking_time = datetime.now().strftime('%H:%M:%S')  # Local time without timezone
        
        # Double check for already booked slots before processing
        existing_bookings = TurfBooked.objects.filter(selected_date=selected_date, paid=True)
        booked_slots = []
        
        # Collect all booked slots from existing confirmed bookings
        for booking in existing_bookings:
            booking_slots_str = booking.slots.replace('[', '').replace(']', '').replace("'", '').replace('"', '')
            booking_slots = [slot.strip() for slot in booking_slots_str.split(',') if slot.strip()]
            for slot in booking_slots:
                if slot not in booked_slots:
                    booked_slots.append(slot)
        
        # Also check turfBooking model for any slots marked as booked for this date
        booked_slot_records = turfBooking.objects.filter(days=selected_date, isBooked=True)
        for record in booked_slot_records:
            if record.time_slot not in booked_slots:
                booked_slots.append(record.time_slot)
        
        # Check if any of our requested slots are already booked
        conflicting_slots = []
        for slot in slots:
            if slot in booked_slots:
                conflicting_slots.append(slot)
        
        # If there are conflicts, inform the user and redirect them back
        if conflicting_slots:
            conflict_message = f"The following slot(s) have just been booked by someone else: {', '.join(conflicting_slots)}. Please select different time slots."
            messages.error(request, conflict_message)
            return redirect('slot_details')
        
        # Generate a random alphanumeric string for the order ID
        import random
        import string
        random_string = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
        payment_id = f"order_{random_string}"
        
        # Instead of creating a pending booking, just pass the details to the template
        # The booking will only be created when the user clicks "Pay Now"
        
        # Create details dictionary to pass to template
        details = {
            'username': username,
            'email': email,
            'selectedDate': selected_date,
            'currentDate': current_date,
            'totalAmount': total_amount,
            'bookedSlots': slots,
            'payment_id': payment_id,
            'booking_time': booking_time
        }
        
        return render(request, 'turfBilling.html', {'payment': {"id": payment_id}, 'details': details})

@csrf_exempt
def success(request):
    if request.method == "POST" or request.method == "GET":
        # Get payment/booking ID from the request
        order_id = request.POST.get('order_id', '') or request.GET.get('order_id', '')
        payment_method = request.POST.get('payment_method', 'credit_card')
        
        # Check if this is a new booking from the payment form
        username = request.POST.get('username', '')
        email = request.POST.get('email', '')
        selected_date = request.POST.get('selected_date', '')
        current_date = request.POST.get('current_date', '')
        total_amount = request.POST.get('total_amount', 0)
        booking_time = request.POST.get('booking_time', datetime.now().strftime('%H:%M:%S'))
        booked_slots = request.POST.getlist('booked_slots')
        
        # Create the booking record with payment confirmed
        if username and email and selected_date and order_id:
            # Check if the booking already exists (to prevent duplicate submissions)
            existing_booking = TurfBooked.objects.filter(payment_id=order_id).first()
            if existing_booking:
                booking = existing_booking
            else:
                # Create a new booking record with paid=True
                booking = TurfBooked(
                    name=username,
                    email=email,
                    amount=total_amount,
                    selected_date=selected_date,
                    current_date=current_date,
                    booking_time=booking_time,
                    slots=str(booked_slots),
                    payment_id=order_id,
                    payment_method=payment_method,  # The actual payment method from the form
                    paid=True,  # Mark as paid since payment is confirmed
                    is_dummy_payment=True
                )
                booking.save()
        else:
            # If missing required parameters, show error
            messages.error(request, "Invalid booking details provided!")
            return redirect('book_now')
        
        # Parse the slots string into a list
        if isinstance(booking.slots, str):
            booking_slots_str = booking.slots.replace('[', '').replace(']', '').replace("'", '').replace('"', '')
            booked_slots = [slot.strip() for slot in booking_slots_str.split(',') if slot.strip()]
        
        # Mark slots as booked - only after payment is confirmed
        for slot in booked_slots:
            # Update turfBooking model to mark this slot as booked
            try:
                slot_obj = turfBooking.objects.get(time_slot=slot)
                slot_obj.isBooked = True
                slot_obj.days = booking.selected_date
                slot_obj.save()
            except turfBooking.DoesNotExist:
                # Create the slot if it doesn't exist
                new_slot = turfBooking(time_slot=slot, isBooked=True, days=booking.selected_date)
                new_slot.save()
        
        # Update slot bookings in the weekly matrix
        choosenDay = datetime.strptime(booking.selected_date, "%Y-%m-%d").strftime("%A")
        day_index = {
            "Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
            "Friday": 4, "Saturday": 5, "Sunday": 6
        }
        day_idx = day_index.get(choosenDay, 0)
        
        # Update the bookslot model
        try:
            # Update bookslot model
            matrix = bookslot.objects.get(id='1')
            matrix_week = json.loads(matrix.week)
            
            # Get slot mapping
            slot_mapping = {
                '6-7 am': 1, '7-8 am': 2, '8-9 am': 3, '9-10 am': 4, 
                '10-11 am': 5, '11-12 pm': 6, '12-1 pm': 7, '1-2 pm': 8, 
                '2-3 pm': 9, '3-4 pm': 10, '4-5 pm': 11, '5-6 pm': 12,
                '6-7 pm': 13, '7-8 pm': 14, '8-9 pm': 15, '9-10 pm': 16,
                '10-11 pm': 17, '11-12 am': 18, '12-1 am': 19
            }
            
            # Mark slots as booked in the matrix
            for slot_time in booked_slots:
                slot_id = slot_mapping.get(slot_time)
                if slot_id:
                    matrix_week[day_idx][slot_id] = 1
            
            # Save the updated matrix
            matrix.week = json.dumps(matrix_week)
            matrix.save()
        except Exception as e:
            # Just log the error if we can't update the matrix
            print(f"Error updating booking matrix: {str(e)}")
        
        # If email is enabled, try to send a confirmation email
        if getattr(settings, 'ENABLE_EMAIL', False):
            try:
                # Prepare context for email templates
                email_context = {
                    'name': booking.name,
                    'amount': booking.amount,
                    'payment_method': booking.payment_method,
                    'selected_date': booking.selected_date,
                    'current_date': booking.current_date,
                    'booking_time': booking.booking_time,
                    'payment_id': booking.payment_id,
                    'slots': booked_slots,
                    'is_dummy': booking.is_dummy_payment
                }

                # Render both versions of the email
                message_plain = render_to_string('booking_confirmation_email.txt', email_context)
                message_html = render_to_string('booking_confirmation_email.html', email_context)

                # Create the email message
                subject = 'The SK Sports Park - Turf Booking Confirmation and Receipt'
                from_email = settings.DEFAULT_FROM_EMAIL
                to_email = [booking.email]

                # Send a single email
                send_mail(
                    subject=subject,
                    message=message_plain,
                    from_email=from_email,
                    recipient_list=to_email,
                    fail_silently=False
                )

                messages.success(request, "Booking confirmation email sent successfully!")
                
            except Exception as e:
                # Log the detailed error for debugging
                print(f"Error sending email - Type: {type(e).__name__}, Error: {str(e)}")
                if hasattr(e, 'smtp_error'):
                    print(f"SMTP Error: {e.smtp_error}")
                messages.warning(request, "Booking confirmed but there was an issue sending the confirmation email. Our team has been notified.")
        
        messages.success(request, "Booking confirmed successfully!")
        
        # Render the success template with booking details
        context = {
            'booking': booking,
            'booked_slots': booked_slots,
            'now': datetime.now().strftime('%Y-%m-%d')
        }
        return render(request, 'success.html', context)


@login_required(login_url='login')
def orderHistory(request):
    my_bookings = TurfBooked.objects.filter(paid=True).filter(email=request.user.email)
    currentDate = date.today().strftime("%Y-%m-%d")
    return render(request, 'orderHistory.html', {'bookings': my_bookings, 'currentDate': currentDate})


def delete_booking(request, id):
    # Handle both GET and POST requests for admin dashboard
    try:
        booking = TurfBooked.objects.get(id=id)
        selectedDate = booking.selected_date
        
        # Parse slots string correctly based on its format
        if isinstance(booking.slots, str):
            # Clean up the string representation
            slots_str = booking.slots.replace('[', '').replace(']', '').replace("'", '').replace('"', '')
            slots = [slot.strip() for slot in slots_str.split(',') if slot.strip()]
        else:
            slots = []

        bookedSlots = []
        
        # Map slot times to slot IDs
        slot_mapping = {
            '6-7 am': 1, '7-8 am': 2, '8-9 am': 3, '9-10 am': 4, 
            '10-11 am': 5, '11-12 pm': 6, '12-1 pm': 7, '1-2 pm': 8, 
            '2-3 pm': 9, '3-4 pm': 10, '4-5 pm': 11, '5-6 pm': 12,
            '6-7 pm': 13, '7-8 pm': 14, '8-9 pm': 15, '9-10 pm': 16,
            '10-11 pm': 17, '11-12 am': 18, '12-1 am': 19
        }
        
        # Get slot IDs for each booked time slot
        for slot_time in slots:
            slot_id = slot_mapping.get(slot_time)
            if slot_id:
                bookedSlots.append(slot_id)
        
        # Get the day of the booking
        choosenDay = datetime.strptime(selectedDate, "%Y-%m-%d").strftime("%A")
        
        # Map day names to array indices
        day_index = {
            "Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
            "Friday": 4, "Saturday": 5, "Sunday": 6
        }
        
        day_idx = day_index.get(choosenDay, 0)
        
        try:
            matrix = bookslot.objects.get(id='1')
            matrix_week = json.loads(matrix.week)
            
            # Update the bookslot model
            for slot_id in bookedSlots:
                if 1 <= slot_id <= 19:
                    matrix_week[day_idx][slot_id] = 0  # Mark as available
            
            # Save changes to the bookslot matrix
            matrix.week = json.dumps(matrix_week)
            matrix.save()
        except Exception as e:
            # Log error but continue with deletion
            print(f"Error updating matrix: {str(e)}")
        
        # Update the turfBooking model for each cancelled slot
        for slot_time in slots:
            try:
                slot_obj = turfBooking.objects.filter(time_slot=slot_time, days=selectedDate).first()
                if slot_obj:
                    slot_obj.isBooked = False
                    slot_obj.days = ''  # Clear the date to make it available
                    slot_obj.save()
            except Exception as e:
                # Log error but continue with deletion
                print(f"Error updating slot {slot_time}: {str(e)}")
        
        # Store booking info for message
        booking_info = f"Booking #{id} for {selectedDate}"
        
        # Delete the booking
        booking.delete()
        
        # Add success message
        if request.user.is_staff:
            messages.success(request, f"{booking_info} has been cancelled successfully!")
        else:
            messages.success(request, "Your booking has been cancelled successfully!")
        
    except TurfBooked.DoesNotExist:
        # Handle the case where the booking with the given ID doesn't exist
        messages.error(request, "Booking not found! It may have been already cancelled or doesn't exist.")
    
    # Determine where to redirect based on user role
    if request.user.is_staff:
        return redirect('owner_bookings')
    else:
        return redirect('orderHistory')


def allBookings(request):
    bookings = TurfBooked.objects.filter(paid = True).order_by('selected_date', 'booking_time')
    currentDate = date.today().strftime("%Y-%m-%d")
    return render(request, 'allBookings.html', {'bookings': bookings, 'currentDate': currentDate})


def searchBooking(request):
    query = request.POST['query']
    bookings = TurfBooked.objects.filter(name__icontains=query)
    return render(request, 'allBookings.html', {'bookings': bookings, 'query': query})


def check_slot_status(request):
    """
    API endpoint to check slot availability in real-time
    Returns a JSON response with booked slots for a specific date
    Only considers slots from paid/confirmed bookings
    """
    selected_date = request.GET.get('date')
    
    if not selected_date:
        return JsonResponse({'error': 'Date parameter is required'}, status=400)
    
    # Get all confirmed (paid) bookings for the selected date
    existing_bookings = TurfBooked.objects.filter(selected_date=selected_date, paid=True)
    
    # Collect all booked slots for this date
    booked_slots = []
    
    for booking in existing_bookings:
        # Extract slots from the booking.slots string
        slots_str = booking.slots
        # Clean up the string representation
        slots_str = slots_str.replace('[', '').replace(']', '').replace("'", '').replace('"', '')
        slot_list = [slot.strip() for slot in slots_str.split(',') if slot.strip()]
        
        # Add each slot to our booked list
        for slot in slot_list:
            if slot not in booked_slots:
                booked_slots.append(slot)
    
    # Also check turfBooking model for any additional booked slots
    # Only consider slots that are marked as both booked and belong to the selected date
    booked_slot_records = turfBooking.objects.filter(days=selected_date, isBooked=True)
    for record in booked_slot_records:
        if record.time_slot not in booked_slots:
            booked_slots.append(record.time_slot)
    
    return JsonResponse({
        'date': selected_date,
        'booked_slots': booked_slots
    })


#
# present = datetime.now()
# dayTobeDeleated = (datetime.now() - timedelta(days=1)).strftime("%A")
# print(dayTobeDeleated)
#
# schedule.every().day.at("00:00").do(deleteRecord, dayTobeDeleated)

def check_username(request):
    """API endpoint to check if a username already exists"""
    username = request.GET.get('username', '')
    exists = User.objects.filter(username=username).exists()
    return JsonResponse({'exists': exists})

def check_email(request):
    """API endpoint to check if an email already exists"""
    email = request.GET.get('email', '')
    exists = User.objects.filter(email=email).exists()
    return JsonResponse({'exists': exists})

def clean_pending_bookings():
    """Helper function to clean up stale bookings"""
    # This function is now a placeholder since we no longer create pending bookings
    # We can keep it for future use or logging purposes
    return 0
