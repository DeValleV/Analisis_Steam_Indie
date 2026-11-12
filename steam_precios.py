#!/usr/bin/env python3
"""
Extrae precios reales de la tienda de Steam para los appids del catalogo.

Uso tipico:
    py -3.12 steam_precios.py --only-indie --limit 500
    py -3.12 steam_precios.py --only-indie          # catalogo indie completo
    py -3.12 steam_precios.py --appids 10,570,730

Modos:
  bulk   (default) usa IStoreBrowseService/GetItems, que acepta hasta 200
         appids por peticion. El catalogo indie completo (~19.400 juegos)
         se resuelve en ~200 peticiones, sin toparse con el rate limit de
         store.steampowered.com.
  single usa store.steampowered.com/api/appdetails, un appid por peticion.
         Es el endpoint publico clasico; sirve como respaldo.

No requiere dependencias externas (solo biblioteca estandar), asi funciona
igual con Python 3.12 y 3.14. Es reanudable: si se interrumpe, al volver a
ejecutarlo salta los appids que ya tienen un resultado definitivo.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

STORE_URL = "https://store.steampowered.com/api/appdetails"
BULK_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"

# Estados definitivos: si un appid ya tiene uno de estos, no se vuelve a pedir.
FINAL_STATUS = {"ok", "gratis", "sin_precio", "no_encontrado"}

# Moneda por region (se intenta detectar dinamicamente; esto es respaldo).
CC_CURRENCY = {
    "cr": "CRC", "us": "USD", "mx": "MXN", "br": "BRL", "ar": "USD",
    "cl": "CLP", "co": "COP", "pe": "PEN", "es": "EUR", "fr": "EUR",
    "de": "EUR", "it": "EUR", "gb": "GBP", "ca": "CAD", "au": "AUD",
    "jp": "JPY", "kr": "KRW", "cn": "CNY", "in": "INR", "ru": "RUB",
}

# Limita la tasa global de peticiones.
_throttle_lock = threading.Lock()
_last_request = 0.0
MIN_INTERVAL = 0.25  # segundos entre peticiones, en total

# Motivos de error, para diagnostico al final.
REASONS: dict[str, int] = {}
_reasons_lock = threading.Lock()


def configure_stdout() -> None:
    """Evita UnicodeEncodeError al imprimir simbolos como el colon (U+20A1)."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def note_reason(reason: str) -> None:
    with _reasons_lock:
        REASONS[reason] = REASONS.get(reason, 0) + 1


def throttle() -> None:
    global _last_request
    with _throttle_lock:
        now = time.monotonic()
        wait = MIN_INTERVAL - (now - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


def http_get(url: str, timeout: int) -> tuple[int, bytes, float]:
    throttle()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "TEC-BigData-SteamPriceFetcher/1.0 (proyecto academico)",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
            return resp.status, raw, 0.0
    except urllib.error.HTTPError as exc:
        try:
            retry_after = float(exc.headers.get("Retry-After") or 0)
        except (TypeError, ValueError):
            retry_after = 0.0
        return exc.code, b"", retry_after


def get_json(url: str, timeout: int, retries: int,
             cooldown: float = 60.0) -> dict | None:
    """GET con reintentos. En 429/5xx espera una ventana completa de Steam."""
    delay = 1.0
    for attempt in range(1, retries + 1):
        try:
            status, raw, retry_after = http_get(url, timeout)
            if status == 429 or 500 <= status < 600:
                if attempt < retries:
                    wait = retry_after if retry_after > 0 else cooldown
                    print(f"  ... HTTP {status}; esperando {wait:.0f}s "
                          f"(intento {attempt}/{retries})")
                    time.sleep(wait + random.uniform(0, 2))
                    delay = min(delay * 2, 30)
                    continue
                note_reason(f"http_{status}")
                return None
            if status != 200:
                note_reason(f"http_{status}")
                return None
            return json.loads(raw)
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                json.JSONDecodeError, OSError) as exc:
            if attempt < retries:
                time.sleep(delay + random.uniform(0, 0.5))
                delay = min(delay * 2, 30)
            else:
                note_reason(type(exc).__name__)
                return None
    return None


def empty_row(appid: int, name: str, status: str = "error") -> dict:
    return {
        "appid": appid,
        "name": name,
        "currency": "",
        "initial": "",
        "final": "",
        "discount_percent": "",
        "final_formatted": "",
        "is_free": "",
        "status": status,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# Modo bulk: varios appids por peticion (recomendado)
# ---------------------------------------------------------------------------

def detect_currency(cc: str, lang: str, timeout: int) -> str:
    """Una peticion a appdetails para saber que moneda usa la region."""
    fallback = CC_CURRENCY.get(cc.lower(), "")
    for appid in (10, 1091500, 292030):
        url = f"{STORE_URL}?appids={appid}&cc={cc}&l={lang}&filters=price_overview"
        payload = get_json(url, timeout, retries=2)
        if not payload:
            continue
        price = (payload.get(str(appid)) or {}).get("data") or {}
        if isinstance(price, dict):
            po = price.get("price_overview") or {}
            if po.get("currency"):
                return po["currency"]
    return fallback


def bulk_fetch(batch: list[tuple[int, str]], cc: str, lang: str,
               currency: str, timeout: int, retries: int,
               cooldown: float) -> list[dict]:
    """Consulta un lote de appids. Devuelve una fila por appid del lote."""
    payload = {
        "ids": [{"appid": appid} for appid, _ in batch],
        "context": {"country_code": cc.upper(), "language": lang},
    }
    url = BULK_URL + "?input_json=" + urllib.parse.quote(json.dumps(payload))
    data = get_json(url, timeout, retries, cooldown)

    rows: dict[int, dict] = {}
    if data:
        for item in (data.get("response") or {}).get("store_items") or []:
            try:
                appid = int(item.get("appid") or item.get("id") or 0)
            except (TypeError, ValueError):
                appid = 0
            if appid:
                rows[appid] = item

    result: list[dict] = []
    for appid, catalog_name in batch:
        item = rows.get(appid)
        # Los appids invalidos vuelven como success=15 / visible=false / appid=0.
        if (not item or appid == 0 or item.get("success") not in (1, True)
                or item.get("visible") is False):
            result.append(empty_row(appid, catalog_name, "no_encontrado"))
            continue

        row = empty_row(appid, item.get("name") or catalog_name)
        po = item.get("best_purchase_option") or {}
        final = po.get("final_price_in_cents")

        if final not in (None, ""):
            row.update(
                currency=currency,
                initial=po.get("original_price_in_cents") or final,
                final=final,
                discount_percent=po.get("discount_pct", 0),
                final_formatted=po.get("formatted_final_price", ""),
                is_free="False",
                status="ok",
            )
        elif item.get("is_free") is True:
            row.update(currency=currency, initial=0, final=0,
                       discount_percent=0, is_free="True", status="gratis")
        else:
            row.update(is_free="False", status="sin_precio")
        result.append(row)
    return result


# ---------------------------------------------------------------------------
# Modo single: un appid por peticion (respaldo)
# ---------------------------------------------------------------------------

def single_fetch(appid: int, name: str, cc: str, lang: str, timeout: int,
                 retries: int, cooldown: float) -> dict:
    row = empty_row(appid, name)

    payload = get_json(
        f"{STORE_URL}?appids={appid}&cc={cc}&l={lang}&filters=price_overview",
        timeout, retries, cooldown)
    if payload is None:
        return row
    entry = payload.get(str(appid)) or {}
    if not entry.get("success"):
        row["status"] = "no_encontrado"
        return row

    data = entry.get("data")
    if not isinstance(data, dict) or not data:
        # data == [] ocurre con juegos gratis y con juegos retirados.
        payload = get_json(f"{STORE_URL}?appids={appid}&cc={cc}&l={lang}",
                           timeout, retries, cooldown)
        if payload is None:
            return row
        entry = payload.get(str(appid)) or {}
        if not entry.get("success"):
            row["status"] = "no_encontrado"
            return row
        data = entry.get("data")
        if not isinstance(data, dict):
            data = {}

    if data.get("name"):
        row["name"] = data["name"]

    price = data.get("price_overview")
    if isinstance(price, dict) and price.get("final") is not None:
        row.update(
            currency=price.get("currency", ""),
            initial=price.get("initial", ""),
            final=price.get("final", ""),
            discount_percent=price.get("discount_percent", ""),
            final_formatted=price.get("final_formatted", ""),
            is_free="False",
            status="ok",
        )
    elif data.get("is_free") is True:
        row.update(currency="CRC" if cc == "cr" else "", initial=0,
                   final=0, discount_percent=0, is_free="True",
                   status="gratis")
    else:
        row.update(is_free="False", status="sin_precio")
    return row


# ---------------------------------------------------------------------------
# Archivos y CLI
# ---------------------------------------------------------------------------

FIELDS = ["appid", "name", "currency", "initial", "final",
          "discount_percent", "final_formatted", "is_free", "status",
          "fetched_at"]


def is_empty_file(path: str) -> bool:
    """Un archivo con solo saltos de linea/BOM cuenta como vacio."""
    if not os.path.exists(path):
        return True
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return not fh.read().strip()
    except OSError:
        return True


def read_appids(args) -> list[tuple[int, str]]:
    if args.appids:
        return [(int(x.strip()), "") for x in args.appids.split(",") if x.strip()]

    rows: list[tuple[int, str]] = []
    seen: set[int] = set()
    with open(args.input, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            try:
                appid = int(row["appid"])
            except (KeyError, TypeError, ValueError):
                continue
            if args.only_indie:
                genres = [g.strip().lower() for g in (row.get("genres") or "").split(";")]
                tags = [t.strip().lower() for t in (row.get("steamspy_tags") or "").split(";")]
                if "indie" not in genres and "indie" not in tags:
                    continue
            if appid in seen:
                continue
            seen.add(appid)
            rows.append((appid, row.get("name", "")))
    return rows


def already_done(output: str) -> set[int]:
    done: set[int] = set()
    if is_empty_file(output):
        return done
    try:
        with open(output, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                try:
                    if row.get("status") in FINAL_STATUS:
                        done.add(int(row["appid"]))
                except (TypeError, ValueError):
                    continue
    except (OSError, csv.Error):
        pass
    return done


def main() -> int:
    global MIN_INTERVAL
    configure_stdout()
    parser = argparse.ArgumentParser(
        description="Descarga precios reales de Steam para los appids del catalogo.")
    parser.add_argument("--input", default="steam.csv")
    parser.add_argument("--output", default="steam_precios_reales.csv")
    parser.add_argument("--cc", default="cr", help="pais/region (default: cr)")
    parser.add_argument("--lang", default="spanish")
    parser.add_argument("--appids", default="", help="lista manual: 10,570,730")
    parser.add_argument("--only-indie", action="store_true",
                        help="solo juegos con genero/tag Indie")
    parser.add_argument("--limit", type=int, default=0,
                        help="maximo de appids (0 = todos)")
    parser.add_argument("--mode", choices=("bulk", "single"), default="bulk",
                        help="bulk = varios appids por peticion (default); "
                             "single = appdetails clasico")
    parser.add_argument("--batch-size", type=int, default=100,
                        help="appids por peticion en modo bulk (max 200)")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--min-interval", type=float, default=MIN_INTERVAL,
                        help="segundos minimos entre peticiones (default: 0.25)")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=6)
    parser.add_argument("--cooldown", type=float, default=60.0,
                        help="segundos de espera ante HTTP 429 (default: 60)")
    parser.add_argument("--force", action="store_true",
                        help="ignora el progreso previo y vuelve a pedir todo")
    args = parser.parse_args()

    MIN_INTERVAL = max(0.0, args.min_interval)
    args.batch_size = max(1, min(args.batch_size, 200))

    appids = read_appids(args)
    if args.limit:
        appids = appids[:args.limit]
    if not appids:
        print("No hay appids para procesar.", file=sys.stderr)
        return 1

    done = set() if args.force else already_done(args.output)
    pending = [(a, n) for a, n in appids if a not in done]
    done_in_scope = len(appids) - len(pending)

    print(f"Catalogo: {len(appids)} appids | ya descargados: {done_in_scope} | "
          f"pendientes: {len(pending)}")
    print(f"Region: {args.cc} | modo: {args.mode} | workers: {args.workers} | "
          f"salida: {args.output}")

    currency = ""
    if args.mode == "bulk" and pending:
        currency = CC_CURRENCY.get(args.cc.lower(), "")
        if not currency:
            currency = detect_currency(args.cc, args.lang, args.timeout)
        print(f"Moneda para {args.cc}: {currency or '(desconocida)'}")

    first_write = args.force or is_empty_file(args.output)
    encoding = "utf-8-sig" if first_write else "utf-8"
    stats: dict[str, int] = {}
    t0 = time.time()

    with open(args.output, "w" if first_write else "a",
              newline="", encoding=encoding) as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        if first_write:
            writer.writeheader()
            fh.flush()

        def write_rows(rows: list[dict]) -> None:
            for row in rows:
                writer.writerow(row)
                stats[row["status"]] = stats.get(row["status"], 0) + 1
            fh.flush()

        if args.mode == "bulk":
            batches = [pending[i:i + args.batch_size]
                       for i in range(0, len(pending), args.batch_size)]
            print(f"Lotes: {len(batches)} de hasta {args.batch_size} appids")
            done_count = 0
            with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
                futures = {
                    pool.submit(bulk_fetch, batch, args.cc, args.lang, currency,
                                args.timeout, args.retries, args.cooldown): batch
                    for batch in batches
                }
                for fut in as_completed(futures):
                    batch = futures[fut]
                    try:
                        rows = fut.result()
                    except Exception as exc:
                        print(f"  ! lote fallido: {type(exc).__name__}: {exc}")
                        rows = [empty_row(a, n) for a, n in batch]
                    write_rows(rows)
                    done_count += len(rows)
                    elapsed = time.time() - t0
                    rate = done_count / elapsed if elapsed else 0
                    eta = (len(pending) - done_count) / rate if rate else 0
                    print(f"  {done_count}/{len(pending)} | {rate:.1f} appids/s | "
                          f"ETA {eta / 60:.1f} min | {stats}")
        else:
            with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
                futures = {
                    pool.submit(single_fetch, appid, name, args.cc, args.lang,
                                args.timeout, args.retries, args.cooldown): appid
                    for appid, name in pending
                }
                for i, fut in enumerate(as_completed(futures), start=1):
                    try:
                        write_rows([fut.result()])
                    except Exception as exc:
                        print(f"  ! appid {futures[fut]}: {type(exc).__name__}: {exc}")
                        write_rows([empty_row(futures[fut], "")])
                    if i % 50 == 0 or i == len(pending):
                        elapsed = time.time() - t0
                        rate = i / elapsed if elapsed else 0
                        eta = (len(pending) - i) / rate if rate else 0
                        print(f"  {i}/{len(pending)} | {rate:.1f} appids/s | "
                              f"ETA {eta / 60:.0f} min | {stats}")

    print(f"Listo en {(time.time() - t0) / 60:.1f} min. Resultados: {stats}")
    if REASONS:
        print(f"Errores por causa: {REASONS}")
    print(f"Archivo: {os.path.abspath(args.output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
