"""Launchpad tools: 1-click launch of configured environments.

A launchpad environment maps a friendly name (for example "PR1 Production" or
"QA Stack") to a URL and the account email used to sign in. LaunchEnvironment
opens the URL, signs in with the vault credential for that account, and then
classifies the post-submit page so the agent can hand control back to the user
when MFA or CAPTCHA is required.
"""

import json
import time

MFA_PHRASES = (
    "verification code",
    "enter the code",
    "one-time code",
    "one time passcode",
    "one-time passcode",
    "authenticator",
    "approve the request",
    "approve sign-in",
    "approve the sign-in",
    "number matching",
    "mfa",
    "two-factor",
    "two factor",
    "2fa",
    "security key",
    "passkey",
    "check your phone",
    "verification pending",
    "additional verification",
)

CAPTCHA_PHRASES = (
    "captcha",
    "are you human",
    "verify you are human",
    "i'm not a robot",
    "im not a robot",
    "robot check",
    "human verification",
    "security check puzzle",
)

LOGIN_FORM_PHRASES = ("password", "sign in", "log in", "forgot password")


class LaunchpadError(RuntimeError):
    pass


def normalize_environments(config):
    raw = config.get("environments") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    if not isinstance(raw, dict):
        return {}
    result = {}
    for name, value in raw.items():
        label = str(name).strip()
        if not label or not isinstance(value, dict):
            continue
        url = str(value.get("url") or "").strip()
        account = str(value.get("account") or "").strip()
        if url and account:
            result[label] = {"url": url, "account": account}
    return result


def list_environments(config):
    envs = normalize_environments(config)
    if not envs:
        return {
            "ok": True,
            "environments": [],
            "note": (
                "No launchpad environments are configured yet. Add them in the "
                "sidecar Launchpad (Manage) or set the 'environments' key in "
                "config.json as {\"Name\": {\"url\": ..., \"account\": ...}}."
            ),
        }
    items = [
        {"name": name, "url": value["url"], "account": value["account"]}
        for name, value in sorted(envs.items())
    ]
    return {
        "ok": True,
        "environments": items,
        "note": "Launch one with LaunchEnvironment after the user confirms.",
    }


def classify_page(text):
    lowered = str(text or "").lower()
    if any(phrase in lowered for phrase in CAPTCHA_PHRASES):
        return "captcha_detected"
    if any(phrase in lowered for phrase in MFA_PHRASES):
        return "mfa_required"
    return None


def read_page_text(executor, depth=4):
    page = executor({"tool": "ReadPage", "arguments": {"filter": "text", "depth": depth}})
    if isinstance(page, dict):
        text = page.get("text")
        if isinstance(text, dict):
            text = text.get("body") or text.get("text") or ""
        return str(text or "")
    return str(page)


def _find_environment(config, requested):
    envs = normalize_environments(config)
    key = next(
        (name for name in envs if name.lower() == str(requested).strip().lower()),
        None,
    )
    if not key:
        available = ", ".join(sorted(envs)) or "(none configured)"
        raise LaunchpadError(
            f"Unknown launchpad environment '{requested}'. Configured: {available}."
        )
    return key, envs[key]


def launch_environment(config, executor, vault_module, requested_name, confirm, allow_sensitive=False):
    key, env = _find_environment(config, requested_name)
    url = env["url"]
    account = env["account"]
    base = {"ok": True, "environment": key, "url": url, "account": account}

    if not confirm:
        return dict(
            base,
            status="preview",
            handoff=False,
            note=(
                "This launch will open the environment and sign in with the vault "
                "credential for this account, filling and submitting the login "
                "form. Re-run with confirm=true to proceed."
            ),
        )

    if not allow_sensitive:
        return dict(
            base,
            ok=False,
            status="approval_required",
            handoff=False,
            error=(
                "LaunchEnvironment performs a sign-in and needs explicit approval: "
                "re-run with sensitive actions allowed after the user confirms."
            ),
        )

    if executor is None:
        raise LaunchpadError("No browser session is attached for LaunchEnvironment.")
    if vault_module is None:
        raise LaunchpadError(
            "The vault is not connected; LaunchEnvironment signs in with vault "
            "credentials. Run VaultConnect first or configure vault_uri and "
            "vault_name in the settings."
        )

    started = time.time()
    client = vault_module.VaultClient(config)
    try:
        client.login(executor, url, account, submit=True)
    except vault_module.VaultError as error:
        message = str(error)
        text = read_page_text(executor)
        detected = classify_page(text)
        if detected:
            return dict(
                base,
                status=detected,
                handoff=True,
                page_snippet=text[:800],
                note=(
                    "The sign-in flow paused on a challenge page; hand control "
                    "back to the user to finish it."
                ),
            )
        if "expired or incorrect" in message.lower():
            return dict(
                base,
                ok=False,
                status="password_expired",
                handoff=False,
                page_snippet=text[:800],
                error=message,
                note=(
                    "Offer VaultResetPassword for this account (automated reset "
                    "works for OPS-BPS-Secure accounts only)."
                ),
            )
        raise LaunchpadError(message)

    text = read_page_text(executor)
    detected = classify_page(text)
    if detected:
        return dict(
            base,
            status=detected,
            handoff=True,
            page_snippet=text[:800],
            note=(
                "MFA or verification is required; hand control back to the user "
                "to complete the second factor, then continue."
            )
            if detected == "mfa_required"
            else (
                "A CAPTCHA or human-verification page is shown; hand control "
                "back to the user to solve it."
            ),
        )

    lowered = text.lower()
    if any(phrase in lowered for phrase in LOGIN_FORM_PHRASES) and "password" in lowered:
        return dict(
            base,
            status="login_form_still_present",
            handoff=True,
            page_snippet=text[:800],
            note=(
                "The login form still appears on the page; ask the user whether "
                "the sign-in completed or inspect further with ReadPage."
            ),
        )

    return dict(
        base,
        status="signed_in",
        handoff=False,
        elapsed_seconds=round(time.time() - started, 1),
        page_snippet=text[:800],
        note="Sign-in submitted. Verify the page state before reporting success.",
    )


def dispatch_tool(name, arguments, config, allow_sensitive, executor=None, vault_module=None):
    arguments = arguments or {}
    try:
        if name == "ListEnvironments":
            return list_environments(config)
        if name == "LaunchEnvironment":
            requested = str(arguments.get("name") or "").strip()
            if not requested:
                raise LaunchpadError("Provide the environment name to launch.")
            return launch_environment(
                config,
                executor,
                vault_module,
                requested,
                bool(arguments.get("confirm")),
                allow_sensitive,
            )
        raise LaunchpadError(f"Unknown launchpad tool '{name}'.")
    except (LaunchpadError, ValueError) as error:
        return {"error": str(error)}
    except Exception as error:  # defensive: never crash the agent loop
        return {"error": f"Launchpad tool failure: {error}"}