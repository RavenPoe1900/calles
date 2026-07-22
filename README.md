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

Respuesta:

```json
{
  "direccion": "Plaza de Armas, Baratillo, Catedral, La Habana Vieja, ...",
  "calle_principal": "Baratillo",
  "entrecalles": ["Enna", "O'Reilly", "Obispo", "Tacón"],
  "lat": 23.14022,
  "lon": -82.34938
}
```

## Notas de uso (importante)

- Nominatim/Overpass son servicios **gratuitos y compartidos**. Poné un `User-Agent`
  real (ya está en `main.py`) y no abuses del rate limit.
- Para producción con volumen: montá tu propio Nominatim/Overpass, o usá un proveedor
  de pago. El endpoint público no garantiza SLA.
