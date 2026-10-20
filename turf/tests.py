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

from .models import (
    Feedback,
    Turf,
    TurfBooked,
    TurfBookingPayment,
    TurfSettings,
    bookslot,
    turfBooking,
)
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
        self.assertContains(response, 'enter any 4–10 digit OTP')
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
            f"{reverse('slot_details')}?date=2030-01-01&turf_id={Turf.objects.get(is_default=True).pk}",
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
        self.assertTrue(booking.turf.is_default)
        self.assertEqual(booking.turf_name, booking.turf.name)
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


class MultiTurfManagementTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='turf-manager',
            email='manager@example.com',
            password='TestingPass!123',
            is_staff=True,
        )
        self.user = User.objects.create_user(
            username='turf-customer',
            email='customer@example.com',
            password='TestingPass!123',
        )
        self.default_turf = Turf.objects.get(is_default=True)
        self.second_turf = Turf.objects.create(
            name='Practice Field',
            slot_price=900,
            description='A second bookable field',
            is_active=True,
        )

    def test_owner_can_create_edit_and_remove_turfs_without_losing_booking_history(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse('owner_turf_create'),
            {
                'name': 'Training Ground',
                'slot_price': '750',
                'description': 'Training sessions',
                'address': 'North entrance',
                'contact_phone': '',
                'contact_email': '',
                'is_active': 'on',
            },
        )

        self.assertRedirects(response, reverse('owner_turfs'))
        turf = Turf.objects.get(name='Training Ground')
        response = self.client.post(
            reverse('owner_turf_edit', args=[turf.pk]),
            {
                'name': 'Training Ground East',
                'slot_price': '800',
                'description': 'Updated details',
                'address': 'East entrance',
                'contact_phone': '',
                'contact_email': '',
                'is_active': 'on',
            },
        )
        self.assertRedirects(response, reverse('owner_turfs'))
        turf.refresh_from_db()
        self.assertEqual(turf.name, 'Training Ground East')
        self.assertEqual(turf.slot_price, 800)

        booking = TurfBooked.objects.create(
            name='Customer',
            email='customer@example.com',
            turf=turf,
            turf_name=turf.name,
            amount=800,
            selected_date='2030-01-01',
            current_date='2029-12-31',
            payment_id='pay_training',
        )
        response = self.client.post(reverse('owner_turf_delete', args=[turf.pk]))

        self.assertRedirects(response, reverse('owner_turfs'))
        turf.refresh_from_db()
        booking.refresh_from_db()
        self.assertFalse(turf.is_active)
        self.assertEqual(booking.turf_id, turf.pk)
        self.assertEqual(booking.turf_name, 'Training Ground East')

    def test_customer_can_see_availability_independently_per_turf(self):
        self.client.force_login(self.user)
        TurfBooked.objects.create(
            name='Existing customer',
            email='existing@example.com',
            turf=self.default_turf,
            turf_name=self.default_turf.name,
            amount=500,
            selected_date='2030-01-01',
            current_date='2029-12-31',
            slots='["6-7 am"]',
            payment_id='pay_default_slot',
            paid=True,
        )

        second_response = self.client.get(
            reverse('slot_details'),
            {'date': '2030-01-01', 'turf_id': self.second_turf.pk},
        )
        default_response = self.client.get(
            reverse('slot_details'),
            {'date': '2030-01-01', 'turf_id': self.default_turf.pk},
        )

        self.assertEqual(second_response.status_code, 200)
        self.assertFalse(second_response.context['time_slots'][0]['booked'])
        self.assertEqual(second_response.context['slot_price'], 900)
        self.assertTrue(default_response.context['time_slots'][0]['booked'])

    @patch('turf.views._razorpay_request')
    def test_checkout_uses_selected_turf_price_and_keeps_turf_in_session(self, razorpay_request):
        self.client.force_login(self.user)
        turfBooking.objects.create(
            time_slot='6-7 am',
            isBooked=True,
            days='2030-01-01',
        )
        razorpay_request.return_value = {
            'id': 'order_secondturf',
            'amount': 90000,
            'currency': 'INR',
        }

        response = self.client.post(
            reverse('turfBilling'),
            {
                'selected_date': '2030-01-01',
                'turf_id': str(self.second_turf.pk),
                'slots': ['6-7 am'],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Practice Field')
        self.assertEqual(razorpay_request.call_args.args[2]['amount'], 90000)
        self.assertEqual(
            self.client.session['pending_razorpay_booking']['turf_id'],
            self.second_turf.pk,
        )

    @override_settings(
        RAZORPAY_MODE='test',
        RAZOR_KEY_ID='rzp_test_key',
        RAZOR_KEY_SECRET='test_secret',
        TURF_TIME_ZONE='UTC',
    )
    @patch('turf.views._razorpay_request')
    def test_payment_confirmation_saves_the_selected_turf(self, razorpay_request):
        self.client.force_login(self.user)
        session = self.client.session
        session['pending_razorpay_booking'] = {
            'order_id': 'order_secondturf',
            'user_id': self.user.pk,
            'turf_id': self.second_turf.pk,
            'turf_name': self.second_turf.name,
            'amount': 900,
            'selected_date': '2030-01-01',
            'current_date': '2029-12-31',
            'booking_time': '09:00:00',
            'slots': ['6-7 am'],
        }
        session.save()
        payment_id = 'pay_secondturf'
        signature = hmac.new(
            b'test_secret',
            b'order_secondturf|pay_secondturf',
            hashlib.sha256,
        ).hexdigest()
        razorpay_request.return_value = {
            'id': payment_id,
            'order_id': 'order_secondturf',
            'amount': 90000,
            'currency': 'INR',
            'status': 'captured',
            'method': 'upi',
        }

        response = self.client.post(
            reverse('success'),
            {
                'razorpay_order_id': 'order_secondturf',
                'razorpay_payment_id': payment_id,
                'razorpay_signature': signature,
            },
        )

        self.assertEqual(response.status_code, 200)
        booking = TurfBooked.objects.get(payment_id=payment_id)
        self.assertEqual(booking.turf_id, self.second_turf.pk)
        self.assertEqual(booking.turf_name, self.second_turf.name)

    def test_non_staff_cannot_manage_turfs(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('owner_turfs'))

        self.assertRedirects(response, reverse('book_now'))


class FeedbackTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='feedback-user',
            email='feedback@example.com',
            password='TestingPass!123',
        )
        self.staff = User.objects.create_user(
            username='feedback-admin',
            email='admin-feedback@example.com',
            password='TestingPass!123',
            is_staff=True,
        )

    def test_feedback_requires_login(self):
        response = self.client.get(reverse('feedback'))

        self.assertRedirects(
            response,
            f"{reverse('login')}?next={reverse('feedback')}",
        )

    def test_homepage_displays_feedback_to_anonymous_visitors(self):
        Feedback.objects.create(
            user=self.user,
            rating=4,
            message='The turf was clean and well maintained.',
            admin_response='Thank you for visiting!',
        )

        response = self.client.get(reverse('index'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'The turf was clean and well maintained.')
        self.assertContains(response, 'Thank you for visiting!')
        self.assertNotContains(response, self.user.username)

    def test_user_can_submit_and_only_sees_their_feedback(self):
        another_user = User.objects.create_user(
            username='another-feedback-user',
            email='another-feedback@example.com',
            password='TestingPass!123',
        )
        Feedback.objects.create(
            user=another_user,
            rating=1,
            message='Private feedback',
        )
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('feedback'),
            {'rating': '5', 'message': 'Great facilities!'},
        )

        self.assertRedirects(response, reverse('feedback'))
        feedback_item = Feedback.objects.get(user=self.user)
        self.assertEqual(feedback_item.rating, 5)
        self.assertEqual(feedback_item.message, 'Great facilities!')
        self.assertEqual(feedback_item.user_id, self.user.pk)

        response = self.client.get(reverse('feedback'))
        self.assertContains(response, 'Great facilities!')
        self.assertNotContains(response, 'Private feedback')

    def test_staff_can_update_feedback_status_and_reply(self):
        feedback_item = Feedback.objects.create(
            user=self.user,
            rating=2,
            message='The lights were not working.',
        )
        self.client.force_login(self.staff)

        response = self.client.get(reverse('owner_feedback'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'The lights were not working.')

        response = self.client.post(
            reverse('owner_feedback'),
            {
                'feedback_id': str(feedback_item.pk),
                'status': Feedback.STATUS_RESOLVED,
                'admin_response': 'Thank you. The lights have been fixed.',
            },
        )

        self.assertRedirects(response, reverse('owner_feedback'))
        feedback_item.refresh_from_db()
        self.assertEqual(feedback_item.status, Feedback.STATUS_RESOLVED)
        self.assertEqual(feedback_item.admin_response, 'Thank you. The lights have been fixed.')

    def test_non_staff_cannot_manage_feedback(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('owner_feedback'))

        self.assertRedirects(response, reverse('book_now'))

    def test_invalid_feedback_rating_is_rejected(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('feedback'),
            {'rating': '6', 'message': 'Invalid rating'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Feedback.objects.exists())


@override_settings(TURF_TIME_ZONE='UTC')
class BookingEditTests(TestCase):
    NOW = datetime(2030, 1, 1, 4, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.user = User.objects.create_user(
            username='booking-editor',
            email='booking-editor@example.com',
            password='TestingPass!123',
        )
        self.client.force_login(self.user)
        self.turf = Turf.objects.get(is_default=True)
        bookslot.objects.get_or_create(
            pk=1,
            defaults={'week': json.dumps([[0] * 20 for _ in range(7)])},
        )
        self.booking = TurfBooked.objects.create(
            name=self.user.username,
            email=self.user.email,
            turf=self.turf,
            turf_name=self.turf.name,
            amount=1000,
            selected_date='2030-01-02',
            current_date='2030-01-01',
            booking_time='04:00:00',
            slots='["6-7 am", "7-8 am"]',
            payment_id='pay_bookingedit',
            payment_method='upi',
            paid=True,
            is_dummy_payment=False,
        )
        self.payment = TurfBookingPayment.objects.create(
            booking=self.booking,
            payment_id=self.booking.payment_id,
            amount=1000,
        )

    @patch('turf.views._current_time')
    def test_order_history_shows_edit_slots_for_future_booking(self, now):
        now.return_value = self.NOW

        response = self.client.get(reverse('orderHistory'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Edit Time Slots')
        self.assertContains(response, 'name="slots"')

    @patch('turf.views._current_time')
    @patch('turf.views.send_mail')
    def test_user_can_change_slots_without_changing_the_price(self, send_mail, now):
        now.return_value = self.NOW

        response = self.client.post(
            reverse('edit_booking', args=[self.booking.pk]),
            {'slots': ['8-9 am', '9-10 am']},
        )

        self.assertRedirects(response, reverse('orderHistory'))
        self.booking.refresh_from_db()
        self.assertEqual(json.loads(self.booking.slots), ['8-9 am', '9-10 am'])
        self.assertEqual(self.booking.amount, 1000)
        send_mail.assert_called_once()
        self.assertEqual(
            send_mail.call_args.kwargs['recipient_list'],
            [self.user.email],
        )
        self.assertEqual(
            send_mail.call_args.kwargs['subject'],
            'SK Sports Park booking updated',
        )
        self.assertIn('Updated time slots: 8-9 am, 9-10 am', send_mail.call_args.kwargs['message'])
        self.assertTrue(
            turfBooking.objects.filter(
                time_slot='8-9 am',
                days='2030-01-02',
                isBooked=True,
            ).exists()
        )

    @patch('turf.views._current_time')
    @patch('turf.views.send_mail', side_effect=OSError('mail server unavailable'))
    def test_booking_change_succeeds_and_reports_email_failure(
        self,
        send_mail,
        now,
    ):
        now.return_value = self.NOW

        response = self.client.post(
            reverse('edit_booking', args=[self.booking.pk]),
            {'slots': ['8-9 am', '9-10 am']},
            follow=True,
        )

        self.booking.refresh_from_db()
        self.assertEqual(json.loads(self.booking.slots), ['8-9 am', '9-10 am'])
        self.assertContains(response, 'Booking updated, but we could not send the email notification.')
        send_mail.assert_called_once()

    @patch('turf.views._current_time')
    @patch('turf.views._request_razorpay_refund')
    def test_reducing_slot_count_refunds_difference_and_updates_booking(
        self,
        request_refund,
        now,
    ):
        now.return_value = self.NOW
        request_refund.return_value = ('rfnd_edit_123', 'processed')

        response = self.client.post(
            reverse('edit_booking', args=[self.booking.pk]),
            {'slots': ['6-7 am']},
        )

        self.assertRedirects(response, reverse('orderHistory'))
        self.booking.refresh_from_db()
        self.payment.refresh_from_db()
        self.assertEqual(json.loads(self.booking.slots), ['6-7 am'])
        self.assertEqual(self.booking.amount, 500)
        self.assertEqual(self.payment.refunded_amount, 500)
        request_refund.assert_called_once_with(
            self.payment.payment_id,
            50000,
            self.booking.pk,
        )

    @patch('turf.views._current_time')
    @patch('turf.views._request_razorpay_refund')
    def test_reduction_inside_two_hour_cutoff_is_rejected(
        self,
        request_refund,
        now,
    ):
        now.return_value = datetime(2030, 1, 2, 4, 30, tzinfo=timezone.utc)

        response = self.client.post(
            reverse('edit_booking', args=[self.booking.pk]),
            {'slots': ['6-7 am']},
        )

        self.assertRedirects(response, reverse('orderHistory'))
        self.booking.refresh_from_db()
        self.assertEqual(json.loads(self.booking.slots), ['6-7 am', '7-8 am'])
        self.assertEqual(self.booking.amount, 1000)
        request_refund.assert_not_called()

    @patch('turf.views._current_time')
    @patch('turf.views._request_razorpay_refund')
    def test_slot_reduction_can_refund_across_multiple_payment_records(
        self,
        request_refund,
        now,
    ):
        now.return_value = self.NOW
        self.booking.amount = 1500
        self.booking.slots = '["6-7 am", "7-8 am", "8-9 am"]'
        self.booking.save(update_fields=['amount', 'slots'])
        self.payment.amount = 500
        self.payment.save(update_fields=['amount'])
        first_additional = TurfBookingPayment.objects.create(
            booking=self.booking,
            payment_id='pay_edit_additional_1',
            amount=500,
        )
        second_additional = TurfBookingPayment.objects.create(
            booking=self.booking,
            payment_id='pay_edit_additional_2',
            amount=500,
        )
        request_refund.side_effect = [
            ('rfnd_edit_2', 'processed'),
            ('rfnd_edit_1', 'processed'),
        ]

        response = self.client.post(
            reverse('edit_booking', args=[self.booking.pk]),
            {'slots': ['6-7 am']},
        )

        self.assertRedirects(response, reverse('orderHistory'))
        self.booking.refresh_from_db()
        first_additional.refresh_from_db()
        second_additional.refresh_from_db()
        self.assertEqual(self.booking.amount, 500)
        self.assertEqual(json.loads(self.booking.slots), ['6-7 am'])
        self.assertEqual(first_additional.refunded_amount, 500)
        self.assertEqual(second_additional.refunded_amount, 500)
        self.assertEqual(request_refund.call_count, 2)

    @patch('turf.views._current_time')
    def test_edit_rejects_slots_booked_by_another_customer(self, now):
        now.return_value = self.NOW
        TurfBooked.objects.create(
            name='another-user',
            email='another-user@example.com',
            turf=self.turf,
            turf_name=self.turf.name,
            amount=500,
            selected_date=self.booking.selected_date,
            current_date='2030-01-01',
            booking_time='04:00:00',
            slots='["9-10 am"]',
            payment_id='pay_bookingedit_conflict',
            paid=True,
            is_dummy_payment=True,
        )

        response = self.client.post(
            reverse('edit_booking', args=[self.booking.pk]),
            {'slots': ['6-7 am', '9-10 am']},
        )

        self.assertRedirects(response, reverse('orderHistory'))
        self.booking.refresh_from_db()
        self.assertEqual(json.loads(self.booking.slots), ['6-7 am', '7-8 am'])

    @patch('turf.views._current_time')
    @patch('turf.views._request_razorpay_refund')
    def test_cancellation_refunds_original_and_added_slot_payments(
        self,
        request_refund,
        now,
    ):
        now.return_value = self.NOW
        self.booking.amount = 1500
        self.booking.slots = '["6-7 am", "7-8 am", "8-9 am"]'
        self.booking.save(update_fields=['amount', 'slots'])
        additional_payment = TurfBookingPayment.objects.create(
            booking=self.booking,
            payment_id='pay_edit_extra',
            amount=500,
        )
        request_refund.side_effect = [
            ('rfnd_original', 'processed'),
            ('rfnd_added', 'processed'),
        ]

        response = self.client.post(
            reverse('delete_booking', args=[self.booking.pk]),
        )

        self.assertRedirects(response, reverse('orderHistory'))
        self.assertEqual(
            request_refund.call_args_list,
            [
                ((self.booking.payment_id, 100000, self.booking.pk),),
                ((additional_payment.payment_id, 50000, self.booking.pk),),
            ],
        )
        self.payment.refresh_from_db()
        additional_payment.refresh_from_db()
        self.assertEqual(self.payment.refunded_amount, 1000)
        self.assertEqual(additional_payment.refunded_amount, 500)

    @override_settings(RAZOR_KEY_SECRET='test_secret')
    @patch('turf.views._current_time')
    @patch('turf.views._razorpay_request')
    @patch('turf.views.send_mail')
    def test_adding_slot_requires_and_confirms_incremental_payment(
        self,
        send_mail,
        razorpay_request,
        now,
    ):
        now.return_value = self.NOW
        razorpay_request.side_effect = [
            {
                'id': 'order_bookingedit',
                'amount': 50000,
                'currency': 'INR',
            },
            {
                'id': 'pay_edit_extra',
                'order_id': 'order_bookingedit',
                'amount': 50000,
                'currency': 'INR',
                'status': 'captured',
                'method': 'upi',
            },
        ]
        response = self.client.post(
            reverse('edit_booking', args=[self.booking.pk]),
            {'slots': ['6-7 am', '7-8 am', '8-9 am']},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Pay additional ₹500')

        payment_id = 'pay_edit_extra'
        signature = hmac.new(
            b'test_secret',
            b'order_bookingedit|pay_edit_extra',
            hashlib.sha256,
        ).hexdigest()
        response = self.client.post(
            reverse('success'),
            {
                'razorpay_order_id': 'order_bookingedit',
                'razorpay_payment_id': payment_id,
                'razorpay_signature': signature,
            },
        )

        self.assertRedirects(response, reverse('orderHistory'))
        self.booking.refresh_from_db()
        self.assertEqual(json.loads(self.booking.slots), ['6-7 am', '7-8 am', '8-9 am'])
        self.assertEqual(self.booking.amount, 1500)
        self.assertTrue(
            TurfBookingPayment.objects.filter(
                booking=self.booking,
                payment_id=payment_id,
                amount=500,
            ).exists()
        )
        send_mail.assert_called_once()
        self.assertIn(
            'Updated time slots: 6-7 am, 7-8 am, 8-9 am',
            send_mail.call_args.kwargs['message'],
        )
