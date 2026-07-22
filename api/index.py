"""
API de Entrecalles por Coordenada (Versión Topológica).

Dado un punto (lat, lon) devuelve:
  - direccion: dirección completa (Nominatim)
  - calle_principal: la calle sobre la que cae el punto
  - entrecalles: las calles que CRUZAN realmente en la intersección más cercana

Uso de datos: © OpenStreetMap contributors.
"""

import asyncio
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from shapely.geometry import Point, LineString
from shapely.ops import transform
import pyproj

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

HEADERS = {"User-Agent": "EntreCallesAPI/2.0 (contacto: karlitafq24@gmail.com)"}

app = FastAPI(
    title="API de Entrecalles",
    description="Devuelve calle principal y entrecalles reales usando topología de intersecciones (nodos).",
    version="3.0.0",
)

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
    distancia_a_interseccion_m: float | None = None


# Transformador de Grados (WGS84) a Metros (Web Mercator)
project_to_meters = pyproj.Transformer.from_crs(
    "epsg:4326", "epsg:3857", always_xy=True
).transform


async def _reverse_geocode(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    params = {"lat": lat, "lon": lon, "format": "json", "addressdetails": 1}
    r = await client.get(NOMINATIM_URL, params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


async def _calles_topologia_exacta(
    client: httpx.AsyncClient, lat: float, lon: float, radio_busqueda_m: int = 60
) -> tuple[str, list[str], float]:
    """
    Lógica topológica para encontrar entrecalles reales:
    1. Encuentra la calle más cercana (principal).
    2. Encuentra el NODO (esquina) de esa calle más cercano al punto GPS.
    3. Identifica qué otras calles comparten ese nodo (entrecalles exactas).
    """
    query = f"""
    [out:json][timeout:25];
    way(around:{radio_busqueda_m},{lat},{lon})["highway"]["name"];
    out geom;
    """
    
    punto_metros = transform(project_to_meters, Point(lon, lat))
    ways = []
    ultimo_error: Exception | None = None

    for url in OVERPASS_MIRRORS:
        try:
            r = await client.post(url, data=query, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()

            for el in data.get("elements", []):
                if el.get("type") != "way" or "geometry" not in el or "tags" not in el:
                    continue

                nombre = el["tags"].get("name", "").strip()
                if not nombre or nombre.lower() in ["sin nombre", "unnamed"]:
                    continue

                coords = el["geometry"]
                if not coords or len(coords) < 2:
                    continue

                try:
                    # Calcular distancia perpendicular a la calle
                    linea_wgs84 = LineString([(c["lon"], c["lat"]) for c in coords])
                    linea_metros = transform(project_to_meters, linea_wgs84)
                    dist_m = linea_metros.distance(punto_metros)
                    
                    ways.append({
                        "id": el["id"],
                        "name": nombre,
                        "distance_m": dist_m,
                        "geometry": coords
                    })
                except Exception:
                    continue

            if ways: # Si encontramos datos, rompemos el loop de mirrors
                break
                
        except httpx.HTTPError as e:
            ultimo_error = e
            await asyncio.sleep(0.5)
            continue

    if not ways:
        raise HTTPException(
            status_code=502,
            detail=f"Servidores Overpass sin respuesta. Último error: {ultimo_error}",
        )

    # 1. La calle con menor distancia perpendicular es la principal
    ways.sort(key=lambda x: x["distance_m"])
    calle_principal = ways[0]["name"]
    distancia_principal = round(ways[0]["distance_m"], 2)
    main_geom = ways[0]["geometry"]
    main_id = ways[0]["id"]

    # 2. Encontrar el NODO de la calle principal más cercano al punto del usuario
    # Este nodo representa la intersección (esquina) más próxima a donde estás parado
    target_node_m = None
    min_node_dist = float('inf')

    for coord in main_geom:
        node_pt = Point(coord["lon"], coord["lat"])
        node_pt_m = transform(project_to_meters, node_pt)
        d = punto_metros.distance(node_pt_m)
        if d < min_node_dist:
            min_node_dist = d
            target_node_m = node_pt_m

    entrecalles = set()
    tolerance_m = 8.0 # 8 metros de tolerancia para compensar desalineaciones en OSM

    # 3. Buscar otras calles que compartan este nodo de intersección
    for way in ways:
        if way["id"] == main_id:
            continue # Ignorar la propia calle principal
        
        for coord in way["geometry"]:
            other_node_pt = Point(coord["lon"], coord["lat"])
            other_node_pt_m = transform(project_to_meters, other_node_pt)
            
            # Si un nodo de otra calle está a menos de 8m del nodo de intersección principal
            if other_node_pt_m.distance(target_node_m) < tolerance_m:
                entrecalles.add(way["name"])
                break # Ya confirmamos que esta calle cruza aquí, pasar a la siguiente

    # 4. Fallback geométrico: Si OSM tiene los nodos mal unidos, 
    # usamos la distancia perpendicular como red de seguridad (< 45m)
    if not entrecalles:
        for way in ways:
            if way["name"] != calle_principal and way["distance_m"] < 45.0:
                entrecalles.add(way["name"])

    return calle_principal, sorted(list(entrecalles)), round(min_node_dist, 2)


@app.get("/entrecalles", response_model=EntrecallesResponse)
async def obtener_entrecalles(
    lat: float = Query(..., ge=-90, le=90, description="Latitud"),
    lon: float = Query(..., ge=-180, le=180, description="Longitud"),
    radio_m: int = Query(60, ge=20, le=150, description="Radio de búsqueda en metros"),
):
    async with httpx.AsyncClient() as client:
        try:
            geo = await _reverse_geocode(client, lat, lon)
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Error en Nominatim: {e}")

        direccion = geo.get("display_name", "No disponible")
        calle_principal, entrecalles, dist_interseccion = await _calles_topologia_exacta(
            client, lat, lon, radio_m
        )

    return EntrecallesResponse(
        direccion=direccion,
        calle_principal=calle_principal,
        entrecalles=entrecalles,
        lat=lat,
        lon=lon,
        distancia_a_interseccion_m=dist_interseccion,
    )


@app.get("/health")
async def health():
    return {"status": "ok", "version": "3.0.0 (Topológica)"}