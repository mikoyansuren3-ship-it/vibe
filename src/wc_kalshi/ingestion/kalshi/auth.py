"""Kalshi API-key + RSA request signing.

Per the Kalshi docs (research.md §1.2) each request carries three headers:

    KALSHI-ACCESS-KEY        the Key ID
    KALSHI-ACCESS-TIMESTAMP  current time in milliseconds
    KALSHI-ACCESS-SIGNATURE  base64( RSA-PSS-SHA256( timestamp + METHOD + path ) )

The signed message concatenates the millisecond timestamp, the upper-case HTTP
method, and the request path *including* the ``/trade-api/v2`` prefix but
*excluding* the query string. PSS uses MGF1/SHA-256 with salt length = digest
length.

This module has no network dependencies and is fully unit-testable with a
locally generated throwaway key.
"""

from __future__ import annotations

import base64
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.padding import PSS

ACCESS_KEY_HEADER = "KALSHI-ACCESS-KEY"
TIMESTAMP_HEADER = "KALSHI-ACCESS-TIMESTAMP"
SIGNATURE_HEADER = "KALSHI-ACCESS-SIGNATURE"


def path_from_url(url: str) -> str:
    """Extract the signable path (no scheme/host, no query) from a full URL.

    ``https://external-api.kalshi.com/trade-api/v2/markets?limit=2`` -> ``/trade-api/v2/markets``
    """
    return urlsplit(url).path


class KalshiSigner:
    """Signs requests with an RSA private key using Kalshi's RSA-PSS scheme."""

    def __init__(self, key_id: str, private_key_pem: str) -> None:
        if not key_id:
            raise ValueError("Kalshi key_id is required")
        self.key_id = key_id
        loaded = serialization.load_pem_private_key(
            private_key_pem.encode() if isinstance(private_key_pem, str) else private_key_pem,
            password=None,
        )
        if not isinstance(loaded, rsa.RSAPrivateKey):
            raise TypeError("Kalshi private key must be an RSA private key")
        self._key: rsa.RSAPrivateKey = loaded

    def sign_text(self, message: str) -> str:
        """Return the base64 RSA-PSS-SHA256 signature of ``message``."""
        signature = self._key.sign(
            message.encode("utf-8"),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")

    def headers(
        self, method: str, path: str, *, timestamp_ms: int | None = None
    ) -> dict[str, str]:
        """Build the three signed auth headers for a request.

        ``path`` must be the path that will appear in the URL, including the
        ``/trade-api/v2`` prefix and excluding the query string.
        """
        ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
        message = f"{ts}{method.upper()}{path}"
        return {
            ACCESS_KEY_HEADER: self.key_id,
            TIMESTAMP_HEADER: ts,
            SIGNATURE_HEADER: self.sign_text(message),
        }

    def headers_for_url(self, method: str, url: str) -> dict[str, str]:
        """Convenience: sign using the path extracted from a full URL."""
        return self.headers(method, path_from_url(url))


def generate_test_keypair() -> tuple[str, rsa.RSAPublicKey]:
    """Generate a throwaway RSA key. Returns (private_pem, public_key).

    Used by the test-suite to verify signatures without any real credentials.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()
