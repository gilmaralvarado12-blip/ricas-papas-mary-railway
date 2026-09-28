from datetime import timedelta
import math

from django.conf import settings
from django.utils import timezone


CLIENT_SESSION_TIMEOUT_SECONDS = 60 * 60 * 2
CLIENT_ACTIVITY_SYNC_INTERVAL_SECONDS = 60 * 5
CLIENT_SESSION_EXPIRY_KEY = 'client_session_expires_at'
CLIENT_ACTIVITY_SYNC_KEY = 'client_session_last_activity_sync'


def is_public_client(user):
    return (
        getattr(user, 'is_authenticated', False)
        and getattr(user, 'rol', None) == 'CLIENTE'
        and not getattr(user, 'is_staff', False)
        and not getattr(user, 'is_superuser', False)
    )


def get_client_session_expiry(session):
    expires_at = session.get(CLIENT_SESSION_EXPIRY_KEY)
    if (
        isinstance(expires_at, (int, float))
        and not isinstance(expires_at, bool)
        and math.isfinite(expires_at)
        and expires_at > 0
    ):
        return float(expires_at)
    return None


def renew_client_session(session, now=None):
    now = now or timezone.now()
    expires_at = now + timedelta(seconds=CLIENT_SESSION_TIMEOUT_SECONDS)
    session[CLIENT_SESSION_EXPIRY_KEY] = expires_at.timestamp()
    session.set_expiry(expires_at)
    return expires_at


class SessionExpiryByRoleMiddleware:
    ADMIN_SESSION_TIMEOUT_SECONDS = 60 * 60 * 24 * 7

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if getattr(request, 'user', None) and request.user.is_authenticated:
            if getattr(request.user, 'rol', None) == 'CLIENTE':
                if is_public_client(request.user):
                    if get_client_session_expiry(request.session) is None:
                        renew_client_session(request.session)
                else:
                    request.session.pop(CLIENT_SESSION_EXPIRY_KEY, None)
                    request.session.pop(CLIENT_ACTIVITY_SYNC_KEY, None)
                    request.session.set_expiry(settings.SESSION_COOKIE_AGE)
            else:
                request.session.pop(CLIENT_SESSION_EXPIRY_KEY, None)
                request.session.pop(CLIENT_ACTIVITY_SYNC_KEY, None)
                request.session.set_expiry(self.ADMIN_SESSION_TIMEOUT_SECONDS)

        return self.get_response(request)
