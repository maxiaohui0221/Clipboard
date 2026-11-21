"""Read the website and CRM, then save new proposals for human review."""

import json
from collections import Counter

from crm_client import CRMClient
from matching import match_accounts
from scraper import scrape_locations
from storage import DEFAULT_DB_PATH, Storage


def run_pipeline(db_path=DEFAULT_DB_PATH):
    crm = CRMClient()
    locations = scrape_locations()
    accounts = crm.list_accounts()
    findings = match_accounts(locations, accounts)
    new_proposals = Storage(db_path).save_findings(findings)

    return {
        "website_locations": len(locations),
        "crm_accounts": len(accounts),
        "actions": dict(Counter(finding["action"] for finding in findings)),
        "new_proposals": new_proposals,
    }


if __name__ == "__main__":
    print(json.dumps(run_pipeline(), indent=2))
