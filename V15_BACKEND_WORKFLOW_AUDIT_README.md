# V15 backend workflow audit

V15 adds persistent, backend-only auditing to the V14 PO extraction and
insertion workflow. It does not add a log panel, polling, or other logging UI
to the frontend.

## Log files

- `logs/upload_workflow.jsonl`: structured JSON Lines history for later
  diagnosis, filtering, and maintenance.
- `logs/upload_workflow_live.log`: readable live event stream for `tail -F`.
- systemd journal: the same workflow identifier, stage, status, and message are
  also emitted through the application logger.

Each uploaded file receives a UUID workflow identifier. Events contain UTC
timestamps and an increasing per-workflow sequence number. The audit covers
file validation, temporary storage, PDF rendering, OCR by page, classification,
stamp OCR, Content Understanding, Oracle connection and lookup, parent and
detail transactions, commits, rollbacks, errors, cleanup, and completion.

The application does not write PDF contents, OCR text, detail rows, party name,
reference, quantity, count, certification, net rate, credentials, or tokens to
the structured audit details. Log files use mode `0640`. Each file rotates at
50 MiB and retains ten backups.

Audit writes are best-effort. An unavailable or full logging filesystem must
not prevent the document transaction from running.

## Live maintenance commands

```bash
cd /home/ajsmgpt/Estimation_Mixing_Sheet

tail -F logs/upload_workflow_live.log
```

Follow the application journal:

```bash
sudo journalctl -u estimation-mixing.service -f -o short-iso
```

Find all structured events for one workflow ID:

```bash
workflow_id="PASTE-WORKFLOW-UUID-HERE"
grep -hF "\"workflow_id\":\"$workflow_id\"" \
    logs/upload_workflow.jsonl* | python3 -m json.tool --json-lines
```

## Verification

```bash
server_venv/bin/python -m py_compile \
    app.py \
    workflow_audit.py \
    po_content_validation.py \
    migrate_po_details_v14.py

PYTHONPATH=. server_venv/bin/python -m unittest discover -v
```

The verified release result is `Ran 70 tests` and `OK`.

V14's database migration remains required before enabling live insertion on a
server that has not already applied it. No additional V15 database migration is
required for audit logging.
