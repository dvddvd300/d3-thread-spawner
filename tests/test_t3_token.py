"""Tests for T3 session-token reconstruction from the auth_sessions store.

Recent T3 Code no longer writes a browser session cookie; the desktop
authenticates via a bootstrap-issued bearer session recorded in state.sqlite.
d3-thread-spawner rebuilds a valid signed token from that session's claims plus
the raw server signing key. These tests lock in that the reconstructed token
matches T3's own HMAC scheme and that session selection is correct.
"""

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from d3_thread_spawner import t3  # noqa: E402


SECRET = b"0123456789abcdef0123456789abcdef"  # 32 bytes, like server-signing-key
FULL_SCOPES = [
    "orchestration:read", "orchestration:operate", "terminal:operate",
    "review:write", "relay:read", "access:read", "access:write", "relay:write",
]


def _verify_like_t3(token: str, secret: bytes) -> dict:
    """Mirror SessionCredentialService.verify(): recompute the HMAC over the
    encoded payload, check it in constant analog, and return decoded claims.
    """
    encoded, signature = token.split(".")
    expected = base64.urlsafe_b64encode(
        hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest()
    ).rstrip(b"=").decode("ascii")
    assert hmac.compare_digest(signature, expected), "signature mismatch"
    pad = "=" * (-len(encoded) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded + pad).decode("utf-8"))


class IsoToEpochMsTest(unittest.TestCase):
    def test_millisecond_precision_roundtrips(self):
        # 2026-07-06T15:33:34.020Z was verified against the live server.
        self.assertEqual(t3._iso_to_epoch_ms("2026-07-06T15:33:34.020Z"), 1783352014020)

    def test_offset_form_matches_z_form(self):
        self.assertEqual(
            t3._iso_to_epoch_ms("2026-07-06T15:33:34.020Z"),
            t3._iso_to_epoch_ms("2026-07-06T15:33:34.020+00:00"),
        )


class ReconstructSessionTokenTest(unittest.TestCase):
    def test_scopes_claims_verify_and_match_key_order(self):
        row = {
            "session_id": "cc0c70a7-f352-4129-a175-23d8343b08d1",
            "subject": "desktop-bootstrap",
            "scopes": json.dumps(FULL_SCOPES),
            "method": "bearer-access-token",
            "issued_at": "2026-07-06T15:33:34.020Z",
            "expires_at": "2026-08-05T15:33:34.020Z",
        }
        token = t3._reconstruct_session_token(row, SECRET)
        claims = _verify_like_t3(token, SECRET)
        self.assertEqual(list(claims.keys()),
                         ["v", "kind", "sid", "sub", "scopes", "method", "iat", "exp"])
        self.assertEqual(claims["scopes"], FULL_SCOPES)
        self.assertEqual(claims["method"], "bearer-access-token")
        self.assertEqual(claims["iat"], 1783352014020)

    def test_role_schema_fallback(self):
        row = {
            "session_id": "abc", "subject": "browser", "role": "owner",
            "method": "browser-session-cookie",
            "issued_at": "2026-07-06T15:33:34.020Z",
            "expires_at": "2026-08-05T15:33:34.020Z",
        }
        claims = _verify_like_t3(t3._reconstruct_session_token(row, SECRET), SECRET)
        self.assertEqual(list(claims.keys()),
                         ["v", "kind", "sid", "sub", "role", "method", "iat", "exp"])
        self.assertEqual(claims["role"], "owner")


class TokenFromStateDbTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.secrets = os.path.join(self.dir, "secrets")
        os.makedirs(self.secrets)
        with open(os.path.join(self.secrets, t3.SIGNING_KEY_FILENAME), "wb") as f:
            f.write(SECRET)
        self.db = os.path.join(self.dir, "state.sqlite")
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE auth_sessions ("
            "session_id TEXT PRIMARY KEY, subject TEXT, scopes TEXT, method TEXT, "
            "issued_at TEXT, expires_at TEXT, revoked_at TEXT)"
        )
        conn.commit()
        conn.close()

    def _insert(self, sid, scopes, method, issued, expires, revoked=None):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO auth_sessions VALUES (?,?,?,?,?,?,?)",
            (sid, "desktop-bootstrap", json.dumps(scopes), method, issued, expires, revoked),
        )
        conn.commit()
        conn.close()

    def test_prefers_newest_operate_session(self):
        # An older operate-capable session and a newer read-only one.
        self._insert("old-op", FULL_SCOPES, "bearer-access-token",
                     "2026-07-01T00:00:00.000Z", "2099-01-01T00:00:00.000Z")
        self._insert("new-op", FULL_SCOPES, "bearer-access-token",
                     "2026-07-05T00:00:00.000Z", "2099-01-01T00:00:00.000Z")
        self._insert("newest-readonly", ["orchestration:read"], "bearer-access-token",
                     "2026-07-06T00:00:00.000Z", "2099-01-01T00:00:00.000Z")
        token = t3._token_from_state_db(self.db, self.secrets)
        claims = _verify_like_t3(token, SECRET)
        self.assertEqual(claims["sid"], "new-op")

    def test_skips_expired_revoked_and_dpop(self):
        self._insert("expired", FULL_SCOPES, "bearer-access-token",
                     "2020-01-01T00:00:00.000Z", "2020-02-01T00:00:00.000Z")
        self._insert("revoked", FULL_SCOPES, "bearer-access-token",
                     "2026-07-06T00:00:00.000Z", "2099-01-01T00:00:00.000Z",
                     revoked="2026-07-06T01:00:00.000Z")
        self._insert("dpop", FULL_SCOPES, "dpop-access-token",
                     "2026-07-06T00:00:00.000Z", "2099-01-01T00:00:00.000Z")
        self._insert("good", FULL_SCOPES, "bearer-access-token",
                     "2026-07-04T00:00:00.000Z", "2099-01-01T00:00:00.000Z")
        claims = _verify_like_t3(t3._token_from_state_db(self.db, self.secrets), SECRET)
        self.assertEqual(claims["sid"], "good")

    def test_missing_files_return_none(self):
        self.assertIsNone(t3._token_from_state_db("/nope/state.sqlite", self.secrets))
        self.assertIsNone(t3._token_from_state_db(self.db, "/nope/secrets"))

    def test_no_usable_session_returns_none(self):
        self._insert("expired", FULL_SCOPES, "bearer-access-token",
                     "2020-01-01T00:00:00.000Z", "2020-02-01T00:00:00.000Z")
        self.assertIsNone(t3._token_from_state_db(self.db, self.secrets))


if __name__ == "__main__":
    unittest.main()
