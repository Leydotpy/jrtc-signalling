"""Run against a Synq checkout/environment with this gateway package installed.

Example (from Synq, using its Python 3.14 environment):
uv run --frozen --with /path/to/jrtc-signalling python \
  /path/to/jrtc-signalling/scripts/check_django_authority.py --synq .
No Janus, broker, IdP or network calls. Keys and users are temporary test data.
"""

import argparse
import os
import sys
import uuid
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--synq", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.synq.resolve() / "src"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "conf.test_settings")
    import django

    django.setup()
    from apps.meetings.models import GatewayAuthorizationOutbox, Participant
    from apps.meetings.services.gateway_admission import request_intent, review_intent
    from apps.meetings.services.gateway_tickets import check_ticket
    from apps.meetings.services.lifecycle import MeetingLifecycleService
    from apps.meetings.tests.test_gateway_tickets import GatewayTicketFixture
    from asgiref.sync import async_to_sync, sync_to_async
    from django.test import TestCase, override_settings
    from django.test.runner import DiscoverRunner
    from jsonschema import Draft202012Validator

    from jrtc_signalling.auth import (
        AuthConfig,
        AuthError,
        AuthorizationSession,
        Grant,
        TicketVerifier,
    )
    from jrtc_signalling.schema_definitions import COMMAND_DATA

    class DjangoAuthority:
        async def check(self, claims, *, operation, consume):
            result = await sync_to_async(check_ticket)(
                claims=claims, operation=operation, consume=consume
            )
            return Grant(**result)

        async def aclose(self):
            pass

    class HandoffTests(GatewayTicketFixture, TestCase):
        def verifier(self):
            return TicketVerifier(
                AuthConfig(
                    "https://synq.example",
                    {"test-key": self.key.public_key()},
                    frozenset({"https://meet.example"}),
                    "https://synq.example/authority/",
                    "x" * 32,
                )
            )

        def test_signed_ticket_shared_replay_and_revocation(self):
            result, _ = self.issue()
            claims = self.verifier().verify(result["ticket"])
            binding = AuthorizationSession(DjangoAuthority(), claims)
            async_to_sync(binding.check)("session.hello", consume=True)
            duplicate = AuthorizationSession(DjangoAuthority(), claims)
            with self.assertRaises(AuthError):
                async_to_sync(duplicate.check)("session.hello", consume=True)
            MeetingLifecycleService.update_participant_permissions(
                session=self.session,
                actor_profile=self.host,
                participant=self.participant,
                updates={"is_muted": True},
            )
            with self.assertRaises(AuthError):
                async_to_sync(binding.check)("media.publisher.join")

        def test_lobby_admission_and_bound_refresh(self):
            intent = self.waiting()
            result, _ = self.issue(self.guest)
            lobby = AuthorizationSession(
                DjangoAuthority(), self.verifier().verify(result["ticket"])
            )
            async_to_sync(lobby.check)("session.hello", consume=True)
            with self.assertRaises(AuthError):
                async_to_sync(lobby.authorize)("media.publisher.join", {})
            MeetingLifecycleService.review_join_request(
                join_request=intent, reviewer_profile=self.host, approve=True
            )
            fresh, _ = self.issue(
                self.guest,
                purpose="refresh",
                participant_session_id=result["participant_session_id"],
                generation=result["generation"],
            )
            admitted = AuthorizationSession(
                DjangoAuthority(), self.verifier().verify(fresh["ticket"])
            )
            async_to_sync(admitted.check)("session.refresh", consume=True)
            async_to_sync(admitted.authorize)("media.publisher.join", {})

        @override_settings(GATEWAY_ADMISSION_ENABLED=True)
        def test_gateway_admission_outbox_refresh_and_removal(self):
            self.room.feature_flags = {"gateway_control_plane": True}
            self.room.save(update_fields=["feature_flags"])
            intent = request_intent(session_id=self.session.pk, profile_id=self.guest.pk)
            result, _ = self.issue(self.guest)
            lobby = AuthorizationSession(
                DjangoAuthority(), self.verifier().verify(result["ticket"])
            )
            async_to_sync(lobby.check)("session.hello", consume=True)
            with self.assertRaises(AuthError):
                async_to_sync(lobby.authorize)("media.publisher.join", {})
            review_intent(
                session_id=self.session.pk,
                join_request_id=intent["join_request_id"],
                actor_profile_id=self.host.pk,
                decision_id=uuid.uuid4(),
                approve=True,
                expected_epoch=0,
            )
            fresh, _ = self.issue(
                self.guest,
                purpose="refresh",
                participant_session_id=result["participant_session_id"],
                generation=result["generation"],
            )
            admitted = AuthorizationSession(
                DjangoAuthority(), self.verifier().verify(fresh["ticket"])
            )
            async_to_sync(admitted.check)("session.refresh", consume=True)
            async_to_sync(admitted.authorize)("media.publisher.join", {})
            participant = Participant.objects.get(pk=intent["participant_id"])
            self.assertIsNone(participant.joined_at)
            MeetingLifecycleService.remove_participant(
                session=self.session, actor_profile=self.host, participant=participant
            )
            with self.assertRaises(AuthError):
                async_to_sync(admitted.check)("media.publisher.join")
            records = list(
                GatewayAuthorizationOutbox.objects.filter(participant=participant).order_by(
                    "authorization_epoch"
                )
            )
            self.assertEqual(
                [record.action for record in records],
                ["participant.admit", "participant.disconnect"],
            )
            self.assertEqual([record.authorization_epoch for record in records], [1, 2])
            for record in records:
                Draft202012Validator(COMMAND_DATA[record.action]).validate(record.data)

        @override_settings(GATEWAY_ADMISSION_ENABLED=True)
        def test_other_authority_outbox_payloads_match_gateway_contract(self):
            self.room.feature_flags = {"gateway_control_plane": True}
            self.room.save(update_fields=["feature_flags"])
            intent = request_intent(session_id=self.session.pk, profile_id=self.guest.pk)
            review_intent(
                session_id=self.session.pk,
                join_request_id=intent["join_request_id"],
                actor_profile_id=self.host.pk,
                decision_id=uuid.uuid4(),
                approve=False,
                expected_epoch=0,
            )
            MeetingLifecycleService.update_participant_permissions(
                session=self.session,
                actor_profile=self.host,
                participant=self.participant,
                updates={"is_muted": True},
            )
            second_guest = self.profile("second_guest")
            second_intent = request_intent(session_id=self.session.pk, profile_id=second_guest.pk)
            review_intent(
                session_id=self.session.pk,
                join_request_id=second_intent["join_request_id"],
                actor_profile_id=self.host.pk,
                decision_id=uuid.uuid4(),
                approve=True,
                expected_epoch=0,
            )
            MeetingLifecycleService.leave_participant(session=self.session, profile=second_guest)
            MeetingLifecycleService.end_session(session=self.session, actor_profile=self.host)
            records = list(GatewayAuthorizationOutbox.objects.all())
            self.assertEqual(len(records), 6)
            self.assertEqual(
                {record.action for record in records},
                set(COMMAND_DATA) - {"meeting.reconcile"},
            )
            for record in records:
                Draft202012Validator(COMMAND_DATA[record.action]).validate(record.data)

    # Register for Django's normal runner, including creation/destruction of test DB.
    HandoffTests.__module__ = __name__
    globals()["HandoffTests"] = HandoffTests
    raise SystemExit(bool(DiscoverRunner(verbosity=1).run_tests([__name__ + ".HandoffTests"])))


if __name__ == "__main__":
    main()
