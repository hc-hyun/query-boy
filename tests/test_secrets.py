from __future__ import annotations

import base64

import pytest

from query_man.managed.secrets import (
    SecretDecryptionError,
    SecretKeyConfigurationError,
    SourceSecretCipher,
)


def test_source_secret_round_trip_is_bound_to_source_and_generation() -> None:
    cipher = SourceSecretCipher(b"k" * 32)
    encrypted = cipher.encrypt("third-source", 3, "reader-password")

    assert encrypted.ciphertext != b"reader-password"
    assert cipher.decrypt("third-source", 3, encrypted) == "reader-password"
    with pytest.raises(SecretDecryptionError):
        cipher.decrypt("other-source", 3, encrypted)
    with pytest.raises(SecretDecryptionError):
        cipher.decrypt("third-source", 4, encrypted)


def test_source_secret_key_requires_urlsafe_base64_32_bytes() -> None:
    encoded = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
    SourceSecretCipher.from_base64(encoded)

    with pytest.raises(SecretKeyConfigurationError):
        SourceSecretCipher.from_base64(base64.urlsafe_b64encode(b"short").decode("ascii"))


def test_mutation_request_hash_is_keyed_deterministic_and_domain_separated() -> None:
    request = b'{"credential":"low-entropy-secret"}'
    first = SourceSecretCipher(b"a" * 32)
    second = SourceSecretCipher(b"b" * 32)

    digest = first.mutation_request_hash(request)

    assert digest == first.mutation_request_hash(request)
    assert digest != first.mutation_request_hash(request + b" ")
    assert digest != second.mutation_request_hash(request)
    assert digest.startswith("hmac-sha256:")
    assert len(digest) == len("hmac-sha256:") + 64
    assert "low-entropy-secret" not in digest
    with pytest.raises(ValueError):
        first.mutation_request_hash(b"")
