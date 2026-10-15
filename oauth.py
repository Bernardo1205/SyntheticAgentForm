import os
from urllib.parse import urlencode

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, JSONResponse
from dotenv import load_dotenv

load_dotenv()

router = APIRouter(
    prefix="/auth", # all auth routes are under /auth
    tags=["auth"] # for documentation purposes
)


# --- Google OAuth client -----------------------------------------------------
# creates a manager object from the authlib library that knows how to do the whole
# OAuth back-and-forth dance with any provider you register.
oauth = OAuth()

# Register the Google OAuth provider with the necessary configuration.
# What we want is to get an access token that allows us to read the structure of a Google Form and its responses.
oauth.register(
    name="google",
    #Use client_id / client_secret to speak with the google OAuth server
    client_id=os.environ["GOOGLE_CLIENT_ID"],
    client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={
        # forms.body.readonly -> read a form's questions/structure
        # forms.responses.readonly -> read existing responses (drop if unused)
        "scope": (
            "openid email profile "
            "https://www.googleapis.com/auth/forms.body.readonly "
            "https://www.googleapis.com/auth/forms.responses.readonly"
        ),
    },
)

# --- 1. Kick off login ---------------------------------------------------------
@router.get("/google")
async def auth_google(request: Request):
    # Send the user to Google's OAuth login page. After they log in, Google will redirect them back to our callback route.
    redirect_uri = request.url_for("auth_google_callback")
    return await oauth.google.authorize_redirect(
        request,
        redirect_uri,
        access_type="offline",
        prompt="consent",
    )

# --- 2. OAuth callback: exchange code for tokens --------------------------------
@router.get("/google/callback")
async def auth_google_callback(request: Request):
    existing_tokens = request.session.get("tokens") or {}
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as exc:  # noqa: BLE001 - surface any OAuth failure to the user
        return RedirectResponse(url=f"/?{urlencode({'auth_error': str(exc)})}")

    # token = {access_token, refresh_token, expires_at, id_token, userinfo, ...}
    userinfo = token.get("userinfo", {})

    # Store tokens in the session. For a real multi-user product, persist
    # these (encrypted) in a database keyed by your app's user id instead —
    # sessions are ephemeral and tied to one browser/cookie.
    refresh_token = token.get("refresh_token") or existing_tokens.get("refresh_token")
    if not token.get("refresh_token"):
        if not refresh_token:
            message = "Google did not return a refresh token. Revoke this app's access in your Google Account, then sign in again."
            return RedirectResponse(url=f"/?{urlencode({'auth_error': message})}")

    request.session["tokens"] = {
        "access_token": token["access_token"],
        "refresh_token": refresh_token,
        "expires_at": token.get("expires_at"),
        "scope": token.get("scope"),
        "token_type": token.get("token_type", "Bearer"),
    }
    request.session["user"] = {
        "email": userinfo.get("email"),
        "name": userinfo.get("name"),
    }

    return RedirectResponse(url="/form")


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return JSONResponse({"ok": True})
