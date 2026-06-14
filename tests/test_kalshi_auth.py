"""RSA-PSS request signing — verified offline with a throwaway key."""

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.padding import PSS

from wc_kalshi.ingestion.kalshi.auth import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    KalshiSigner,
    generate_test_keypair,
    path_from_url,
)


def _verify(public_key, signature_b64, message):
    import base64

    public_key.verify(
        base64.b64decode(signature_b64),
        message.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_headers_present_and_signature_verifies():
    pem, public_key = generate_test_keypair()
    signer = KalshiSigner("key-123", pem)
    headers = signer.headers("GET", "/trade-api/v2/markets", timestamp_ms=1700000000000)

    assert headers["KALSHI-ACCESS-KEY"] == "key-123"
    assert headers[TIMESTAMP_HEADER] == "1700000000000"
    assert SIGNATURE_HEADER in headers

    message = "1700000000000" + "GET" + "/trade-api/v2/markets"
    _verify(public_key, headers[SIGNATURE_HEADER], message)  # raises if invalid


def test_signature_changes_with_method_path_and_time():
    pem, _ = generate_test_keypair()
    signer = KalshiSigner("k", pem)
    base = signer.headers("GET", "/trade-api/v2/markets", timestamp_ms=1)[SIGNATURE_HEADER]
    diff_method = signer.headers("POST", "/trade-api/v2/markets", timestamp_ms=1)[SIGNATURE_HEADER]
    diff_path = signer.headers("GET", "/trade-api/v2/events", timestamp_ms=1)[SIGNATURE_HEADER]
    diff_time = signer.headers("GET", "/trade-api/v2/markets", timestamp_ms=2)[SIGNATURE_HEADER]
    assert len({base, diff_method, diff_path, diff_time}) == 4


def test_path_from_url_strips_host_and_query():
    url = "https://external-api.demo.kalshi.co/trade-api/v2/markets?limit=2&cursor=x"
    assert path_from_url(url) == "/trade-api/v2/markets"


def test_headers_for_url_signs_path_only():
    pem, public_key = generate_test_keypair()
    signer = KalshiSigner("k", pem)
    url = "https://x/trade-api/v2/portfolio/balance?foo=bar"
    headers = signer.headers_for_url("GET", url)
    ts = headers[TIMESTAMP_HEADER]
    _verify(public_key, headers[SIGNATURE_HEADER], ts + "GET" + "/trade-api/v2/portfolio/balance")
