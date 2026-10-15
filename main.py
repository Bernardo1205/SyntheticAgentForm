import os

from dotenv import load_dotenv
from fastapi import FastAPI, Form
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
import responses

import oauth
import forms
import pages

load_dotenv()

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")

# --- Session middleware -------------------------------------------------------
# Gives each user a session cookie, used to store their OAuth tokens and info.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "dev-only-change-me"),
)

# --- Mount the split-out routers ----------------------------------------------
app.include_router(pages.router)
app.include_router(oauth.router)
app.include_router(forms.router)

app.include_router(responses.router)


# --- Routes that didn't fit elsewhere ------------------------------------------



