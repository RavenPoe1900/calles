"""Punto de entrada para desarrollo local.

En local seguí usando:

    uvicorn main:app --reload

El código real de la API vive en `api/index.py`, que es también el
punto de entrada que ejecuta Vercel como función serverless.
"""

from api.index import app  # noqa: F401
