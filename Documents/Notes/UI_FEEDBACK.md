# UI feedback log (M0 onwards)

James's feedback on the running UI, one section per page. Screenshots of every page are in
[UI_TOUR_M0.md](UI_TOUR_M0.md). To run the build yourself, see `Documents/Operations/TRY_IT.md`.

How to add an entry: add a bullet under the page, in this form:

```
- [ ] YYYY-MM-DD · <what you saw or want> · priority: must / should / nice · (theme / desktop or mobile, if relevant)
```

Agents: pick open items, fix them, then tick the box and append `→ done YYYY-MM-DD <commit or task ID>`. Items
that need a design or architecture decision get `→ needs decision:` and a short question. Never delete entries.

## General (shell, navigation, theme, typography, density)

## Sign in

## Dashboard
KPI tiles, live transfers, traffic chart, upstream health, recent activity, storage by format.

## Repositories (list)

## Create repository
Format and type picker, presets, the form, test upstream.

## Repository: Overview

## Repository: Set me up
Docker, npm, apt and raw snippets.

## Repository: Settings

## Browse: Docker
Images, tags, manifest and layers, pull commands.

## Browse: npm
Packages, versions.

## Browse: apt
Suites, packages.

## Browse: raw
Folders, files, download URLs.

## Browse (all repositories)

## Activity
Live tail, filters, CSV export.

## Storage
Volume usage, usage by format, largest repositories.

## Administration: auth providers
Local and LDAP sign-in providers, test connection.

## Administration: audit log
Filters, detail drawer with the change diff, export.

## Profile and API tokens

## Placeholders ("soon" items)
Search, Cleanup, Tasks, and the Administration pages other than auth providers and the audit log. Note any you want sooner.
