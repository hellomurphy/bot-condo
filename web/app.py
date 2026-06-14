"""FastAPI application — routes wired thin, logic in sub-modules."""
import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    FileResponse,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from web import state, scrape_runner, forms, db_queries
from web import ph_routes
from output.excel import export_excel

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

FORM_DEFAULTS = {
    "TARGET_BUDGET": "12000",
    "MAX_BUDGET": "15000",
    "MAX_MOVE_IN_COST": "",
    "MIN_SIZE_SQM": "24",
    "MAX_SCROLL_ROUNDS": "8",
    "MAX_POSTS_PER_RUN": "150",
    "MUST_HAVE_WASHER": False,
    "NEED_PARKING": False,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    from web.ph_poller import poll_loop
    task = asyncio.create_task(poll_loop())
    state.ph_poller["task"] = task
    yield
    task.cancel()
    # Kill any orphaned subprocesses on shutdown
    for run in state.runs.values():
        proc = run.get("process")
        if proc and proc.returncode is None:
            proc.kill()


app = FastAPI(lifespan=lifespan)
app.include_router(ph_routes.router)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "defaults": FORM_DEFAULTS,
    })


@app.post("/start")
async def start(request: Request):
    if scrape_runner.is_run_active():
        return JSONResponse({"error": "A scrape is already running"}, status_code=409)

    raw = await request.form()
    data = dict(raw)
    # FB_GROUP_URLS อาจส่งมาหลาย values (multiple inputs)
    data["FB_GROUP_URLS"] = raw.getlist("FB_GROUP_URLS")

    env, errors = forms.parse_form(data)
    if errors:
        return JSONResponse({"errors": errors}, status_code=422)

    run_id = str(uuid.uuid4())
    state.new_run(run_id)

    asyncio.create_task(scrape_runner.launch(run_id, env))

    return JSONResponse({"run_id": run_id})


@app.post("/stop/{run_id}")
async def stop(run_id: str):
    if run_id not in state.runs:
        return JSONResponse({"error": "Run not found"}, status_code=404)
    await scrape_runner.stop(run_id)
    return JSONResponse({"status": "stopped"})


@app.get("/stream/{run_id}")
async def stream(run_id: str):
    if run_id not in state.runs:
        return PlainTextResponse("Run not found", status_code=404)

    async def event_generator():
        run = state.runs[run_id]
        queue: asyncio.Queue = run["queue"]

        # Replay stored logs first
        for line in list(run["logs"]):
            yield f"data: {json.dumps(line)}\n\n"

        # If already finished, send done immediately
        if run["status"] in ("done", "stopped", "failed"):
            yield f'data: {json.dumps({"done": True, "status": run["status"]})}\n\n'
            return

        # Stream live
        heartbeat_interval = 15  # seconds
        while True:
            try:
                line = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval)
                if line is None:
                    yield f'data: {json.dumps({"done": True, "status": run["status"]})}\n\n'
                    break
                yield f"data: {json.dumps(line)}\n\n"
            except asyncio.TimeoutError:
                yield 'data: {"ping":true}\n\n'

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/status/{run_id}")
async def run_status(run_id: str):
    run = state.runs.get(run_id)
    if not run:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse({"status": run["status"]})


@app.get("/results", response_class=HTMLResponse)
async def results(request: Request):
    rows = db_queries.get_results_rows()
    return templates.TemplateResponse(request, "results.html", {
        "rows": rows,
        "count": len(rows),
    })


@app.post("/results/delete/{listing_id}")
async def delete_result(listing_id: int):
    deleted = db_queries.delete_listing(listing_id)
    if not deleted:
        return JSONResponse({"error": "Listing not found"}, status_code=404)
    return JSONResponse({"status": "deleted", "listing_id": listing_id})


class CheckPostBody(BaseModel):
    url: str


@app.post("/check_post")
async def check_post(body: CheckPostBody):
    from scraper.browser import has_credentials, launch_context
    from scraper.feed import scrape_comments, filter_listing_comments
    import config as cfg

    if not has_credentials():
        return JSONResponse({"error": "no_credentials"}, status_code=403)

    url = body.url.strip()
    if not url or "facebook.com" not in url:
        return JSONResponse({"error": "invalid_url"}, status_code=422)

    pw, context = await launch_context(headless=True)
    try:
        comments = await scrape_comments(context, url, max_comments=cfg.SEEKING_MAX_COMMENTS_PER_POST, max_clicks=20)
        accepted, _ = filter_listing_comments(comments)
        previews = [c[:60].replace("\n", " ") + ("…" if len(c) > 60 else "") for c in accepted[:5]]
        return JSONResponse({
            "total": len(comments),
            "qualified": len(accepted),
            "previews": previews,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        await context.close()
        await pw.stop()


@app.get("/export")
async def export():
    rows = db_queries.get_results_rows()
    if not rows:
        return PlainTextResponse("No listings to export yet", status_code=404)
    path = export_excel(rows)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=Path(path).name,
    )
