from __future__ import annotations

from django.conf import settings


def analytics(request):
    """
    Inject analytics configuration for templates like `product_detail.html`.

    All values can be overridden via Django settings / environment so that
    1matrix.io can control the ingest endpoint and public key without any
    template changes.
    """

    return {
        "analytics": {
            "ingest_url": getattr(
                settings,
                "ANALYTICS_INGEST_URL",
                "https://1matrix.io/api/gs-analytics/ingest/",
            ),
            "sample_rate": getattr(settings, "ANALYTICS_SAMPLE_RATE", 1.0),
            "require_consent": getattr(settings, "ANALYTICS_REQUIRE_CONSENT", False),
            # Public RSA key (PEM) used by the frontend Web Crypto layer to
            # encrypt analytics payloads before sending them to 1matrix.io.
            "remote_public_key_pem": getattr(
                settings,
                "ANALYTICS_REMOTE_PUBLIC_KEY_PEM",
                "",
            ),
            # Public RSA key used for encrypting payloads to the local
            # `/api/analytics/ingest/` endpoint. The matching private key
            # stays only on the server inside Django settings / environment.
            "local_public_key_pem": getattr(
                settings,
                "ANALYTICS_LOCAL_PUBLIC_KEY_PEM",
                "",
            ),
        }
    }


