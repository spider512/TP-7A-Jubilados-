import base64
import hashlib
import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jwt import PyJWKClient
from pydantic import BaseModel, ConfigDict
from sqlalchemy import DateTime, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./municipio.db")
PADRON_URL = os.environ.get("PADRON_URL", "http://localhost:8090")
KC_PUBLIC = os.environ.get("KEYCLOAK_PUBLIC", "http://localhost:8180/realms/municipio")
KC_INTERNAL = os.environ.get("KEYCLOAK_INTERNAL", KC_PUBLIC)
SESSION_SECRET = os.environ.get("SESSION_SECRET", "solo-desarrollo-local")
DEMO_IDENTITIES = json.loads(os.environ.get("DEMO_IDENTITIES", '{"vecino1":"JUB-001","vecino2":"NO-003"}'))
APP_PUBLIC_URL = os.environ.get("APP_PUBLIC_URL", "http://localhost:8080").rstrip("/")
REDIRECT_URI = APP_PUBLIC_URL + "/auth/callback"
# SessionMiddleware signs cookies. Encrypt the OIDC logout hint before storing it.
logout_cipher = Fernet(base64.urlsafe_b64encode(
    hashlib.sha256(b"municipio-logout\0" + SESSION_SECRET.encode()).digest()
))
BENEFITS = [
    {"id": "tarifa-social", "nombre": "Tarifa social"},
    {"id": "descuento-transporte", "nombre": "Descuento en transporte"},
]


class Base(DeclarativeBase):
    pass


class Application(Base):
    __tablename__ = "solicitudes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    beneficio_id: Mapped[str] = mapped_column(String(80), nullable=False)
    solicitante_id: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    solicitante_nombre: Mapped[str] = mapped_column(String(160), nullable=False)
    estado: Mapped[str] = mapped_column(String(20), nullable=False, default="pendiente")
    creada_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resuelta_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    motivo: Mapped[str | None] = mapped_column(String(500))
    agente_id: Mapped[str | None] = mapped_column(String(160))


engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(engine)
app = FastAPI(title="Beneficios municipales", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
jwks = PyJWKClient(f"{KC_INTERNAL}/protocol/openid-connect/certs")

BENEFIT_NAMES = {benefit["id"]: benefit["nombre"] for benefit in BENEFITS}
# Local time for contemporary records in the Argentine municipal demo.
PORTAL_TIMEZONE = timezone(timedelta(hours=-3))


def portal_date(value: datetime) -> str:
    # SQLite, used for local tests, can return stored UTC timestamps without an offset.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(PORTAL_TIMEZONE).strftime("%d/%m/%Y")


templates.env.filters["benefit_name"] = lambda value: BENEFIT_NAMES.get(value, value)
templates.env.filters["portal_date"] = portal_date


@app.on_event("startup")
def init_db():
    Base.metadata.create_all(engine)


def problem(status: int, title: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


def request_error(request: Request, status: int, title: str, detail: str):
    path = request.url.path
    accepts_html = any(
        entry.split(";", 1)[0].strip() == "text/html"
        for entry in request.headers.get("accept", "").split(",")
    )
    if (path.startswith("/web/") or path == "/auth/callback") and accepts_html:
        return templates.TemplateResponse(
            request, "error.html",
            {"user": request.session.get("user"), "title": title, "detail": detail,
             "show_login": status == 401 or path == "/auth/callback"},
            status_code=status,
            headers={"Vary": "Accept", "Cache-Control": "no-store"},
        )
    return problem(status, title, detail)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    titles = {400: "Entrada inválida", 401: "Autenticación requerida", 403: "Acceso denegado", 404: "No encontrado", 409: "Conflicto", 503: "Servicio no disponible"}
    return request_error(request, exc.status_code, titles.get(exc.status_code, "Error"), str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    return request_error(request, 400, "Entrada inválida", "Revisá los datos enviados.")


def db_session():
    with SessionLocal() as session:
        yield session


def decode_token(token: str) -> dict:
    try:
        key = jwks.get_signing_key_from_jwt(token)
        return jwt.decode(token, key.key, algorithms=["RS256"], audience="municipio-api", issuer=KC_PUBLIC)
    except Exception as exc:
        raise HTTPException(401, "Token de acceso inválido o vencido") from exc


def api_user(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Falta el token de acceso")
    return decode_token(authorization[7:])


def role(user: dict, expected: str):
    if expected not in user.get("realm_access", {}).get("roles", []):
        raise HTTPException(403, "Tu rol no permite esta operación")


def view_model(item: Application) -> dict:
    return {
        "id": item.id,
        "beneficioId": item.beneficio_id,
        "solicitanteId": item.solicitante_id,
        "estado": item.estado,
        "creadaEn": item.creada_en.isoformat(),
        "resueltaEn": item.resuelta_en.isoformat() if item.resuelta_en else None,
        "motivo": item.motivo,
    }


def create_application(session: Session, user: dict, benefit: str) -> Application:
    role(user, "vecino")
    if benefit not in {b["id"] for b in BENEFITS}:
        raise HTTPException(400, "Beneficio inexistente")
    if user.get("preferred_username") not in DEMO_IDENTITIES:
        raise HTTPException(409, "La cuenta no tiene vínculo de prueba con el padrón")
    item = Application(
        id=str(uuid.uuid4()),
        beneficio_id=benefit,
        solicitante_id=user["sub"],
        solicitante_nombre=user.get("preferred_username", ""),
        creada_en=datetime.now(timezone.utc),
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def find_application(session: Session, item_id: str) -> Application:
    item = session.get(Application, item_id)
    if not item:
        raise HTTPException(404, "Solicitud inexistente")
    return item


def decide_application(session: Session, user: dict, item_id: str, state: str, reason: str) -> Application:
    role(user, "agente")
    item = find_application(session, item_id)
    if item.estado != "pendiente":
        raise HTTPException(409, "La solicitud ya tiene una decisión")
    if state not in {"aprobada", "rechazada"} or not reason.strip():
        raise HTTPException(400, "Indicá una decisión y un motivo")
    if state == "aprobada":
        identifier = DEMO_IDENTITIES.get(item.solicitante_nombre)
        if not identifier:
            raise HTTPException(409, "No existe vínculo con el padrón")
        try:
            response = httpx.post(f"{PADRON_URL}/verificaciones", json={"identificador": identifier}, timeout=3.0)
        except httpx.RequestError as exc:
            raise HTTPException(503, "No se pudo consultar el padrón; la solicitud sigue pendiente") from exc
        if response.status_code == 503:
            raise HTTPException(503, "El padrón no está disponible; la solicitud sigue pendiente")
        if response.status_code != 200:
            raise HTTPException(503, "El padrón devolvió una respuesta inesperada")
        try:
            result = response.json()
        except ValueError as exc:
            raise HTTPException(503, "El padrón devolvió una respuesta inesperada; la solicitud sigue pendiente") from exc
        condition = result.get("condicion") if isinstance(result, dict) else None
        if not isinstance(condition, str) or condition not in {"jubilado", "pensionado", "no_registrado"}:
            raise HTTPException(503, "El padrón devolvió una respuesta inesperada; la solicitud sigue pendiente")
        if condition == "no_registrado":
            raise HTTPException(409, "El padrón no confirma la condición requerida")
    item.estado = state
    item.motivo = reason.strip()[:500]
    item.resuelta_en = datetime.now(timezone.utc)
    item.agente_id = user["sub"]
    session.commit()
    session.refresh(item)
    return item


class NewApplication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    beneficioId: str


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    estado: str
    motivo: str


@app.get("/api/v1/beneficios")
def list_benefits():
    return BENEFITS


@app.post("/api/v1/solicitudes", status_code=201)
def post_application(body: NewApplication, user: dict = Depends(api_user), session: Session = Depends(db_session)):
    item = create_application(session, user, body.beneficioId)
    return JSONResponse(status_code=201, headers={"Location": f"/api/v1/solicitudes/{item.id}"}, content=view_model(item))


@app.get("/api/v1/solicitudes")
def list_applications(estado: str, user: dict = Depends(api_user), session: Session = Depends(db_session)):
    role(user, "agente")
    if estado != "pendiente":
        raise HTTPException(400, "Solo se admite estado=pendiente en este corte")
    return [view_model(item) for item in session.scalars(select(Application).where(Application.estado == estado))]


@app.get("/api/v1/solicitudes/{id}")
def get_application(id: str, user: dict = Depends(api_user), session: Session = Depends(db_session)):
    item = find_application(session, id)
    if "agente" not in user.get("realm_access", {}).get("roles", []) and item.solicitante_id != user["sub"]:
        raise HTTPException(403, "No podés consultar una solicitud ajena")
    return view_model(item)


@app.patch("/api/v1/solicitudes/{id}")
def patch_application(id: str, body: Decision, user: dict = Depends(api_user), session: Session = Depends(db_session)):
    return view_model(decide_application(session, user, id, body.estado, body.motivo))


@app.get("/login")
def login(request: Request):
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    request.session["oidc_state"] = state
    request.session["oidc_verifier"] = verifier
    query = urlencode({"client_id": "municipio-web", "redirect_uri": REDIRECT_URI, "response_type": "code", "scope": "openid profile", "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "login"})
    return RedirectResponse(f"{KC_PUBLIC}/protocol/openid-connect/auth?{query}")


@app.get("/auth/callback")
def callback(request: Request, code: str, state: str):
    expected_state = request.session.get("oidc_state")
    verifier = request.session.get("oidc_verifier")
    if not expected_state or not verifier or not secrets.compare_digest(state, expected_state):
        raise HTTPException(400, "Estado de login inválido")
    response = httpx.post(
        f"{KC_INTERNAL}/protocol/openid-connect/token",
        data={"grant_type": "authorization_code", "client_id": "municipio-web", "code": code, "redirect_uri": REDIRECT_URI, "code_verifier": verifier},
        timeout=10.0,
    )
    if response.status_code != 200:
        raise HTTPException(401, "No se pudo completar el inicio de sesión")
    tokens = response.json()
    claims = decode_token(tokens["access_token"])
    request.session.clear()
    request.session["user"] = {"sub": claims["sub"], "preferred_username": claims.get("preferred_username", ""), "realm_access": claims.get("realm_access", {})}
    request.session["csrf"] = secrets.token_urlsafe(24)
    if id_token := tokens.get("id_token"):
        request.session["logout_hint"] = logout_cipher.encrypt(id_token.encode()).decode()
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    user = request.session.get("user")
    stored_hint = request.session.get("logout_hint")
    request.session.clear()
    if not user:
        return RedirectResponse("/", status_code=303)

    params = {"client_id": "municipio-web", "post_logout_redirect_uri": APP_PUBLIC_URL + "/"}
    if stored_hint:
        try:
            params["id_token_hint"] = logout_cipher.decrypt(stored_hint.encode()).decode()
        except InvalidToken:
            pass  # Keycloak can ask for confirmation if the hint is unavailable.
    return RedirectResponse(
        f"{KC_PUBLIC}/protocol/openid-connect/logout?{urlencode(params)}",
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def web_user(request: Request) -> dict:
    user = request.session.get("user")
    if not user:
        raise HTTPException(401, "Iniciá sesión")
    return user


def check_csrf(request: Request, token: str):
    if not secrets.compare_digest(token, request.session.get("csrf", "")):
        raise HTTPException(403, "Formulario vencido; volvé a cargar la página")


@app.get("/", response_class=HTMLResponse)
def home(request: Request, session: Session = Depends(db_session)):
    user = request.session.get("user")
    items = []
    if user:
        query = select(Application)
        if "agente" in user.get("realm_access", {}).get("roles", []):
            query = query.where(Application.estado == "pendiente")
        else:
            query = query.where(Application.solicitante_id == user["sub"])
        items = list(session.scalars(query.order_by(Application.creada_en.desc())))
    return templates.TemplateResponse(request, "home.html", {"user": user, "items": items, "benefits": BENEFITS, "csrf": request.session.get("csrf", "")})


@app.post("/web/solicitudes")
def web_create(request: Request, beneficio: str = Form(), csrf: str = Form(), session: Session = Depends(db_session)):
    check_csrf(request, csrf)
    item = create_application(session, web_user(request), beneficio)
    return RedirectResponse(f"/web/solicitudes/{item.id}", status_code=303)


@app.get("/web/solicitudes/{item_id}", response_class=HTMLResponse)
def web_detail(item_id: str, request: Request, session: Session = Depends(db_session)):
    user = web_user(request)
    item = find_application(session, item_id)
    if "agente" not in user.get("realm_access", {}).get("roles", []) and item.solicitante_id != user["sub"]:
        raise HTTPException(403, "No podés ver una solicitud ajena")
    return render_web_detail(request, item, user)


def render_web_detail(request: Request, item: Application, user: dict, error: HTTPException | None = None, reason: str = ""):
    return templates.TemplateResponse(
        request, "detail.html",
        {"item": item, "user": user, "csrf": request.session.get("csrf", ""),
         "decision_error": error.detail if error else None, "submitted_reason": reason},
        status_code=error.status_code if error else 200,
    )


@app.post("/web/solicitudes/{item_id}/decision")
def web_decide(item_id: str, request: Request, estado: str = Form(), motivo: str = Form(), csrf: str = Form(), session: Session = Depends(db_session)):
    check_csrf(request, csrf)
    user = web_user(request)
    try:
        decide_application(session, user, item_id, estado, motivo)
    except HTTPException as exc:
        if exc.status_code not in {400, 409, 503}:
            raise
        return render_web_detail(request, find_application(session, item_id), user, error=exc, reason=motivo[:500])
    return RedirectResponse(f"/web/solicitudes/{item_id}", status_code=303)
