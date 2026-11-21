# ADR 0009: Substring search on a multikey index of name suffixes

Status: proposed (2026-11-21, owner approval pending). Implemented in T-U06.

## Context
SPECIFICATION §4.6 asks for global search by name "prefix and substring", with results in < 150 ms p95 at 1M
assets, and ARCHITECTURE §9 requires every request-path query to be index-backed (no collection scan, no in-memory
sort; checked by `explain()` in the search integration test). Since T-U06 (2026-11-09) search reads prefix ranges of
`name_lc` / `base_lc` (`search_name`, `search_base`) with exact keyset paging. A substring cannot be answered from
those indexes: a `$regex` without a `^` anchor scans the whole index.

Options considered:

1. **Unanchored `$regex` on `name_lc`** (an index scan with a key filter). No new data, but every substring query
   reads every key of every searchable collection: about 1M keys for a 1M-asset repository, far over the budget.
2. **MongoDB `text` index.** Tokenises on word boundaries and stems: `ginx` or `0012345` (inside
   `artifact-0012345.tar.gz`) are not found, results have no stable order for keyset paging, and a collection has one
   text index.
3. **Trigrams** (`name_tri` = the name's distinct 3-character substrings, index `name_tri, name_lc, repo_id, _id`):
   one trigram of the query is an equality bound, results come in name order, the full test is a regex on the
   candidates. Selective only when some trigram of the query is rare. Tried first and rejected: a query whose
   trigrams are all common reads the whole posting list of the rarest one, even when few names match (on the 1M
   fixture `artifact` occurs only at the start of names, so its substring stream scanned 1M keys to find none;
   digit runs such as `01234` share common trigrams). It also needs per-query frequency probes to pick a trigram.
4. **Name suffixes** (`name_sfx` = every suffix of `name_lc` from its second character on, at least 3 characters,
   cut to 16, distinct; multikey index `search_sfx` = `name_sfx, repo_id, _id`): a name contains `q` after its first
   character iff one of its suffixes starts with `q`, so a prefix range over `name_sfx` finds exactly the names
   holding the term (terms longer than 16 characters: the range of their first 16, then the full test on
   `name_lc`). The scan reads only matching keys.
5. **An external search engine** (OpenSearch, Meilisearch). A new stateful component for one feature; rejected
   (SPEC §2 keeps the deployment to MongoDB + Valkey).

## Decision
Option 4.
- Writers set `name_sfx` with `name_lc` through `waystation_common.searchkeys` (`name_keys`, `file_keys`); migration
  `0004_search_suffixes` backfills documents that have `name_lc` and no `name_sfx` (batched, idempotent). Index
  `search_sfx` (partial on `name_sfx` existing) on the same collections as `search_name`.
- A search for a term of at least 3 characters runs the prefix streams first (unchanged) and then one substring
  stream per source: prefix matches come first, then substring matches. A regex on `name_lc` drops what a prefix
  stream already found (term at the start of the name, or of its last `/` segment where the source has `base_lc`),
  so each document is listed once, also in facet counts.
- Order and paging: a multikey index cannot provide a sort, so substring streams have no `sort`; they are read with
  `hint` and `min`/`max` index bounds (the keyset), and a single index scan returns its keys in order. A document's
  position is `(smallest matching suffix, repo_id, _id)`; the cursor carries it like any stream key. A name holding
  the term twice has two keys in the range: the scan finds it at the first one; read again from a later cursor it is
  skipped as already listed (the page read continues past such repeats, bounded).
- Substring facet counts stop at `FACET_CAP` like prefix counts, and after 1 s (`maxTimeMS`), when they become lower
  bounds (`capped`).

## Consequences
- Storage: one index key per suffix (about name length - 3 keys per document, each at most 16 characters plus
  `repo_id` and `_id`). On the 1M-asset perf fixture (`artifact-NNNNNNN.tar.gz`, 20 suffixes per name) the
  `search_sfx` index of `assets` is the largest search index (figure in `Documents/CHANGELOG.md`, T-U06
  2026-11-21). Writes of searchable documents (cache fills, apt/yum listing generations) update more index keys.
- Substring results are ordered by the name from the match on (`copy-of-setup.sh` and `nginx-setup.sh` both sort as
  `setup.sh`), not by the whole name. Prefix results keep name order.
- Terms of 1-2 characters match as prefixes only (`MIN_TERM`).
- The ARCHITECTURE §9 rule holds: substring queries are index scans (`explain()` checked in the integration test),
  bounded by the number of matching keys, never a collection scan or an in-memory sort.
