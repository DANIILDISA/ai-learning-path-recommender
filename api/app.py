"""FastAPI application: routing, CORS, and JSON-only errors.

This is the whole contract between the backend and any frontend -- the
phase-5 plan's "Принципы, обязательные для этой фазы" requires the frontend
to reach the backend over HTTP alone, so what is *not* exposed here is not
reachable at all. Static web assets (`web/`) are wired in on top of this in
a later task; today `/api/*` is the entire surface.

Every error path returns JSON with a message a person can read -- never an
HTML traceback page and never a raw exception's English text.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api.data_routes import router as data_router
from api.routes import router
from api.state import get_state
from core.paths import WEB_DIR


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Loading here means the first real request never pays the multi-second
    # corpus load itself; see api/state.py for why this happens at most once
    # per process no matter how many requests follow.
    get_state()
    yield


app = FastAPI(title="ИИ-агент индивидуальной траектории обучения", lifespan=_lifespan)

# The frontend is a replaceable static layer served from a different origin
# during development (and potentially embedded in the customer's portal
# later) -- CORS must not be the thing standing between them.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(data_router, prefix="/api/data")
app.include_router(router, prefix="/api")

class _RevalidatingStaticFiles(StaticFiles):
    """Статика, которую браузер обязан перепроверять на каждый заход.

    Starlette не ставит `Cache-Control`, и браузер применяет эвристическое
    кэширование: файл считается свежим, пока не надоест, без обращения к
    серверу. Для продукта, который обновляют заменой папки, это означает, что
    после обновления человек продолжает видеть прежний интерфейс -- прежний
    JavaScript против нового API, то есть пустые экраны и ошибки в консоли.

    `no-cache` не запрещает кэш: файл по-прежнему лежит у браузера, но перед
    использованием тот спрашивает сервер, не изменился ли он, и получает 304,
    если нет. Продукт работает на локальной машине, так что эта проверка
    ничего не стоит.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Static web UI, mounted last and at "/" so it only ever catches what the
# `/api` router above did not already claim -- Starlette matches routes in
# the order they were added, so `/api/*` always resolves to the router first.
# `html=True` serves `web/index.html` for `GET /`. `check_dir=False` means a
# half-built checkout (web/ not unpacked yet) fails a request with a 404
# instead of refusing to start the whole API.
app.mount(
    "/", _RevalidatingStaticFiles(directory=WEB_DIR, html=True, check_dir=False), name="web"
)


@app.exception_handler(HTTPException)
async def _http_exception_handler(request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": "Некорректные данные запроса.", "errors": exc.errors()},
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"detail": "Внутренняя ошибка сервера. Обратитесь к разработчику продукта."},
    )
