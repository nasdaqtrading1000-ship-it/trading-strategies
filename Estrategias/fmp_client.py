"""
Cliente comun para Financial Modeling Prep.

Evita que los errores impriman la API key completa en pantalla.
"""

import requests


def fmp_get_json(url, api_key, params=None, timeout=15):
    params = dict(params or {})
    params["apikey"] = api_key

    response = requests.get(
        url,
        params=params,
        timeout=timeout,
    )

    if response.status_code == 403:
        raise RuntimeError(
            "FMP 403 Forbidden. La clave no tiene permiso para este endpoint, "
            "esta mal configurada, ha superado el limite o el plan no lo permite."
        )

    if response.status_code == 401:
        raise RuntimeError(
            "FMP 401 Unauthorized. Revisa que FMP_API_KEY sea correcta."
        )

    if response.status_code == 429:
        raise RuntimeError(
            "FMP 429 Too Many Requests. Has superado el limite de peticiones."
        )

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(
            f"FMP HTTP {response.status_code}: {response.reason}"
        ) from exc

    return response.json()
