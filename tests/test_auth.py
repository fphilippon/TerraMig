import unittest

from terramig.persistence import MemoryPersistence
from terramig.services.auth import AuthenticationError, AuthService


class AuthServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.persistence = MemoryPersistence()
        self.auth = AuthService(self.persistence)

    def test_bootstrap_admin_requires_password_change(self) -> None:
        issued = self.auth.login("admin", "admin")
        self.assertEqual(issued.user.username, "admin")
        self.assertTrue(issued.user.must_change_password)
        self.assertNotEqual(issued.token, issued.csrf_token)
        stored = self.persistence.get_user_by_username("admin")
        self.assertNotEqual(stored.password_hash, "admin")
        self.assertNotEqual(stored.password_salt, "")

    def test_password_change_invalidates_old_session(self) -> None:
        initial = self.auth.login("admin", "admin")
        authenticated = self.auth.authenticate(initial.token)
        replacement = self.auth.change_password(
            authenticated, "admin", "New-Local-Password-2026!"
        )
        with self.assertRaisesRegex(AuthenticationError, "Session expired"):
            self.auth.authenticate(initial.token)
        current = self.auth.authenticate(replacement.token)
        self.assertFalse(current.user.must_change_password)
        self.auth.login("admin", "New-Local-Password-2026!")

    def test_csrf_requires_matching_cookie_header_and_session_hash(self) -> None:
        issued = self.auth.login("admin", "admin")
        authenticated = self.auth.authenticate(issued.token)
        self.auth.verify_csrf(
            authenticated, issued.csrf_token, issued.csrf_token
        )
        with self.assertRaisesRegex(AuthenticationError, "CSRF"):
            self.auth.verify_csrf(authenticated, issued.csrf_token, "wrong")

    def test_weak_replacement_password_is_rejected(self) -> None:
        issued = self.auth.login("admin", "admin")
        authenticated = self.auth.authenticate(issued.token)
        with self.assertRaisesRegex(AuthenticationError, "12 characters"):
            self.auth.change_password(authenticated, "admin", "short")


if __name__ == "__main__":
    unittest.main()
