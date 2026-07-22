"""
API de Entrecalles por Coordenada.

Dado un punto (lat, lon) devuelve:
  - direccion: dirección completa (Nominatim / OpenStreetMap)
  - calle_principal: la calle sobre la que cae el punto
  - entrecalles: calles cercanas / cruces

Uso de datos: © OpenStreetMap contributors.

Este es el punto de entrada que ejecuta Vercel como función serverless.
"""

import asyncio

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"

# El servidor público de Overpass da 504/429 de forma intermitente.
# Probamos varios espejos en orden hasta que uno responda.
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

# Identifícate: Nominatim/Overpass exigen un User-Agent real de contacto.
HEADERS = {"User-Agent": "EntreCallesAPI/1.0 (contacto: karlitafq24@gmail.com)"}

app = FastAPI(
    title="API de Entrecalles",
    description="Devuelve calle principal y entrecalles a partir de una coordenada GPS (OpenStreetMap).",
    version="1.0.0",
)

# Permite que un frontend/cliente web lo consuma. Restringí allow_origins en prod.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


class EntrecallesResponse(BaseModel):
    direccion: str
    calle_principal: str
    entrecalles: list[str]
    lat: float
    lon: float


async def _reverse_geocode(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    params = {"lat": lat, "lon": lon, "format": "json", "addressdetails": 1}
    r = await client.get(NOMINATIM_URL, params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


async def _calles_cercanas(
    client: httpx.AsyncClient, lat: float, lon: float, radio_m: int
) -> list[str]:
    """Todas las calles con nombre dentro del radio, vía Overpass.

    Recorre los espejos en orden; si uno falla (504/429/timeout) prueba el
    siguiente. Solo se rinde si todos fallan.
    """
    query = f"""
    [out:json][timeout:25];
    way(around:{radio_m},{lat},{lon})["highway"]["name"];
    out tags;
    """
    ultimo_error: Exception | None = None
    for url in OVERPASS_MIRRORS:
        try:
            r = await client.post(url, data=query, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()
            nombres = {
                el["tags"]["name"]
                for el in data.get("elements", [])
                if el.get("tags", {}).get("name")
            }
            return sorted(nombres)
        except httpx.HTTPError as e:
            ultimo_error = e
            await asyncio.sleep(0.5)  # pequeña pausa antes del siguiente espejo
            continue
    raise HTTPException(
        status_code=502,
        detail=f"Todos los servidores Overpass fallaron. Último error: {ultimo_error}",
    )


@app.get("/entrecalles", response_model=EntrecallesResponse)
async def entrecalles(
    lat: float = Query(..., ge=-90, le=90, description="Latitud"),
    lon: float = Query(..., ge=-180, le=180, description="Longitud"),
    radio_m: int = Query(80, ge=10, le=500, description="Radio de búsqueda en metros"),
):
    async with httpx.AsyncClient() as client:
        try:
            geo = await _reverse_geocode(client, lat, lon)
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Error consultando Nominatim: {e}")

        address = geo.get("address", {})
        direccion = geo.get("display_name", "No disponible")
        calle_principal = address.get(
            "road", address.get("pedestrian", "No identificada")
        )

        todas = await _calles_cercanas(client, lat, lon, radio_m)

    entrecalles = [c for c in todas if c != calle_principal]

    return EntrecallesResponse(
        direccion=direccion,
        calle_principal=calle_principal,
        entrecalles=entrecalles,
        lat=lat,
        lon=lon,
    )


@app.get("/health")
async def health():
    return {"status": "ok"}
