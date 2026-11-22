# Security notes (v1.0.1)

How SMP's core protects accounts, what operators must configure, and the decisions taken on the two security reviews. Read with [architecture.md](architecture.md).

## Operator checklist

1. **`SMP_SECRET_KEY`**: required, at least 32 random characters. The server refuses to start without it.
   - **Generate:** `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
   - **Where to keep it:** in a file passed as `SMP_SECRET_KEY_FILE` (e.g. a Docker secret, `chmod 600`) rather than in the environment. Never store it next to database backups: key + DB dump = readable unused one-time passwords.
2. **TLS reverse proxy** in front of the server (nginx/Caddy). Then:
   - keep `SMP_COOKIE_SECURE=true` (the default);
   - set `SMP_FORWARDED_ALLOW_IPS` to the proxy's address. Otherwise every client appears to come from the proxy and shares one login-throttle budget.
3. **Request rate limiting at the proxy.** The app throttles logins itself (below), but general request floods belong at the proxy:

   ```nginx
   limit_req_zone $binary_remote_addr zone=smp_login:10m rate=10r/m;
   limit_req_zone $binary_remote_addr zone=smp_all:10m   rate=20r/s;

   server {
       listen 443 ssl;
       # ssl_certificate ...;
       location = /api/v1/auth/login {
           limit_req zone=smp_login burst=5 nodelay;
           proxy_pass http://127.0.0.1:8000;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
           proxy_set_header Host $host;
       }
       location / {
           limit_req zone=smp_all burst=40 nodelay;
           proxy_pass http://127.0.0.1:8000;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
           proxy_set_header Host $host;
       }
   }
   ```
4. **`SMP_DNS_SYNC_TOKEN`**: required by Compose, ≥32 random characters, same value on the server and the DNS sinkhole (`DNS_SYNC_TOKEN`). The sinkhole's port 8080 serves the block page to every client, so its `/api/blocklist/*` endpoints only accept this Bearer token; with no token set they are disabled.
5. **Local development over plain http:** set `SMP_COOKIE_SECURE=false`, otherwise the browser drops the session cookie and you can't stay logged in. Never use it in production.

### Rotating `SMP_SECRET_KEY`
1. Set the new key as `SMP_SECRET_KEY` and the old one as `SMP_SECRET_KEY_PREVIOUS`, then restart. Both keys decrypt, and new data uses the new key.
2. Run `smp reencrypt-otps`. It re-encrypts every stored one-time password with the new key.
3. Remove `SMP_SECRET_KEY_PREVIOUS` and restart.

If a key is lost or leaked, rotate it and issue new one-time passwords to accounts that aren't activated yet. Logins are unaffected either way: they check the argon2 hash, not the encrypted copy.

## How accounts are protected
- **Passwords:**
  - **Hashing:** argon2id (64 MiB, t=3) in worker threads, at most `SMP_HASH_CONCURRENCY` at once (default: half the CPUs), so a login flood can't stall the server or exhaust RAM.
  - **Policy (`smp/core/passwords.py`):** at least 8 characters, letters and digits, at least 5 distinct characters, not a common password, not containing the username, no leading or trailing spaces.
- **One-time passwords:**
  - **Generation:** individual (never shared), random ~59 bits, valid for `core.otp_valid_days`.
  - **Storage:** argon2 hash (16 MiB) plus a Fernet-encrypted copy for login slips.
  - **Use:** the first login forces a password change, which clears the OTP and rotates the session.
- **Login throttling (`smp/core/throttle.py`):**
  - **Limits:** 5 failed logins for one account (from any IP, within 15 min) lock it for 60 s; plus all attempts per IP per minute.
  - **No enumeration:** unknown usernames are locked exactly like real ones.
  - **Known trade-off:** anyone can keep an account (e.g. the admin's) locked by failing 5 times a minute. The short lockout keeps that a nuisance; longer lockouts are marked `TODO(evaluate)` in `throttle.py`.
  - **Restarts:** the state is rebuilt from the audit log at startup.
- **Sessions:** random 256-bit tokens; only their SHA-256 is stored. Cookies are HttpOnly and `SameSite=Strict`, with an Origin check on state-changing requests. A password change ends all sessions.
- **Audit:** append-only (enforced by a database trigger). Logins, failures, resets and exports are all logged. Secrets (passwords, OTPs) never appear in it. If a failed-login audit row can't be written, it goes to the server log instead.

## Review decisions

### Third review: attack run (v1.0.1)
Each finding was first reproduced with a working exploit against a test database.

| Finding | Decision |
|---|---|
| API tokens outlived the person who created them (a deleted admin's token kept working), and a token could mint child tokens that survived revoking the parent | **Fixed:** deleting a user or removing their admin role deletes their tokens and, transitively, tokens created by those tokens; audited as `core.token.delete_with_user` / `core.token.delete_with_admin_role` (id, name, permissions only). Disabling an admin without demoting them leaves their tokens working (a disabled admin can be re-enabled; demote to cut off their tokens). |
| No per-account limit on password guessing: rotating source IPs (an IPv6 /64) reset the per-IP+username counter | **Fixed:** per-account lockout, 5 failures → 60 s, from any IP (see above). |
| Students could create short links with any code on the school's own domain (phishing) | **Fixed:** students no longer have `shortlinks.links.create`; migration 0007 disables links students already made (disabled, not deleted). |
| Any teacher could block a whole TLD (`com` with subdomains) and cut the school off; domain validation was minimal | **Fixed:** allowlist checked before the blocklist; public suffixes can be neither blocked nor allowed; strict domain validation. |
| DNS hosts import has no size limit | **Accepted:** authenticated (teachers); parsing is now strict and the import does one query instead of one per domain. |
| Login time reveals accounts that still have their one-time password (cheap OTP hash vs. expensive dummy hash) | **Deferred:** `TODO(evaluate)` in `security.py`; schools run SMP behind NAT. |
| The DNS resolver answers anyone who can reach port 1053 (open resolver) | **Deferred:** `TODO(evaluate)` in the Corefile and `docker-compose.yml`; behind NAT it isn't reachable from outside. |
| `/api/docs` and `/api/openapi.json` were public | **Fixed:** admins (signed-in users with the admin role) only; API tokens are refused. |

### v1.0.1
| Finding | Decision |
|---|---|
| The DNS sinkhole's `/api/blocklist/import` and `/entries` accepted writes from anyone who could reach port 8080 (every client, since it also serves the block page), trusting a client-sent `X-Username`; `/sync` was open when no token was set | **Fixed:** every `/api/blocklist/*` endpoint requires `Authorization: Bearer <DNS_SYNC_TOKEN>` (constant-time compare); with no token configured the API answers 503 instead of being open; Compose requires the token. The block page stays public. Covered by `schoolfilter_test.go`. |
| Slow clients could hold block-page connections open | **Fixed:** `ReadHeaderTimeout` of 10 s. |

### Second review (v1.0.0)
| Finding | Decision |
|---|---|
| Leaked `SMP_SECRET_KEY` lets an attacker "forge OTPs" | Partly. Login verifies the argon2 hash, so a key alone forges nothing. Key + DB dump does expose *unused* OTPs, so: `SMP_SECRET_KEY_FILE`, key rotation (`SMP_SECRET_KEY_PREVIOUS`, `smp reencrypt-otps`) and the guidance above. |
| Restart clears login throttling | **Fixed:** re-seeded from the audit log on startup (no Redis needed for a single process). |
| 429 vs 401 enables username enumeration | **Not an issue:** the throttle applies identically to existing and unknown usernames (`test_throttle_is_per_ip_and_username_and_reveals_nothing`). 429 stays so users know to wait. |
| OTP hash memory too low (DoS) | Raised 8 → 16 MiB as suggested. Note: a cheaper hash means *less* DoS potential, and OTP guessing is throttled and infeasible anyway. |
| Hash semaphore not tunable | **Fixed:** `SMP_HASH_CONCURRENCY`. |
| Key length check allows 31 chars | **Not an issue:** `< 32` already rejected 31. Added a placeholder check (too few distinct characters). |
| Decrypt failure "leaks" key state | Only admins/class teachers see the export. **Changed:** failures are now logged instead of silent. |
| Failed-login audit lost if commit fails | **Fixed:** falls back to the server log and still answers 401. |
| Room-scoped permissions could be granted globally | **Not an issue:** roles are fixed (admin/teacher/student). Admin can't be room-scoped; a teacher assigned school-wide is a deliberate choice. |
| Strip whitespace from passwords | **Rejected:** silently altering a secret breaks logins. Surrounding whitespace is now refused with a clear message instead. |
| Duplicate e-mails | **Fixed:** unique per school, case-insensitive (DB index + friendly 409). |
| Per-IP limit generous / no global limit | Lowered to 20 attempts/min/IP. Global request limiting belongs at the proxy (config above): an app-wide login cap would let an attacker lock *everyone* out. |
| Secret key only checked when first used | **Fixed:** the server refuses to start. |
| "No tests for OTP lifecycle / throttling" | They existed (`tests/test_auth.py`). Added more in `tests/test_security_v1.py`. |

### First review
See git history (`Security hardening: per-user one-time passwords…`): shared default password → individual OTPs; lockout → throttle; argon2 off the event loop; streamed upload limit; password policy; session rotation; no credential-less accounts.
