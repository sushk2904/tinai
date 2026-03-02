from fastapi import FastAPI
from contextlib import asynccontextmanager

# We will add Neon and Redis connection pools here later
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("TINAI Engine Booting Up...")
    # Startup: Connect to DB and Redis
    yield
    # Shutdown: Close connections cleanly
    print("TINAI Engine Shutting Down...")

app = FastAPI(
    title="TINAI Execution Layer",
    description="Adaptive AI Routing & Reliability API",
    version="1.0.0",
    lifespan=lifespan
)

@app.get("/health")
async def health_check():
    """
    Validates that the Control Plane is actively accepting requests.
    """
    return {
        "status": "healthy",
        "service": "TINAI API",
        "routing_engine": "standby"
    }

@app.get("/")
async def root():
    return {"message": "TINAI Control Plane is live."}