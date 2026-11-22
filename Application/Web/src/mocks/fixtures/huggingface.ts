// SPDX-License-Identifier: GPL-3.0-or-later
// Hugging Face Hub models and datasets for any huggingface proxy created in mock mode: real public repositories
// (a test model, GPT-2, a sentence-transformers model and the IMDB dataset) with the files a client would have pulled,
// at fake (deterministic) commits. GPT-2 also has an older commit cached from before main moved on.
import type { HuggingfaceCommit, HuggingfaceFile, HuggingfaceModel, HuggingfaceRef, HuggingfaceRevisionList } from '@/api/types'

import { fakeSha256, hash, KiB, MiB, rng } from '../rng'

const NOW = Date.now()
const DAY = 86_400_000
const HOUR = 3_600_000

interface CommitSeed {
  key: string // stable seed of the fake commit id
  revisions: string[] // revisions that resolved to it when last checked
  files: [path: string, bytes: number][]
  ageDays: number // first file cached this many days ago
}

interface ModelSeed {
  repoId: string
  kind: 'model' | 'dataset'
  commits: CommitSeed[] // most recently cached first
  popularity: number // downloads scale
}

const SEEDS: ModelSeed[] = [
  {
    repoId: 'julien-c/dummy-unknown',
    kind: 'model',
    popularity: 40,
    commits: [
      {
        key: 'dummy-main',
        revisions: ['main'],
        ageDays: 3,
        files: [
          ['.gitattributes', 445],
          ['config.json', 496],
          ['merges.txt', 2 * KiB],
          ['pytorch_model.bin', 66 * KiB],
          ['vocab.json', 8 * KiB],
        ],
      },
    ],
  },
  {
    repoId: 'openai-community/gpt2',
    kind: 'model',
    popularity: 900,
    commits: [
      {
        key: 'gpt2-main',
        revisions: ['main'],
        ageDays: 6,
        files: [
          ['config.json', 665],
          ['generation_config.json', 124],
          ['merges.txt', 456 * KiB],
          ['model.safetensors', 523 * MiB],
          ['tokenizer.json', 1300 * KiB],
          ['tokenizer_config.json', 26],
          ['vocab.json', 1018 * KiB],
        ],
      },
      {
        key: 'gpt2-previous',
        revisions: [],
        ageDays: 48,
        files: [
          ['config.json', 665],
          ['model.safetensors', 523 * MiB],
        ],
      },
    ],
  },
  {
    repoId: 'sentence-transformers/all-MiniLM-L6-v2',
    kind: 'model',
    popularity: 600,
    commits: [
      {
        key: 'minilm-main',
        revisions: ['main', 'v1.0'],
        ageDays: 12,
        files: [
          ['1_Pooling/config.json', 190],
          ['config.json', 612],
          ['config_sentence_transformers.json', 116],
          ['model.safetensors', 87 * MiB],
          ['modules.json', 349],
          ['sentence_bert_config.json', 53],
          ['special_tokens_map.json', 112],
          ['tokenizer.json', 455 * KiB],
          ['tokenizer_config.json', 350],
          ['vocab.txt', 226 * KiB],
        ],
      },
      {
        key: 'minilm-pr',
        revisions: ['refs/pr/21'],
        ageDays: 2,
        files: [['onnx/model.onnx', 86 * MiB]],
      },
    ],
  },
  {
    repoId: 'stanfordnlp/imdb',
    kind: 'dataset',
    popularity: 120,
    commits: [
      {
        key: 'imdb-main',
        revisions: ['main'],
        ageDays: 20,
        files: [
          ['README.md', 7 * KiB],
          ['plain_text/test-00000-of-00001.parquet', 20 * MiB],
          ['plain_text/train-00000-of-00001.parquet', 20 * MiB],
          ['plain_text/unsupervised-00000-of-00001.parquet', 40 * MiB],
        ],
      },
      {
        key: 'imdb-parquet',
        revisions: ['refs/convert/parquet'],
        ageDays: 9,
        files: [['plain_text/train/0000.parquet', 20 * MiB]],
      },
    ],
  },
]

const iso = (ms: number) => new Date(ms).toISOString()
const nameOf = (seed: ModelSeed) => (seed.kind === 'dataset' ? `datasets/${seed.repoId}` : seed.repoId)
/** A fake commit id: 40 hex digits, the same on every load. */
const commitOf = (c: CommitSeed) => fakeSha256(`huggingface:${c.key}`).slice(0, 40)

function filesOf(repo: string, seed: ModelSeed, c: CommitSeed): HuggingfaceFile[] {
  const next = rng(hash(`huggingface:${repo}:${c.key}`))
  return [...c.files]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([path, size]) => {
      const cachedAt = NOW - c.ageDays * DAY + next() * HOUR
      const downloads = Math.round(seed.popularity * (0.2 + next()) / (c.revisions.includes('main') ? 1 : 6))
      return { path, size, sha256: fakeSha256(`${seed.repoId}/${c.key}/${path}`), cached_at: iso(cachedAt), downloads, last_downloaded_at: downloads ? iso(NOW - next() * 2 * DAY) : null }
    })
}

function commitsOf(repo: string, seed: ModelSeed): { commit: HuggingfaceCommit; files: HuggingfaceFile[] }[] {
  return seed.commits.map((c) => {
    const files = filesOf(repo, seed, c)
    const last = files.map((f) => f.last_downloaded_at ?? '').sort().at(-1) || null
    return {
      commit: {
        commit: commitOf(c),
        revisions: c.revisions,
        files: files.length,
        size_bytes: files.reduce((sum, f) => sum + f.size, 0),
        cached_at: files.map((f) => f.cached_at ?? '').sort()[0] || null,
        downloads: files.reduce((sum, f) => sum + f.downloads, 0),
        last_downloaded_at: last,
      },
      files,
    }
  })
}

function summary(repo: string, seed: ModelSeed): HuggingfaceModel {
  const commits = commitsOf(repo, seed)
  const main = seed.commits.find((c) => c.revisions.includes('main'))
  const lastDownloaded = commits.map((c) => c.commit.last_downloaded_at ?? '').sort().at(-1) || null
  return {
    name: nameOf(seed),
    repo_id: seed.repoId,
    kind: seed.kind,
    latest_commit: main ? commitOf(main) : null,
    revisions: seed.commits.reduce((n, c) => n + c.revisions.length, 0),
    files: commits.reduce((n, c) => n + c.commit.files, 0),
    size_bytes: commits.reduce((n, c) => n + c.commit.size_bytes, 0),
    downloads: commits.reduce((n, c) => n + c.commit.downloads, 0),
    last_downloaded_at: lastDownloaded,
    updated_at: iso(NOW - Math.min(...seed.commits.map((c) => c.ageDays)) * DAY),
  }
}

function find(model: string): ModelSeed | undefined {
  return SEEDS.find((s) => nameOf(s) === model.trim())
}

/** Models and datasets of a huggingface proxy, by name; ``q`` is a case-insensitive substring. */
export function huggingfaceModelList(repo: string, q?: string): HuggingfaceModel[] {
  const needle = q?.trim().toLowerCase()
  return SEEDS.filter((s) => !needle || nameOf(s).toLowerCase().includes(needle))
    .map((s) => summary(repo, s))
    .sort((a, b) => a.name.localeCompare(b.name))
}

/** A model's refs (most recently resolved first) and cached commits; null when unknown. */
export function huggingfaceRevisionsFor(repo: string, model: string): HuggingfaceRevisionList | null {
  const seed = find(model)
  if (!seed) return null
  const commits = commitsOf(repo, seed)
  const refs: HuggingfaceRef[] = seed.commits
    .flatMap((c) => c.revisions.map((revision, i) => ({ revision, commit: commitOf(c), resolved_at: iso(NOW - (c.ageDays * DAY) / 4 - i * HOUR) })))
    .sort((a, b) => (b.resolved_at ?? '').localeCompare(a.resolved_at ?? ''))
  return { model: summary(repo, seed), refs, commits: commits.map((c) => c.commit).sort((a, b) => (b.cached_at ?? '').localeCompare(a.cached_at ?? '')) }
}

/** The cached files of one commit of a model (by path); null when the model or commit is unknown. */
export function huggingfaceFilesFor(repo: string, model: string, commit: string): { model: string; commit: string; files: HuggingfaceFile[] } | null {
  const seed = find(model)
  if (!seed) return null
  const found = commitsOf(repo, seed).find((c) => c.commit.commit === commit.trim().toLowerCase())
  return found ? { model: nameOf(seed), commit: found.commit.commit, files: found.files } : null
}
