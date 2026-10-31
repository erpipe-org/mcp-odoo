# Attachment writes

Use the standard `preview_write` → `validate_write` → `execute_approved_write`
workflow for both uploading a file and linking an existing attachment.
Verify the target document and its ID before preparing the write.

## Upload with a document link

Example arguments to `validate_write` (IDs and paths are illustrative):

```json
{
  "model": "ir.attachment",
  "operation": "create",
  "values": {
    "name": "offer.pdf",
    "datas_from_path": "/uploads/offer.pdf",
    "res_model": "crm.lead",
    "res_id": 42
  }
}
```

The path must exist on the MCP server under `ODOO_MCP_ATTACHMENT_UPLOAD_ROOTS`.
Validation replaces `datas_from_path` with a content fingerprint in the approval;
execution uses the bytes stored server-side. Use the approval returned by
validation unchanged. Odoo infers the MIME type from the filename and content.

## Link an existing attachment

Pass the same arguments to preview and validation:

```json
{
  "model": "ir.attachment",
  "operation": "write",
  "record_ids": [501],
  "values": {
    "res_model": "crm.lead",
    "res_id": 42
  }
}
```

Then pass the returned approval and `confirm=true` to `execute_approved_write`.
This updates the existing attachment's ownership without copying or replacing
its bytes. Re-read `res_model`, `res_id`, and `checksum` after execution.

## Validation and access

Odoo marks these fields `readonly` for the UI, although its ORM accepts them in
`create` and `write`. The validator makes a narrow exception for `ir.attachment`:

- Supply both fields in each values object, including every linked batch entry.
  `res_model` must be a technical model name and `res_id` a positive integer.
  Partial links, clearing the link, and composer placeholders with ID zero are
  outside this exception. Context defaults do not complete a partial pair.
- Metadata must identify `res_model` as `char` and `res_id` as
  `many2one_reference` (or the legacy `integer` type).
- Other readonly fields, including `res_field`, `checksum`, and `mimetype`,
  retain their normal checks. Writes unrelated to resource linking are unchanged.
- Execution still requires trusted live metadata, a matching unexpired approval,
  confirmation, and `ODOO_MCP_ENABLE_WRITES=1`. Odoo decides access to the
  attachment and target record; validation is not an access check or a dry run.

## Chatter and downloads

Linking an attachment does not itself post a message or send an email in standard
Odoo. Posting a note or sending a message is a separate authorized operation.

Adding an ID to a message's `attachment_ids` is not equivalent to setting the
attachment's `res_model` / `res_id`. In Odoo 16, `message_post` automatically
reassigns attachments from `mail.compose.message` created by the current user,
but does not assign an attachment whose model is empty. Set the document link
at upload time, or repair it before reusing the attachment in the chatter.

An unlinked private attachment may be readable by its creator while failing to
download for another user who can read the opportunity. Verify downloads with
the intended user's permissions as well as checking the stored file bytes.

## Repeatable verification

Unit tests include realistic readonly metadata, upload fingerprints, single and
batch creation, existing-attachment linking, invalid links, and approval gates:

```bash
uv run python -m pytest tests/test_attachment_link.py tests/test_attachment_upload.py
```

The integration test starts disposable Docker Compose databases, exercises the
MCP workflow, and removes the stacks afterward:

```bash
uv run --python 3.12 --with-editable . scripts/odoo_attachment_smoke.py \
  --versions 16.0 17.0 18.0 19.0 --timeout 360
```

It covers XML-RPC on Odoo 16–19 and JSON-2 on Odoo 19. A second internal user is
denied an orphan's HTTP download, then downloads identical bytes after the
creator links the attachment through MCP. It also checks a new linked upload,
batch creation, checksum preservation, and rejection of unrelated readonly
fields. The fixture uses `res.partner` as the target document and requires no
production data or credentials.
