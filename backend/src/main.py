import os, logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from redis import Redis
from src.config.database import Session
from src.config.settings import REDIS_URL
from src.middlewares.requests import RequestControl
from src.routes import accounting, auth, datasets, jobs, reports
from src.controllers import catalog
from migrations.initial import migrate
@asynccontextmanager
async def lifespan(app):
    migrate()
    try:
        from src.services.accounting import recover_interrupted
        with Session() as db:
            recovered=recover_interrupted(db)
            if recovered: logging.warning('Recovered settlements: %s',recovered)
    except Exception:
        logging.exception('Settlement recovery failed; backend remains available')
    if os.getenv('SEED_DEMO','true').lower()=='true':
        try:
            from scripts.seed_demo import seed
            seed()
        except Exception:logging.exception('Demo seed failed; backend remains available')
    yield
app=FastAPI(title='业务分析工作室',lifespan=lifespan)
app.add_middleware(RequestControl)
@app.exception_handler(Exception)
async def unhandled(request:Request,exc:Exception):
    logging.exception('Request failed')
    return JSONResponse({'detail':'服务暂不可用，请稍后重试'},status_code=500)
@app.get('/health')
def health():
    with Session() as db:db.execute(text('SELECT 1'))
    r=Redis.from_url(REDIS_URL,socket_timeout=2);r.ping();r.close()
    return {'status':'healthy'}
for router in [auth.router,datasets.router,jobs.router,reports.router,catalog.router,accounting.router]:app.include_router(router)
