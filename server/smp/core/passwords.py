"""Password policy for passwords people choose (not for generated one-time passwords).

Deliberately modest: these are school accounts, many used by children. The rules
stop the passwords that get guessed first, without composition rules nobody can
remember. Messages are Hungarian because they're shown to end users verbatim.
"""

MIN_LENGTH = 8

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
    "barcelona1", "realmadrid1", "ronaldo7", "messi1010", "liverpool1", "chelsea123", "arsenal123",
}


def password_problem(password: str, username: str = "") -> str | None:
    """None if acceptable, otherwise a message for the user."""
    if password != password.strip():
        # Not stripped silently (that would change someone's secret behind their
        # back); usually a copy-paste accident, so say so.
        return "A jelszó nem kezdődhet és nem végződhet szóközzel."
    if len(password) < MIN_LENGTH:
        return f"A jelszó legalább {MIN_LENGTH} karakter legyen."
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        return "A jelszó tartalmazzon betűt és számot is."
    if len(set(password.casefold())) < 5:
        return "A jelszó legalább 5 különböző karakterből álljon."
    if password.casefold() in COMMON:
        return "Ez a jelszó túl gyakori, könnyen kitalálható. Válassz másikat."
    if len(username) >= 3 and username.casefold() in password.casefold():
        return "A jelszó nem tartalmazhatja a felhasználónevet."
    return None
