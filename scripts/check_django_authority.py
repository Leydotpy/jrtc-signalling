"""Run against a Synq checkout/environment with this gateway package installed.

Example (from Synq, using its Python 3.14 environment):
uv run --frozen --with /path/to/jrtc-signalling python \
  /path/to/jrtc-signalling/scripts/check_django_authority.py --synq .
No Janus, broker, IdP or network calls. Keys and users are temporary test data.
"""

import argparse
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--synq", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.synq.resolve() / "src"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "conf.test_settings")
    import django

    django.setup()
    from apps.meetings.services.gateway_tickets import check_ticket
    from apps.meetings.services.lifecycle import MeetingLifecycleService
    from apps.meetings.tests.test_gateway_tickets import GatewayTicketFixture
    from asgiref.sync import async_to_sync, sync_to_async
    from django.test import TestCase
    from django.test.runner import DiscoverRunner

    from jrtc_signalling.auth import (
        AuthConfig,
        AuthError,
        AuthorizationSession,
        Grant,
        TicketVerifier,
    )

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

    # Register for Django's normal runner, including creation/destruction of test DB.
    HandoffTests.__module__ = __name__
    globals()["HandoffTests"] = HandoffTests
    raise SystemExit(bool(DiscoverRunner(verbosity=1).run_tests([__name__ + ".HandoffTests"])))


if __name__ == "__main__":
    main()
