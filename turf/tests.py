import json
import hashlib
import hmac
import re
from datetime import datetime, timezone
from urllib.error import HTTPError
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import TurfBooked, TurfSettings, turfBooking
from .views import (
    RazorpayError,
    RefundProcessingError,
    _booking_start_datetime,
    _request_razorpay_refund,
)


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class PasswordResetOtpTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='otp-test-user',
            email='otp-test@example.com',
            password='OldPassword!123',
        )

    def request_code(self):
        response = self.client.post(
            reverse('password_reset'),
            {'email': self.user.email},
        )
        self.assertRedirects(response, reverse('password_reset_verify'))
        self.assertEqual(len(mail.outbox), 1)
        match = re.search(r'\b(\d{6})\b', mail.outbox[0].body)
        self.assertIsNotNone(match)
        return match.group(1)

    def test_email_otp_resets_password_once_verified(self):
        otp = self.request_code()
        reset_data = self.client.session['password_reset_otp']
        self.assertNotEqual(reset_data['otp_hash'], otp)

        response = self.client.post(
            reverse('password_reset_verify'),
            {'otp': otp},
        )
        self.assertRedirects(response, reverse('password_reset_new_password'))

        new_password = 'NewStrongPassword!2026'
        response = self.client.post(
            reverse('password_reset_new_password'),
            {
                'new_password1': new_password,
                'new_password2': new_password,
            },
        )
        self.assertRedirects(response, reverse('password_reset_complete'))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(new_password))
        self.assertNotIn('password_reset_otp', self.client.session)

    def test_invalid_otp_does_not_verify_reset(self):
        otp = self.request_code()
        wrong_otp = f'{(int(otp) + 1) % 1_000_000:06d}'

        response = self.client.post(
            reverse('password_reset_verify'),
            {'otp': wrong_otp},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.client.session['password_reset_otp']['verified'])

    def test_password_cannot_be_changed_before_otp_verification(self):
        response = self.client.get(reverse('password_reset_new_password'))

        self.assertRedirects(response, reverse('password_reset'))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('OldPassword!123'))

    def test_unknown_email_does_not_send_mail_or_reveal_account_existence(self):
        response = self.client.post(
            reverse('password_reset'),
            {'email': 'unknown@example.com'},
        )

        self.assertRedirects(response, reverse('password_reset_verify'))
        self.assertEqual(mail.outbox, [])
        response = self.client.post(
            reverse('password_reset_verify'),
            {'otp': '123456'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'The code is invalid.')

    def test_five_invalid_codes_invalidate_reset_session(self):
        otp = self.request_code()
        wrong_otp = f'{(int(otp) + 1) % 1_000_000:06d}'

        for _ in range(5):
            response = self.client.post(
                reverse('password_reset_verify'),
                {'otp': wrong_otp},
            )

        self.assertRedirects(response, reverse('password_reset'))
        self.assertNotIn('password_reset_otp', self.client.session)


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class RegistrationEmailConfirmationTests(TestCase):
    def register(self):
        return self.client.post(
            reverse('signup'),
            {
                'username': 'new-user',
                'emailid': 'new-user@example.com',
                'password': 'TestingPass!123',
                'password_confirm': 'TestingPass!123',
                'captcha': '1234',
                'hidden_captcha': '1234',
            },
        )

    def test_registration_sends_notification_and_activates_account(self):
        response = self.register()

        self.assertRedirects(response, reverse('login'))
        user = User.objects.get(username='new-user')
        self.assertTrue(user.is_active)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['new-user@example.com'])
        self.assertEqual(mail.outbox[0].subject, 'Registration successful - SK Sports Park')
        self.assertIn('account has been created successfully', mail.outbox[0].body)
        self.assertNotIn('/confirm/', mail.outbox[0].body)


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    ENABLE_EMAIL=False,
    RAZORPAY_MODE='test',
    RAZOR_KEY_ID='rzp_test_key',
    RAZOR_KEY_SECRET='test_secret',
    TURF_TIME_ZONE='UTC',
)
class RazorpayBookingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='razorpay-user',
            email='razorpay-user@example.com',
            password='TestingPass!123',
        )
        self.client.force_login(self.user)
        TurfSettings.objects.create(slot_price=500)

    @patch('turf.views._razorpay_request')
    def test_checkout_creates_server_priced_order_and_keeps_details_in_session(
        self,
        razorpay_request,
    ):
        razorpay_request.return_value = {
            'id': 'order_test123',
            'amount': 100000,
            'currency': 'INR',
        }

        response = self.client.post(
            reverse('turfBilling'),
            {
                'selected_date': '2030-01-01',
                'slots': ['7-8 am', '6-7 am'],
                'total_amount': '1',
                'username': 'forged-name',
                'email': 'forged@example.com',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Pay ₹1000 securely')
        self.assertContains(response, 'Razorpay TEST MODE')
        self.assertContains(response, 'order_test123')
        razorpay_request.assert_called_once()
        self.assertEqual(razorpay_request.call_args.args[:2], ('POST', 'orders'))
        self.assertEqual(razorpay_request.call_args.args[2]['amount'], 100000)
        self.assertEqual(
            self.client.session['pending_razorpay_booking']['slots'],
            ['6-7 am', '7-8 am'],
        )
        self.assertEqual(
            self.client.session['pending_razorpay_booking']['user_id'],
            self.user.pk,
        )

    @patch(
        'turf.views._razorpay_request',
        side_effect=RazorpayError('Razorpay is not configured. Please contact the venue.'),
    )
    def test_checkout_error_keeps_user_on_checkout_with_clear_message(
        self,
        razorpay_request,
    ):
        response = self.client.post(
            reverse('turfBilling'),
            {
                'selected_date': '2030-01-01',
                'slots': ['6-7 am'],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Razorpay is not configured.')
        self.assertContains(response, '6-7 am')
        self.assertContains(response, 'Payment unavailable')
        razorpay_request.assert_called_once()

    @patch('turf.views.urlopen', side_effect=HTTPError(
        'https://api.razorpay.com/v1/orders',
        401,
        'Unauthorized',
        {},
        None,
    ))
    def test_razorpay_unauthorized_response_explains_credential_checks(
        self,
        urlopen,
    ):
        from .views import _razorpay_request

        with self.assertRaisesRegex(
            RazorpayError,
            'valid, paired credentials.*test or live',
        ):
            _razorpay_request('POST', 'orders', {'amount': 50000})

        urlopen.assert_called_once()

    @override_settings(RAZOR_KEY_ID='rzp_live_key')
    @patch('turf.views.urlopen')
    def test_test_mode_rejects_live_credentials_before_api_call(self, urlopen):
        from .views import _razorpay_request

        with self.assertRaisesRegex(RazorpayError, 'configured for test mode'):
            _razorpay_request('POST', 'orders', {'amount': 50000})

        urlopen.assert_not_called()

    @patch('turf.views._razorpay_request')
    def test_slot_conflict_returns_to_selected_date_with_availability_message(
        self,
        razorpay_request,
    ):
        turfBooking.objects.create(
            time_slot='6-7 am',
            isBooked=True,
            days='2030-01-01',
        )

        response = self.client.post(
            reverse('turfBilling'),
            {
                'selected_date': '2030-01-01',
                'slots': ['6-7 am'],
            },
        )

        self.assertRedirects(
            response,
            f"{reverse('slot_details')}?date=2030-01-01",
            fetch_redirect_response=False,
        )
        response = self.client.get(response.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'These slots are no longer available')
        self.assertContains(response, '2030-01-01')
        razorpay_request.assert_not_called()

    @patch('turf.views._razorpay_request')
    def test_payment_callback_requires_valid_signature_and_captured_amount(
        self,
        razorpay_request,
    ):
        session = self.client.session
        session['pending_razorpay_booking'] = {
            'order_id': 'order_test123',
            'user_id': self.user.pk,
            'amount': 500,
            'selected_date': '2030-01-01',
            'current_date': '2029-12-31',
            'booking_time': '09:00:00',
            'slots': ['6-7 am'],
        }
        session.save()
        payment_id = 'pay_test123'
        signature = hmac.new(
            b'test_secret',
            b'order_test123|pay_test123',
            hashlib.sha256,
        ).hexdigest()
        razorpay_request.return_value = {
            'id': payment_id,
            'order_id': 'order_test123',
            'amount': 50000,
            'currency': 'INR',
            'status': 'captured',
            'method': 'upi',
        }

        response = self.client.post(
            reverse('success'),
            {
                'razorpay_order_id': 'order_test123',
                'razorpay_payment_id': payment_id,
                'razorpay_signature': signature,
            },
        )

        self.assertEqual(response.status_code, 200)
        booking = TurfBooked.objects.get(payment_id=payment_id)
        self.assertTrue(booking.paid)
        self.assertFalse(booking.is_dummy_payment)
        self.assertEqual(booking.razorpay_order_id, 'order_test123')
        self.assertEqual(booking.amount, 500)
        self.assertEqual(booking.payment_method, 'upi')
        self.assertNotIn('pending_razorpay_booking', self.client.session)

    @patch('turf.views._razorpay_request')
    def test_invalid_checkout_signature_never_confirms_a_booking(self, razorpay_request):
        session = self.client.session
        session['pending_razorpay_booking'] = {
            'order_id': 'order_test123',
            'user_id': self.user.pk,
            'amount': 500,
            'selected_date': '2030-01-01',
            'current_date': '2029-12-31',
            'booking_time': '09:00:00',
            'slots': ['6-7 am'],
        }
        session.save()

        response = self.client.post(
            reverse('success'),
            {
                'razorpay_order_id': 'order_test123',
                'razorpay_payment_id': 'pay_test123',
                'razorpay_signature': 'not-a-valid-signature',
            },
        )

        self.assertRedirects(response, reverse('book_now'))
        self.assertFalse(TurfBooked.objects.exists())
        razorpay_request.assert_not_called()

    @patch('turf.views._razorpay_request')
    def test_tampered_or_uncaptured_payment_never_confirms_booking(
        self,
        razorpay_request,
    ):
        session = self.client.session
        session['pending_razorpay_booking'] = {
            'order_id': 'order_test123',
            'user_id': self.user.pk,
            'amount': 500,
            'selected_date': '2030-01-01',
            'current_date': '2029-12-31',
            'booking_time': '09:00:00',
            'slots': ['6-7 am'],
        }
        session.save()
        payment_id = 'pay_test123'
        signature = hmac.new(
            b'test_secret',
            b'order_test123|pay_test123',
            hashlib.sha256,
        ).hexdigest()
        razorpay_request.return_value = {
            'id': payment_id,
            'order_id': 'order_test123',
            'amount': 100,
            'currency': 'INR',
            'status': 'authorized',
        }

        response = self.client.post(
            reverse('success'),
            {
                'razorpay_order_id': 'order_test123',
                'razorpay_payment_id': payment_id,
                'razorpay_signature': signature,
            },
        )

        self.assertRedirects(response, reverse('book_now'))
        self.assertFalse(TurfBooked.objects.exists())


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    TIME_ZONE='UTC',
    TURF_TIME_ZONE='UTC',
    RAZOR_KEY_ID='rzp_test_key',
    RAZOR_KEY_SECRET='test_secret',
)
class BookingCancellationTests(TestCase):
    NOW = datetime(2030, 1, 1, 4, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.user = User.objects.create_user(
            username='booking-owner',
            email='booking-owner@example.com',
            password='TestingPass!123',
        )
        self.client.force_login(self.user)

    def create_booking(self, **overrides):
        values = {
            'name': self.user.username,
            'email': self.user.email,
            'amount': 500,
            'selected_date': '2030-01-01',
            'current_date': '2029-12-20',
            'booking_time': '10:00:00',
            'slots': '["6-7 am"]',
            'payment_id': 'pay_livepaymentid',
            'payment_method': 'upi',
            'paid': True,
            'is_dummy_payment': False,
        }
        values.update(overrides)
        return TurfBooked.objects.create(**values)

    @patch('turf.views.urlopen')
    def test_razorpay_refund_uses_payment_id_and_amount_in_paise(self, urlopen):
        response = urlopen.return_value.__enter__.return_value
        response.read.return_value = b'{"id":"rfnd_test_123","status":"processed"}'

        refund_id, refund_status = _request_razorpay_refund(
            'pay_livepaymentid',
            50000,
            12,
        )

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.razorpay.com/v1/payments/pay_livepaymentid/refund')
        self.assertEqual(request.method, 'POST')
        self.assertEqual(json.loads(request.data), {
            'amount': 50000,
            'notes': {'booking_id': '12'},
        })
        self.assertEqual((refund_id, refund_status), ('rfnd_test_123', 'processed'))

    def test_slot_start_uses_the_earliest_booked_slot_and_rolls_midnight_forward(self):
        booking = self.create_booking(
            selected_date='2030-01-01',
            slots='["7-8 am", "12-1 am", "6-7 am"]',
        )

        self.assertEqual(
            _booking_start_datetime(booking),
            datetime(2030, 1, 1, 6, 0, tzinfo=timezone.utc),
        )

        booking.slots = '["12-1 am"]'
        self.assertEqual(
            _booking_start_datetime(booking),
            datetime(2030, 1, 2, 0, 0, tzinfo=timezone.utc),
        )

    @patch('turf.views._current_time')
    @patch('turf.views._request_razorpay_refund')
    def test_live_payment_cancelled_two_hours_before_slot_is_refunded_and_emailed(
        self,
        request_refund,
        now,
    ):
        now.return_value = self.NOW
        booking = self.create_booking()
        slot = turfBooking.objects.create(
            time_slot='6-7 am',
            isBooked=True,
            days='2030-01-01',
        )
        request_refund.return_value = ('rfnd_test_123', 'processed')

        response = self.client.post(reverse('delete_booking', args=[booking.pk]))

        self.assertRedirects(response, reverse('orderHistory'))
        booking.refresh_from_db()
        self.assertTrue(booking.cancelled)
        self.assertEqual(booking.refund_id, 'rfnd_test_123')
        self.assertEqual(booking.refund_status, 'processed')
        slot.refresh_from_db()
        self.assertFalse(slot.isBooked)
        self.assertEqual(slot.days, '')
        request_refund.assert_called_once_with(booking.payment_id, 50000, booking.pk)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('has been processed', mail.outbox[0].body)

    @patch('turf.views._current_time')
    @patch('turf.views._request_razorpay_refund')
    def test_live_payment_cancelled_inside_two_hours_gets_no_refund(
        self,
        request_refund,
        now,
    ):
        now.return_value = datetime(2030, 1, 1, 5, 0, tzinfo=timezone.utc)
        booking = self.create_booking()

        self.client.post(reverse('delete_booking', args=[booking.pk]))

        booking.refresh_from_db()
        self.assertTrue(booking.cancelled)
        self.assertEqual(booking.refund_status, 'not_eligible')
        request_refund.assert_not_called()
        self.assertIn('less than 2 hours', mail.outbox[0].body)

    @patch('turf.views._current_time')
    @patch('turf.views._request_razorpay_refund')
    def test_unconfirmed_gateway_refund_is_not_retried_on_repeat_cancel(
        self,
        request_refund,
        now,
    ):
        now.return_value = self.NOW
        booking = self.create_booking()
        request_refund.side_effect = RefundProcessingError('provider unavailable')

        self.client.post(reverse('delete_booking', args=[booking.pk]))
        response = self.client.post(reverse('delete_booking', args=[booking.pk]))

        booking.refresh_from_db()
        self.assertTrue(booking.cancelled)
        self.assertEqual(booking.refund_status, 'review_required')
        self.assertEqual(request_refund.call_count, 1)
        self.assertRedirects(response, reverse('orderHistory'))
        self.assertIn('review it', mail.outbox[0].body)

    def test_user_cannot_cancel_another_users_booking(self):
        other_user = User.objects.create_user(
            username='another-user',
            email='another-user@example.com',
            password='TestingPass!123',
        )
        booking = self.create_booking(email=other_user.email)

        response = self.client.post(reverse('delete_booking', args=[booking.pk]))

        self.assertRedirects(response, reverse('orderHistory'))
        booking.refresh_from_db()
        self.assertFalse(booking.cancelled)
        self.assertEqual(mail.outbox, [])
