# Bellhaven CRM Reconciliation

This project compares the [Bellhaven website](https://analyst-assessment-production.up.railway.app/) with an assigned CRM sandbox. The pipeline gathers evidence and queues proposals. A reviewer approves each write through the Streamlit app.

## Setup

The project was tested with Python 3.12. Run the following commands from the project directory:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Replace the placeholder in `.env` with the token from the assessment. Load it into the shell before running the pipeline or the app:

```sh
set -a
. ./.env
set +a
```

Keep `.env` private. The app and pipeline use a local SQLite file named `review.db` for the queue and audit history. Preserve that file across runs and use a separate checkout for each CRM sandbox.

## Run and review

```sh
python pipeline.py
python -m streamlit run app.py --server.address 127.0.0.1
```

The pipeline prints a JSON summary with website, CRM, finding, and new proposal counts. In the app, **Find new proposals** runs the same checks. Open **Pending**, select a proposal, and compare the website evidence with the CRM records.

- For a **review** finding, choose an action after verifying the evidence. The app can prepare an update or create an account from the website, record a status and note, or mark a CRM account as a duplicate.
- Use **Approve and apply** to send the selected change to the CRM API. The app reads the account back to verify the applied values. **Reject** saves the decision and reviewer note locally.
- **History** shows the decision, the before and after values, and any execution error. Use **Apply approved change** to retry a failed write.
- For an applied field update supported by **Undo change**, open the resulting proposal in **Pending** and approve it to restore the prior values. Use **Reapply change** on the applied undo to create a separate approval. History retains the original decision and both later operations.

### Daily schedule

Set `PROJECT_DIR` in `schedule.cron` to the absolute path of the checkout, then add the configuration to your user crontab with `crontab -e`. It runs `pipeline.py` daily at 08:00 in the scheduler's local time. It loads `.env`, uses the project's virtual environment, and appends output to `pipeline.log`. The scheduled run queues proposals for a reviewer.

## Matching and write rules

1. The scraper follows the homepage, the communities directory, and its pagination. Each facility record includes its name, street, city, state, ZIP code, care offerings, and source URL.
2. The matcher normalizes casing, punctuation, and common street abbreviations. It looks for a matching street, city, and state, or a matching name. A unique address with a matching or sufficiently similar name can support an update. Multiple records at the same address, substantially different names, or a same-state name match with a different address go to review. ZIP is checked after a match.
3. A confirmed match can produce a targeted update to the name, parent, ZIP, or care type. A listing without a plausible CRM match becomes a create proposal. Bellhaven CRM accounts absent from the website go to review so a reviewer can decide whether their status or ownership needs attention.
4. Before changing a parent, the app checks billing history and outstanding accounts receivable. When both are positive, it creates a new account under Bellhaven, preserves the old account's parent and billing information, and records the new account ID in the old account's `chow_current_account` field.
5. Once a duplicate is confirmed, the reviewer selects the account to retain. The approved change marks the duplicate `Inactive` and sets its `duplicate_of_account` to the retained account's ID.

Repeated runs use the stored proposals to skip identical findings that have already been queued or decided. Material changes in a record or in the candidate set can produce a follow-up review. Before applying updates, the app checks the current CRM values against the proposal. For facility creation and change of ownership, it checks for an existing matching account and records the new ID so retries can resume an interrupted write.

## Decisions from this review

| Facility or case | Decision and evidence |
| --- | --- |
| Bellhaven Crossings of Lima | Changed the parent from Harborview Care Group to Bellhaven Senior Living. The facility name and address matched, and the existing account had no outstanding balance. |
| Bellhaven of Portsmouth | Corrected the billing ZIP from 45626 to 45662 using the official facility listing. The facility name and street matched. |
| Bellhaven of Marietta and Bellhaven of Tiffin | Created Bellhaven accounts and linked the old Cedar Trail accounts to them. Both old accounts had revenue history and outstanding receivables, so their original parent relationships were retained. |
| Bellhaven of Owosso | Kept the account whose phone number and administrator matched the website. Marked the other same-name, same-address record inactive and linked it to the retained account. |
| Bellhaven of Ashtabula | Kept the CRM billing PO box after the phone number and administrator confirmed the facility match. The website provides the facility's physical address. |
| Bellhaven of Kettering | Created a Bellhaven account from the official listing. Three CRM records shared its address and listed different names and phone numbers. The follow-up review was rejected because the evidence was insufficient to mark the other accounts as duplicates. |
| Bellhaven of Zanesville | Created a Bellhaven account and retained the Cedar Trail record. The records shared an address and administrator and listed different phone numbers. The facility's ownership history remained unconfirmed. |
| Alliance, Coldwater, and Sandusky | Marked the accounts **Needs Review** because they are absent from the website. Sandusky also has a Millstone account at the same address, and the ownership change remains unverified. |

On November 21, 2026, all 35 website facilities had a current account under Bellhaven in the assigned CRM sandbox. Ashtabula's billing PO box was the one address exception. The review history retains the individual decisions and the evidence behind them.
