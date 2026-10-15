from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory="templates")


@router.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"user": request.session.get("user")},
    )


@router.get("/form", response_class=HTMLResponse)
async def form_page(request: Request):
    if not request.session.get("user"):
        return RedirectResponse(url="/")
    return templates.TemplateResponse(request, "form_url.html")