"""Giriş kilidi — saf kural (docs/05 §8, docs/09 §4): 5 hatalı denemede 15 dk kilit.

Saat parametre olarak gelir (domain saati okumaz).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

MAX_FAILED_ATTEMPTS = 5
LOCK_DURATION = timedelta(minutes=15)
MIN_PASSWORD_LENGTH = 8


@dataclass(frozen=True, slots=True)
class LoginState:
    failed_count: int
    locked_until: datetime | None

    def is_locked(self, now: datetime) -> bool:
        return self.locked_until is not None and now < self.locked_until

    def after_failure(self, now: datetime) -> LoginState:
        """Hatalı deneme: sayaç artar; sınıra ulaşınca kilitlenir ve sayaç sıfırlanır."""
        count = self.failed_count + 1
        if count >= MAX_FAILED_ATTEMPTS:
            return LoginState(failed_count=0, locked_until=now + LOCK_DURATION)
        return LoginState(failed_count=count, locked_until=None)

    @staticmethod
    def after_success() -> LoginState:
        return LoginState(failed_count=0, locked_until=None)


def normalize_email(email: str) -> str:
    """E-posta küçük harfe çevrilir (docs/03 `users.email`). Kırpılır."""
    return email.strip().lower()


def password_problem(password: str) -> str | None:
    """Parola kuralı ihlali varsa Türkçe açıklama, yoksa None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Parola en az {MIN_PASSWORD_LENGTH} karakter olmalı."
    return None
