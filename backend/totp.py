"""Time-based one-time passwords (RFC 6238, HMAC-SHA1, 30 s, 6 digits) with the standard library only.

Works with Google Authenticator, Aegis, Microsoft Authenticator, 1Password...: the user types the secret (or the
otpauth:// URI) into the app. Backup codes are single-use, stored as SHA-256 hashes.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP, DIGITS = 30, 6
BACKUP_ALPHABET = 'abcdefghjkmnpqrstuvwxyz23456789'  # no 0/o, 1/l/i
ISSUER = 'Fe2Gen'


def generate_secret():
    """20 random bytes (160 bits, the RFC 4226 recommendation) as 32 base32 characters."""
    return base64.b32encode(secrets.token_bytes(20)).decode()


def _key(secret):
    clean = ''.join(str(secret).split()).upper()
    return base64.b32decode(clean + '=' * (-len(clean) % 8))


def counter(t=None, step=STEP):
    return int((time.time() if t is None else t) // step)


def code_at(secret, count, digits=DIGITS):
    mac = hmac.new(_key(secret), struct.pack('>Q', count), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack('>I', mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** digits).zfill(digits)


def code(secret, t=None, step=STEP, digits=DIGITS):
    return code_at(secret, counter(t, step), digits)


def verify(secret, given, t=None, window=1, after=None):
    """The time step that given matches within +/- window steps, or None. Every candidate is compared in
    constant time and none is skipped early. after: the last step already used, so a code cannot be replayed."""
    given = ''.join(str(given or '').split())
    if len(given) != DIGITS or not given.isdigit():
        return None
    now = counter(t)
    match = None
    for count in range(now - window, now + window + 1):
        if hmac.compare_digest(code_at(secret, count), given) and (after is None or count > after) and match is None:
            match = count
    return match


def uri(username, secret):
    label = quote(f'{ISSUER}:{username}', safe=':@')  # the usual form: otpauth://totp/Fe2Gen:user?...
    return f'otpauth://totp/{label}?secret={secret}&issuer={ISSUER}&algorithm=SHA1&digits={DIGITS}&period={STEP}'


def backup_codes(n=8):
    """n single-use codes such as "k7mq-x2rf" (about 49 bits each)."""
    raw = [''.join(secrets.choice(BACKUP_ALPHABET) for _ in range(8)) for _ in range(n)]
    return [c[:4] + '-' + c[4:] for c in raw]


def hash_backup(given):
    return hashlib.sha256(''.join(str(given or '').lower().split()).replace('-', '').encode()).hexdigest()
