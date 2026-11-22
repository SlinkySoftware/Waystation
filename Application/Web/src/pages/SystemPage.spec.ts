// SPDX-License-Identifier: GPL-3.0-or-later
// The System page against the mock API (MSW handlers answered in-process): status cards and the support bundle.
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { QLayout, QPageContainer } from 'quasar'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'

import SystemPage from './SystemPage.vue'

vi.mock('@/api/client', async () => {
  const { getResponse } = await import('msw')
  const { handlers } = await import('@/mocks/handlers')
  async function get(path: string, init?: { parseAs?: string }) {
    const request = new Request(new URL(`/api/v1${path}`, location.origin), { method: 'GET' })
    const response = (await getResponse(handlers, request))!
    if (init?.parseAs === 'blob') return { data: response.ok ? await response.blob() : undefined, error: response.ok ? undefined : await response.json(), response }
    const body: unknown = await response.json()
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    return body
  }
  return {
    api: { GET: get },
    call: async <T,>(promise: Promise<T>) => promise,
    errorMessage: (e: unknown) => String(e),
  }
})

const Shell = defineComponent({ render: () => h(QLayout, () => h(QPageContainer, () => h(SystemPage))) })

describe('SystemPage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.setItem('ws.mock.session', 'admin')
  })
  afterEach(() => {
    document.body.innerHTML = ''
    localStorage.clear()
  })

  it('shows the support bundle card with what it holds and that secrets are removed', async () => {
    const wrapper = mount(Shell, { attachTo: document.body })
    await flushPromises()
    await vi.waitFor(() => expect(wrapper.text()).toContain('MongoDB'))
    expect(wrapper.text()).toContain('Support bundle')
    expect(wrapper.text()).toContain('Passwords, tokens, keys and credentials in URLs are removed')
    expect(wrapper.find('[data-test="support-bundle"]').text()).toContain('Download support bundle')
    wrapper.unmount()
  })

  it('downloads the bundle as a zip file named by the server', async () => {
    const blobs: Blob[] = []
    const create = vi.fn((blob: Blob) => (blobs.push(blob), 'blob:bundle'))
    Object.assign(URL, { createObjectURL: create, revokeObjectURL: vi.fn() })
    const names: string[] = []
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      names.push(this.download)
    })
    const wrapper = mount(Shell, { attachTo: document.body })
    await flushPromises()
    await wrapper.find('[data-test="support-bundle"]').trigger('click')
    await vi.waitFor(() => expect(names).toHaveLength(1))
    expect(names[0]).toMatch(/^waystation-support-waystation-\d{8}T\d{6}Z\.zip$/)
    const bytes = new Uint8Array(await blobs[0]!.arrayBuffer())
    expect([bytes[0], bytes[1]]).toEqual([0x50, 0x4b]) // "PK": a zip archive
    wrapper.unmount()
  })
})
