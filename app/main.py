from fastapi import FastAPI

from app.core.config import get_settings

app = FastAPI()


@app.on_event("startup")
def load_config() -> None:
    # Ensure env-based settings are loaded and validated at app startup.
    app.state.settings = get_settings()


@app.get("/health")
def health():
    return {"status": "ok"}
