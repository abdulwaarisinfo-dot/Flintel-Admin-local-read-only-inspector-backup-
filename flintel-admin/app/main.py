import os, secrets, time
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from . import config, db, backup

app = FastAPI(title="Flintel Admin", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware,
                   secret_key=os.getenv("SESSION_SECRET") or secrets.token_hex(32),
                   same_site="strict", max_age=8 * 3600, session_cookie="flintel_admin")
templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))
templates.env.filters["size"] = lambda b: ("%.2f MB" % ((b or 0) / 1048576))


def authed(request: Request) -> bool:
    return request.session.get("auth") is True


@app.get("/health")
def health():
    return {"status": "ok", "database": "up" if db.ping() else "down"}


@app.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    expected = os.getenv("ADMIN_PASSWORD", "")
    if not expected:
        err = "ADMIN_PASSWORD is not set in .env, so login is disabled."
    elif secrets.compare_digest(password.encode(), expected.encode()):
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
    else:
        time.sleep(1)
        err = "Incorrect password."
    return templates.TemplateResponse(request, "login.html", {"error": err}, status_code=401)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/")
def dashboard(request: Request):
    if not authed(request):
        return RedirectResponse("/login", status_code=303)
    ctx = {"info": db.collect_status(), "backups": backup.list_backups(),
           "flash": request.session.pop("flash", None)}
    return templates.TemplateResponse(request, "index.html", ctx)


@app.post("/backup")
def create_backup(request: Request):
    if not authed(request):
        return RedirectResponse("/login", status_code=303)
    if not backup.start_backup():
        request.session["flash"] = {"ok": False, "msg": "A backup is already running."}
    return RedirectResponse("/", status_code=303)


@app.get("/backup/status")
def backup_status(request: Request):
    if not authed(request):
        return JSONResponse({"error": "login required"}, status_code=401)
    return dict(backup.STATE, backups=backup.list_backups())


@app.get("/api/counts")
def api_counts(request: Request, force: bool = False):
    """Exact COUNT(*) on demand (slow on big tables, so never done on page load)."""
    if not authed(request):
        return JSONResponse({"error": "login required"}, status_code=401)
    try:
        return db.exact_counts(force)
    except Exception as e:
        print("counts error:", type(e).__name__, getattr(e, "orig", e))
        return JSONResponse({"error": db.friendly_error(e)}, status_code=500)


@app.get("/messages")
def messages(request: Request, before: str | None = None, limit: int = 50):
    if not authed(request):
        return RedirectResponse("/login", status_code=303)
    try:
        page = db.browse(before, limit)
        err = None
    except Exception as e:
        print("browse error:", type(e).__name__, getattr(e, "orig", e))
        page, err = None, (str(e) if isinstance(e, RuntimeError) else db.friendly_error(e))
    return templates.TemplateResponse(request, "messages.html", {"page": page, "error": err, "table": db.TABLE})


@app.get("/download/{name}")
def download(request: Request, name: str):
    if not authed(request):
        return RedirectResponse("/login", status_code=303)
    p = backup.backup_path(name)
    if not p:
        raise HTTPException(404, "Backup not found")
    return FileResponse(p, media_type="application/gzip" if p.name.endswith(".gz") else "application/json", filename=p.name)
