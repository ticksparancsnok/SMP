# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Password policy for passwords people choose (not for generated one-time passwords).

Deliberately modest: these are school accounts, many used by children. The rules
stop the passwords that get guessed first, without composition rules nobody can
remember. Each problem has a stable code; the web UI shows it in the user's
language, other clients get the English message.
"""

from smp.core.errors import Problem

MIN_LENGTH = 8
MIN_DISTINCT = 5

# Very common passwords (international lists) plus local favourites, compared
# case-insensitively. Together with the letter+digit and distinct-character rules
# this is a reasonable floor; a zxcvbn-style estimator can replace it later.
COMMON = {
    "password", "password1", "password12", "password123", "password1234", "passw0rd", "p@ssw0rd", "p@ssword1",
    "12345678", "123456789", "1234567890", "12345678a", "a12345678", "1q2w3e4r", "1q2w3e4r5t", "1qaz2wsx",
    "qwerty12", "qwerty123", "qwertz12", "qwertz123", "qwertzui", "qwertyui", "asdfghjk", "asdf1234",
    "abc12345", "abcd1234", "abc123456", "aa123456", "a1b2c3d4", "zxcvbnm1", "iloveyou1", "iloveyou",
    "admin123", "admin1234", "administrator1", "letmein1", "welcome1", "welcome123", "monkey123", "dragon12",
    "football1", "baseball1", "sunshine1", "princess1", "superman1", "batman123", "starwars1", "master123",
    "trustno1", "changeme1", "test1234", "teszt123", "teszt1234", "user1234", "login123", "secret123",
    "jelszo12", "jelszo123", "jelszo1234", "jelszó123", "jelszó1234", "iskola123", "iskola1234", "iskola2025",
    "iskola2026", "iskola2027", "diak1234", "diák1234", "tanulo123", "tanuló123", "tanar123", "tanár123",
    "osztaly123", "magyar123", "magyarorszag1", "budapest1", "budapest123", "szeretlek1", "szeretlek123",
    "minecraft1", "minecraft123", "fortnite1", "fortnite123", "roblox123", "pokemon123", "naruto123",
    "barcelona1", "realmadrid1", "ronaldo7", "messi1010", "liverpool1", "chelsea123", "arsenal123", "logiscool",
    "scoolcode", "kaki", "valami", "valami123",
}


def password_problem(password: str, username: str = "") -> Problem | None:
    """None if acceptable, otherwise what's wrong (code + English message)."""
    if password != password.strip():
        # Not stripped silently (that would change someone's secret behind their
        # back); usually a copy-paste accident, so say so.
        return Problem("password.surrounding_space", "The password must not start or end with a space.")
    if len(password) < MIN_LENGTH:
        return Problem(
            "password.too_short", f"The password must be at least {MIN_LENGTH} characters long.", {"min": MIN_LENGTH}
        )
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        return Problem("password.letters_and_digits", "The password must contain both letters and digits.")
    if len(set(password.casefold())) < MIN_DISTINCT:
        return Problem(
            "password.too_few_distinct",
            f"The password must contain at least {MIN_DISTINCT} different characters.",
            {"min": MIN_DISTINCT},
        )
    if password.casefold() in COMMON:
        return Problem("password.too_common", "This password is too common and easy to guess. Choose another one.")
    if len(username) >= 3 and username.casefold() in password.casefold():
        return Problem("password.contains_username", "The password must not contain the username.")
    return None
