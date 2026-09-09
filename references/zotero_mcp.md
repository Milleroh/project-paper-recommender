# Zotero MCP Operations

Use the user's personal Zotero library. Do not use the old local-import script
and do not switch to Web API credentials. The MCP calls below are the source of
truth for collection and item keys.

## Resolve library and collection

1. Call `mcp__zotero__get_libraries` and paginate with `limit`/`offset` until
   the personal library is identified. Keep its numeric `libraryID`.
2. Call `mcp__zotero__get_collections` with that `libraryID`, `recursive=false`,
   and paginate all top-level collections. Search for an exact name equal to
   the Codex project name.
3. If exactly one top-level collection matches, call
   `mcp__zotero__get_collection_details` and reuse its key. Check a stored
   project mapping with this same call before trusting the mapping.
4. If none matches, call `mcp__zotero__create_collection` with `name` equal to
   the project name and no `parentCollection`.
5. If multiple top-level collections have the same name, stop with
   `needs_input` and show their keys and paths. Never choose by list order.

Save the collection key and library ID locally only after the MCP response has
been read successfully, using the helper's `project` command with
`--collection-key`, `--collection-name`, and `--library-id`. A project rename is handled by the state helper and
the user decision: `keep` preserves the old key/name; `sync` means call
`mcp__zotero__update_collection` first and then save the new name.

## Find or create the item

Search the entire library, not just the target collection:

1. Call `mcp__zotero__search_library` with the normalized DOI, PMID, arXiv ID,
   or exact title as appropriate. Paginate if the result is truncated.
2. Call `mcp__zotero__get_item_details` for each plausible match. Compare DOI
   first, then PMID/arXiv, then title plus first author, year, and venue. A
   title-only collision is not enough to overwrite or merge anything.
3. With a verified stable identifier, call
   `mcp__zotero__add_by_identifier` using:

```json
{
  "identifiers": ["10.x/..."],
  "libraryID": 123,
  "collectionKey": "ABC12345",
  "saveAttachments": false,
  "skipExisting": true,
  "fileExisting": true,
  "titleDuplicates": "flag"
}
```

Use `arXiv:<id>` and `PMID: <number>` prefixes when that makes the identifier
type unambiguous. `titleDuplicates="flag"` keeps a possible preprint/final
collision available for review; never use `skip`, because it can move a newly
created item to the trash.

4. If the item already exists, or the importer reports an existing item key,
   call `mcp__zotero__add_items_to_collection` with the item key and target
   collection. This adds membership without removing other collections or
   replacing user metadata.
5. Re-read the regular item. If a verified abstract, URL, or other required
   field is missing, use `mcp__zotero__write_metadata` to fill only that blank
   field. Never replace a non-empty user field or abstract.
6. If no stable identifier exists, use `mcp__zotero__write_item` with verified
   metadata only. Do not guess item type or authors. Re-read the resulting item
   with `get_item_details`.

Do not treat an uncertain timeout as failure until a fresh library search or
item-detail read confirms that no write occurred. Record the item stage after
the external result is verified.

## Recommendation note

Before writing a note, read the complete child-note list from
`mcp__zotero__get_item_details` and look for this exact visible marker:

```text
codex-paper-recommendation:<project_id>:<work_id>
```

If present, reuse its note key and do not overwrite it, even if a user has
edited the note. Otherwise call `mcp__zotero__write_note` once with
`action="create"`, `parentKey` set to the regular Zotero item key, and this
content structure:

```markdown
codex-paper-recommendation:<project_id>:<work_id>

## 项目论文推荐
- 所属项目：<project name>
- 推荐日期：<YYYY-MM-DD>
- 依据：摘要 / 全文

### 中文概述
<summary>

### 与项目的关联
<relevance_reason>

### 阅读重点
<reading_focus>

### 书目信息
- DOI/PMID/arXiv：<identifier>
- 链接：<url>
```

The note stores the Chinese reason, reading focus, project, date, abstract
basis, and stable link. Do not download or attach the PDF automatically.

## Stage protocol

After each external mutation or verification, call the local helper:

```bash
python3 /Users/ou/.codex/skills/project-paper-recommender/scripts/recommendation_state.py \
  archive-stage --input archive-stage.json --lock-token LOCK_TOKEN
```

The envelope includes `operation_id`; each paper result includes `work_id`, `stage`, `status`,
`item_action` (`created` or `reused`), collection/item/note keys, and `error`
when applicable. Use `partial` when an item exists but collection membership
or note creation is unfinished. Use `needs_input` for duplicate collections,
identity collisions, or flagged preprint/final pairs. Use `failed` only for an
actual confirmed failure. The final `complete` or `verify` stage becomes
`saved` only after collection membership and note content are re-read.

On retry, call `pending` first and perform only incomplete stages. Never create
a second item or note just because the previous local result was lost.
