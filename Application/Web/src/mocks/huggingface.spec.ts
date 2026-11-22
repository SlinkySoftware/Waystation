// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import type { Repository } from '@/api/types'

import { huggingfaceFilesFor, huggingfaceModelList, huggingfaceRevisionsFor } from './fixtures/huggingface'
import { validatePrefetch } from './prefetch'
import { setupGuide } from './setup'

describe('mock Hugging Face proxies', () => {
  it('lists public models and datasets by name, filtered by substring', () => {
    const names = huggingfaceModelList('huggingface').map((m) => m.name)
    expect(names).toEqual(expect.arrayContaining(['julien-c/dummy-unknown', 'openai-community/gpt2', 'sentence-transformers/all-MiniLM-L6-v2', 'datasets/stanfordnlp/imdb']))
    expect([...names].sort((a, b) => a.localeCompare(b))).toEqual(names)
    expect(huggingfaceModelList('huggingface', 'GPT').map((m) => m.name)).toEqual(['openai-community/gpt2'])
    const imdb = huggingfaceModelList('huggingface', 'imdb')[0]!
    expect(imdb).toMatchObject({ repo_id: 'stanfordnlp/imdb', kind: 'dataset' })
    expect(imdb.latest_commit).toMatch(/^[0-9a-f]{40}$/)
  })

  it('shows revisions and cached commits, then the files of a commit', () => {
    const gpt2 = huggingfaceRevisionsFor('huggingface', 'openai-community/gpt2')!
    expect(gpt2.refs.map((r) => r.revision)).toEqual(['main'])
    expect(gpt2.commits).toHaveLength(2)
    const main = gpt2.commits.find((c) => c.revisions.includes('main'))!
    expect(main.commit).toBe(gpt2.model.latest_commit)
    expect(gpt2.commits.some((c) => !c.revisions.length)).toBe(true)
    const files = huggingfaceFilesFor('huggingface', 'openai-community/gpt2', main.commit)!
    expect(files.files.map((f) => f.path)).toContain('model.safetensors')
    expect(files.files.every((f) => /^[0-9a-f]{64}$/.test(f.sha256))).toBe(true)
    expect(files.files.length).toBe(main.files)
    expect(huggingfaceFilesFor('huggingface', 'openai-community/gpt2', '0'.repeat(40))).toBeNull()
    expect(huggingfaceRevisionsFor('huggingface', 'stanfordnlp/imdb')).toBeNull()
    expect(huggingfaceRevisionsFor('huggingface', 'datasets/stanfordnlp/imdb')!.refs.map((r) => r.revision)).toContain('refs/convert/parquet')
  })

  it('checks warm items like the server: repo id, optional @revision, optional :path', () => {
    const warm = [
      'julien-c/dummy-unknown',
      'gpt2',
      'openai-community/gpt2@main:config.json',
      'datasets/stanfordnlp/imdb@refs/convert/parquet',
      'a/b/c',
      'openai-community/gpt2@main/x',
      'openai-community/gpt2:../etc/passwd',
      '-bad/name',
    ]
    const body = { proxy: { prefetch: { refresh_popular: false, warm, schedule: null } } } as never
    expect(validatePrefetch(body, 'huggingface').map((e) => e.field)).toEqual(['proxy.prefetch.warm.4', 'proxy.prefetch.warm.5', 'proxy.prefetch.warm.6', 'proxy.prefetch.warm.7'])
  })

  it('offers hf CLI and Python snippets pointing HF_ENDPOINT at the repository', () => {
    const guide = setupGuide({ name: 'huggingface', format: 'huggingface', type: 'proxy', online: true } as Repository)
    expect(guide.clients.map((c) => c.id)).toEqual(['hf-cli', 'hf-python'])
    const cli = guide.clients[0]!.snippets
    const endpoint = cli.find((s) => s.id === 'hf-endpoint' && !s.os)!
    expect(endpoint.content).toMatch(/^export HF_ENDPOINT=\S+\/repository\/huggingface$/)
    expect(cli.find((s) => s.id === 'hf-endpoint' && s.os === 'windows')!.content).toMatch(/^\$env:HF_ENDPOINT = ".+\/repository\/huggingface"$/)
    expect(cli.find((s) => s.id === 'hf-token' && !s.os)!.content).toContain('export HF_TOKEN={{TOKEN}}')
    expect(cli.map((s) => s.content).join('\n')).toContain('huggingface-cli download julien-c/dummy-unknown')
    expect(guide.clients[1]!.snippets.map((s) => s.id)).toEqual(['hf-python', 'hf-transformers'])
  })
})
