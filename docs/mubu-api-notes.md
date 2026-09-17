# Notes on the Mubu web API

These notes document what was reverse-engineered from the Mubu web client so that maintainers
can repair this project if Mubu changes things. They describe an **unofficial** API; there is
no vendor support for anything on this page.

## Transport

* Base URL: `https://api2.mubu.com/v3/api`
* Every request is `POST` with a JSON body.
* Response envelope: `{"code": 0, "data": {...}, "msg": "..."}`. `code == 0` means success.
  Note that failures still come back with HTTP 200.

## Required headers

```
Content-Type: application/json;charset=UTF-8
Jwt-Token: <token>            # after login
data-unique-id: <uuid>        # stable per client session
x-session-id: <uuid>          # stable per client session
x-request-id: <uuid>          # new per request
x-reg-entrance: https://mubu.com/app
Origin: https://mubu.com
Referer: https://mubu.com/
```

Missing some of these makes endpoints such as `/list/import_doc` fail with
`code: 17, msg: "illegal request"` even though the token is perfectly valid.

## Endpoints used by this project

| Purpose | Endpoint | Body |
| --- | --- | --- |
| Log in | `/user/phone_login` | `{phone, password, callbackType: 0}` → `{token, id, name, memberId?}` |
| List a folder | `/list/get` | `{folderId}` → `{folders, documents, ...}` (`"0"` is the root) |
| Read a document | `/document/edit/get` | `{docId, password: "", isFromDocDir: true}` → `{definition, baseVersion, ...}` |
| Create a document | `/list/create_doc` | `{folderId, name, type: 0}` → `{id}` — **creates an empty document** |
| Create a document **with content** | `/list/import_doc` | `{name, folderId, itemCount, define}` → `{id}` |
| Create a folder | `/list/create_folder` | `{folderId, name}` → `{folder, id}` |

The token (JWT) is valid for roughly two hours; the login response's `memberId` is only needed
for the collaborative-editing path, which this project does not use.

## Writing document content

`POST /list/create_doc` **ignores** `content` — passing Markdown or a node tree there silently
produces an empty document (this is why other third-party projects that advertise "Markdown
import" through that endpoint do not actually work).

The web client's import flow instead parses the file **client-side** and uploads a node tree:

```json
{
  "name": "标题",
  "folderId": "0",
  "itemCount": 3,
  "define": "{\"nodes\":[{\"id\":\"root\",\"text\":\"标题\",\"children\":[{\"id\":\"n1\",\"text\":\"要点\",\"children\":[]}]}]}"
}
```

Node fields: `id` (any unique string), `text`, `children`, `note`, `finish` (checkbox state).
The first node's `text` becomes the document title node; the document name is the separate
`name` field.

## Why there is no "update document" tool

Editing an existing document goes through a WebSocket collaboration protocol
(`/colla/register` issues a ticket, then changes are exchanged as changesets;
`/changeset/fetch` pulls missed ones). There is no simple HTTP call to replace a document body,
and reimplementing the collaboration protocol would risk corrupting user data — so this project
deliberately stops at creating new documents.

## Error codes seen in practice

| code | Meaning |
| --- | --- |
| `0` | Success |
| `2` | Login expired / not signed in |
| `6` | Permission error (missing or encrypted document, wrong id) |
| `17` | Illegal request (missing headers, or the endpoint changed) |
| `1204` | Phone number or password incorrect |

## Node fields still to be confirmed on real documents

The Markdown converter currently relies on fields that **have been observed** (`text`,
`children`, `note`, `finish`/`checked`) plus a *heuristic* for images and links. The following
still need to be confirmed against real documents that contain them, using
`mubu-web-mcp inspect <doc-id>` (which prints field names, counts and URL host names only):

| Content | Status |
| --- | --- |
| Image field name(s), order, dimensions, original file name | **unconfirmed** — heuristic: field name containing `img`/`image`/`pic`/`photo`, or a URL ending in an image extension |
| Attachment field name(s) | unconfirmed |
| Plain hyperlink field(s) | unconfirmed |
| Internal document link field(s) and target document id | unconfirmed |
| Node ordering field | unconfirmed — falls back to the order returned by the listing API |
| Folder/document ordering field | partially confirmed: `seq` is used when present |
| Tables / formulas / tags / due dates / highlights | unconfirmed |

### Confirmed on a real document (2026-09-14, id `<doc-id>`)

Node fields actually returned by `get_doc` for an outline with an image, a note and a task:

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | str (10 chars) | node id |
| `text` | str | node body (may be long; one child had 824 chars) |
| `children` | list | nested nodes |
| `note` | str | node note — **confirmed** |
| `images` | list[dict] | image attachments on that node |
| `collapsed` | bool | collapsed state |
| `deadline` | int (ms) | due date, `0` when unset |
| `remindAt` | int (ms) | reminder time, `0` when unset |
| `taskStatus` | int | task state (`0` = open) |
| `modified` | int (ms) | last modification timestamp |
| `idx` | int | ordering index (seen in the redacted structure report) |

Image entry structure — one dict per image, order preserved:

```json
{"id": "UtYC56OetU", "uri": "document_image/<user-id>_2ffa166b-02c2-46e8-d3a9-7a4a29d1ee03.png",
 "w": 66, "ow": 800, "oh": 800}
```

* `uri` is a **relative path**, not an absolute URL, and it embeds the uploader's user id.
* `w` is the display width; `ow`/`oh` are the original dimensions.
* Downloading: `https://api2.mubu.com/v3/<uri>` returns the image
  (`200 image/png`, PNG magic bytes) when the `Jwt-Token` header is present.
  `https://mubu.com/<uri>` also works; `https://assets.mubu.com/<uri>` returns 404.
  Both working hosts are covered by the `*.mubu.com` asset whitelist.

Implementation still to do (0.4.0): read images from the `images` field instead of guessing by
field name / URL suffix, build `<API origin>/<uri>`, keep `w`/`ow`/`oh` in `assets.json`, and map
`deadline` / `remindAt` / `taskStatus` / `collapsed` into Markdown metadata.

Until these are confirmed, `--assets` downloads only `*.mubu.com` URLs that look like images,
and anything unrecognised is reported as a limitation rather than silently dropped.

## Asset download security (implemented)

* HTTPS only; `http://`, usernames/passwords in the URL and non-443 ports are refused.
* Host must be `mubu.com` or `*.mubu.com`; lookalikes such as `mubu.com.example.com` or
  `notmubu.com` are refused.
* Redirects are followed manually, one hop at a time, with the target validated before the
  next request; the `Jwt-Token` header is only attached to allowed hosts.
* Maximum size, timeout and redirect count are bounded; non-image MIME types are rejected.
