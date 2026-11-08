// SPDX-License-Identifier: GPL-3.0-or-later
// The only way the UI talks to the server (ARCH §3): the generated openapi-fetch client for /api/v1.
import createClient, { type Middleware } from 'openapi-fetch'

import type { paths } from './schema'
import type { FieldError, Problem } from './types'

export const API_BASE = '/api/v1'

const UNSAFE = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

let csrfToken: string | null = null
let onUnauthorized: (() => void) | null = null

/** Double-submit CSRF token from the session (sent as X-CSRF-Token on unsafe methods). */
export function setCsrfToken(token: string | null): void {
  csrfToken = token
}

/** Called when any request comes back 401 (session expired): the auth store clears and routes to sign-in. */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler
}

/** An RFC 9457 problem returned by the API. */
export class ApiError extends Error {
  readonly status: number
  readonly problem: Problem

  constructor(problem: Problem) {
    super(problem.detail ?? problem.title)
    this.name = 'ApiError'
    this.status = problem.status
    this.problem = problem
  }

  /** Field-level errors keyed by dotted path (`proxy.upstream_url`). */
  get fieldErrors(): Record<string, string> {
    const out: Record<string, string> = {}
    for (const e of this.problem.errors ?? ([] as FieldError[])) out[e.field] ??= e.message
    return out
  }
}

const middleware: Middleware = {
  onRequest({ request }) {
    if (csrfToken && UNSAFE.has(request.method)) request.headers.set('X-CSRF-Token', csrfToken)
    return request
  },
  onResponse({ request, response }) {
    if (response.status === 401 && !request.url.endsWith('/auth/login') && !request.url.endsWith('/auth/me')) {
      onUnauthorized?.()
    }
    return response
  },
}

export const api = createClient<paths>({ baseUrl: API_BASE, credentials: 'same-origin' })
api.use(middleware)

function toProblem(error: unknown, response: Response): Problem {
  if (error && typeof error === 'object' && 'title' in error) return error as Problem
  return { type: 'about:blank', title: response.statusText || 'Request failed', status: response.status }
}

/** Unwrap an openapi-fetch result: the data, or throw an ApiError carrying the problem document. */
export async function call<T>(promise: Promise<{ data?: T; error?: unknown; response: Response }>): Promise<T> {
  let result: { data?: T; error?: unknown; response: Response }
  try {
    result = await promise
  } catch (cause) {
    throw new ApiError({
      type: 'about:blank',
      title: 'Network error',
      status: 0,
      detail: cause instanceof Error ? cause.message : String(cause),
    })
  }
  if (!result.response.ok) throw new ApiError(toProblem(result.error, result.response))
  return result.data as T
}

/** A user-facing message for any thrown value. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.problem.detail ?? error.problem.title
  if (error instanceof Error) return error.message
  return String(error)
}
