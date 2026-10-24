
import os
import tempfile
import logging
from pathlib import Path

from django.conf import settings
from django.http import HttpResponse, HttpResponseServerError
from django.shortcuts import get_object_or_404
from django.urls import reverse

from py_pkpass.models import (
    Pass,
    Barcode,
    BarcodeFormat,
    EventTicket,
)

from .models import EventAttendee

logger = logging.getLogger(__name__)


def download_apple_wallet_pass(request, token):
    attendee = get_object_or_404(
        EventAttendee.objects.select_related("event", "ticket_tier"),
        checkin_token=token,
    )
    event = attendee.event

    # Only issue Apple Wallet passes for eligible paid RSVPs.
    if not attendee.is_paid or not event.enable_qr_checkins:
        return HttpResponse(
            "An Apple Wallet pass is not available for this RSVP.",
            status=403,
            content_type="text/plain",
        )

    # Read real Apple credentials from Django settings.
    pass_type_identifier = getattr(
        settings, "APPLE_PASS_TYPE_IDENTIFIER", ""
    ).strip()
    team_identifier = getattr(
        settings, "APPLE_TEAM_IDENTIFIER", ""
    ).strip()
    organization_name = getattr(
        settings, "APPLE_PASS_ORGANIZATION_NAME", "YoursEvently"
    ).strip()
    cert_password = getattr(
        settings, "APPLE_PASS_CERT_PASSWORD", ""
    )

    if (
        not pass_type_identifier
        or not team_identifier
        or not cert_password
        or "yourdomain" in pass_type_identifier.lower()
        or team_identifier == "YOUR_TEAM_ID"
    ):
        logger.error("Apple Wallet credentials are not configured.")
        return HttpResponseServerError(
            "Apple Wallet is not configured yet."
        )

    assets_dir = Path(settings.BASE_DIR) / "secrets" / "pass_assets"

    cert_path = assets_dir / "certificate.pem"
    key_path = assets_dir / "private.key"
    wwdr_path = assets_dir / "wwdr.pem"
    icon_path = assets_dir / "icon.png"
    logo_path = assets_dir / "logo.png"

    required_files = (
        cert_path,
        key_path,
        wwdr_path,
        icon_path,
        logo_path,
    )

    missing_files = [
        str(path.name)
        for path in required_files
        if not path.is_file()
    ]

    if missing_files:
        logger.error(
            "Apple Wallet assets are missing: %s",
            ", ".join(missing_files),
        )
        return HttpResponseServerError(
            "Apple Wallet assets are incomplete."
        )

    verify_url = request.build_absolute_uri(
        reverse(
            "events:verify_checkin",
            kwargs={
                "slug": event.slug,
                "token": attendee.checkin_token,
            },
        )
    )

    # Build the event-ticket fields using py-pkpass models.
    ticket_info = EventTicket()
    ticket_info.addPrimaryField(
        "event",
        event.title,
        "EVENT",
    )
    ticket_info.addSecondaryField(
        "attendee",
        attendee.full_name or "Guest",
        "ATTENDEE",
    )
    ticket_info.addSecondaryField(
        "tier",
        attendee.ticket_tier.name
        if attendee.ticket_tier
        else "Standard REventAttendee",
        "TICKET TYPE",
    )

    event_date = event.event_date
    date_value = (
        event_date.strftime("%b %d, %Y %I:%M %p")
        if event_date
        else "See event details"
    )
    ticket_info.addAuxiliaryField(
        "date",
        date_value,
        "DATE",
    )

    passfile = Pass(
        ticket_info,
        passTypeIdentifier=pass_type_identifier,
        organizationName=organization_name,
        teamIdentifier=team_identifier,
    )

    passfile.serialNumber = str(attendee.checkin_token)
    passfile.description = f"Ticket for {event.title}"
    passfile.backgroundColor = "rgb(2, 6, 23)"
    passfile.foregroundColor = "rgb(241, 245, 249)"
    passfile.labelColor = "rgb(148, 163, 184)"

    passfile.barcode = Barcode(
        message=verify_url,
        format=BarcodeFormat.QR,
        altText=str(attendee.checkin_token),
    )

    # Add required Wallet image assets.
    with open(icon_path, "rb") as icon_file:
        passfile.addFile("icon.png", icon_file)

    with open(logo_path, "rb") as logo_file:
        passfile.addFile("logo.png", logo_file)

    # Generate the pass in a unique temporary location.
    fd, temp_output_path = tempfile.mkstemp(
        suffix=".pkpass",
        dir=str(settings.BASE_DIR),
    )
    os.close(fd)
    os.unlink(temp_output_path)

    try:
        passfile.create(
            str(cert_path),
            str(key_path),
            str(wwdr_path),
            cert_password,
            temp_output_path,
        )

        with open(temp_output_path, "rb") as pass_file:
            pass_bytes = pass_file.read()

        if not pass_bytes:
            raise ValueError("The generated Apple Wallet pass is empty.")

        response = HttpResponse(
            pass_bytes,
            content_type="application/vnd.apple.pkpass",
        )
        response["Content-Disposition"] = (
            f'attachment; filename="{event.slug}-ticket.pkpass"'
        )
        return response

    except Exception:
        logger.exception(
            "Apple Wallet pass generation failed for RSVP %s.",
            attendee.pk,
        )
        return HttpResponseServerError(
            "Unable to generate the Apple Wallet pass."
        )

    finally:
        if os.path.exists(temp_output_path):
            os.remove(temp_output_path)



import json
import time
import requests
import jwt

from django.conf import settings
from google.auth.transport.requests import Request
from google.oauth2 import service_account


def generate_google_wallet_link(attendee, ticket=None):
    """
    Generate a Google Wallet pass for an individual ticket or
    an older RSVP-level pass.

    Args:
        rsvp: The parent RSVP record.
        ticket: Optional IndividualTicket belonging to that RSVP.
    """
    key_path = str(settings.GOOGLE_WALLET_KEY_PATH)

    # 1. Authenticate with Google Wallet API.
    credentials = service_account.Credentials.from_service_account_file(
        key_path,
        scopes=[
            'https://www.googleapis.com/auth/wallet_object.issuer'
        ],
    )

    auth_req = Request()
    credentials.refresh(auth_req)

    issuer_id = settings.GOOGLE_WALLET_ISSUER_ID
    event = attendee.event

    # Each individual ticket gets its own token and Wallet object.
    checkin_token = (
        ticket.checkin_token if ticket else attendee.checkin_token
    )

    if ticket:
        object_id = f"{issuer_id}.ticket_{checkin_token}"
        ticket_number = str(ticket.ticket_number)
    else:
        # Preserve compatibility with existing EventAttendee-level passes.
        object_id = f"{issuer_id}.rsvp_{checkin_token}"
        ticket_number = str(attendee.id)

    class_id = f"{issuer_id}.{event.slug}"

    if ticket:
        object_id = f"{issuer_id}.ticket_{checkin_token}"
        ticket_number = str(ticket.ticket_number)
    else:
        # Preserve compatibility with existing RSVP-level passes.
        object_id = f"{issuer_id}.rsvp_{attendee.checkin_token}"
        ticket_number = str(attendee.id)

    session = requests.Session()
    session.headers.update({
        'Authorization': f'Bearer {credentials.token}',
        'Content-Type': 'application/json',
    })

    # 2. Check whether the EventTicketClass already exists.
    class_url = (
        "https://walletobjects.googleapis.com/walletobjects/v1/"
        f"eventTicketClass/{class_id}"
    )

    get_response = session.get(class_url)

    if get_response.status_code == 404:
        create_url = (
            "https://walletobjects.googleapis.com/walletobjects/v1/"
            "eventTicketClass"
        )

        class_payload = {
            'id': class_id,
            'issuerName': 'YoursEvently',
            'reviewStatus': 'UNDER_REVIEW',
            'eventName': {
                'defaultValue': {
                    'language': 'en-US',
                    'value': event.title or 'Event Ticket',
                }
            },
            'dateTime': {
                'start': event.event_date.isoformat(),
            },
            'venue': {
                'name': {
                    'defaultValue': {
                        'language': 'en-US',
                        'value': event.location_name or 'Online / TBD',
                    }
                },
                'address': {
                    'defaultValue': {
                        'language': 'en-US',
                        'value': event.address or 'TBD',
                    }
                },
            },
        }

        create_response = session.post(
            create_url,
            json=class_payload,
        )

        print(
            f"--- CREATE CLASS API RESPONSE: "
            f"{create_response.status_code} ---"
        )
        print(create_response.text)

    # 3. Load the private key for signing the JWT.
    with open(key_path, 'r') as key_file:
        service_account_info = json.load(key_file)
        private_key = service_account_info['private_key']


    print("Wallet object ID:", object_id)
    print("Wallet ticket number:", ticket_number)
    print("Wallet barcode token:", checkin_token)

    # 4. Build the Google Wallet pass.
    claims = {
        'iss': credentials.service_account_email,
        'aud': 'google',
        'typ': 'savetowallet',
        'iat': int(time.time()),
        'origins': ['http://127.0.0.1:8000', 'localhost'],
        'payload': {
            'eventTicketObjects': [
                {
                    'id': object_id,
                    'classId': class_id,
                    'state': 'ACTIVE',
                    'ticketHolderName': getattr(
                        attendee,
                        'full_name',
                        getattr(attendee, 'name', 'Guest'),
                    ),
                    'ticketNumber': ticket_number,

                    # This QR value matches the token used by
                    # the corresponding check-in URL.
                    'barcode': {
                        'type': 'QR_CODE',
                        'value': str(checkin_token),
                    },

                    'seatInfo': {
                        'seat': {
                            'defaultValue': {
                                'language': 'en-US',
                                'value': getattr(
                                    attendee,
                                    'seat',
                                    'General Admission',
                                ),
                            }
                        }
                    },
                }
            ]
        },
    }

    # 5. Sign the Save to Wallet JWT.
    google_wallet_jwt = jwt.encode(
        claims,
        private_key,
        algorithm='RS256',
        headers={'typ': 'JWT'},
    )

    return (
        "https://pay.google.com/gp/v/save/"
        f"{google_wallet_jwt}"
    )


def get_ticket_context(attendee, ticket=None):
    """
    Gather ticket information for an individual ticket or an
    existing RSVP-level pass.
    """
    event = attendee.event

    checkin_token = (
        ticket.checkin_token if ticket else attendee.checkin_token
    )

    qr_code_data_uri = (
        ticket.get_qr_code_data_uri()
        if ticket
        else attendee.get_qr_code_data_uri()
    )

    return {
        "event_title": event.title,
        "attendee_name": attendee.full_name,
        "tier_name": (
            attendee.ticket_tier.name
            if attendee.ticket_tier
            else "Standard RSVP"
        ),
        "serial_number": str(checkin_token),
        "qr_code_data_uri": qr_code_data_uri,
    }