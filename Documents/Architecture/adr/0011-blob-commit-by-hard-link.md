# ADR 0011: Blob commits place the file with a hard link, falling back to rename

Status: accepted (2026-11-22, owner). Proposed 2026-11-22 with T-F07.

## Context
ADR 0002 and ARCHITECTURE §5 make blob commits atomic renames from `uploads/` into `blobs/sha256/...`. `rename`
replaces an existing target silently, so `BlobStore.commit` checks for the blob first and serialises identical
commits with a per-digest lock, but only within one process. Several server and worker processes (and replicas on
other hosts sharing an NFS export) committing the same content can all pass the existence check and all rename. The
bytes are identical, so nothing is corrupted, but every one of them reports `BlobRef.created`: "commit once" holds
only per process (T-F07 acceptance: "concurrent identical writes commit once"). On an NFS export the new
cross-process test (`test_concurrent_identical_commits_across_processes_commit_once`, six processes released
together) saw all six report `created` with rename.

## Decision
`commit` places the upload with `link(upload, target)` and then removes the upload name:

- `link` fails with `EEXIST` when the target exists, atomically, on local filesystems and NFS, so exactly one
  committer places the blob; the others drop their upload and get `created=False`.
- NFS can retransmit a `link` whose reply was lost and report `EEXIST` although the first attempt succeeded. The
  target is then our own file (`samefile(upload, target)`), which counts as placed.
- On a filesystem without hard links (`EPERM`, `ENOTSUP`/`EOPNOTSUPP`, `ENOSYS`, `EMLINK`: some SMB, FUSE and
  object-store mounts) commit falls back to `rename`, the previous behaviour (benign race, may place twice). Any other
  error fails the commit as before.
- Everything else is unchanged: verify, `fsync` the file, claim through the GC guard, `fsync` the directories,
  `uploads/` on the same filesystem. Between `link` and `unlink` the content has two names; a crash there leaves an
  upload name that `sweep_uploads` removes (the blob keeps its own name), never a partial blob.

## Consequences
- "Commit once" holds across processes and hosts on local disk and NFS; `make test-nfs` (in `make check` and Gitea
  CI) runs the blob store's tests on an NFSv4.1 mount from an NFS-Ganesha container.
- SMB and other mounts without hard links keep working (requirement unchanged: rename atomicity).
- No caller depends on `created` today; it is the hook for exact accounting of newly stored bytes.
