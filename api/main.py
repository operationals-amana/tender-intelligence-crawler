import os

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.auth import get_current_user

from api.routers import (
    associations,
    auth,
    company,
    crawl,
    dashboard,
    recommendations,
    scoring,
    search,
    tenders,
)
from crawler.database import init_db

app = FastAPI(
    title="Tender Intelligence API",
    description="AMANA Solutions - procurement opportunity intelligence",
    version="1.0.0",
)

# In production set CORS_ORIGINS to the deployed frontend origin; the default
# is permissive so local development works out of the box.
origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auth is the only unauthenticated router — everything else requires a token.
app.include_router(auth.router, prefix="/api/auth", tags=["Auth"])

protected = [Depends(get_current_user)]
app.include_router(tenders.router, prefix="/api/tenders", tags=["Tenders"], dependencies=protected)
app.include_router(recommendations.router, prefix="/api/recommendations", tags=["Recommendations"], dependencies=protected)
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["Dashboard"], dependencies=protected)
app.include_router(company.router, prefix="/api/company", tags=["Company"], dependencies=protected)
app.include_router(associations.router, prefix="/api/associations", tags=["Associations"], dependencies=protected)
app.include_router(crawl.router, prefix="/api/crawl", tags=["Crawl"], dependencies=protected)
app.include_router(search.router, prefix="/api/search", tags=["Search"], dependencies=protected)
app.include_router(scoring.router, prefix="/api/scoring", tags=["Scoring"], dependencies=protected)


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/health")
def health():
    return {"status": "ok", "service": "tender-intelligence-api"}


@app.get("/")
def root():
    return {"status": "ok", "service": "tender-intelligence-api", "docs": "/docs"}
