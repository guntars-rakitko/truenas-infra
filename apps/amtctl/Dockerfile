FROM python:3.13-alpine

WORKDIR /app

# Deps: FastAPI + uvicorn, httpx for async + DigestAuth, PyYAML for nodes.yaml.
# Pin minor versions to avoid surprise upgrades on rebuild.
RUN pip install --no-cache-dir \
    fastapi==0.118.* \
    "uvicorn[standard]==0.35.*" \
    "httpx==0.28.*" \
    "PyYAML==6.0.*"

COPY amt.py main.py ./
COPY web ./web

EXPOSE 8000

# `main:app` → main.py module, `app` FastAPI object
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000",
     "--log-level", "info"]
