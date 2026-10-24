from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib import admin
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import TestCase, RequestFactory

from unittest.mock import patch

from .models import Event, TicketOrder, EventAttendee, TicketTier, IndividualTicket
from .stripe_utils import finalize_ticket_order


User = get_user_model()


class TicketOrderModelTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        from datetime import timedelta
        from django.utils import timezone

        cls.host = User.objects.create_user(
            username="ticket_test_host",
            email="host@example.com",
            password="test-password-not-for-production",
        )

        cls.event = Event.objects.create(
            host=cls.host,
            title="Ticket Order Test Event",
            event_date=timezone.now() + timedelta(days=30),
            country="US",
            is_ticketed=True,
        )

        cls.tier = TicketTier.objects.create(
            event=cls.event,
            name="VIP",
            description="VIP admission",
            price=Decimal("50.00"),
            capacity=30,
            sold_count=7,
            reserved_count=2,
        )

    def test_pending_order_can_exist_without_an_rsvp(self):
        order = TicketOrder.objects.create(
            event=self.event,
            ticket_tier=self.tier,
            ticket_quantity=3,
            first_name="Test",
            last_name="Attendee",
            email="attendee@example.com",
            status="pending",
        )

        self.assertIsNone(order.event_attendee_id)
        self.assertEqual(order.status, "pending")
        self.assertEqual(order.ticket_quantity, 3)

    def test_creating_pending_order_does_not_change_tier_inventory(self):
        original_sold = self.tier.sold_count
        original_reserved = self.tier.reserved_count

        TicketOrder.objects.create(
            event=self.event,
            ticket_tier=self.tier,
            ticket_quantity=3,
            first_name="Test",
            last_name="Attendee",
            email="attendee@example.com",
            status="pending",
        )

        self.tier.refresh_from_db()

        self.assertEqual(self.tier.sold_count, original_sold)
        self.assertEqual(self.tier.reserved_count, original_reserved)

    def test_admin_cancellation_does_not_change_inventory(self):
        from events.admin import TicketOrderAdmin

        order = TicketOrder.objects.create(
            event=self.event,
            ticket_tier=self.tier,
            ticket_quantity=3,
            first_name="Test",
            last_name="Attendee",
            email="cancel-test@example.com",
            status=TicketOrder.STATUS_PENDING,
        )

        original_sold = self.tier.sold_count
        original_reserved = self.tier.reserved_count

        request = RequestFactory().post("/admin/")
        request.user = self.host
        request.session = {}
        request._messages = FallbackStorage(request)

        model_admin = TicketOrderAdmin(
            TicketOrder,
            admin.site,
        )

        model_admin.cancel_ticket_orders(
            request,
            TicketOrder.objects.filter(pk=order.pk),
        )

        order.refresh_from_db()
        self.tier.refresh_from_db()

        self.assertEqual(
            order.status,
            TicketOrder.STATUS_CANCELLED,
        )
        self.assertFalse(order.inventory_released)
        self.assertEqual(self.tier.sold_count, original_sold)
        self.assertEqual(self.tier.reserved_count, original_reserved)


class TicketOrderFinalizationTests(TicketOrderModelTests):

    def create_pending_order(self, **overrides):
        data = {
            "event": self.event,
            "ticket_tier": self.tier,
            "ticket_quantity": 3,
            "first_name": "Test",
            "last_name": "Attendee",
            "email": "attendee@example.com",
            "status": TicketOrder.STATUS_PENDING,
            "stripe_session_id": "cs_test_ticket_order",
        }
        data.update(overrides)
        return TicketOrder.objects.create(**data)

    def paid_session(self, order):
        return {
            "id": order.stripe_session_id,
            "payment_status": "paid",
            "metadata": {"pending_order_id": str(order.pk)},
        }

    @patch("events.utils.stripe.checkout.Session.retrieve")
    @patch("events.utils.send_attendance_confirmation")
    def test_paid_order_creates_one_rsvp_and_increments_sold_count(
        self, mock_confirmation, mock_session_retrieve
    ):
        mock_session_retrieve.return_value = {
            "currency": "usd",
            "amount_total": 15000,
        }

        order = self.create_pending_order()
        original_sold = self.tier.sold_count

        result = finalize_ticket_order(self.paid_session(order))

        self.assertIsNotNone(result)

        self.assertEqual(
            EventAttendee.objects.filter(
                event=self.event,
                email=order.email,
                ticket_tier=self.tier,
            ).count(),
            1,
        )

        order.refresh_from_db()
        self.tier.refresh_from_db()

        self.assertEqual(order.status, TicketOrder.STATUS_PAID)
        self.assertEqual(order.event_attendee_id, result.pk)
        self.assertEqual(self.tier.sold_count, original_sold + 3)

    @patch("events.utils.stripe.checkout.Session.retrieve")
    @patch("events.views.send_attendance_confirmation")
    def test_repeated_paid_webhook_does_not_duplicate_rsvp_or_sales(
        self, mock_confirmation, mock_session_retrieve
    ):
        mock_session_retrieve.return_value = {
            "currency": "usd",
            "amount_total": 15000,
        }

        order = self.create_pending_order()
        session = self.paid_session(order)
        original_sold = self.tier.sold_count

        first_result = finalize_ticket_order(session)
        second_result = finalize_ticket_order(session)

        self.tier.refresh_from_db()

        self.assertEqual(first_result.pk, second_result.pk)
        self.assertEqual(
            EventAttendee.objects.filter(
                event=self.event,
                email=order.email,
                ticket_tier=self.tier,
            ).count(),
            1,
        )
        self.assertEqual(self.tier.sold_count, original_sold + 3)

    def test_unpaid_session_does_not_finalize_order(self):
        order = self.create_pending_order()
        session = self.paid_session(order)
        session["payment_status"] = "unpaid"
        original_sold = self.tier.sold_count

        result = finalize_ticket_order(session)

        order.refresh_from_db()
        self.tier.refresh_from_db()

        self.assertIsNone(result)
        self.assertEqual(order.status, TicketOrder.STATUS_PENDING)
        self.assertIsNone(order.event_attendee_id)
        self.assertEqual(self.tier.sold_count, original_sold)
        self.assertFalse(
            EventAttendee.objects.filter(event=self.event).exists()
        )

    @patch("events.utils.stripe.checkout.Session.retrieve")
    @patch("events.stripe_utils.refund_ticket_order")
    def test_cancelled_order_does_not_create_rsvp_or_sell_tickets(
        self, mock_refund, mock_session_retrieve
    ):
        mock_session_retrieve.return_value = {
            "currency": "usd",
            "amount_total": 15000,
        }

        order = self.create_pending_order(
            status=TicketOrder.STATUS_CANCELLED
        )
        original_sold = self.tier.sold_count

        result = finalize_ticket_order(
            self.paid_session(order)
        )

        order.refresh_from_db()
        self.tier.refresh_from_db()

        self.assertIsNone(result)
        self.assertIsNone(order.event_attendee_id)
        self.assertEqual(self.tier.sold_count, original_sold)
        self.assertFalse(
            EventAttendee.objects.filter(event=self.event).exists()
        )
        mock_refund.assert_called_once_with(order.pk)

    def test_refunded_order_does_not_create_rsvp_or_sell_tickets(self):
        order = self.create_pending_order(
            status=TicketOrder.STATUS_REFUNDED
        )
        original_sold = self.tier.sold_count

        result = finalize_ticket_order(self.paid_session(order))

        order.refresh_from_db()
        self.tier.refresh_from_db()

        self.assertIsNone(result)
        self.assertIsNone(order.event_attendee_id)
        self.assertEqual(self.tier.sold_count, original_sold)
        self.assertFalse(
            EventAttendee.objects.filter(event=self.event).exists()
        )

    @patch("events.views.send_attendance_confirmation")
    def test_qr_enabled_order_creates_one_individual_ticket_per_quantity(
        self, mock_confirmation
    ):
        self.event.enable_qr_checkins = True
        self.event.save(update_fields=["enable_qr_checkins"])

        order = self.create_pending_order()
        finalize_ticket_order(self.paid_session(order))

        order.refresh_from_db()

        self.assertEqual(
            IndividualTicket.objects.filter(
                event_attendee=order.event_attendee
            ).count(),
            order.ticket_quantity,
        )
        self.assertSetEqual(
            set(
                IndividualTicket.objects.filter(
                    event_attendee=order.event_attendee
                ).values_list("ticket_number", flat=True)
            ),
            {1, 2, 3},
        )