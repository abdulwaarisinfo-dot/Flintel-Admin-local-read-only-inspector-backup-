import os, secrets, time
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, FileResponse
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
    try:
        name = backup.create_backup()
        request.session["flash"] = {"ok": True, "msg": "Backup created: " + name}
    except backup.BackupError as e:
        request.session["flash"] = {"ok": False, "msg": str(e)}
    return RedirectResponse("/", status_code=303)


@app.get("/download/{name}")
def download(request: Request, name: str):
    if not authed(request):
        return RedirectResponse("/login", status_code=303)
    p = backup.backup_path(name)
    if not p:
        raise HTTPException(404, "Backup not found")
    return FileResponse(p, media_type="application/json", filename=p.name)
