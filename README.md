# API de Entrecalles

Devuelve la **calle principal** y las **entrecalles** cercanas a una coordenada GPS,
usando datos de OpenStreetMap (Nominatim + Overpass).

## Correr en local

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

Docs interactivas: http://localhost:8000/docs

## Endpoint

```
GET /entrecalles?lat=23.14022&lon=-82.34938&radio_m=80
```

Respuesta (v4):

```json
{
  "direccion": "Plaza de Armas, Baratillo, Catedral, La Habana Vieja, ...",
  "calle_principal": "Baratillo",
  "entrecalles": ["O'Reilly", "Obispo"],
  "entre": { "calle_a": "O'Reilly", "calle_b": "Obispo" },
  "interseccion_mas_cercana": { "calles": ["Obispo"], "distancia_m": 26.88 },
  "distancia_a_calle_principal_m": 15.82,
  "distancia_a_interseccion_m": 26.88,
  "fuente": "topologia-nodos",
  "lat": 23.14022,
  "lon": -82.34938,
  "atribucion": "© OpenStreetMap contributors"
}
```

### Cómo funciona (v4)

- **Distancias en metros reales**: proyección Azimutal Equidistante (AEQD) centrada
  en el punto, no Web Mercator (que infla la distancia ~`1/cos(lat)`, p.ej. +8.6% en
  La Habana).
- **Intersecciones por topología**: dos vías se cruzan si comparten el mismo `node id`
  de OSM, no por "el vértice más cercano". Con respaldo por proximidad si faltan ids.
- **`entre`**: las dos transversales que te flanquean sobre la vía principal ("entre A y B").
- **`fuente`**: `topologia-nodos` (cruce real por nodo) o `proximidad-geometrica` (respaldo).

Colección Postman con tests automáticos: [`API-Entrecalles-v4.postman_collection.json`](API-Entrecalles-v4.postman_collection.json).

## Notas de uso (importante)

- Nominatim/Overpass son servicios **gratuitos y compartidos**. Poné un `User-Agent`
  real (ya está en `main.py`) y no abuses del rate limit.
- Para producción con volumen: montá tu propio Nominatim/Overpass, o usá un proveedor
  de pago. El endpoint público no garantiza SLA.
