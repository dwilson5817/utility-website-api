from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .router import router

# The /interrail prefix is applied by the API layer (API Gateway maps /interrail
# to this function), so the app itself serves bare paths: /manifest, /stations,
# /departures.
app = FastAPI(title="Interrail API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
