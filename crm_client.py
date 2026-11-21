"""Read CRM accounts and send changes approved by a reviewer."""

import os
from urllib.parse import quote

import requests


API_BASE_URL = "https://analyst-assessment-production.up.railway.app/api/v1"


class CRMClient:
    def __init__(self, token=None):
        self.token = token or os.getenv("CRM_API_TOKEN")
        if not self.token:
            raise ValueError("Set CRM_API_TOKEN before connecting to the CRM.")

    def _request(self, method, path, *, params=None, payload=None):
        response = requests.request(
            method,
            API_BASE_URL + path,
            headers={"Authorization": f"Bearer {self.token}"},
            params=params,
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def list_accounts(self):
        accounts = []
        page = 1
        while True:
            result = self._request("GET", "/accounts", params={"page": page})
            accounts.extend(result["data"])
            if result["page"] * result["page_size"] >= result["total"]:
                return accounts
            page += 1

    def get_account(self, account_id):
        return self._request("GET", f"/accounts/{quote(account_id, safe='')}")

    def create_account(self, fields):
        return self._request("POST", "/accounts", payload=fields)

    def update_account(self, account_id, fields):
        return self._request("PATCH", f"/accounts/{quote(account_id, safe='')}", payload=fields)
