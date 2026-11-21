from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Receipt Approval System")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.get("/health")
def health():
    # Reporting the extractor also proves that env vars reach the app (e.g. through compose).
    return {"status": "ok", "extractor": settings.extractor}


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"extractor": settings.extractor})
