"""Compare Bellhaven's website with CRM accounts and propose changes for review."""

import json
import re
from collections import Counter


PARENT_NAME = "Bellhaven Senior Living (Parent Account)"
STREET_WORDS = {
    "avenue": "ave", "boulevard": "blvd", "drive": "dr", "lane": "ln",
    "road": "rd", "street": "st", "pk": "pike", "north": "n", "south": "s",
    "east": "e", "west": "w", "northeast": "ne", "northwest": "nw",
    "southeast": "se", "southwest": "sw",
}
CARE_TYPES = {
    "Assisted Living": "Assisted Living",
    "Memory Support": "Memory Care",
    "Short-Term Rehabilitation & Nursing": "Skilled Nursing",
}
COMMON_NAME_WORDS = {
    "and", "at", "care", "center", "centre", "health", "healthcare", "home",
    "living", "manor", "nursing", "of", "rehab", "rehabilitation", "senior", "the",
}


def normalize(text):
    text = (text or "").lower().replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", text))


def names_support_match(website_name, crm_name):
    website = normalize(website_name)
    crm = normalize(crm_name)
    if website == crm:
        return True

    shared_words = set(website.split()).intersection(crm.split())
    return len(shared_words - COMMON_NAME_WORDS) >= 2


def street_key(street):
    return " ".join(STREET_WORDS.get(word, word) for word in normalize(street).split())


def address_key(street, city, state):
    return street_key(street), normalize(city), normalize(state)


def find_candidates(location, accounts):
    name = normalize(location["name"])
    address = address_key(location["address"], location["city"], location["state"])
    address_matches = []
    name_only_matches = []

    for account in accounts:
        account_address = address_key(
            account["billing_street"], account["billing_city"], account["billing_state"],
        )
        if all(address) and account_address == address:
            address_matches.append(account)
        elif normalize(account["name"]) == name:
            name_only_matches.append(account)

    return address_matches, name_only_matches


def finding(action, reason, location=None, account=None, *, fields=None, candidates=()):
    return {
        "action": action,
        "reason": reason,
        "location": location,
        "account": account,
        "fields": fields or {},
        "candidates": list(candidates),
    }


def account_changes(location, account, parent_id):
    care_types = [CARE_TYPES.get(care, care) for care in location["care_offerings"]]
    new_fields = {
        "name": location["name"],
        "parent_id": parent_id,
        "billing_street": location["address"],
        "billing_city": location["city"],
        "billing_state": location["state"],
        "billing_zip": location["zip"],
        "care_type": care_types[0],
        "status": "Active",
    }
    if account is None:
        return finding(
            "create", "No CRM account matches the website location.", location, fields=new_fields,
        )

    reason = "Street address, city, and state match."
    if account["status"] != "Active":
        return finding(
            "review", reason + " Confirm the account status before making changes.", location, account,
        )

    parent_changes = account["parent_id"] != parent_id
    if parent_changes and account["lifetime_revenue"] > 0 and account["outstanding_ar"] > 0:
        return finding(
            "chow",
            reason + " The account needs a new parent and has both revenue history and open AR. "
            "Create a new account and set chow_current_account on the old one.",
            location, account, fields=new_fields,
        )

    changes = {}
    if parent_changes:
        changes["parent_id"] = parent_id
    if normalize(new_fields["name"]) != normalize(account["name"]):
        changes["name"] = new_fields["name"]
    if new_fields["billing_zip"][:5] != (account["billing_zip"] or "")[:5]:
        changes["billing_zip"] = new_fields["billing_zip"]
    if account["care_type"] not in care_types:
        changes["care_type"] = new_fields["care_type"]

    if changes:
        return finding(
            "update", reason + " CRM fields differ from the website or expected parent.",
            location, account, fields=changes,
        )
    return finding("matched", reason + " The account already agrees with the website.", location, account)


def match_accounts(locations, accounts):
    """Return findings with supporting records and any proposed CRM fields."""
    if not locations:
        raise ValueError("No website locations to match.")

    parents = [account for account in accounts if account["name"] == PARENT_NAME]
    if len(parents) != 1:
        raise ValueError("Expected exactly one Bellhaven parent account.")
    parent_id = parents[0]["account_id"]
    parent_ids = {account["parent_id"] for account in accounts} | {parent_id}
    facilities = [
        account for account in accounts
        if account["account_id"] not in parent_ids
        and not account["duplicate_of_account"]
        and not account["chow_current_account"]
    ]

    matches = [find_candidates(location, facilities) for location in locations]
    website_matches = Counter(
        account["account_id"] for by_address, _ in matches for account in by_address
    )
    findings, seen = [], set()

    for location, (by_address, by_name) in zip(locations, matches):
        seen.update(account["account_id"] for account in by_address)
        candidates = by_name
        if len(by_address) > 1 or any(
            website_matches[account["account_id"]] > 1 for account in by_address
        ):
            result = finding(
                "review", "Conflicting matches at this address. Confirm which account represents the facility.",
                location,
            )
            candidates = by_address + by_name
        elif by_address:
            account = by_address[0]
            if names_support_match(location["name"], account["name"]):
                result = account_changes(location, account, parent_id)
            else:
                result = finding(
                    "review", "The address matches, but the names differ substantially. "
                    "Confirm the facility identity before making changes.", location,
                )
                candidates = by_address + by_name
        else:
            state = normalize(location["state"])
            possible = [
                account for account in by_name
                if normalize(account["billing_state"]) in ("", state)
            ]
            if possible:
                seen.update(account["account_id"] for account in possible)
                result = finding(
                    "review", "The name matches, but the address needs verification.", location,
                )
            else:
                result = account_changes(location, None, parent_id)
                if by_name:
                    result["reason"] = "The CRM accounts with the same name are in different states."

        result["candidates"] = candidates
        findings.append(result)

    for account in facilities:
        if (
            account["parent_id"] == parent_id
            and account["account_id"] not in seen
            and account["status"] != "Inactive"
        ):
            address = address_key(
                account["billing_street"], account["billing_city"], account["billing_state"],
            )
            others_at_address = [
                other for other in facilities
                if other["parent_id"] != parent_id
                and all(address)
                and address_key(other["billing_street"], other["billing_city"], other["billing_state"]) == address
            ]
            findings.append(finding(
                "review", "This Bellhaven CRM account has no match on the website. Confirm current ownership.",
                account=account, candidates=others_at_address,
            ))

    return findings


if __name__ == "__main__":
    from crm_client import CRMClient
    from scraper import scrape_locations

    findings = match_accounts(scrape_locations(), CRMClient().list_accounts())
    print(json.dumps(findings, indent=2, ensure_ascii=False))
