"""
Backend simples para consultar precos/produtos do Royal Supermercados.

Rode:
    python royal_prices_api.py

Endpoints:
    GET /health
    GET /royal/precos
    GET /royal/precos?q=arroz
    GET /royal/precos?limit=20
    GET /royal/precos?offers_only=1
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
    DEFAULT_PRODUCT_ASSET_BASE = "https://produto-assets-vipcommerce-com-br.br-se1.magaluobjects.com"
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
        self.site_filial_id = ""
        self.token = ""
        self.token_created_at = 0.0
        self.product_asset_base = ""
        self.departments = []
        self.cache = {"all": {"created_at": 0.0, "items": []}, "offers": {"created_at": 0.0, "items": []}}
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
        self.site_filial_id = str(data["id"])

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

        omni = self.session.get(self._api_url(f"/loja/omnichannel/{self.site_filial_id}"), headers=self._headers(), timeout=30)
        omni.raise_for_status()
        for location in omni.json().get("data", {}).get("localizacaoArquivos", []):
            if location.get("model") == "Produto":
                self.product_asset_base = str(location.get("localizacao") or "").rstrip("/")
                break

    def _load_departments(self):
        if self.departments:
            return

        self.bootstrap()
        url = self._api_url(
            f"/filial/{self.FILIAL_ID}/centro_distribuicao/{self.CD_ID}/loja/"
            "classificacoes_mercadologicas/departamentos/arvore"
        )
        response = self.session.get(url, headers=self._headers(), timeout=30)
        response.raise_for_status()

        departments = []

        def walk(items):
            for item in items or []:
                item_id = item.get("classificacao_mercadologica_id")
                level = str(item.get("nivel") or "").strip().lower()
                name = str(item.get("descricao") or "").strip()
                if item_id is not None and level.startswith("depart"):
                    departments.append({"id": int(item_id), "name": name})
                walk(item.get("children") or [])

        walk(response.json().get("data") or [])
        self.departments = departments

    def _fetch_listing(self, path: str, limit: Optional[int] = None, query: str = "") -> list:
        products = []
        page = 1
        total_pages = None

        while total_pages is None or page <= total_pages:
            url = self._api_url(
                f"/filial/{self.FILIAL_ID}/centro_distribuicao/{self.CD_ID}/loja/{path}?page={page}&"
            )
            response = self.session.get(url, headers=self._headers(), timeout=30)
            response.raise_for_status()
            payload = response.json()

            paginator = payload.get("paginator") or {}
            total_pages = int(paginator.get("total_pages") or page)

            for product in payload.get("data") or []:
                formatted_product = self._format_product(product)
                if query and query not in str(formatted_product.get("name") or "").lower():
                    continue
                products.append(formatted_product)
                if limit and len(products) >= limit:
                    return products

            page += 1

        return products

    def get_products(
        self,
        limit: Optional[int] = None,
        query: str = "",
        refresh: bool = False,
        offers_only: bool = False,
    ) -> list:
        cache_key = "offers" if offers_only else "all"
        cache = self.cache[cache_key]
        can_use_cache = cache["items"] and time.time() - cache["created_at"] < self.cache_ttl
        if not refresh and can_use_cache:
            items = cache["items"]
            if query:
                items = [item for item in items if query in str(item.get("name") or "").lower()]
            return items[:limit] if limit else items

        self.bootstrap()

        if offers_only:
            products = self._fetch_listing("produtos/em-oferta", limit=limit, query=query)
        else:
            self._load_departments()
            products = []
            seen_ids = set()
            for department in self.departments:
                path = f"classificacoes_mercadologicas/departamentos/{department['id']}/produtos"
                remaining = limit - len(products) if limit else None
                for product in self._fetch_listing(path, limit=remaining, query=query):
                    product_id = product.get("product_id")
                    if product_id in seen_ids:
                        continue
                    seen_ids.add(product_id)
                    products.append(product)
                    if limit and len(products) >= limit:
                        break
                if limit and len(products) >= limit:
                    break

        if not query and not limit:
            self.cache[cache_key] = {"created_at": time.time(), "items": products}
        return products

    def get_offers(self, limit: Optional[int] = None, query: str = "", refresh: bool = False) -> list:
        return self.get_products(limit=limit, query=query, refresh=refresh, offers_only=True)

    @staticmethod
    def _to_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _image_url(self, filename: str) -> str:
        if not filename:
            return ""
        if filename.startswith("http"):
            return filename
        asset_base = self.product_asset_base or self.DEFAULT_PRODUCT_ASSET_BASE
        return f"{asset_base}/500x500/{filename}"

    def _format_product(self, product: dict) -> dict:
        offer = product.get("oferta") or {}
        price = self._to_float(product.get("preco"))
        sale_price = self._to_float(offer.get("preco_oferta") or offer.get("menor_preco"))
        image = str(product.get("imagem") or "")

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
            "image": image,
            "image_url": self._image_url(image),
            "is_offer": bool(product.get("em_oferta") or offer),
        }


royal = RoyalClient()


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "royal-prices-api"})


@app.route("/", methods=["GET"])
def index():
    return jsonify(
        {
            "service": "royal-prices-api",
            "endpoints": [
                "/health",
                "/royal/precos",
                "/royal/precos?q=arroz",
                "/royal/precos?limit=20",
                "/royal/precos?offers_only=1",
            ],
        }
    )


@app.route("/royal/precos", methods=["GET"])
def royal_prices():
    try:
        limit = request.args.get("limit", type=int)
        refresh = request.args.get("refresh", "false").lower() in {"1", "true", "sim"}
        offers_only = request.args.get("offers_only", "false").lower() in {"1", "true", "sim"}
        query = (request.args.get("q") or "").strip().lower()

        items = royal.get_products(limit=limit, query=query, refresh=refresh, offers_only=offers_only)

        return jsonify({"success": True, "count": len(items), "offers_only": offers_only, "items": items})
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else 500
        return jsonify({"success": False, "error": str(error)}), status
    except Exception as error:
        return jsonify({"success": False, "error": str(error)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
