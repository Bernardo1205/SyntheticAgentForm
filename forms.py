import os
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from dotenv import load_dotenv

load_dotenv()

router = APIRouter(prefix="/api", tags=["forms"])


def get_forms_client(request: Request):
    """Build an authenticated Forms API client from the session's tokens."""
    tokens = request.session.get("tokens")
    if not tokens:
        raise HTTPException(status_code=401, detail="Not logged in. Go to /auth/google first.")

    expires_at = tokens.get("expires_at")
    expiry = None
    if expires_at is not None:
        try:
            expiry = datetime.fromtimestamp(float(expires_at))
        except (TypeError, ValueError, OSError):
            expiry = None

    creds = Credentials(
        token=tokens["access_token"],
        refresh_token=tokens.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        scopes=tokens.get("scope", "").split(),
        expiry=expiry,
    )

    if creds.expired:
        if not creds.refresh_token:
            request.session.pop("tokens", None)
            request.session.pop("user", None)
            raise HTTPException(
                status_code=401,
                detail=(
                    "Google refresh token is missing. Revoke this app's access in your Google Account, "
                    "then sign in again to obtain a new refresh token."
                ),
            )

        try:
            creds.refresh(GoogleAuthRequest())
        except Exception as exc:  # noqa: BLE001 - surface refresh failures clearly to the client
            request.session.pop("tokens", None)
            request.session.pop("user", None)
            raise HTTPException(status_code=401, detail=f"Failed to refresh Google access token: {exc}") from exc

        tokens["access_token"] = creds.token
        if creds.expiry is not None:
            tokens["expires_at"] = int(creds.expiry.timestamp())
        request.session["tokens"] = tokens

    return build("forms", "v1", credentials=creds)


@router.get("/me")
async def me(request: Request):
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in.")
    return {"user": user}


@router.get("/forms/{form_id}")
async def get_form(form_id: str, request: Request):
    forms_service = get_forms_client(request)
    try:
        data = forms_service.forms().get(formId=form_id).execute()
    except HttpError as err:
        status = err.resp.status
        detail = (
            "Access denied — the logged-in account must be an editor/owner of this form."
            if status == 403
            else "Failed to fetch form."
        )
        raise HTTPException(status_code=status, detail=detail) from err

    questions = []
    for item in data.get("items", []):
        q_item = item.get("questionItem")
        if not q_item:
            continue
        question = q_item["question"]
        q_type = next((k for k in question if k.endswith("Question")), None)
        questions.append(
            {
                "itemId": item.get("itemId"),
                "title": item.get("title"),
                "questionId": question.get("questionId"),
                "required": question.get("required", False),
                "type": q_type,
                "raw": question,
            }
        )
    print(f"[Forms API] Fetched form {form_id} with {len(questions)} questions for user {request.session.get('user', {}).get('email')}")

    return {
        "formId": data.get("formId"),
        "title": data.get("info", {}).get("title"),
        "description": data.get("info", {}).get("description"),
        "questions": questions,
    }


@router.get("/forms/{form_id}/responses")
async def get_form_responses(form_id: str, request: Request):
    forms_service = get_forms_client(request)
    try:
        data = forms_service.forms().responses().list(formId=form_id).execute()
    except HttpError as err:
        raise HTTPException(status_code=err.resp.status, detail="Failed to fetch responses.") from err
    return data