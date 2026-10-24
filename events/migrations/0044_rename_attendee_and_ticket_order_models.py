
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0043_pendingticketorder_campaign_id_and_more"),
    ]

    operations = [
        # Rename existing models.
        migrations.RenameModel(
            old_name="RSVP",
            new_name="EventAttendee",
        ),
        migrations.RenameModel(
            old_name="RSVPGuest",
            new_name="FreeAttendeeGuest",
        ),
        migrations.RenameModel(
            old_name="PendingTicketOrder",
            new_name="TicketOrder",
        ),

        # Remove the old constraint before renaming its field.
        migrations.RemoveConstraint(
            model_name="individualticket",
            name="unique_ticket_number_per_rsvp",
        ),

        # Rename existing relationship fields.
        migrations.RenameField(
            model_name="freeattendeeguest",
            old_name="rsvp",
            new_name="event_attendee",
        ),
        migrations.RenameField(
            model_name="individualticket",
            old_name="rsvp",
            new_name="event_attendee",
        ),
        migrations.RenameField(
            model_name="ticketorder",
            old_name="rsvp",
            new_name="event_attendee",
        ),
        migrations.RenameField(
            model_name="feedback",
            old_name="rsvp",
            new_name="event_attendee",
        ),

        # Update relationship names.
        migrations.AlterField(
            model_name="eventattendee",
            name="event",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="attendees",
                to="events.event",
            ),
        ),
        migrations.AlterField(
            model_name="eventattendee",
            name="ticket_tier",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="attendees",
                to="events.tickettier",
            ),
        ),
        migrations.AlterField(
            model_name="freeattendeeguest",
            name="event_attendee",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="plus_ones",
                to="events.eventattendee",
            ),
        ),
        migrations.AlterField(
            model_name="individualticket",
            name="event_attendee",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="individual_tickets",
                to="events.eventattendee",
            ),
        ),
        migrations.AlterField(
            model_name="ticketorder",
            name="event",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="ticket_orders",
                to="events.event",
            ),
        ),
        migrations.AlterField(
            model_name="ticketorder",
            name="ticket_tier",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="ticket_orders",
                to="events.tickettier",
            ),
        ),
        migrations.AlterField(
            model_name="ticketorder",
            name="event_attendee",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="ticket_order",
                to="events.eventattendee",
            ),
        ),
        migrations.AlterField(
            model_name="feedback",
            name="event_attendee",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="feedbacks",
                to="events.eventattendee",
            ),
        ),

        # Add the constraint using the renamed field.
        migrations.AddConstraint(
            model_name="individualticket",
            constraint=models.UniqueConstraint(
                fields=("event_attendee", "ticket_number"),
                name="unique_ticket_number_per_attendee",
            ),
        ),
    ]
