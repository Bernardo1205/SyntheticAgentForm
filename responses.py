from fastapi import APIRouter, Form

router = APIRouter()

@router.post("/submit-form-url")
async def submit_form_url(
    form_url: str = Form(...),
    num_responses: int = Form(..., gt=0),
    target_audience: str = Form(..., min_length=3),
    user_id: str = Form(None),
):
    # Placeholder for the next agent step: parse the form and fill it.
    return {
        "status": "success",
        "message": "Synthetic generation request received",
        "user_id": user_id,
        "form_url": form_url,
        "num_responses": num_responses,
        "target_audience": target_audience,
    }