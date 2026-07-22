"""
API de Entrecalles por Coordenada (v5 - Topología por nodos, métrica real y
bracketing por rumbo).

Dado un punto (lat, lon) devuelve:
  - direccion:        dirección textual (Nominatim, reverse geocoding).
  - calle_principal:  la vía sobre la que cae el punto (distancia + jerarquía vial).
  - entre:            las dos transversales que te FLANQUEAN ("entre A y B").
  - entrecalles:      unión de esas transversales (compatibilidad).
  - interseccion_mas_cercana: la esquina más próxima y qué calles la forman.

Diseño geoespacial (por qué cada decisión):

  1. Métrica REAL — proyección Azimutal Equidistante (AEQD) centrada en el punto.
     Web Mercator infla la distancia ~1/cos(lat) (+8.6% en La Habana, >30% en
     latitudes altas); AEQD da metros verdaderos al mm en el entorno local.

  2. Intersecciones por TOPOLOGÍA — dos vías se cruzan si comparten el mismo
     `node id` de OSM. No "el vértice más cercano" (que suele ser un nodo de
     curva a mitad de cuadra). Respaldo por proximidad si faltaran los ids.

  3. Radio DESACOPLADO — la calle principal se detecta en un radio corto
     (donde de verdad estás parado), pero las intersecciones que te acotan se
     buscan en un anillo ancho, porque la próxima esquina puede estar a más de
     una cuadra. Sin esto, "entre A y B" devuelve solo un lado.

  4. Principal por DISTANCIA + JERARQUÍA — con error de GPS (5-20 m urbano) un
     callejón `service` puede quedar más cerca que la avenida sobre la que
     realmente vas. Dentro de una banda de tolerancia manda la clase de vía.

  5. Bracketing por RUMBO LOCAL — el lado "anterior/siguiente" se decide con la
     dirección local de la principal y una proyección con signo, no fusionando
     segmentos (cuyo orden interno no es monótono a lo largo de la calle).

  6. Transversal por PERPENDICULARIDAD — en una esquina con varias calles, la
     que "cruza" es la de rumbo más perpendicular a la principal, no la primera
     alfabéticamente.

  7. Nominatim y Overpass en paralelo; degradación elegante si Nominatim falla;
     filtro de tipos de `highway` no transitables.

Uso de datos: © OpenStreetMap contributors.
"""

import asyncio
import math
from functools import lru_cache

import httpx
import pyproj
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from shapely.geometry import Point, LineString
from shapely.ops import transform

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

HEADERS = {"User-Agent": "EntreCallesAPI/5.0 (contacto: karlitafq24@gmail.com)"}
ATRIBUCION = "© OpenStreetMap contributors"

# Valores de highway que NO son vías transitables (no cuentan como "calle").
HIGHWAY_EXCLUIDOS = {
    "bus_stop", "platform", "services", "rest_area", "construction",
    "proposed", "raceway", "street_lamp", "elevator", "corridor",
    "escape", "planned", "razed", "abandoned", "disused",
}

# Jerarquía vial: a mayor rango, más "importante" es la calle (más probable que
# sea la que una persona nombraría al ubicarse). Default = 3 si no está listada.
HIGHWAY_RANK = {
    "motorway": 9, "trunk": 9, "motorway_link": 8, "trunk_link": 8,
    "primary": 8, "primary_link": 7, "secondary": 7, "secondary_link": 6,
    "tertiary": 6, "tertiary_link": 5, "unclassified": 4, "residential": 4,
    "living_street": 3, "pedestrian": 3, "road": 3, "busway": 3,
    "service": 2, "track": 1, "footway": 1, "path": 1, "cycleway": 1, "steps": 0,
}
RANK_DEFAULT = 3

# Parámetros geométricos (todos en METROS REALES sobre la proyección AEQD).
FETCH_EXTRA_M = 140      # anillo extra para capturar las esquinas que te acotan.
FETCH_MAX_M = 320        # tope duro del radio de descarga (coste Overpass).
CLASS_TOL_M = 12.0       # banda dentro de la cual manda la jerarquía, no la distancia.
NODE_SNAP_M = 2.0        # snap para "mismo nodo" si Overpass no diera ids.
TOL_FALLBACK_M = 35.0    # red de seguridad geométrica si no hay topología usable.
EPS_M = 0.5              # zona muerta alrededor del punto para el signo del lado.

app = FastAPI(
    title="API de Entrecalles",
    description=(
        "Calle principal y entrecalles reales a partir de una coordenada GPS, "
        "usando topología de intersecciones (nodos OSM), distancias en metros "
        "reales (AEQD) y bracketing por rumbo local."
    ),
    version="5.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


class TransversalLado(BaseModel):
    calle: str | None = None
    distancia_m: float | None = None
    calles_en_esquina: list[str] = []


class Interseccion(BaseModel):
    calles: list[str]
    distancia_m: float


class EntreCalles(BaseModel):
    calle_a: str | None = None
    calle_b: str | None = None
    lado_a: TransversalLado = TransversalLado()
    lado_b: TransversalLado = TransversalLado()


class EntrecallesResponse(BaseModel):
    direccion: str
    calle_principal: str
    entrecalles: list[str]
    entre: EntreCalles
    interseccion_mas_cercana: Interseccion | None = None
    distancia_a_calle_principal_m: float
    distancia_a_interseccion_m: float | None = None
    fuente: str
    advertencia: str | None = None
    lat: float
    lon: float
    atribucion: str = ATRIBUCION


# --------------------------------------------------------------------------- #
# Proyección métrica local: AEQD centrada en el punto (metros reales al mm).
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=512)
def _aeqd_transform(lat_key: float, lon_key: float):
    proj_aeqd = pyproj.Proj(
        proj="aeqd", lat_0=lat_key, lon_0=lon_key, datum="WGS84", units="m"
    )
    transformer = pyproj.Transformer.from_proj(
        pyproj.Proj("epsg:4326"), proj_aeqd, always_xy=True
    )
    return transformer.transform


def _get_projector(lat: float, lon: float):
    # Redondeo a 4 decimales (~11 m): comparte transformador entre puntos vecinos.
    return _aeqd_transform(round(lat, 4), round(lon, 4))


# --------------------------------------------------------------------------- #
# Geocodificación inversa (Nominatim). No bloquea la topología si falla.
# --------------------------------------------------------------------------- #
async def _reverse_geocode(client: httpx.AsyncClient, lat: float, lon: float) -> str:
    params = {"lat": lat, "lon": lon, "format": "json", "addressdetails": 1}
    try:
        r = await client.get(NOMINATIM_URL, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.json().get("display_name", "No disponible")
    except (httpx.HTTPError, ValueError):
        return "No disponible"


# --------------------------------------------------------------------------- #
# Descarga de vías desde Overpass (con failover entre mirrors).
# --------------------------------------------------------------------------- #
async def _fetch_overpass(
    client: httpx.AsyncClient, lat: float, lon: float, radio_m: int
) -> list[dict]:
    query = f"""
    [out:json][timeout:25];
    way(around:{radio_m},{lat},{lon})["highway"]["name"];
    out geom;
    """
    ultimo_error: Exception | None = None
    for url in OVERPASS_MIRRORS:
        try:
            r = await client.post(url, data=query, headers=HEADERS, timeout=30)
            r.raise_for_status()
            elements = r.json().get("elements", [])
            if elements:
                return elements
        except (httpx.HTTPError, ValueError) as e:
            ultimo_error = e
            await asyncio.sleep(0.4)
            continue
    raise HTTPException(
        status_code=502,
        detail=f"Servidores Overpass sin respuesta. Último error: {ultimo_error}",
    )


def _normalizar_ways(elements: list[dict], project) -> list[dict]:
    """Elementos crudos de Overpass -> vías con geometría proyectada (metros).

    `nodes` (ids OSM) y `geometry` (coords) vienen como arrays paralelos; los
    conservamos alineados con las coordenadas ya proyectadas a AEQD.
    """
    ways: list[dict] = []
    for el in elements:
        if el.get("type") != "way" or "geometry" not in el or "tags" not in el:
            continue

        tags = el["tags"]
        highway = tags.get("highway")
        if highway in HIGHWAY_EXCLUIDOS:
            continue

        nombre = tags.get("name", "").strip()
        if not nombre or nombre.lower() in ("sin nombre", "unnamed"):
            continue

        coords = el["geometry"]
        if not coords or len(coords) < 2:
            continue

        node_ids = el.get("nodes") or []
        if len(node_ids) != len(coords):
            node_ids = [None] * len(coords)

        try:
            linea_m = transform(project, LineString([(c["lon"], c["lat"]) for c in coords]))
            xy = list(linea_m.coords)  # [(x, y), ...] alineado con node_ids
        except Exception:
            continue

        ways.append(
            {
                "id": el["id"],
                "name": nombre,
                "highway": highway,
                "rank": HIGHWAY_RANK.get(highway, RANK_DEFAULT),
                "node_ids": node_ids,
                "xy": xy,
                "line_m": linea_m,
            }
        )
    return ways


def _clave_nodo(node_id, x: float, y: float):
    """Identidad de nodo: id OSM, o coordenada AEQD snapeada a NODE_SNAP_M."""
    if node_id is not None:
        return ("id", node_id)
    return ("xy", round(x / NODE_SNAP_M), round(y / NODE_SNAP_M))


def _direccion_local(principal_ways: list[dict], px: float, py: float):
    """Vector unitario de la dirección de la principal en el segmento más cercano
    al punto. Es el eje contra el que se mide "adelante/atrás" (bracketing)."""
    mejor = None  # (dist2_al_segmento, (ux, uy))
    for w in principal_ways:
        pts = w["xy"]
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            dx, dy = bx - ax, by - ay
            long2 = dx * dx + dy * dy
            if long2 == 0:
                continue
            # Proyección del punto sobre el segmento [a, b], recortada a [0, 1].
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / long2))
            fx, fy = ax + t * dx, ay + t * dy
            d2 = (px - fx) ** 2 + (py - fy) ** 2
            if mejor is None or d2 < mejor[0]:
                norm = math.sqrt(long2)
                mejor = (d2, (dx / norm, dy / norm))
    return mejor[1] if mejor else (1.0, 0.0)


def _rumbo_way_en_nodo(way: dict, idx: int):
    """Vector unitario de la vía a su paso por el nodo de índice `idx`."""
    pts = way["xy"]
    i0 = max(0, idx - 1)
    i1 = min(len(pts) - 1, idx + 1)
    ax, ay = pts[i0]
    bx, by = pts[i1]
    dx, dy = bx - ax, by - ay
    norm = math.hypot(dx, dy)
    return (dx / norm, dy / norm) if norm else None


def _analizar_topologia(ways: list[dict], punto_m: Point, radio_principal: float):
    """Núcleo geoespacial. Devuelve un dict con todos los campos de salida."""
    px, py = punto_m.x, punto_m.y

    # 1. Distancia perpendicular real de cada vía al punto.
    for w in ways:
        w["dist"] = w["line_m"].distance(punto_m)

    # 2. Principal = dentro de una banda de tolerancia, la de mayor jerarquía;
    #    desempate por menor distancia. Solo se consideran vías "cercanas".
    cercanas = [w for w in ways if w["dist"] <= radio_principal] or ways
    min_d = min(w["dist"] for w in cercanas)
    banda = [w for w in cercanas if w["dist"] <= min_d + CLASS_TOL_M]
    principal_way = max(banda, key=lambda w: (w["rank"], -w["dist"]))
    principal = principal_way["name"]

    principal_ways = [w for w in ways if w["name"] == principal]
    dist_principal = round(min(w["dist"] for w in principal_ways), 2)

    # 3. Índice nodo -> {nombres}; y conjunto de nodos de la principal.
    nodo_a_calles: dict = {}
    nodos_principal: set = set()
    for w in ways:
        for node_id, (x, y) in zip(w["node_ids"], w["xy"]):
            clave = _clave_nodo(node_id, x, y)
            nodo_a_calles.setdefault(clave, set()).add(w["name"])
            if w["name"] == principal:
                nodos_principal.add(clave)

    # 4. Dirección local de la principal (eje del bracketing).
    ux, uy = _direccion_local(principal_ways, px, py)

    # 5. Intersecciones reales sobre la principal: nodos por los que pasa además
    #    otra calle con nombre distinto. Para cada una: posición con signo sobre
    #    el eje, distancia euclídea y la transversal más perpendicular.
    intersecciones = []  # {s, dist, cruces:set, calle_perp:str, xy}
    vistos = set()
    for w in ways:
        for idx, (node_id, (x, y)) in enumerate(zip(w["node_ids"], w["xy"])):
            clave = _clave_nodo(node_id, x, y)
            if clave not in nodos_principal or clave in vistos:
                continue
            cruces = {n for n in nodo_a_calles.get(clave, set()) if n != principal}
            if not cruces:
                continue
            vistos.add(clave)

            s = (x - px) * ux + (y - py) * uy          # proyección con signo
            dist = math.hypot(x - px, y - py)

            # Transversal "que cruza" = la de rumbo más perpendicular a la principal.
            calle_perp, mejor_perp = None, -1.0
            for otra in ways:
                if otra["name"] == principal or otra["name"] not in cruces:
                    continue
                for j, (oid, (ox, oy)) in enumerate(zip(otra["node_ids"], otra["xy"])):
                    if _clave_nodo(oid, ox, oy) != clave:
                        continue
                    v = _rumbo_way_en_nodo(otra, j)
                    if v is None:
                        continue
                    perp = abs(ux * v[1] - uy * v[0])  # |sin(ángulo)| ∈ [0, 1]
                    if perp > mejor_perp:
                        mejor_perp, calle_perp = perp, otra["name"]
            if calle_perp is None:
                calle_perp = sorted(cruces)[0]

            intersecciones.append(
                {"s": s, "dist": dist, "cruces": cruces, "calle_perp": calle_perp}
            )

    entre = EntreCalles()
    interseccion_cercana: Interseccion | None = None
    dist_interseccion: float | None = None
    entrecalles: set = set()

    if intersecciones:
        # 5a. Esquina más cercana (euclídea).
        mas_cercana = min(intersecciones, key=lambda it: it["dist"])
        dist_interseccion = round(mas_cercana["dist"], 2)
        interseccion_cercana = Interseccion(
            calles=sorted(mas_cercana["cruces"]), distancia_m=dist_interseccion
        )

        # 5b. Bracketing por signo: transversal más próxima detrás (s<0) y delante (s>0).
        detras = [it for it in intersecciones if it["s"] < -EPS_M]
        delante = [it for it in intersecciones if it["s"] > EPS_M]
        lado_a = max(detras, key=lambda it: it["s"], default=None)  # s más cercano a 0
        lado_b = min(delante, key=lambda it: it["s"], default=None)

        if lado_a:
            entre.calle_a = lado_a["calle_perp"]
            entre.lado_a = TransversalLado(
                calle=lado_a["calle_perp"],
                distancia_m=round(abs(lado_a["s"]), 2),
                calles_en_esquina=sorted(lado_a["cruces"]),
            )
            entrecalles |= lado_a["cruces"]
        if lado_b:
            entre.calle_b = lado_b["calle_perp"]
            entre.lado_b = TransversalLado(
                calle=lado_b["calle_perp"],
                distancia_m=round(abs(lado_b["s"]), 2),
                calles_en_esquina=sorted(lado_b["cruces"]),
            )
            entrecalles |= lado_b["cruces"]

        # Si el punto quedó fuera de toda esquina por un lado, al menos reportar
        # la más cercana como entrecalle.
        if not entrecalles:
            entrecalles |= mas_cercana["cruces"]

        fuente = "topologia-nodos"
    else:
        # 6. Fallback geométrico: sin topología usable, aproximar por cercanía real.
        for w in ways:
            if w["name"] != principal and w["dist"] < TOL_FALLBACK_M:
                entrecalles.add(w["name"])
        fuente = "proximidad-geometrica"

    advertencia = None
    if dist_principal > 30.0:
        advertencia = (
            f"El punto está a {dist_principal} m de la calle más próxima; "
            "el resultado es aproximado (posible interior de manzana o zona sin mapear)."
        )

    return {
        "calle_principal": principal,
        "entrecalles": sorted(entrecalles),
        "entre": entre,
        "interseccion_mas_cercana": interseccion_cercana,
        "distancia_a_calle_principal_m": dist_principal,
        "distancia_a_interseccion_m": dist_interseccion,
        "fuente": fuente,
        "advertencia": advertencia,
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/entrecalles", response_model=EntrecallesResponse)
async def obtener_entrecalles(
    lat: float = Query(..., ge=-90, le=90, description="Latitud (WGS84)"),
    lon: float = Query(..., ge=-180, le=180, description="Longitud (WGS84)"),
    radio_m: int = Query(
        60, ge=20, le=200,
        description="Radio para detectar la calle principal (m). Las intersecciones "
                    "que acotan se buscan en un anillo más ancho automáticamente.",
    ),
):
    project = _get_projector(lat, lon)
    punto_m = transform(project, Point(lon, lat))

    # Radio de descarga desacoplado: ancho para capturar las esquinas que acotan.
    radio_fetch = min(radio_m + FETCH_EXTRA_M, FETCH_MAX_M)

    async with httpx.AsyncClient() as client:
        direccion, elements = await asyncio.gather(
            _reverse_geocode(client, lat, lon),
            _fetch_overpass(client, lat, lon, radio_fetch),
        )

    ways = _normalizar_ways(elements, project)
    if not ways:
        raise HTTPException(
            status_code=404,
            detail="No se encontraron calles con nombre en el radio indicado.",
        )

    r = _analizar_topologia(ways, punto_m, float(radio_m))

    return EntrecallesResponse(
        direccion=direccion,
        lat=lat,
        lon=lon,
        **r,
    )


@app.get("/health")
async def health():
    return {"status": "ok", "version": "5.0.0 (Topología + AEQD + rumbo)"}
