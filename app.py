"""Local review page for Bellhaven CRM proposals."""

import streamlit as st

from crm_client import CRMClient
from matching import PARENT_NAME, account_changes, address_key, finding
from pipeline import run_pipeline
from storage import DEFAULT_DB_PATH, Storage


BILLING_FIELDS = ("billing_street", "billing_city", "billing_state", "billing_zip")
REVERSIBLE_UPDATE_FIELDS = {"name", "parent_id", "care_type", *BILLING_FIELDS}
ACCOUNT_FIELDS = (
    "name", *BILLING_FIELDS, "parent_id", "status", "duplicate_of_account",
    "chow_current_account",
)


def has_fields(account, fields):
    return all(account.get(field) == value for field, value in fields.items())


def billing_address(account):
    return address_key(*(account[field] for field in BILLING_FIELDS[:3]))


def check_account(current, snapshot, changes):
    for field in set(ACCOUNT_FIELDS) | changes.keys():
        if current.get(field) not in (snapshot.get(field), changes.get(field, snapshot.get(field))):
            raise ValueError("The CRM account changed since this proposal. Refresh and review it again.")


def has_open_billing(account):
    return account["lifetime_revenue"] > 0 and account["outstanding_ar"] > 0


def create_once(crm, storage, proposal, plan):
    fields = plan["fields"]
    account_id = proposal["new_account_id"]
    if not account_id:
        accounts = [
            account for account in crm.list_accounts()
            if not account["duplicate_of_account"] and not account["chow_current_account"]
        ]
        exact = [account for account in accounts if has_fields(account, fields)]
        if len(exact) > 1:
            raise ValueError("Multiple CRM accounts already have these values. Review them before continuing.")
        if exact:
            if plan["action"] == "chow" and not exact[0]["created_by_candidate"]:
                raise ValueError("A separate existing account already matches the new account. Verify it first.")
            account_id = exact[0]["account_id"]
        else:
            address = billing_address(fields)
            reviewed = {
                account_id for account_id, account in crm_candidates(plan).items()
                if billing_address(account) == address
            }
            if any(
                account["account_id"] not in reviewed and billing_address(account) == address
                for account in accounts
            ):
                raise ValueError("Another CRM account now shares this address. Refresh and review it first.")
            created = crm.create_account(fields)
            account_id = created.get("account_id") or (created.get("data") or {}).get("account_id")
            if not account_id:
                raise ValueError("CRM did not return an account ID. Retry to check whether it was created.")
        storage.record_created_account(proposal["id"], account_id)

    if not has_fields(crm.get_account(account_id), fields):
        raise ValueError("The new CRM account could not be verified against the approved values.")
    return account_id


def apply_change(crm, storage, proposal):
    plan = proposal.get("resolution", proposal)
    action, fields = plan["action"], plan["fields"]
    if action == "create":
        create_once(crm, storage, proposal, plan)
        return
    if action not in ("update", "chow") or not fields:
        raise ValueError("Choose a concrete CRM change before approving this proposal.")

    snapshot = plan["account"]
    account_id = snapshot["account_id"]
    current = crm.get_account(account_id)
    if action == "chow":
        linked_id = current["chow_current_account"]
        link = {"chow_current_account": linked_id} if linked_id else {}
        check_account(current, snapshot, link)
        if linked_id:
            saved_id = proposal["new_account_id"]
            if (saved_id and saved_id != linked_id) or not has_fields(crm.get_account(linked_id), fields):
                raise ValueError("The old account already links to a different CRM record.")
            storage.record_created_account(proposal["id"], linked_id)
            return
        if not proposal["new_account_id"] and not has_open_billing(current):
            raise ValueError("Billing balances changed. Refresh the proposal before moving this account.")
        new_id = create_once(crm, storage, proposal, plan)
        fields = {"chow_current_account": new_id}
        check_account(crm.get_account(account_id), snapshot, fields)
    else:
        check_account(current, snapshot, fields)
        if has_fields(current, fields):
            return
        if fields.get("parent_id", current["parent_id"]) != current["parent_id"] and has_open_billing(current):
            raise ValueError("This account now requires CHOW. Refresh and approve the new proposal.")
        survivor_id = fields.get("duplicate_of_account")
        if survivor_id:
            survivor = crm.get_account(survivor_id)
            check_account(survivor, crm_candidates(plan)[survivor_id], {})
            if (
                survivor_id == account_id or survivor["status"] != "Active"
                or survivor["duplicate_of_account"] or survivor["chow_current_account"]
            ):
                raise ValueError("Choose a current, active account to keep.")

    crm.update_account(account_id, fields)
    if not has_fields(crm.get_account(account_id), fields):
        raise ValueError("The CRM account could not be verified against the approved values.")


def execute_proposal(storage, crm, proposal_id):
    proposal = storage.start_execution(proposal_id)
    try:
        apply_change(crm, storage, proposal)
    except Exception as error:
        return storage.finish_execution(proposal_id, error=str(error))
    return storage.finish_execution(proposal_id)


def reversal_fields(proposal):
    plan = proposal.get("resolution", proposal)
    fields = plan["fields"]
    account = plan["account"]
    if (
        proposal["status"] != "applied" or plan["action"] != "update"
        or not account or not fields or not fields.keys() <= REVERSIBLE_UPDATE_FIELDS
        or any(not isinstance(account.get(field), str) for field in fields)
    ):
        return {}
    return {field: account[field] for field, value in fields.items() if account[field] != value}


def queue_reversal(storage, crm, proposal):
    fields = reversal_fields(proposal)
    if not fields:
        raise ValueError("This change cannot be reversed from History.")

    plan = proposal.get("resolution", proposal)
    snapshot = plan["account"]
    current = crm.get_account(snapshot["account_id"])
    check_account(current, snapshot, plan["fields"])
    if not has_fields(current, plan["fields"]):
        raise ValueError("The CRM no longer has the values applied by this proposal.")

    kind = "reapply" if proposal.get("history_action") == "undo" else "undo"
    number = proposal["id"]
    reason = (
        f"Reapply the values that proposal #{number} reversed."
        if kind == "reapply" else f"Restore the values from before proposal #{number}."
    )
    change = finding("update", reason, plan["location"], current, fields=fields)
    change.update(history_action=kind, history_source_id=number)
    return storage.save_findings([change])


def crm_candidates(proposal):
    accounts = proposal["candidates"]
    if proposal["account"]:
        accounts = [proposal["account"], *accounts]
    return {account["account_id"]: account for account in accounts}


def show_evidence(proposal):
    st.write(proposal["reason"])
    location = proposal["location"]
    if location:
        st.subheader("Website")
        st.write(location["name"])
        st.write(f'{location["address"]}, {location["city"]}, {location["state"]} {location["zip"]}')
        st.caption(", ".join(location["care_offerings"]))
        st.link_button("Open website listing", location["source_url"])

    accounts = crm_candidates(proposal)
    if accounts:
        st.subheader("CRM records")
        st.table([
            {
                "Account": account["name"],
                "ID": account_id,
                "Parent": account["parent_name"] or "—",
                "Billing address": ", ".join(account[field] for field in BILLING_FIELDS if account[field]),
                "Care": account["care_type"],
                "Phone": account["phone"],
                "Status": account["status"],
                "Lifetime revenue": account["lifetime_revenue"],
                "Open AR": account["outstanding_ar"],
            }
            for account_id, account in accounts.items()
        ])


def review_resolution(proposal, note):
    accounts = crm_candidates(proposal)
    location = proposal["location"]
    key = f"review-{proposal['id']}"
    options = []
    if location:
        options.append("Create from website")
        if accounts:
            options.append("Update from website")
    if accounts:
        options.append("Set status and note")
    if len(accounts) > 1:
        options.append("Mark duplicate")
    choice = st.selectbox("After checking the evidence, choose a change", options, index=None, key=key)
    if choice is None:
        return None

    account = None
    if choice != "Create from website":
        account_id = st.selectbox(
            "CRM account to change", list(accounts),
            format_func=lambda item: f"{accounts[item]['name']} ({item})",
            index=0 if len(accounts) == 1 else None, key=f"{key}-account",
        )
        if account_id is None:
            return None
        account = accounts[account_id]

    if choice in ("Create from website", "Update from website"):
        parents = [row for row in CRMClient().list_accounts() if row["name"] == PARENT_NAME]
        if len(parents) != 1:
            raise ValueError("Expected exactly one Bellhaven parent account.")
        plan = account_changes(location, account, parents[0]["account_id"])
        if account and plan["action"] in ("update", "matched"):
            changes = {field: value for field, value in plan["fields"].items() if field not in BILLING_FIELDS}
            if st.checkbox("Use the website address as the billing address", key=f"{key}-billing"):
                for field, source in zip(BILLING_FIELDS, ("address", "city", "state", "zip")):
                    if account[field] != location[source]:
                        changes[field] = location[source]
            if not changes:
                st.info("There are no changes to apply. You can reject the proposal and record why.")
                return None
            plan = finding("update", "Reviewer confirmed the website match.", location, account, fields=changes)
        if plan["action"] == "review":
            st.warning(plan["reason"])
            return None
        plan["candidates"] = list(accounts.values())
        return plan

    if choice == "Mark duplicate":
        survivor = st.selectbox(
            "CRM account to keep", [item for item in accounts if item != account_id],
            format_func=lambda item: f"{accounts[item]['name']} ({item})",
            index=None, key=f"{key}-survivor",
        )
        if survivor is None:
            return None
        fields = {"status": "Inactive", "duplicate_of_account": survivor}
        reason = f"Reviewer identified this account as a duplicate of {survivor}."
    else:
        status = st.selectbox("New CRM status", ["Needs Review", "Inactive", "Active"], key=f"{key}-status")
        fields = {"status": status}
        reason = proposal["reason"]
    fields["note"] = "\n".join(filter(None, (account["note"], note.strip() or reason)))
    return finding("update", reason, location, account, fields=fields, candidates=accounts.values())


def show_change(plan):
    if not plan["fields"]:
        return
    st.subheader("Proposed change")
    if plan["action"] == "chow":
        st.write("Create the account below, then link the old CRM account to it using its new ID.")
    account = plan["account"] if plan["action"] == "update" else {}
    st.table([
        {"Field": field, "Current": str(account.get(field) or "—"), "After approval": str(value)}
        for field, value in plan["fields"].items()
    ])


def report_result(proposal):
    if proposal["status"] == "applied":
        st.session_state["notice"] = ("success", f"Proposal #{proposal['id']} applied and verified in CRM.")
    else:
        st.session_state["notice"] = ("error", f"Proposal #{proposal['id']} failed. {proposal['error']}")
    st.rerun()


def proposal_label(proposal):
    if "history_action" in proposal:
        return f"{proposal['history_action'].capitalize()} of #{proposal['history_source_id']}"
    return proposal["action"]


def main():
    st.set_page_config(page_title="Bellhaven CRM Review", layout="wide")
    st.title("Bellhaven CRM Review")
    storage = Storage(DEFAULT_DB_PATH)
    if "notice" in st.session_state:
        kind, message = st.session_state.pop("notice")
        (st.success if kind == "success" else st.error)(message)

    if st.sidebar.button("Find new proposals"):
        try:
            with st.spinner("Checking the website and CRM..."):
                summary = run_pipeline(DEFAULT_DB_PATH)
            st.success(f"{summary['new_proposals']} new proposals added.")
        except Exception as error:
            st.error(str(error))

    view = st.sidebar.radio("Proposals", ["Pending", "History"])
    proposals = storage.list_proposals("pending") if view == "Pending" else [
        proposal for proposal in storage.list_proposals(None) if proposal["status"] != "pending"
    ]
    if not proposals:
        st.info("No proposals in this view.")
        return

    by_id = {proposal["id"]: proposal for proposal in proposals}
    proposal_id = st.sidebar.selectbox(
        f"Choose a proposal ({len(proposals)})", list(by_id),
        format_func=lambda item: (
            f"#{item} {(by_id[item]['location'] or by_id[item]['account'] or {}).get('name', '')}"
            f" · {proposal_label(by_id[item])}"
        ),
    )
    proposal = by_id[proposal_id]
    st.header((proposal["location"] or proposal["account"] or {}).get("name", "CRM proposal"))
    st.caption(f"#{proposal_id} · {proposal_label(proposal)} · {proposal['status']}")
    show_evidence(proposal)

    if view == "History":
        show_change(proposal.get("resolution", proposal))
        if proposal["decision_note"]:
            st.write(proposal["decision_note"])
        if proposal["new_account_id"]:
            st.caption(f"New CRM account ID: {proposal['new_account_id']}")
        if proposal["error"]:
            st.error(proposal["error"])
        if proposal["status"] in ("approved", "failed") and st.button("Apply approved change"):
            try:
                report_result(execute_proposal(storage, CRMClient(), proposal_id))
            except Exception as error:
                st.error(str(error))
        if reversal_fields(proposal):
            label = "Reapply change" if proposal.get("history_action") == "undo" else "Undo change"
            if st.button(label, key=f"reverse-{proposal_id}"):
                try:
                    if queue_reversal(storage, CRMClient(), proposal):
                        st.success("Proposal added. Open Pending to review and approve it.")
                    else:
                        st.info("A proposal for this change is already in Pending or History.")
                except Exception as error:
                    st.error(str(error))
        return

    note = st.text_area("Reviewer note", key=f"note-{proposal_id}")
    plan = proposal
    if proposal["action"] == "review":
        try:
            plan = review_resolution(proposal, note)
        except Exception as error:
            st.error(str(error))
            plan = None
    if plan:
        show_change(plan)

    approve, reject = st.columns(2)
    if approve.button("Approve and apply", type="primary", disabled=not plan, key=f"approve-{proposal_id}"):
        try:
            crm = CRMClient()
            resolution = plan if proposal["action"] == "review" else None
            storage.record_decision(proposal_id, approved=True, note=note, resolution=resolution)
            report_result(execute_proposal(storage, crm, proposal_id))
        except Exception as error:
            st.error(str(error))
    if reject.button("Reject", key=f"reject-{proposal_id}"):
        try:
            storage.record_decision(proposal_id, approved=False, note=note)
            st.session_state["notice"] = ("success", f"Proposal #{proposal_id} rejected.")
            st.rerun()
        except Exception as error:
            st.error(str(error))


if __name__ == "__main__":
    main()
