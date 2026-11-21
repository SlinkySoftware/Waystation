// SPDX-License-Identifier: GPL-3.0-or-later
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { Repository } from '@/api/types'
import { seedRepositories } from '@/mocks/fixtures/repositories'

import CloneRepositoryDialog from './CloneRepositoryDialog.vue'

const post = vi.hoisted(() => vi.fn())

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, api: { POST: post }, call: async <T,>(promise: Promise<T>) => promise }
})

const source = seedRepositories().find((r) => r.name === 'dockerhub-proxy')!

function mountDialog() {
  return mount(CloneRepositoryDialog, { props: { modelValue: true, source }, attachTo: document.body })
}

async function submit(name: string): Promise<void> {
  const input = document.querySelector<HTMLInputElement>('[data-test="clone-name"] input, input[data-test="clone-name"]')!
  input.value = name
  input.dispatchEvent(new Event('input'))
  await flushPromises()
  document.querySelector<HTMLFormElement>('.form-dialog form, form.form-dialog__form')!.requestSubmit()
  await flushPromises()
}

describe('CloneRepositoryDialog', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    post.mockReset()
    document.body.innerHTML = ''
  })

  it('suggests a name, keeps the source settings and explains what docker leaves behind', async () => {
    mountDialog()
    await flushPromises()
    const input = document.querySelector<HTMLInputElement>('[data-test="clone-name"] input, input[data-test="clone-name"]')!
    expect(input.value).toBe('dockerhub-proxy-copy')
    expect(document.body.textContent).toContain('Clone dockerhub-proxy')
    expect(document.body.textContent).toContain('Cached and uploaded content is not copied')
    expect(document.body.textContent).toContain('Docker aliases and the default-repository setting stay with the original')
  })

  it('posts the new name and emits the created repository', async () => {
    const created = { ...source, name: 'hub-two' } as Repository
    post.mockResolvedValue(created)
    const w = mountDialog()
    await flushPromises()
    await submit('hub-two')
    expect(post).toHaveBeenCalledWith('/repositories/{name}/clone', {
      params: { path: { name: 'dockerhub-proxy' } },
      body: { name: 'hub-two', description: source.description, online: source.online },
    })
    expect(w.emitted('cloned')?.[0]).toEqual([created])
  })

  it('shows the server field error on the name', async () => {
    const { ApiError } = await import('@/api/client')
    post.mockRejectedValue(
      new ApiError({ type: 'about:blank', title: 'Conflict', status: 409, detail: 'taken', errors: [{ field: 'name', message: 'A repository with this name already exists.', code: 'conflict' }] }),
    )
    const w = mountDialog()
    await flushPromises()
    await submit('npm')
    expect(w.emitted('cloned')).toBeUndefined()
    expect(document.body.textContent).toContain('A repository with this name already exists.')
  })
})
