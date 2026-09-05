"""
Backend simples para consultar precos/ofertas do Royal Supermercados.

Rode:
    python royal_prices_api.py

Endpoints:
    GET /health
    GET /royal/precos
    GET /royal/precos?q=arroz
    GET /royal/precos?limit=20
"""

import os
import time
from typing import Optional

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS


load_dotenv()

app = Flask(__name__)
CORS(app)


class RoyalClient:
    SITE_URL = "https://www.royalsupermercados.com.br"
    API_ROOT = "https://services.vipcommerce.com.br/api-admin/v1"
    PUBLIC_AUTH_KEY = "df072f85df9bf7dd71b6811c34bdbaa4f219d98775b56cff9dfa5f8ca1bf8469"
    PUBLIC_USERNAME = "loja"
    FILIAL_ID = "2"
    CD_ID = "1"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0",
                "Origin": self.SITE_URL,
                "Referer": f"{self.SITE_URL}/ofertas",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        self.organization_id = ""
        self.domain_key = ""
        self.token = ""
        self.token_created_at = 0.0
        self.cache = {"created_at": 0.0, "items": []}
        self.cache_ttl = int(os.getenv("ROYAL_CACHE_TTL_SECONDS", "900"))

    def _api_url(self, path: str) -> str:
        return f"{self.API_ROOT}/org/{self.organization_id}{path}"

    def _headers(self) -> dict:
        headers = {
            "DomainKey": self.domain_key,
            "OrganizationId": self.organization_id,
            "FilialID": self.FILIAL_ID,
            "sessao-id": "royal-prices-api",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def bootstrap(self):
        if self.token and time.time() - self.token_created_at < 3600:
            return

        dominio_url = f"{self.API_ROOT}/organizacoes/filiais/dominio/royalsupermercados.com.br"
        dominio = self.session.get(dominio_url, timeout=30)
        dominio.raise_for_status()
        data = dominio.json()["data"]

        self.organization_id = str(data["organizacao"]["id"])
        self.domain_key = data["organizacao"]["enderecoServidor"]

        login = self.session.post(
            self._api_url("/auth/loja/login"),
            headers=self._headers(),
            json={
                "domain": self.domain_key,
                "username": self.PUBLIC_USERNAME,
                "key": self.PUBLIC_AUTH_KEY,
            },
            timeout=30,
        )
        login.raise_for_status()
        self.token = login.json()["data"]
        self.token_created_at = time.time()

    def get_offers(self, limit: Optional[int] = None, refresh: bool = False) -> list:
        if not refresh and not limit and self.cache["items"] and time.time() - self.cache["created_at"] < self.cache_ttl:
            return self.cache["items"]

        self.bootstrap()

        products = []
        page = 1
        total_pages = None

        while total_pages is None or page <= total_pages:
            url = self._api_url(
                f"/filial/{self.FILIAL_ID}/centro_distribuicao/{self.CD_ID}/loja/produtos/em-oferta?page={page}&"
            )
            response = self.session.get(url, headers=self._headers(), timeout=30)
            response.raise_for_status()
            payload = response.json()

            paginator = payload.get("paginator") or {}
            total_pages = int(paginator.get("total_pages") or page)

            for product in payload.get("data") or []:
                products.append(self._format_product(product))
                if limit and len(products) >= limit:
                    return products

            page += 1

        self.cache = {"created_at": time.time(), "items": products}
        return products

    @staticmethod
    def _to_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _format_product(self, product: dict) -> dict:
        offer = product.get("oferta") or {}
        price = self._to_float(product.get("preco"))
        sale_price = self._to_float(offer.get("preco_oferta") or offer.get("menor_preco"))

        return {
            "product_id": product.get("produto_id"),
            "name": product.get("descricao"),
            "brand": (product.get("marca") or {}).get("descricao") if isinstance(product.get("marca"), dict) else product.get("marca"),
            "price": price,
            "sale_price": sale_price,
            "old_price": self._to_float(offer.get("preco_antigo")),
            "final_price": sale_price if sale_price is not None else price,
            "unit": product.get("unidade_sigla"),
            "barcode": product.get("codigo_barras"),
            "image": product.get("imagem"),
            "is_offer": bool(product.get("em_oferta") or offer),
        }


royal = RoyalClient()


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "royal-prices-api"})


@app.route("/royal/precos", methods=["GET"])
def royal_prices():
    try:
        limit = request.args.get("limit", type=int)
        refresh = request.args.get("refresh", "false").lower() in {"1", "true", "sim"}
        query = (request.args.get("q") or "").strip().lower()

        items = royal.get_offers(limit=limit, refresh=refresh)
        if query:
            items = [item for item in items if query in str(item.get("name") or "").lower()]

        return jsonify({"success": True, "count": len(items), "items": items})
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else 500
        return jsonify({"success": False, "error": str(error)}), status
    except Exception as error:
        return jsonify({"success": False, "error": str(error)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
