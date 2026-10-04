# Signing In

The web service needs to know who is asking. This page explains how people sign in and how Cograil turns that sign-in into a Principal. OIDC stands for OpenID Connect, the standard that Google, Microsoft Entra ID and most other providers use. The CLI does not sign anyone in (see `docs/architecture.md`).

## The Two Modes

Set `COGRAIL_AUTH` to `dev` or `oidc`. If it is unset, or holds anything else, the service refuses to start with `AuthNotConfigured`. Cograil fails closed.

Settings come only from environment variables. They never come from Workspace files. `.env.example` lists them all.

Routers take the signed-in Principal with `Depends(current_principal)`. It answers 401 when nobody is signed in. The app is set up once:

```python
install_auth(app, auth_settings(os.environ), workspace)
```

## Dev Mode

With `COGRAIL_AUTH=dev`, every request is the same Principal.

- `COGRAIL_DEV_PRINCIPAL` is required. It is the Principal id.
- `COGRAIL_DEV_GROUPS` is optional. It is a comma-separated list of groups.
- The Principal also gets any groups that `principals.yaml` gives that id.

Cograil logs the warning `auth.dev_mode` at start-up. Use dev mode for local work and demos only. Never deploy it.

## OIDC Mode

With `COGRAIL_AUTH=oidc`, Cograil uses the authorization code flow (through authlib), with `state` and `nonce` checks. It asks for the scopes `openid email profile`.

| Route | What it does |
| --- | --- |
| `GET /auth/login` | Redirects the browser to the provider. |
| `GET /auth/callback` | Exchanges the code and checks the ID token: signature (through the provider's JWKS), issuer, audience, nonce and expiry. |
| `POST /auth/logout` | Clears the session. |
| `GET /auth/me` | Returns the Principal: `id`, `aliases`, `groups` and `kind`. It works in both modes. |

After sign-in, the browser is redirected to `/`.

### Settings

| Variable | Meaning |
| --- | --- |
| `COGRAIL_OIDC_METADATA_URL` | Required. The provider's `.well-known/openid-configuration` URL. |
| `COGRAIL_OIDC_CLIENT_ID` | Required. The client id from the provider. |
| `COGRAIL_OIDC_CLIENT_SECRET` | Required. The client secret from the provider. |
| `COGRAIL_OIDC_ALLOWED_DOMAINS` | Required. Comma-separated email domains that may sign in. Case does not matter. |
| `COGRAIL_SESSION_SECRET` | Required. At least 32 characters. Rotating it signs everyone out. |
| `COGRAIL_OIDC_ID_CLAIM` | The claim that names the Principal. The default is `email`. It must hold an email address. |
| `COGRAIL_OIDC_GROUPS_CLAIM` | The claim that lists groups. The default is `groups`. |
| `COGRAIL_OIDC_REDIRECT_URL` | The full public callback URL, for example `https://cograil.example.com/auth/callback`. Set it when a proxy (Cloud Run, a load balancer) hides the public scheme or host. Otherwise Cograil works the URL out from the request. |

Make a session secret like this:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## The Session

The session cookie is `cograil_session`. It is signed with `COGRAIL_SESSION_SECRET` (itsdangerous, through Starlette's `SessionMiddleware`). It is HttpOnly, Secure and SameSite=Lax, and it lasts 8 hours.

- Groups are fixed at sign-in. They do not change until the session ends.
- A tampered cookie is ignored, and the request gets a 401.
- The cookie is signed, not encrypted. The browser can read the id and groups inside it. Nothing secret goes in it.

## When Sign-In Is Refused

Cograil answers 403 and stores nothing when:

- the id claim does not hold an email address;
- the id claim is `email` and `email_verified` is not true;
- the email's domain is not in `COGRAIL_OIDC_ALLOWED_DOMAINS`;
- the email names a Principal of `kind: system` in `principals.yaml` (only people sign in);
- the provider returns an error, or a `state`, `nonce`, issuer, audience or signature check fails.

Each refusal logs `auth.denied`. Each sign-in logs `auth.signed_in`.

## Who the Principal Is

Principal ids are trimmed and lower-cased everywhere: in `principals.yaml`, in the CLI's `--as`, and at sign-in. So `Alice@Example.com` and `alice@example.com` are one Principal. Gates compare ids after this step, so spelling cannot bypass or miss an Approval check.

An entry in `principals.yaml` may list `aliases`. These are other emails of the same person, for example an address in another domain. Signing in with an alias gives the canonical id.

```yaml
# principals.yaml
principals:
  - id: alice@example.com
    aliases: [asmith@corp.example.com]
    groups: [all-employees]
```

`cograil validate` rejects a Workspace when:

- an id or alias names two Principals;
- a Colleague's `escalation_contact` is an alias. Use the canonical id.

`cograil approve --as <alias>` decides as the canonical Principal.

## Groups From Claims

An Audience may list `claims` in `audiences.yaml`. These are values of the provider's groups claim. A signed-in Principal who has one of them joins that Audience and gets its `groups`.

```yaml
# audiences.yaml
audiences:
  - name: finance
    groups: [finance]
    claims: ["5b2c1f0e-0000-0000-0000-000000000000"]   # Entra group object id
  - name: managers
    groups: [managers]
    claims: []
```

- Claim values that no Audience lists are ignored. Claims never become groups directly.
- Matching is exact. Case matters.
- The final groups are the groups in `principals.yaml` for that Principal, plus the groups of every Audience whose claims match.

## Setting Up Google

1. In the Google Cloud console, open APIs & Services, then OAuth consent screen. Choose the Internal user type for a Google Workspace organisation.
2. Open Credentials, then Create credentials, then OAuth client ID. Choose Web application.
3. Add the authorised redirect URI `https://<host>/auth/callback`. For local testing, also add `http://localhost:8000/auth/callback`. Secure cookies work on localhost in current browsers.
4. Set the environment:

```bash
COGRAIL_AUTH=oidc
COGRAIL_OIDC_METADATA_URL=https://accounts.google.com/.well-known/openid-configuration
COGRAIL_OIDC_CLIENT_ID=<client id from the credential>
COGRAIL_OIDC_CLIENT_SECRET=<client secret from the credential>
COGRAIL_OIDC_ALLOWED_DOMAINS=<your Workspace domain>
COGRAIL_SESSION_SECRET=<a generated secret>
```

Keep `COGRAIL_OIDC_ID_CLAIM` at `email`. Google sends `email_verified`.

Google ID tokens carry no group claim. Google users get their groups from `principals.yaml`. For now, Audience `claims` are not used with Google.

## Setting Up Entra ID

1. In the Entra admin centre, open App registrations, then New registration.
2. Choose "Accounts in this organizational directory only (single tenant)". Set the Redirect URI platform to Web, with `https://<host>/auth/callback`.
3. Open Certificates & secrets, then New client secret. Copy the value.
4. Open Token configuration, then Add groups claim. Choose "Security groups" (or "Groups assigned to the application", which is better for large tenants). Choose the ID token and Group ID.
5. Set the environment:

```bash
COGRAIL_AUTH=oidc
COGRAIL_OIDC_METADATA_URL=https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration
COGRAIL_OIDC_CLIENT_ID=<application (client) id>
COGRAIL_OIDC_CLIENT_SECRET=<client secret value>
COGRAIL_OIDC_ID_CLAIM=preferred_username
COGRAIL_OIDC_ALLOWED_DOMAINS=<the verified domains your users sign in with>
COGRAIL_SESSION_SECRET=<a generated secret>
```

- The metadata URL names your tenant, so other tenants cannot sign in.
- Entra's `email` claim is optional and not verified. With the default `email` claim, sign-in is refused. That is why `COGRAIL_OIDC_ID_CLAIM` is `preferred_username`.
- Put the group object ids in the `claims` of your Audiences.

### Group Overage

When a user is in more than 200 groups, Entra leaves out the groups claim and sends `_claim_names` instead. Cograil logs `auth.groups_overage`. The user gets no claim-mapped groups. To avoid this, use "Groups assigned to the application".
