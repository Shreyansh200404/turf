from django.shortcuts import get_object_or_404, render, redirect, HttpResponse
from django.urls import reverse
from django.contrib.auth.models import User, auth
from django.contrib.auth.forms import PasswordResetForm, SetPasswordForm
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from .models import *
from datetime import date, datetime, time as day_time, timedelta
import ast
import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import uuid
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import F, Q, Sum, Count

from django.template.loader import render_to_string
from django.conf import settings
from django.core.mail import send_mail, EmailMessage
from django.http import JsonResponse
from django.template.defaulttags import register
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.utils import timezone as django_timezone
from django.views.decorators.http import require_GET, require_POST
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
from .forms import FeedbackAdminForm, FeedbackForm, TurfForm
try:
    from pytz import timezone
except ImportError:
    # If pytz is not available, create a dummy timezone function
    def timezone(zone=None):
        return None

logger = logging.getLogger(__name__)

PASSWORD_RESET_OTP_SESSION_KEY = 'password_reset_otp'
PASSWORD_RESET_OTP_TIMEOUT = 10 * 60
PASSWORD_RESET_OTP_MAX_ATTEMPTS = 5
CANCELLATION_REFUND_CUTOFF = timedelta(hours=2)
SLOT_RESERVATION_TIMEOUT = timedelta(minutes=10)

SLOT_START_HOURS = {
    '6-7 am': 6, '7-8 am': 7, '8-9 am': 8, '9-10 am': 9,
    '10-11 am': 10, '11-12 pm': 11, '12-1 pm': 12, '1-2 pm': 13,
    '2-3 pm': 14, '3-4 pm': 15, '4-5 pm': 16, '5-6 pm': 17,
    '6-7 pm': 18, '7-8 pm': 19, '8-9 pm': 20, '9-10 pm': 21,
    '10-11 pm': 22, '11-12 am': 23, '12-1 am': 0,
}
SLOT_IDS = {
    '6-7 am': 1, '7-8 am': 2, '8-9 am': 3, '9-10 am': 4,
    '10-11 am': 5, '11-12 pm': 6, '12-1 pm': 7, '1-2 pm': 8,
    '2-3 pm': 9, '3-4 pm': 10, '4-5 pm': 11, '5-6 pm': 12,
    '6-7 pm': 13, '7-8 pm': 14, '8-9 pm': 15, '9-10 pm': 16,
    '10-11 pm': 17, '11-12 am': 18, '12-1 am': 19,
}


class RefundProcessingError(Exception):
    pass


class RazorpayError(Exception):
    pass


def _razorpay_credentials():
    mode = getattr(settings, 'RAZORPAY_MODE', 'test')
    key_id = getattr(settings, 'RAZOR_KEY_ID', '').strip()
    key_secret = getattr(settings, 'RAZOR_KEY_SECRET', '').strip()
    expected_prefix = {'test': 'rzp_test_', 'live': 'rzp_live_'}.get(mode)
    if expected_prefix is None:
        raise RazorpayError('RAZORPAY_MODE must be set to either test or live.')
    if not key_id or not key_secret:
        raise RazorpayError(
            f'Razorpay {mode} credentials are not configured. Please contact the venue.'
        )
    if not key_id.startswith(expected_prefix):
        raise RazorpayError(
            f'Razorpay is configured for {mode} mode but the API key does not match. '
            'Use a matching key ID and secret from the same Razorpay mode.'
        )
    return key_id, key_secret


class BookingSlotsUnavailable(Exception):
    def __init__(self, slots):
        self.slots = slots


def _active_turf(turf_id=None):
    turfs = Turf.objects.filter(is_active=True)
    if turf_id:
        try:
            turf_id = int(turf_id)
        except (TypeError, ValueError):
            return None
        return turfs.filter(pk=turf_id).first()
    return turfs.filter(is_default=True).first() or turfs.order_by('name', 'pk').first()


def _current_time():
    return django_timezone.now()


def _booking_slots(booking):
    try:
        slots = json.loads(booking.slots)
    except (TypeError, ValueError):
        try:
            slots = ast.literal_eval(booking.slots)
        except (SyntaxError, TypeError, ValueError):
            return []
    if not isinstance(slots, list):
        return []
    return [str(slot).strip() for slot in slots if str(slot).strip()]


def _booking_start_datetime(booking):
    try:
        selected_date = date.fromisoformat(booking.selected_date)
    except (TypeError, ValueError):
        return None

    slots = _booking_slots(booking)
    if not slots:
        return None
    slot_candidates = [slot for slot in slots if slot in SLOT_IDS]
    if not slot_candidates:
        return None
    first_slot = min(slot_candidates, key=SLOT_IDS.get)
    start_hour = SLOT_START_HOURS[first_slot]
    if first_slot == '12-1 am':
        selected_date += timedelta(days=1)

    start = datetime.combine(selected_date, day_time(hour=start_hour))
    return django_timezone.make_aware(
        start,
        ZoneInfo(getattr(settings, 'TURF_TIME_ZONE', settings.TIME_ZONE)),
    )


def _slot_start_datetime(selected_date, slot):
    booking_date = date.fromisoformat(selected_date)
    slot_hour = SLOT_START_HOURS[slot]
    if slot == '12-1 am':
        booking_date += timedelta(days=1)
    return django_timezone.make_aware(
        datetime.combine(booking_date, day_time(hour=slot_hour)),
        ZoneInfo(getattr(settings, 'TURF_TIME_ZONE', settings.TIME_ZONE)),
    )


def _update_default_turf_slot_records(turf, selected_date, old_slots, new_slots):
    if not turf.is_default:
        return

    removed_slots = set(old_slots) - set(new_slots)
    added_slots = set(new_slots) - set(old_slots)
    try:
        chosen_day = datetime.strptime(selected_date, '%Y-%m-%d').strftime('%A')
        day_idx = {
            'Monday': 0, 'Tuesday': 1, 'Wednesday': 2,
            'Thursday': 3, 'Friday': 4, 'Saturday': 5, 'Sunday': 6,
        }[chosen_day]
        matrix, _ = bookslot.objects.select_for_update().get_or_create(
            pk=1,
            defaults={'week': json.dumps([[0] * 20 for _ in range(7)])},
        )
        matrix_week = json.loads(matrix.week)
        for slot in removed_slots:
            matrix_week[day_idx][SLOT_IDS[slot]] = 0
        for slot in added_slots:
            matrix_week[day_idx][SLOT_IDS[slot]] = 1
        matrix.week = json.dumps(matrix_week)
        matrix.save(update_fields=['week'])
    except (bookslot.DoesNotExist, ValueError, KeyError, IndexError, TypeError):
        logger.exception('Could not update the booking matrix for %s', selected_date)

    for slot in removed_slots:
        turfBooking.objects.filter(
            time_slot=slot,
            days=selected_date,
            isBooked=True,
        ).update(isBooked=False, days='')
    for slot in added_slots:
        record, _ = turfBooking.objects.get_or_create(
            time_slot=slot,
            defaults={'isBooked': True, 'days': selected_date},
        )
        record.isBooked = True
        record.days = selected_date
        record.save(update_fields=['isBooked', 'days'])


def _booking_slot_conflicts(
    turf,
    selected_date,
    slots,
    exclude_booking_id,
    existing_slots,
    exclude_reservation_id=None,
):
    bookings = TurfBooked.objects.filter(
        turf=turf,
        selected_date=selected_date,
        paid=True,
        cancelled=False,
    ).exclude(pk=exclude_booking_id)
    occupied_slots = set()
    for booking in bookings:
        occupied_slots.update(_booking_slots(booking))
    if turf.is_default:
        legacy_occupied_slots = set(
            turfBooking.objects.filter(
                days=selected_date,
                isBooked=True,
            ).values_list('time_slot', flat=True)
        )
        legacy_occupied_slots.difference_update(set(existing_slots) - occupied_slots)
        occupied_slots.update(legacy_occupied_slots)
    reservations = TurfSlotReservation.objects.filter(
        turf=turf,
        selected_date=selected_date,
        expires_at__gt=_current_time(),
    )
    if exclude_reservation_id:
        reservations = reservations.exclude(reservation_id=exclude_reservation_id)
    occupied_slots.update(reservations.values_list('slot', flat=True))
    return sorted(set(slots) & occupied_slots)


def _reserve_booking_slots(turf, selected_date, slots):
    reservation_id = uuid.uuid4().hex
    now = _current_time()
    try:
        with transaction.atomic():
            Turf.objects.select_for_update().get(pk=turf.pk, is_active=True)
            TurfSlotReservation.objects.filter(
                turf=turf,
                selected_date=selected_date,
                slot__in=slots,
                expires_at__lte=now,
            ).delete()
            conflicts = _booking_slot_conflicts(
                turf,
                selected_date,
                slots,
                None,
                [],
            )
            if conflicts:
                raise BookingSlotsUnavailable(conflicts)

            TurfSlotReservation.objects.bulk_create([
                TurfSlotReservation(
                    turf=turf,
                    selected_date=selected_date,
                    slot=slot,
                    reservation_id=reservation_id,
                    expires_at=now + SLOT_RESERVATION_TIMEOUT,
                )
                for slot in slots
            ])
    except IntegrityError:
        logger.info(
            'Concurrent reservation conflict for turf %s on %s',
            turf.pk,
            selected_date,
        )
        raise BookingSlotsUnavailable(slots)
    return reservation_id


def _release_slot_reservation(reservation_id):
    if reservation_id:
        TurfSlotReservation.objects.filter(reservation_id=reservation_id).delete()


def _record_booking_payment_if_present(booking_id, **payment_values):
    try:
        with transaction.atomic():
            booking = TurfBooked.objects.select_for_update().filter(pk=booking_id).first()
            if booking is None:
                return False
            TurfBookingPayment.objects.create(booking=booking, **payment_values)
            return True
    except DatabaseError:
        logger.exception('Could not record payment for booking %s', booking_id)
        return False


def _send_booking_update_email(request, booking, old_slots, new_slots, old_amount, new_amount):
    difference = new_amount - old_amount
    if difference > 0:
        adjustment = f'Additional payment received: ₹{difference}.'
    elif difference < 0:
        adjustment = f'Price reduction: ₹{-difference} refund requested.'
    else:
        adjustment = 'There was no change to the booking price.'

    try:
        send_mail(
            subject='SK Sports Park booking updated',
            message=(
                f'Hello {booking.name},\n\n'
                'Your turf booking has been updated successfully.\n\n'
                f'Turf: {booking.turf_name}\n'
                f'Date: {booking.selected_date}\n'
                f'Previous time slots: {", ".join(old_slots)}\n'
                f'Updated time slots: {", ".join(new_slots)}\n'
                f'Previous total: ₹{old_amount}\n'
                f'Updated total: ₹{new_amount}\n'
                f'{adjustment}\n'
                f'Booking reference: {booking.payment_id}\n\n'
                'SK Sports Park'
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[booking.email],
            fail_silently=False,
        )
    except Exception:
        logger.exception('Booking %s was updated but its notification email could not be sent', booking.pk)
        messages.warning(
            request,
            'Booking updated, but we could not send the email notification. Please check your email address or contact the venue.',
        )
        return False
    return True


def _request_razorpay_refund(payment_id, amount_paise, booking_id):
    try:
        key_id, key_secret = _razorpay_credentials()
    except RazorpayError as exc:
        raise RefundProcessingError(str(exc)) from exc
    if not re.fullmatch(r'pay_[A-Za-z0-9]+', payment_id):
        raise RefundProcessingError('This booking does not have a valid Razorpay payment ID.')

    authorization = base64.b64encode(
        f'{key_id}:{key_secret}'.encode('utf-8')
    ).decode('ascii')
    payload = json.dumps({
        'amount': amount_paise,
        'notes': {'booking_id': str(booking_id)},
    }).encode('utf-8')
    api_request = Request(
        f'https://api.razorpay.com/v1/payments/{payment_id}/refund',
        data=payload,
        headers={
            'Authorization': f'Basic {authorization}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )

    try:
        with urlopen(api_request, timeout=15) as response:
            refund = json.loads(response.read().decode('utf-8'))
    except HTTPError as exc:
        logger.warning(
            'Razorpay refund request failed for booking %s with HTTP status %s',
            booking_id,
            exc.code,
        )
        raise RefundProcessingError(
            'Razorpay could not confirm the refund. The booking was cancelled for review.'
        ) from exc
    except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.exception('Razorpay refund request failed for booking %s', booking_id)
        raise RefundProcessingError(
            'Could not confirm the Razorpay refund. The booking was cancelled for review.'
        ) from exc

    if not isinstance(refund, dict):
        logger.error('Razorpay returned an unexpected refund response for booking %s', booking_id)
        raise RefundProcessingError(
            'Razorpay returned an unexpected refund response. The booking was cancelled for review.'
        )
    refund_id = refund.get('id')
    refund_status = refund.get('status')
    if not refund_id or refund_status not in {'pending', 'processed'}:
        logger.error('Razorpay returned an unexpected refund response for booking %s', booking_id)
        raise RefundProcessingError(
            'Razorpay returned an unexpected refund response. The booking was cancelled for review.'
        )
    return refund_id, refund_status


def _razorpay_request(method, path, payload=None):
    key_id, key_secret = _razorpay_credentials()

    authorization = base64.b64encode(
        f'{key_id}:{key_secret}'.encode('utf-8')
    ).decode('ascii')
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    api_request = Request(
        f'https://api.razorpay.com/v1/{path}',
        data=data,
        headers={
            'Authorization': f'Basic {authorization}',
            'Content-Type': 'application/json',
        },
        method=method,
    )
    try:
        with urlopen(api_request, timeout=20) as response:
            result = json.loads(response.read().decode('utf-8'))
    except HTTPError as exc:
        logger.warning('Razorpay API returned HTTP %s for %s', exc.code, path)
        if exc.code == 401:
            raise RazorpayError(
                'Razorpay authentication failed. Check that RAZOR_KEY_ID and '
                'RAZOR_KEY_SECRET are valid, paired credentials for the same '
                'Razorpay mode (test or live).'
            ) from exc
        raise RazorpayError('The payment provider could not process this request. Please try again.') from exc
    except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.exception('Razorpay API request failed for %s', path)
        raise RazorpayError('Could not connect to the payment provider. Please try again.') from exc

    if not isinstance(result, dict):
        raise RazorpayError('The payment provider returned an invalid response.')
    return result


def _render_checkout(request, details, order, error=None):
    return render(request, 'turfBilling.html', {
        'details': details,
        'razorpay_order': order,
        'razorpay_key_id': settings.RAZOR_KEY_ID,
        'razorpay_mode': getattr(settings, 'RAZORPAY_MODE', 'test'),
        'checkout_error': error,
    })


def password_reset(request):
    if request.method == 'POST':
        form = PasswordResetForm(request.POST)
        if form.is_valid():
            user = next(form.get_users(form.cleaned_data['email']), None)
            otp = f'{secrets.randbelow(1_000_000):06d}'
            reset_data = {
                'user_id': user.pk if user else None,
                'otp_hash': make_password(otp) if user else None,
                'expires_at': time.time() + PASSWORD_RESET_OTP_TIMEOUT,
                'attempts': 0,
                'verified': False,
            }
            request.session[PASSWORD_RESET_OTP_SESSION_KEY] = reset_data

            if user:
                send_mail(
                    subject='Your SK Sports Park password reset code',
                    message=(
                        f'Your password reset code is {otp}.\n\n'
                        'This code expires in 10 minutes. If you did not request '
                        'a password reset, you can ignore this email.'
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[user.email],
                    fail_silently=False,
                )

            return redirect('password_reset_verify')
    else:
        form = PasswordResetForm()

    form.fields['email'].widget.attrs.update({
        'class': 'form-control',
        'autocomplete': 'email',
        'placeholder': 'Email address',
    })
    return render(request, 'password_reset.html', {'form': form})


def password_reset_verify(request):
    reset_data = request.session.get(PASSWORD_RESET_OTP_SESSION_KEY)
    if not reset_data:
        messages.info(request, 'Please request a new password reset code.')
        return redirect('password_reset')
    if reset_data.get('verified'):
        return redirect('password_reset_new_password')

    if time.time() >= reset_data.get('expires_at', 0):
        request.session.pop(PASSWORD_RESET_OTP_SESSION_KEY, None)
        messages.error(request, 'That code has expired. Please request a new one.')
        return redirect('password_reset')

    if request.method == 'POST':
        otp = request.POST.get('otp', '').strip()
        if not otp.isascii() or not otp.isdigit() or len(otp) != 6:
            messages.error(request, 'Enter the 6-digit code sent to your email.')
        else:
            reset_data['attempts'] += 1
            if (
                reset_data['user_id']
                and reset_data['otp_hash']
                and check_password(otp, reset_data['otp_hash'])
            ):
                reset_data['verified'] = True
                reset_data.pop('otp_hash', None)
                request.session[PASSWORD_RESET_OTP_SESSION_KEY] = reset_data
                return redirect('password_reset_new_password')

            if reset_data['attempts'] >= PASSWORD_RESET_OTP_MAX_ATTEMPTS:
                request.session.pop(PASSWORD_RESET_OTP_SESSION_KEY, None)
                messages.error(
                    request,
                    'Too many incorrect attempts. Please request a new code.',
                )
                return redirect('password_reset')

            request.session[PASSWORD_RESET_OTP_SESSION_KEY] = reset_data
            messages.error(request, 'The code is invalid. Please try again.')

    return render(request, 'password_reset_otp.html')


def password_reset_new_password(request):
    reset_data = request.session.get(PASSWORD_RESET_OTP_SESSION_KEY)
    if (
        not reset_data
        or not reset_data.get('verified')
        or time.time() >= reset_data.get('expires_at', 0)
    ):
        request.session.pop(PASSWORD_RESET_OTP_SESSION_KEY, None)
        messages.error(request, 'Your password reset session expired. Please start again.')
        return redirect('password_reset')

    user = User.objects.filter(
        pk=reset_data.get('user_id'),
        is_active=True,
    ).first()
    if user is None:
        request.session.pop(PASSWORD_RESET_OTP_SESSION_KEY, None)
        messages.error(request, 'This password reset request is no longer valid.')
        return redirect('password_reset')

    form = SetPasswordForm(user, request.POST or None)
    form.fields['new_password1'].widget.attrs.update({
        'class': 'form-control',
        'autocomplete': 'new-password',
    })
    form.fields['new_password2'].widget.attrs.update({
        'class': 'form-control',
        'autocomplete': 'new-password',
    })
    if request.method == 'POST' and form.is_valid():
        form.save()
        request.session.pop(PASSWORD_RESET_OTP_SESSION_KEY, None)
        return redirect('password_reset_complete')

    return render(request, 'password_reset_confirm.html', {'form': form})


@require_GET
def confirm_email(request, uidb64, token):
    try:
        user_id = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=user_id)
    except (
        User.DoesNotExist,
        binascii.Error,
        ValueError,
        TypeError,
        UnicodeDecodeError,
    ):
        user = None

    if user is None or user.is_active or not default_token_generator.check_token(user, token):
        messages.error(request, 'This confirmation link is invalid or has expired.')
        return redirect('signup')

    user.is_active = True
    user.save(update_fields=['is_active'])
    messages.success(request, 'Your email is confirmed. You can now log in.')
    return redirect('login')


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
    public_feedback = Feedback.objects.only(
        'rating',
        'message',
        'admin_response',
        'created_at',
    )
    context = {
        'turf': turf_settings,
        'feedback_items': public_feedback,
    }
    
    if request.user.is_authenticated:
        context['username'] = request.user.username
    return render(request, 'mainpage_index.html', context)


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
    
    turfs = Turf.objects.filter(is_active=True).order_by('name', 'pk')
    if request.user.is_authenticated:
        username = request.user.username
        return render(request, 'booking_index.html', {'username': username, 'turfs': turfs})
    return render(request, 'booking_index.html', {'turfs': turfs})


def turf_details(request):
    # Clean up stale pending bookings
    clean_pending_bookings()
    
    currentDate = date.today().strftime("%Y-%m-%d")
    endDate = (date.today() + timedelta(days=6)).strftime("%Y-%m-%d")
    turfs = Turf.objects.filter(is_active=True).order_by('name', 'pk')
    requested_turf = request.GET.get('turf_id')
    turf = _active_turf(requested_turf)
    if turf is None:
        messages.error(request, 'No turfs are currently available for booking.')
        return redirect('book_now')
    return render(request, 'turfblog.html', {
        'currentDate': currentDate,
        'endDate': endDate,
        'turf': turf,
        'turfs': turfs,
    })


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
        
        # Create an active account and notify the registrant.
        user = None
        try:
            user = User.objects.create_user(
                username=username,
                password=password,
                email=email,
                is_active=True,
            )
            send_mail(
                subject='Registration successful - SK Sports Park',
                message=(
                    f'Hello {username},\n\n'
                    'Your SK Sports Park account has been created successfully. '
                    'You can now sign in using your username and password.'
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[email],
                fail_silently=False,
            )
            messages.success(
                request,
                'Registration successful. A notification email has been sent to your address.',
            )
            return redirect('login')
        except Exception:
            if user is not None:
                user.delete()
            logger.exception('Failed to create account or send its notification email')
            messages.error(
                request,
                'We could not complete registration or send the notification email. Please try again.',
            )
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
        
    total_bookings = TurfBooked.objects.filter(cancelled=False).count()
    paid_bookings = TurfBooked.objects.filter(paid=True, cancelled=False).count()
    total_revenue = (
        TurfBooked.objects.filter(paid=True)
        .exclude(refund_status='processed')
        .aggregate(Sum('amount'))['amount__sum']
        or 0
    )
    
    # For recent activity
    recent_bookings = TurfBooked.objects.order_by('-id')[:10]
    
    # Get analytics data
    today = date.today().strftime("%Y-%m-%d")
    week_start = (date.today() - timedelta(days=date.today().weekday())).strftime("%Y-%m-%d")
    month_start = date.today().replace(day=1).strftime("%Y-%m-%d")
    year_start = date.today().replace(month=1, day=1).strftime("%Y-%m-%d")
    
    # Daily income
    daily_bookings = TurfBooked.objects.filter(
        selected_date=today,
        paid=True,
    ).exclude(refund_status='processed')
    daily_income = daily_bookings.aggregate(Sum('amount'))['amount__sum'] or 0
    daily_count = daily_bookings.filter(cancelled=False).count()
    
    # Weekly income
    weekly_bookings = TurfBooked.objects.filter(
        selected_date__gte=week_start,
        paid=True,
    ).exclude(refund_status='processed')
    weekly_income = weekly_bookings.aggregate(Sum('amount'))['amount__sum'] or 0
    weekly_count = weekly_bookings.filter(cancelled=False).count()
    
    # Monthly income
    monthly_bookings = TurfBooked.objects.filter(
        selected_date__gte=month_start,
        paid=True,
    ).exclude(refund_status='processed')
    monthly_income = monthly_bookings.aggregate(Sum('amount'))['amount__sum'] or 0
    monthly_count = monthly_bookings.filter(cancelled=False).count()
    
    # Yearly income
    yearly_bookings = TurfBooked.objects.filter(
        selected_date__gte=year_start,
        paid=True,
    ).exclude(refund_status='processed')
    yearly_income = yearly_bookings.aggregate(Sum('amount'))['amount__sum'] or 0
    yearly_count = yearly_bookings.filter(cancelled=False).count()
    
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
        try:
            slot_price = int(request.POST.get('slot_price', ''))
        except (TypeError, ValueError):
            messages.error(request, 'Enter a valid positive slot price.')
            return render(request, 'owner_settings.html', {'turf_settings': turf_settings})
        if slot_price < 1:
            messages.error(request, 'Enter a valid positive slot price.')
            return render(request, 'owner_settings.html', {'turf_settings': turf_settings})

        with transaction.atomic():
            turf_settings.name = request.POST.get('name', '').strip()
            turf_settings.slot_price = slot_price
            turf_settings.description = request.POST.get('description', '')
            turf_settings.address = request.POST.get('address', '')
            turf_settings.contact_phone = request.POST.get('contact_phone', '')
            turf_settings.contact_email = request.POST.get('contact_email', '')
            turf_settings.save()

            default_turf = Turf.objects.filter(is_default=True).first()
            if default_turf:
                default_turf.name = turf_settings.name
                default_turf.slot_price = slot_price
                default_turf.description = turf_settings.description
                default_turf.address = turf_settings.address
                default_turf.contact_phone = turf_settings.contact_phone
                default_turf.contact_email = turf_settings.contact_email
                default_turf.save()
        messages.success(request, 'Settings updated successfully!')
        return redirect('owner_settings')
    
    return render(request, 'owner_settings.html', {'turf_settings': turf_settings})

@login_required(login_url='owner_login')
@login_required(login_url='owner_login')
def owner_turfs(request):
    if not request.user.is_staff:
        return redirect('book_now')
    return render(request, 'owner_turfs.html', {
        'turfs': Turf.objects.order_by('name', 'pk'),
    })


@login_required(login_url='owner_login')
def owner_turf_create(request):
    if not request.user.is_staff:
        return redirect('book_now')
    form = TurfForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Turf added successfully.')
        return redirect('owner_turfs')
    return render(request, 'owner_turf_form.html', {'form': form, 'title': 'Add Turf'})


@login_required(login_url='owner_login')
def owner_turf_edit(request, turf_id):
    if not request.user.is_staff:
        return redirect('book_now')
    turf = get_object_or_404(Turf, pk=turf_id)
    form = TurfForm(request.POST or None, instance=turf)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Turf updated successfully.')
        return redirect('owner_turfs')
    return render(request, 'owner_turf_form.html', {
        'form': form,
        'title': f'Edit {turf.name}',
        'turf': turf,
    })


@login_required(login_url='owner_login')
@require_POST
def owner_turf_delete(request, turf_id):
    if not request.user.is_staff:
        return redirect('book_now')
    turf = get_object_or_404(Turf, pk=turf_id)
    turf.is_active = False
    turf.save(update_fields=['is_active'])
    messages.success(request, 'Turf removed from booking. Existing booking history has been retained.')
    return redirect('owner_turfs')


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
        day_bookings = TurfBooked.objects.filter(
            selected_date=day,
            paid=True,
        ).exclude(refund_status='processed')
        bookings_count = day_bookings.filter(cancelled=False).count()
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


@login_required(login_url='owner_login')
def owner_feedback(request):
    if not request.user.is_staff:
        return redirect('book_now')

    feedback_items = Feedback.objects.select_related('user').all()
    bound_feedback = None
    bound_form = None

    if request.method == 'POST':
        feedback_id = request.POST.get('feedback_id', '')
        if not feedback_id.isascii() or not feedback_id.isdigit():
            messages.error(request, 'Select valid feedback to update.')
            return redirect('owner_feedback')
        feedback_item = get_object_or_404(Feedback, pk=feedback_id)
        bound_form = FeedbackAdminForm(request.POST, instance=feedback_item)
        bound_feedback = feedback_item
        if bound_form.is_valid():
            bound_form.save()
            messages.success(request, 'Feedback updated successfully.')
            return redirect('owner_feedback')
        messages.error(request, 'Please correct the feedback update form.')

    feedback_forms = []
    for feedback_item in feedback_items:
        form = (
            bound_form
            if feedback_item.pk == getattr(bound_feedback, 'pk', None)
            else FeedbackAdminForm(instance=feedback_item)
        )
        feedback_forms.append((feedback_item, form))

    return render(request, 'owner_feedback.html', {
        'feedback_forms': feedback_forms,
    })


@login_required(login_url='login')
def slot_details(request):
    # Clean up stale pending bookings
    clean_pending_bookings()
    
    if request.method == 'POST' or request.GET.get('date'):
        currentDate = date.today().strftime("%Y-%m-%d")
        currentTime = datetime.now()  # Get current time
        selectedDate = request.POST.get('date') or request.GET.get('date', '')
        requested_turf = request.POST.get('turf_id') or request.GET.get('turf_id')
        turf = _active_turf(requested_turf)
        if turf is None:
            messages.error(request, 'Select a turf that is available for booking.')
            return redirect('book_now')
        try:
            datetime.strptime(selectedDate, "%Y-%m-%d")
        except ValueError:
            messages.error(request, 'Select a valid booking date.')
            return redirect(f"{reverse('turf_details')}?turf_id={turf.pk}")
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
        existing_bookings = TurfBooked.objects.filter(
            turf=turf,
            selected_date=selectedDate,
            paid=True,
            cancelled=False,
        )
        for booking in existing_bookings:
            slots_str = booking.slots.replace('[', '').replace(']', '').replace("'", '').replace('"', '')
            booking_slots = [slot.strip() for slot in slots_str.split(',') if slot.strip()]
            for slot in booking_slots:
                if slot not in booked_slots:
                    booked_slots.append(slot)
                    
                    if turf.is_default:
                        slot_booking, created = turfBooking.objects.get_or_create(
                            time_slot=slot,
                            defaults={'isBooked': True, 'days': selectedDate},
                        )
                        if not created:
                            slot_booking.isBooked = True
                            slot_booking.days = selectedDate
                            slot_booking.save()

        reserved_slots = TurfSlotReservation.objects.filter(
            turf=turf,
            selected_date=selectedDate,
            expires_at__gt=_current_time(),
        ).values_list('slot', flat=True)
        for slot in reserved_slots:
            if slot not in booked_slots:
                booked_slots.append(slot)

        # 2. Also check turfBooking model for any additional booked slots
        if turf.is_default:
            booked_slot_records = turfBooking.objects.filter(days=selectedDate, isBooked=True)
            for record in booked_slot_records:
                if record.time_slot not in booked_slots:
                    booked_slots.append(record.time_slot)
        
        slot_price = turf.slot_price
        
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
            'turf': turf,
            'turf_id': turf.pk,
        }
        
        return render(request, 'slotbooking.html', context)
    
    return redirect('turf_details')


@login_required(login_url='login')
def turfBilling(request):
    if request.method != 'POST':
        return redirect('slot_details')

    selected_date = request.POST.get('selected_date', '').strip()
    requested_turf = request.POST.get('turf_id')
    turf = _active_turf(requested_turf)
    if turf is None:
        messages.error(request, 'Select a turf that is available for booking.')
        return redirect('book_now')
    slots = request.POST.getlist('slots')
    slots = list(dict.fromkeys(slot.strip() for slot in slots if slot.strip()))

    if not selected_date or not slots or any(slot not in SLOT_IDS for slot in slots):
        messages.error(request, 'Select a valid date and at least one available slot.')
        return redirect('book_now')

    try:
        booking_date = date.fromisoformat(selected_date)
    except ValueError:
        messages.error(request, 'The selected booking date is invalid.')
        return redirect('book_now')

    local_now = django_timezone.localtime(
        django_timezone.now(),
        ZoneInfo(getattr(settings, 'TURF_TIME_ZONE', settings.TIME_ZONE)),
    )
    if booking_date < local_now.date():
        messages.error(request, 'You cannot book a slot in the past.')
        return redirect('book_now')

    slots.sort(key=SLOT_IDS.get)
    slot_price = turf.slot_price
    if slot_price <= 0:
        messages.error(request, 'The booking price is currently invalid. Please contact the venue.')
        return redirect('book_now')
    amount = slot_price * len(slots)
    booking_time = local_now.strftime('%H:%M:%S')
    details = {
        'username': request.user.username,
        'email': request.user.email,
        'turf_name': turf.name,
        'selectedDate': selected_date,
        'currentDate': local_now.strftime('%Y-%m-%d'),
        'totalAmount': amount,
        'bookedSlots': slots,
        'booking_time': booking_time,
    }
    order_receipt = f'turf-{uuid.uuid4().hex[:24]}'

    try:
        reservation_id = _reserve_booking_slots(turf, selected_date, slots)
    except BookingSlotsUnavailable as exc:
        messages.error(
            request,
            f'These slots are no longer available: {", ".join(exc.slots)}.',
        )
        return redirect(
            f"{reverse('slot_details')}?date={selected_date}&turf_id={turf.pk}"
        )

    try:
        order = _razorpay_request('POST', 'orders', {
            'amount': amount * 100,
            'currency': 'INR',
            'receipt': order_receipt,
            'payment_capture': 1,
            'notes': {
                'user_id': str(request.user.pk),
                'selected_date': selected_date,
                'turf': turf.name,
                'slots': ', '.join(slots),
            },
        })
    except RazorpayError as exc:
        _release_slot_reservation(reservation_id)
        return _render_checkout(request, details, None, error=str(exc))

    order_id = order.get('id')
    if (
        not isinstance(order_id, str)
        or not re.fullmatch(r'order_[A-Za-z0-9]+', order_id)
        or order.get('amount') != amount * 100
        or order.get('currency') != 'INR'
    ):
        logger.error('Razorpay returned an invalid order response for user %s', request.user.pk)
        _release_slot_reservation(reservation_id)
        return _render_checkout(
            request,
            details,
            None,
            error='The payment provider returned an invalid order. Please try again.',
        )

    request.session['pending_razorpay_booking'] = {
        'order_id': order_id,
        'user_id': request.user.pk,
        'turf_id': turf.pk,
        'turf_name': turf.name,
        'amount': amount,
        'selected_date': selected_date,
        'current_date': details['currentDate'],
        'booking_time': booking_time,
        'slots': slots,
        'reservation_id': reservation_id,
    }
    request.session.pop('pending_razorpay_booking_edit', None)
    return _render_checkout(request, details, order)


def _apply_booking_edit_payment(request, edit_intent, payment_id, payment, order_id):
    slots = edit_intent.get('new_slots')
    old_slots = edit_intent.get('old_slots')
    try:
        booking_id = int(edit_intent.get('booking_id'))
        expected_amount = int(edit_intent.get('old_amount'))
        new_amount = int(edit_intent.get('new_amount'))
        charge_amount = int(edit_intent.get('charge_amount'))
    except (TypeError, ValueError):
        messages.error(request, 'The booking edit details are invalid. Contact the venue.')
        return
    if (
        not isinstance(slots, list)
        or not slots
        or any(slot not in SLOT_IDS for slot in slots)
        or not isinstance(old_slots, list)
        or charge_amount <= 0
        or new_amount - expected_amount != charge_amount
    ):
        messages.error(request, 'The booking edit details are invalid. Contact the venue.')
        return

    try:
        with transaction.atomic():
            booking = TurfBooked.objects.select_for_update().get(
                pk=booking_id,
                email=request.user.email,
                paid=True,
                cancelled=False,
            )
            current_slots = _booking_slots(booking)
            turf = Turf.objects.select_for_update().filter(pk=booking.turf_id).first()
            now = _current_time()
            booking_start = _booking_start_datetime(booking)
            if (
                turf is None
                or not turf.is_active
                or booking.amount != expected_amount
                or set(current_slots) != set(old_slots)
                or booking.selected_date != edit_intent.get('selected_date')
                or turf.pk != edit_intent.get('turf_id')
                or booking_start is None
                or now >= booking_start
                or any(
                    _slot_start_datetime(booking.selected_date, slot) <= now
                    for slot in slots
                )
            ):
                raise BookingSlotsUnavailable([])

            conflicts = _booking_slot_conflicts(
                turf,
                booking.selected_date,
                slots,
                booking.pk,
                current_slots,
            )
            if conflicts:
                raise BookingSlotsUnavailable(conflicts)

            booking.slots = json.dumps(slots)
            booking.amount = new_amount
            booking.save(update_fields=['slots', 'amount'])
            TurfBookingPayment.objects.create(
                booking=booking,
                payment_id=payment_id,
                amount=charge_amount,
            )
            _update_default_turf_slot_records(
                turf,
                booking.selected_date,
                current_slots,
                slots,
            )
    except BookingSlotsUnavailable as exc:
        try:
            refund_id, refund_status = _request_razorpay_refund(
                payment_id,
                charge_amount * 100,
                order_id,
            )
            _record_booking_payment_if_present(
                booking_id,
                payment_id=payment_id,
                amount=charge_amount,
                refunded_amount=charge_amount,
                refund_id=refund_id,
                refund_status=refund_status,
            )
        except RefundProcessingError:
            logger.exception(
                'Could not refund payment %s after booking edit conflict',
                payment_id,
            )
            _record_booking_payment_if_present(
                booking_id,
                payment_id=payment_id,
                amount=charge_amount,
                refund_status='review_required',
            )
            messages.error(
                request,
                'The additional payment was captured but the requested slots could not be confirmed. Our team will review the refund; contact support with payment ID '
                f'{payment_id}.',
            )
            return
        if exc.slots:
            messages.error(
                request,
                'The selected slots became unavailable before payment completed. The additional payment refund is '
                f'{refund_status} ({refund_id}).',
            )
        else:
            messages.error(
                request,
                'The booking changed or expired before payment completed. The additional payment refund is '
                f'{refund_status} ({refund_id}).',
            )
    except TurfBooked.DoesNotExist:
        try:
            refund_id, refund_status = _request_razorpay_refund(
                payment_id,
                charge_amount * 100,
                order_id,
            )
            messages.error(
                request,
                'The booking no longer exists. The additional payment refund is '
                f'{refund_status} ({refund_id}).',
            )
        except RefundProcessingError:
            logger.exception('Could not refund edit payment %s for missing booking', payment_id)
            messages.error(
                request,
                'The booking no longer exists and the additional payment refund needs review. Contact support with payment ID '
                f'{payment_id}.',
            )
    except (DatabaseError, ValueError, TypeError, KeyError, IndexError):
        logger.exception('Could not apply booking edit payment %s', payment_id)
        _record_booking_payment_if_present(
            booking_id,
            payment_id=payment_id,
            amount=charge_amount,
            refund_status='review_required',
        )
        messages.error(
            request,
            'The additional payment was captured but the booking update failed. Contact support with payment ID '
            f'{payment_id}.',
        )
        return
    else:
        _send_booking_update_email(
            request,
            booking,
            old_slots,
            slots,
            expected_amount,
            new_amount,
        )
        messages.success(request, 'Booking updated and the additional payment was confirmed.')


@login_required(login_url='login')
@require_POST
def success(request):
    edit_intent = request.session.get('pending_razorpay_booking_edit')
    intent = edit_intent or request.session.get('pending_razorpay_booking')
    is_booking_edit = edit_intent is not None
    failure_url = 'orderHistory' if is_booking_edit else 'book_now'
    order_id = request.POST.get('razorpay_order_id', '')
    payment_id = request.POST.get('razorpay_payment_id', '')
    signature = request.POST.get('razorpay_signature', '')

    if (
        not intent
        or intent.get('user_id') != request.user.pk
        or not order_id
        or order_id != intent.get('order_id')
        or not payment_id
        or not signature
    ):
        messages.error(request, 'The payment confirmation is invalid or has expired.')
        return redirect(failure_url)

    key_secret = getattr(settings, 'RAZOR_KEY_SECRET', '')
    expected_signature = hmac.new(
        key_secret.encode('utf-8'),
        f'{order_id}|{payment_id}'.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    if not key_secret or not hmac.compare_digest(expected_signature, signature):
        logger.warning('Invalid Razorpay checkout signature for user %s', request.user.pk)
        messages.error(request, 'Payment verification failed. Your booking has not been confirmed.')
        return redirect(failure_url)

    try:
        payment = _razorpay_request('GET', f'payments/{payment_id}')
    except RazorpayError as exc:
        messages.error(request, str(exc))
        return redirect(failure_url)

    expected_amount = intent.get(
        'charge_amount' if is_booking_edit else 'amount',
        0,
    ) * 100
    if (
        payment.get('id') != payment_id
        or payment.get('order_id') != order_id
        or payment.get('amount') != expected_amount
        or payment.get('currency') != 'INR'
        or payment.get('status') != 'captured'
    ):
        logger.warning('Razorpay payment details failed validation for order %s', order_id)
        messages.error(request, 'Payment is not captured or does not match this booking.')
        return redirect(failure_url)

    if is_booking_edit:
        _apply_booking_edit_payment(request, edit_intent, payment_id, payment, order_id)
        request.session.pop('pending_razorpay_booking_edit', None)
        return redirect('orderHistory')

    reservation_id = intent.get('reservation_id')
    if TurfBooked.objects.filter(payment_id=payment_id, paid=True).exists():
        _release_slot_reservation(reservation_id)
        request.session.pop('pending_razorpay_booking', None)
        existing = TurfBooked.objects.get(payment_id=payment_id, paid=True)
        return render(request, 'success.html', {
            'booking': existing,
            'booked_slots': _booking_slots(existing),
            'now': django_timezone.localdate().strftime('%Y-%m-%d'),
        })

    booked_slots = intent.get('slots', [])
    selected_date = intent.get('selected_date', '')
    intent_turf_id = intent.get('turf_id')
    turf = (
        Turf.objects.filter(pk=intent_turf_id).first()
        if intent_turf_id
        else Turf.objects.filter(is_default=True).first()
    )
    if (
        turf is None
        or not isinstance(booked_slots, list)
        or not booked_slots
        or any(slot not in SLOT_IDS for slot in booked_slots)
    ):
        messages.error(request, 'The booking details are invalid. Please contact support.')
        return redirect('book_now')

    try:
        with transaction.atomic():
            Turf.objects.select_for_update().get(pk=turf.pk)
            initialize_time_slots()
            if reservation_id:
                reserved_slots = set(
                    TurfSlotReservation.objects.select_for_update().filter(
                        turf=turf,
                        selected_date=selected_date,
                        reservation_id=reservation_id,
                        expires_at__gt=_current_time(),
                    ).values_list('slot', flat=True)
                )
                missing_slots = set(booked_slots) - reserved_slots
                if missing_slots:
                    raise BookingSlotsUnavailable(sorted(missing_slots))

            list(
                turfBooking.objects.filter(time_slot__in=booked_slots)
                .order_by('id')
                .select_for_update()
                .values_list('id', flat=True)
            )
            existing_bookings = TurfBooked.objects.select_for_update().filter(
                turf=turf,
                selected_date=selected_date,
                paid=True,
                cancelled=False,
            )
            occupied_slots = set()
            for existing_booking in existing_bookings:
                occupied_slots.update(_booking_slots(existing_booking))
            if turf.is_default:
                occupied_slots.update(
                    turfBooking.objects.filter(
                        days=selected_date,
                        isBooked=True,
                    ).values_list('time_slot', flat=True)
                )
            active_reservations = TurfSlotReservation.objects.select_for_update().filter(
                turf=turf,
                selected_date=selected_date,
                expires_at__gt=_current_time(),
            )
            if reservation_id:
                active_reservations = active_reservations.exclude(
                    reservation_id=reservation_id,
                )
            occupied_slots.update(active_reservations.values_list('slot', flat=True))
            conflicts = sorted(set(booked_slots) & occupied_slots)
            if conflicts:
                raise BookingSlotsUnavailable(conflicts)

            booking = TurfBooked.objects.create(
                name=request.user.username,
                email=request.user.email,
                turf=turf,
                turf_name=turf.name,
                amount=intent['amount'],
                selected_date=selected_date,
                current_date=intent['current_date'],
                booking_time=intent['booking_time'],
                slots=json.dumps(booked_slots),
                payment_id=payment_id,
                razorpay_order_id=order_id,
                payment_method=payment.get('method', 'razorpay'),
                paid=True,
                is_dummy_payment=False,
            )
            _release_slot_reservation(reservation_id)
            TurfBookingPayment.objects.create(
                booking=booking,
                payment_id=payment_id,
                amount=intent['amount'],
            )
            if turf.is_default:
                for slot in booked_slots:
                    slot_record, _ = turfBooking.objects.get_or_create(
                        time_slot=slot,
                        defaults={'isBooked': True, 'days': selected_date},
                    )
                    slot_record.isBooked = True
                    slot_record.days = selected_date
                    slot_record.save(update_fields=['isBooked', 'days'])
    except BookingSlotsUnavailable as exc:
        try:
            refund_id, refund_status = _request_razorpay_refund(
                payment_id,
                expected_amount,
                order_id,
            )
        except RefundProcessingError:
            logger.exception(
                'Could not refund payment %s after booking slots became unavailable',
                payment_id,
            )
            refund_id = ''
            refund_status = 'review_required'

        _release_slot_reservation(reservation_id)
        cancelled_booking = TurfBooked.objects.create(
            name=request.user.username,
            email=request.user.email,
            turf=turf,
            turf_name=turf.name,
            amount=intent['amount'],
            selected_date=selected_date,
            current_date=intent['current_date'],
            booking_time=intent['booking_time'],
            slots=json.dumps(booked_slots),
            payment_id=payment_id,
            razorpay_order_id=order_id,
            payment_method=payment.get('method', 'razorpay'),
            paid=True,
            is_dummy_payment=False,
            cancelled=True,
            cancelled_at=django_timezone.now(),
            refund_id=refund_id,
            refund_status=refund_status,
        )
        request.session.pop('pending_razorpay_booking', None)
        if refund_status == 'review_required':
            messages.error(
                request,
                'Payment succeeded but the selected slot is no longer available. '
                'We could not confirm its refund. Contact support with payment ID '
                f'{payment_id}.',
            )
        else:
            messages.error(
                request,
                'A selected slot was booked before payment completed. The payment refund '
                f'is {refund_status} ({refund_id}). Please select another slot.',
            )
        try:
            send_mail(
                subject='SK Sports Park payment refund update',
                message=(
                    f'Hello {cancelled_booking.name},\n\n'
                    'Your payment was received, but the selected turf slot was no longer '
                    'available. Your booking was not confirmed.\n'
                    f'Payment ID: {payment_id}\n'
                    f'Refund status: {refund_status}\n'
                    f'Refund ID: {refund_id or "Pending support review"}\n\n'
                    'SK Sports Park'
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[cancelled_booking.email],
                fail_silently=False,
            )
        except Exception:
            logger.exception(
                'Could not send slot-unavailable payment email for payment %s',
                payment_id,
            )
        return redirect(f"{reverse('slot_details')}?date={selected_date}")
    except (DatabaseError, ValueError, KeyError, IndexError, TypeError):
        logger.exception('Could not save verified booking for Razorpay payment %s', payment_id)
        _release_slot_reservation(reservation_id)
        try:
            refund_id, refund_status = _request_razorpay_refund(
                payment_id,
                expected_amount,
                order_id,
            )
            messages.error(
                request,
                'Payment was verified but the booking could not be saved. A refund was '
                f'{refund_status} ({refund_id}). Contact support if it does not arrive.',
            )
        except RefundProcessingError:
            logger.exception('Could not refund unsaved booking payment %s', payment_id)
            messages.error(
                request,
                'Payment was verified but the booking could not be saved and its refund '
                'could not be confirmed. Contact support with payment ID '
                f'{payment_id}.',
            )
        return redirect('book_now')

    request.session.pop('pending_razorpay_booking', None)
    if getattr(settings, 'ENABLE_EMAIL', False):
        try:
            email_context = {
                'name': booking.name,
                'turf_name': booking.turf_name,
                'amount': booking.amount,
                'payment_method': booking.payment_method,
                'selected_date': booking.selected_date,
                'current_date': booking.current_date,
                'booking_time': booking.booking_time,
                'payment_id': booking.payment_id,
                'slots': booked_slots,
                'is_dummy': False,
            }
            message_plain = render_to_string(
                'booking_confirmation_email.txt',
                email_context,
            )
            send_mail(
                subject='The SK Sports Park - Turf Booking Confirmation and Receipt',
                message=message_plain,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[booking.email],
                fail_silently=False,
            )
        except Exception:
            logger.exception(
                'Booking %s was confirmed but its email could not be sent',
                booking.pk,
            )
            messages.warning(
                request,
                'Booking confirmed, but the confirmation email could not be sent.',
            )

    return render(request, 'success.html', {
        'booking': booking,
        'booked_slots': booked_slots,
        'now': django_timezone.localdate().strftime('%Y-%m-%d'),
    })


@login_required(login_url='login')
def feedback(request):
    form = FeedbackForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        feedback_item = form.save(commit=False)
        feedback_item.user = request.user
        feedback_item.save()
        messages.success(request, 'Thank you for sharing your feedback.')
        return redirect('feedback')

    return render(request, 'feedback.html', {
        'form': form,
        'feedback_items': Feedback.objects.filter(user=request.user),
    })


@login_required(login_url='login')
def orderHistory(request):
    my_bookings = TurfBooked.objects.filter(
    paid=True,
    email=request.user.email,
    cancelled=False,
    ).order_by('-id')
    now = _current_time()
    for booking in my_bookings:
        booking.selected_slots = _booking_slots(booking)
        booking_start = _booking_start_datetime(booking)
        booking.can_cancel = bool(booking_start and now < booking_start)
        booking.can_edit = booking.can_cancel and bool(booking.turf_id)
        booking.editable_slot_options = [
            slot for slot in sorted(SLOT_IDS, key=SLOT_IDS.get)
            if _slot_start_datetime(booking.selected_date, slot) > now
        ] if booking.can_edit else []
        booking.unavailable_slots = (
            _booking_slot_conflicts(
                booking.turf,
                booking.selected_date,
                booking.editable_slot_options,
                booking.pk,
                booking.selected_slots,
            )
            if booking.can_edit
            else []
        )
        booking.refund_eligible = bool(
            booking_start
            and booking_start - now >= CANCELLATION_REFUND_CUTOFF
            and not booking.is_dummy_payment
        )
    return render(request, 'orderHistory.html', {
        'bookings': my_bookings,
    })


@login_required(login_url='login')
@require_POST
def edit_booking(request, id):
    try:
        with transaction.atomic():
            booking = TurfBooked.objects.select_for_update().get(
                pk=id,
                email=request.user.email,
                paid=True,
                cancelled=False,
            )
            old_slots = _booking_slots(booking)
            now = _current_time()
            booking_start = _booking_start_datetime(booking)
            if booking_start is None or now >= booking_start:
                messages.error(request, 'A booking can only be edited before its first slot starts.')
                return redirect('orderHistory')

            turf = Turf.objects.select_for_update().filter(
                pk=booking.turf_id,
                is_active=True,
            ).first()
            if turf is None:
                messages.error(request, 'This turf is no longer available for booking changes.')
                return redirect('orderHistory')

            new_slots = list(dict.fromkeys(
                slot.strip() for slot in request.POST.getlist('slots') if slot.strip()
            ))
            if not new_slots or any(slot not in SLOT_IDS for slot in new_slots):
                messages.error(request, 'Choose at least one valid time slot.')
                return redirect('orderHistory')
            new_slots.sort(key=SLOT_IDS.get)
            if any(_slot_start_datetime(booking.selected_date, slot) <= now for slot in new_slots):
                messages.error(request, 'Choose only time slots that have not started yet.')
                return redirect('orderHistory')

            conflicts = _booking_slot_conflicts(
                turf,
                booking.selected_date,
                new_slots,
                booking.pk,
                old_slots,
            )
            if conflicts:
                messages.error(
                    request,
                    f'These slots are no longer available: {", ".join(conflicts)}.',
                )
                return redirect('orderHistory')

            if not old_slots or booking.amount % len(old_slots):
                messages.error(
                    request,
                    'We could not determine the original price per slot. Contact the venue to change this booking.',
                )
                return redirect('orderHistory')
            unit_price = booking.amount // len(old_slots)
            new_amount = unit_price * len(new_slots)
            difference = new_amount - booking.amount

            if difference < 0 and not booking.is_dummy_payment:
                if booking_start - now < CANCELLATION_REFUND_CUTOFF:
                    messages.error(
                        request,
                        'A price reduction can only be refunded at least 2 hours before the first slot starts.',
                    )
                    return redirect('orderHistory')

                refundable_payments = list(
                    booking.payment_records.select_for_update().filter(
                        amount__gt=F('refunded_amount')
                    ).order_by('-created_at', '-pk')
                )
                if not refundable_payments:
                    refundable_payments = [TurfBookingPayment.objects.create(
                        booking=booking,
                        payment_id=booking.payment_id,
                        amount=booking.amount,
                    )]
                refund_remaining = -difference
                refund_plan = []
                for payment_record in refundable_payments:
                    refundable_amount = payment_record.amount - payment_record.refunded_amount
                    refund_amount = min(refundable_amount, refund_remaining)
                    if refund_amount:
                        refund_plan.append((payment_record, refund_amount))
                        refund_remaining -= refund_amount
                    if refund_remaining == 0:
                        break
                if refund_remaining:
                    messages.error(
                        request,
                        'The refundable payment balance does not cover this price reduction. Contact the venue to change this booking.',
                    )
                    return redirect('orderHistory')
                refund_statuses = []
                total_refunded = 0
                for payment_record, refund_amount in refund_plan:
                    try:
                        refund_id, payment_refund_status = _request_razorpay_refund(
                            payment_record.payment_id,
                            refund_amount * 100,
                            booking.pk,
                        )
                    except RefundProcessingError as exc:
                        logger.error('Booking %s edit refund failed: %s', booking.pk, exc)
                        if total_refunded:
                            messages.error(
                                request,
                                f'₹{total_refunded} of the requested refund was issued, but the remaining refund could not be confirmed. The booking was not changed; contact the venue for help.',
                            )
                        else:
                            messages.error(
                                request,
                                'We could not confirm the price adjustment refund, so the booking was not changed. Contact the venue if your payment provider shows a refund.',
                            )
                        return redirect('orderHistory')
                    payment_record.refunded_amount += refund_amount
                    payment_record.refund_id = refund_id
                    payment_record.refund_status = payment_refund_status
                    payment_record.save(update_fields=[
                        'refunded_amount',
                        'refund_id',
                        'refund_status',
                    ])
                    total_refunded += refund_amount
                    refund_statuses.append(payment_refund_status)
                refund_status = (
                    'pending' if 'pending' in refund_statuses else 'processed'
                )
            elif difference < 0:
                refund_status = 'no_payment'

            if difference > 0 and not booking.is_dummy_payment:
                order_receipt = f'edit-{uuid.uuid4().hex[:24]}'
                try:
                    order = _razorpay_request('POST', 'orders', {
                        'amount': difference * 100,
                        'currency': 'INR',
                        'receipt': order_receipt,
                        'payment_capture': 1,
                        'notes': {
                            'user_id': str(request.user.pk),
                            'booking_id': str(booking.pk),
                            'adjustment_slots': ', '.join(new_slots),
                        },
                    })
                except RazorpayError as exc:
                    messages.error(request, str(exc))
                    return redirect('orderHistory')
                order_id = order.get('id')
                if (
                    not isinstance(order_id, str)
                    or not re.fullmatch(r'order_[A-Za-z0-9]+', order_id)
                    or order.get('amount') != difference * 100
                    or order.get('currency') != 'INR'
                ):
                    logger.error('Razorpay returned an invalid edit order for booking %s', booking.pk)
                    messages.error(request, 'The payment provider returned an invalid order. Please try again.')
                    return redirect('orderHistory')

                request.session['pending_razorpay_booking_edit'] = {
                    'order_id': order_id,
                    'user_id': request.user.pk,
                    'booking_id': booking.pk,
                    'turf_id': turf.pk,
                    'selected_date': booking.selected_date,
                    'old_amount': booking.amount,
                    'old_slots': old_slots,
                    'new_amount': new_amount,
                    'new_slots': new_slots,
                    'charge_amount': difference,
                }
                request.session.pop('pending_razorpay_booking', None)
                details = {
                    'username': request.user.username,
                    'email': request.user.email,
                    'turf_name': booking.turf_name,
                    'selectedDate': booking.selected_date,
                    'bookedSlots': new_slots,
                    'totalAmount': difference,
                }
                return render(request, 'turfBilling.html', {
                    'details': details,
                    'razorpay_order': order,
                    'razorpay_key_id': settings.RAZOR_KEY_ID,
                    'razorpay_mode': getattr(settings, 'RAZORPAY_MODE', 'test'),
                    'checkout_error': None,
                    'is_booking_edit': True,
                    'booking_new_total': new_amount,
                })

            booking.slots = json.dumps(new_slots)
            booking.amount = new_amount if difference >= 0 or booking.is_dummy_payment else booking.amount + difference
            booking.save(update_fields=['slots', 'amount'])
            _update_default_turf_slot_records(turf, booking.selected_date, old_slots, new_slots)

        _send_booking_update_email(
            request,
            booking,
            old_slots,
            new_slots,
            booking.amount - difference,
            booking.amount,
        )
        if difference < 0 and booking.is_dummy_payment:
            messages.success(request, 'Booking updated. This demo booking did not charge real money.')
        elif difference < 0:
            messages.success(request, f'Booking updated. A refund of ₹{-difference} is {refund_status}.')
        else:
            messages.success(request, 'Booking details updated successfully.')
    except TurfBooked.DoesNotExist:
        messages.error(request, 'Booking not found or you do not have permission to edit it.')
    except (DatabaseError, ValueError, TypeError):
        logger.exception('Could not update booking %s', id)
        messages.error(request, 'We could not update this booking. Please try again or contact the venue.')

    return redirect('orderHistory')


@login_required(login_url='login')
@require_POST
def delete_booking(request, id):
    try:
        with transaction.atomic():
            booking = TurfBooked.objects.select_for_update().get(
                id=id,
                **({} if request.user.is_staff else {'email': request.user.email}),
                cancelled=False,
            )

            if not request.user.is_staff:
                booking_start = _booking_start_datetime(booking)
                now = _current_time()
                if booking_start is None:
                    messages.error(request, 'We could not determine this booking time. Please contact support.')
                    return redirect('orderHistory')
                if now >= booking_start:
                    messages.error(request, 'A booking cannot be cancelled after its start time.')
                    return redirect('orderHistory')

                refund_eligible = (
                    booking.paid
                    and not booking.is_dummy_payment
                    and booking_start - now >= CANCELLATION_REFUND_CUTOFF
                )
                booking.cancelled = True
                booking.cancelled_at = now

                if refund_eligible:
                    booking.refund_status = 'processing'
                elif booking.paid and booking.is_dummy_payment:
                    booking.refund_status = 'no_payment'
                elif booking.paid:
                    booking.refund_status = 'not_eligible'
                else:
                    booking.refund_status = 'not_applicable'
                booking.save(update_fields=[
                    'cancelled',
                    'cancelled_at',
                    'refund_status',
                ])

                slots = _booking_slots(booking)
                booking_name = booking.name
                booking_email = booking.email
                booking_date = booking.selected_date
                booking_reference = booking.payment_id
                booking_amount = booking.amount
                booking_id = booking.pk
                was_paid = booking.paid
                is_dummy_payment = booking.is_dummy_payment
                refund_sources = list(
                    booking.payment_records.select_for_update().filter(
                        amount__gt=F('refunded_amount')
                    )
                )
            else:
                selectedDate = booking.selected_date
                slots = _booking_slots(booking)
                booking_name = booking.name
                booking_email = booking.email
                booking_date = selectedDate
                booking_reference = booking.payment_id
                booking_amount = booking.amount
                booking_id = booking.pk
                refund_eligible = False
                was_paid = booking.paid
                is_dummy_payment = booking.is_dummy_payment

            booking_turf = (
                booking.turf
                if booking.turf_id
                else Turf.objects.filter(is_default=True).first()
            )
            if booking_turf and booking_turf.is_default:
                _update_default_turf_slot_records(
                    booking_turf,
                    booking_date,
                    slots,
                    [],
                )

            if request.user.is_staff:
                booking.delete()

        if not request.user.is_staff:
            if refund_eligible:
                if refund_sources:
                    refund_requests = [
                        (source.payment_id, source.amount - source.refunded_amount, source)
                        for source in refund_sources
                    ]
                else:
                    refund_requests = [(booking_reference, booking_amount, None)]
                total_refunded = 0
                refund_statuses = []
                refund_id = ''
                for payment_id, remaining_amount, source in refund_requests:
                    if remaining_amount <= 0:
                        continue
                    try:
                        refund_id, source_status = _request_razorpay_refund(
                            payment_id,
                            remaining_amount * 100,
                            booking_id,
                        )
                        total_refunded += remaining_amount
                        refund_statuses.append(source_status)
                        if source is not None:
                            source.refunded_amount += remaining_amount
                            source.refund_id = refund_id
                            source.refund_status = source_status
                            source.save(update_fields=[
                                'refunded_amount',
                                'refund_id',
                                'refund_status',
                            ])
                    except RefundProcessingError as exc:
                        logger.error(
                            'Refund for cancelled booking %s payment %s requires review: %s',
                            booking_id,
                            payment_id,
                            exc,
                        )
                        refund_statuses.append('review_required')
                if not refund_statuses:
                    refund_statuses.append('review_required')
                if 'review_required' in refund_statuses:
                    refund_status = 'review_required'
                elif 'pending' in refund_statuses:
                    refund_status = 'pending'
                else:
                    refund_status = 'processed'
                TurfBooked.objects.filter(pk=booking_id).update(
                    refund_id=refund_id,
                    refund_status=refund_status,
                )
                booking_amount = total_refunded
            else:
                refund_status = (
                    'no_payment'
                    if was_paid and is_dummy_payment
                    else 'not_eligible'
                    if was_paid
                    else 'not_applicable'
                )

            if refund_status == 'processed':
                refund_message = (
                    f'Your refund of Rs. {booking_amount} has been processed '
                    f'(refund reference {refund_id}).'
                )
            elif refund_status == 'pending':
                refund_message = (
                    f'Your refund of Rs. {booking_amount} has been initiated '
                    f'(refund reference {refund_id}) and is pending with the payment provider.'
                )
            elif refund_status == 'review_required':
                refund_message = (
                    'Your booking was cancelled, but we could not confirm the refund. '
                    'Our team will review it and contact you.'
                )
            elif refund_status == 'not_eligible':
                refund_message = 'The cancellation was less than 2 hours before the slot, so no refund is due.'
            elif refund_status == 'no_payment':
                refund_message = 'This demo booking did not charge a real payment, so no monetary refund was issued.'
            else:
                refund_message = 'No refund was issued because this booking was not marked as paid.'

            email_sent = True
            try:
                send_mail(
                    subject='SK Sports Park booking cancellation',
                    message=(
                        f'Hello {booking_name},\n\n'
                        f'Your turf booking for {booking_date} ({", ".join(slots)}) '
                        'has been cancelled.\n'
                        f'Booking reference: {booking_reference}\n'
                        f'{refund_message}\n\n'
                        'SK Sports Park'
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[booking_email],
                    fail_silently=False,
                )
            except Exception:
                email_sent = False
                logger.exception('Could not send cancellation email for booking %s', booking_id)

            if refund_status in {'processed', 'pending'}:
                messages.success(request, f'Booking cancelled. {refund_message}')
            elif refund_status == 'review_required':
                messages.warning(request, refund_message)
            else:
                messages.success(request, f'Booking cancelled. {refund_message}')
            if not email_sent:
                messages.warning(
                    request,
                    'Cancellation completed, but we could not send the email notification.',
                )
        else:
            messages.success(request, f'Booking #{booking_id} for {booking_date} was deleted.')

    except TurfBooked.DoesNotExist:
        messages.error(request, "Booking not found or you do not have permission to cancel it.")

    if request.user.is_staff:
        return redirect('owner_bookings')
    return redirect('orderHistory')


def allBookings(request):
    bookings = TurfBooked.objects.filter(
        paid=True,
        cancelled=False,
    ).order_by('selected_date', 'booking_time')
    currentDate = date.today().strftime("%Y-%m-%d")
    return render(request, 'allBookings.html', {'bookings': bookings, 'currentDate': currentDate})


def searchBooking(request):
    query = request.POST['query']
    bookings = TurfBooked.objects.filter(
        name__icontains=query,
        cancelled=False,
    )
    return render(request, 'allBookings.html', {'bookings': bookings, 'query': query})


def check_slot_status(request):
    """
    API endpoint to check slot availability in real-time
    Returns a JSON response with booked slots for a specific date
    Includes confirmed bookings and active checkout reservations
    """
    selected_date = request.GET.get('date')
    
    if not selected_date:
        return JsonResponse({'error': 'Date parameter is required'}, status=400)

    turf_id = request.GET.get('turf_id')
    turf = _active_turf(turf_id)
    if turf is None:
        return JsonResponse({'error': 'A valid turf_id parameter is required'}, status=400)

    # Get all confirmed (paid) bookings for the selected date
    existing_bookings = TurfBooked.objects.filter(
        turf=turf,
        selected_date=selected_date,
        paid=True,
        cancelled=False,
    )
    
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
    
    if turf.is_default:
        booked_slot_records = turfBooking.objects.filter(days=selected_date, isBooked=True)
        for record in booked_slot_records:
            if record.time_slot not in booked_slots:
                booked_slots.append(record.time_slot)

    reserved_slots = TurfSlotReservation.objects.filter(
        turf=turf,
        selected_date=selected_date,
        expires_at__gt=_current_time(),
    ).values_list('slot', flat=True)
    for slot in reserved_slots:
        if slot not in booked_slots:
            booked_slots.append(slot)

    return JsonResponse({
        'date': selected_date,
        'turf_id': turf.pk,
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
