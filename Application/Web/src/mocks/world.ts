// SPDX-License-Identifier: GPL-3.0-or-later
// The mock "world": a fake live-event generator that plays out realistic, bursty client activity (docker pulls of
// tens to hundreds of MiB, npm install bursts, apt and dnf runs, raw downloads) with shared LAN and
// upstream bandwidth, coalescing, and occasional failures. Emits the same LiveEvents the server will send (ARCH §8)
// and keeps the counters the mock REST handlers read.
import type { ActivityEvent, LiveEvent, Repository, Transfer } from '@/api/types'
import { compareIdsDesc } from '@/utils/activity'

import { seedRepositories } from './fixtures/repositories'
import { aptRun, dnfRun, dockerPull, type JobPlan, npmInstall, rawDownload, type Spec, trickle } from './scenarios'
import { between, GiB, MiB, pick } from './rng'

export const TICK_MS = 250
const LAN_BPS = 117e6 // ~1 GbE to clients
const WAN_BPS = 58e6 // ~500 Mbit/s internet uplink for upstream fetches
const HISTORY_BUCKET_S = 10
/** Activity entries the mock keeps (the server's stream keeps ~200k). */
const ACTIVITY_KEEP = 3000

interface Sim {
  t: Transfer
  spec: Spec
  ttfbMs: number
  /** For COALESCED followers: the owner's transfer id. */
  owner: string | null
  failAt: number | null
}

interface Job {
  plan: JobPlan
  queue: Spec[]
  active: Set<string>
  delayMs: number
}

interface Completion {
  at: number
  hit: boolean
}

export type WorldListener = (event: LiveEvent, id: string | null) => void

export class MockWorld {
  readonly repos: Repository[] = seedRepositories()
  storageUsed = this.repos.reduce((a, r) => a + r.stats.size_bytes, 0)
  artifacts = this.repos.reduce((a, r) => a + r.stats.artifact_count, 0)
  readonly storageTotal = 2 * 1024 * GiB
  rateLimit = { limit: 200, remaining: 142, resetAt: Date.now() + 3.2 * 3_600_000 }
  private epelRetryAt = Date.now() + 4 * 60_000 + 12_000
  readonly activity: ActivityEvent[] = []
  /** 10 s buckets of [hitBytes, upstreamBytes] for the last 15 minutes, oldest first. */
  readonly history: { ts: number; hit: number; upstream: number }[] = []

  private sims = new Map<string, Sim>()
  private jobs: Job[] = []
  private listeners = new Set<WorldListener>()
  private completions: Completion[] = []
  private timer: ReturnType<typeof setInterval> | null = null
  private seq = 0
  private sinceTick = 0
  private countdown = { docker: 400, npm: 2500, apt: 5000, yum: 3500, raw: 9000, trickle: 800, misc: 20000 }

  constructor() {
    const now = Date.now()
    const start = Math.floor(now / 1000 / HISTORY_BUCKET_S) * HISTORY_BUCKET_S * 1000
    for (let i = 89; i >= 0; i--) {
      const busy = Math.random() < 0.3
      const hit = between(8, 30) * MiB * (busy ? between(3, 8) : 1)
      this.history.push({ ts: start - i * HISTORY_BUCKET_S * 1000, hit, upstream: hit * between(0.02, busy ? 0.4 : 0.15) })
    }
    this.seedActivity()
  }

  subscribe(listener: WorldListener): () => void {
    this.listeners.add(listener)
    this.start()
    return () => this.listeners.delete(listener)
  }

  start(): void {
    if (this.timer) return
    this.timer = setInterval(() => this.tick(TICK_MS), TICK_MS)
    // Open on a busy moment: a cold multi-hundred-MiB pull, a dnf run and an npm install in flight.
    this.spawn(dockerPull(true, 'ghcr-proxy'))
    this.spawn(dnfRun('rocky'))
    this.spawn(npmInstall())
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer)
    this.timer = null
  }

  transfers(): Transfer[] {
    return [...this.sims.values()].map((s) => ({ ...s.t, client: { ...s.t.client } }))
  }

  private emit(event: LiveEvent, id: string | null = null): void {
    for (const l of this.listeners) l(event, id)
  }

  private nextId(): string {
    return `${Date.now()}-${this.seq++}`
  }

  addActivity(entry: Omit<ActivityEvent, 'id' | 'ts'>): void {
    const event: ActivityEvent = { id: this.nextId(), ts: new Date().toISOString(), ...entry }
    this.activity.unshift(event)
    this.activity.length = Math.min(this.activity.length, ACTIVITY_KEEP)
    this.emit({ type: 'activity', ts: event.ts, event }, event.id)
  }

  // ------------------------------------------------------------------ simulation
  tick(dt: number): void {
    this.schedule(dt)
    this.runJobs(dt)
    this.advance(dt)
    this.sinceTick += dt
    if (this.sinceTick >= 1000) {
      this.sinceTick = 0
      this.statsTick()
    }
  }

  private schedule(dt: number): void {
    const c = this.countdown
    for (const key of Object.keys(c) as (keyof typeof c)[]) c[key] -= dt
    const busy = this.sims.size
    if (c.docker <= 0) {
      this.spawn(dockerPull(Math.random() < 0.3))
      c.docker = between(5000, 14000)
    }
    if (c.npm <= 0) {
      this.spawn(npmInstall())
      c.npm = between(12000, 30000)
    }
    if (c.apt <= 0) {
      this.spawn(aptRun())
      c.apt = between(14000, 35000)
    }
    if (c.yum <= 0) {
      this.spawn(dnfRun())
      c.yum = between(16000, 40000)
    }
    if (c.raw <= 0) {
      this.spawn(rawDownload())
      c.raw = between(15000, 40000)
    }
    if (c.trickle <= 0) {
      if (busy < 60) this.spawn(trickle())
      c.trickle = between(500, 2200)
    }
    if (c.misc <= 0) {
      this.miscActivity()
      c.misc = between(20000, 45000)
    }
  }

  spawn(plan: JobPlan): void {
    this.jobs.push({ plan, queue: [...plan.specs], active: new Set(), delayMs: 0 })
    // Another client pulling the same cold image a moment later coalesces onto the in-flight fetches.
    if (plan.specs.some((s) => s.cache === 'FETCHING') && plan.specs[0]?.format === 'docker' && Math.random() < 0.35) {
      const follower: JobPlan = { ...plan, client: { ...plan.client, ip: plan.client.ip.replace(/\d+$/, String(Math.floor(between(10, 250)))) } }
      this.jobs.push({ plan: follower, queue: [...plan.specs], active: new Set(), delayMs: between(600, 2500) })
    }
  }

  private runJobs(dt: number): void {
    for (const job of this.jobs) {
      job.delayMs -= dt
      if (job.delayMs > 0) continue
      while (job.active.size < job.plan.concurrency && job.queue.length) {
        const spec = job.queue.shift()!
        job.active.add(this.startTransfer(spec, job.plan))
        // Metadata first: the next request waits for the index/manifest.
        if (job.queue.length && spec.size < 200 * 1024 && job.active.size === 1) break
      }
    }
    this.jobs = this.jobs.filter((j) => j.queue.length || j.active.size)
  }

  private startTransfer(spec: Spec, plan: JobPlan): string {
    const id = `t-${this.nextId()}`
    const owner = spec.cache === 'FETCHING' ? [...this.sims.values()].find((s) => s.spec.key === spec.key && s.t.cache === 'FETCHING' && s.t.state === 'active') : undefined
    const cache = owner ? 'COALESCED' : spec.cache
    if (owner) owner.t.followers = (owner.t.followers ?? 0) + 1
    const now = new Date().toISOString()
    const t: Transfer = {
      id,
      repository: spec.repository,
      format: spec.format,
      direction: 'download',
      artifact: spec.artifact,
      path: spec.path,
      component: spec.component,
      client: { ...plan.client },
      cache,
      followers: 0,
      bytes_done: 0,
      bytes_total: spec.size,
      rate_bps: 0,
      upstream_latency_ms: null,
      started_at: now,
      updated_at: now,
      state: 'active',
      error: null,
    }
    const upstream = cache === 'FETCHING' || cache === 'REVALIDATED' || cache === 'ERROR' || cache === 'STALE'
    const ttfb = cache === 'HIT' ? between(1, 6) : upstream ? between(60, cache === 'ERROR' ? 1500 : 420) + (spec.slowMs ?? 0) : 0
    if (upstream && cache !== 'STALE') t.upstream_latency_ms = Math.round(ttfb)
    const sim: Sim = { t, spec, ttfbMs: ttfb, owner: owner?.t.id ?? null, failAt: spec.error && cache === 'FETCHING' ? spec.size * between(0.15, 0.8) : null }
    this.sims.set(id, sim)
    if (cache === 'REVALIDATED' && spec.repository === 'dockerhub-proxy' && this.rateLimit.remaining > 0 && Math.random() < 0.15) this.rateLimit.remaining--
    if (cache === 'FETCHING' && spec.repository === 'dockerhub-proxy' && spec.path.includes('/manifests/') && this.rateLimit.remaining > 0) {
      this.rateLimit.remaining--
      this.emitUpstream('dockerhub-proxy')
    }
    this.emit({ type: 'transfer.started', ts: now, transfer: { ...t, client: { ...t.client } } })
    return id
  }

  private advance(dt: number): void {
    const flowing = [...this.sims.values()].filter((s) => s.ttfbMs <= 0 && s.t.state === 'active' && !s.owner)
    const fetching = flowing.filter((s) => s.t.cache === 'FETCHING')
    const lanShare = LAN_BPS / Math.max(1, flowing.length)
    const wanShare = WAN_BPS / Math.max(1, fetching.length)
    const now = new Date().toISOString()
    const done: Sim[] = []

    for (const sim of this.sims.values()) {
      const t = sim.t
      if (sim.ttfbMs > 0) {
        sim.ttfbMs -= dt
        continue
      }
      if (t.cache === 'ERROR') {
        this.fail(sim, sim.spec.error ?? 'upstream error')
        continue
      }
      let target: number
      if (sim.owner) {
        const owner = this.sims.get(sim.owner)
        // Tail the owner's partial file; once it is committed, finish at LAN speed.
        target = owner && owner.t.state === 'active' ? Math.max(0, owner.t.bytes_done - t.bytes_done) * (1000 / dt) * 0.9 : lanShare * 0.8
        if (owner?.t.state === 'failed') {
          this.fail(sim, 'coalesced fetch failed upstream; retrying as owner')
          continue
        }
      } else if (t.cache === 'FETCHING') {
        target = Math.min(wanShare, lanShare) * between(0.6, 1)
      } else {
        target = lanShare * between(0.75, 1)
      }
      const rate = t.rate_bps === 0 ? target * 0.6 : t.rate_bps * 0.55 + target * 0.45
      t.rate_bps = Math.max(0, rate)
      t.bytes_done = Math.min(t.bytes_total ?? Infinity, t.bytes_done + (t.rate_bps * dt) / 1000)
      t.updated_at = now
      if (sim.failAt !== null && t.bytes_done >= sim.failAt) {
        this.fail(sim, sim.spec.error ?? 'upstream error')
        continue
      }
      if (t.bytes_total !== null && t.bytes_done >= t.bytes_total) {
        done.push(sim)
        continue
      }
      this.emit({ type: 'transfer.progress', ts: now, id: t.id, bytes_done: Math.round(t.bytes_done), bytes_total: t.bytes_total, rate_bps: Math.round(t.rate_bps), followers: t.followers, upstream_latency_ms: t.upstream_latency_ms })
    }
    for (const sim of done) this.finish(sim)
  }

  private release(sim: Sim): void {
    this.sims.delete(sim.t.id)
    for (const job of this.jobs) job.active.delete(sim.t.id)
  }

  private finish(sim: Sim): void {
    const t = sim.t
    const duration = Date.now() - Date.parse(t.started_at)
    t.state = 'finished'
    t.bytes_done = t.bytes_total ?? t.bytes_done
    this.release(sim)
    this.record(t.cache !== 'FETCHING' && t.cache !== 'COALESCED', t.cache === 'FETCHING' ? 0 : t.bytes_done, t.cache === 'FETCHING' ? t.bytes_done : 0)
    this.emit({ type: 'transfer.finished', ts: new Date().toISOString(), id: t.id, bytes_done: t.bytes_done, duration_ms: duration, cache: t.cache })
    const repo = this.repos.find((r) => r.name === t.repository)
    if (repo) {
      repo.stats.requests_24h++
      repo.stats.bytes_served_24h += t.bytes_done
      repo.stats.last_activity_at = new Date().toISOString()
    }
    if (t.cache === 'FETCHING') {
      this.storageUsed += t.bytes_done
      this.artifacts++
      if (repo) {
        repo.stats.size_bytes += t.bytes_done
        repo.stats.artifact_count++
      }
      if (t.bytes_done > 2 * MiB || Math.random() < 0.15) {
        this.addActivity({ kind: 'cache_miss', summary: `Cached ${t.component ?? t.artifact} from upstream`, outcome: 'success', repository: t.repository, format: t.format, artifact: t.artifact, path: t.path, principal: t.client.user ?? t.client.ip, cache: 'FETCHING', bytes: t.bytes_done, duration_ms: duration })
      }
    } else if (t.bytes_done > 50 * MiB || Math.random() < 0.08) {
      this.addActivity({ kind: 'download', summary: `${t.client.user ?? t.client.ip} downloaded ${t.artifact}`, outcome: 'success', repository: t.repository, format: t.format, artifact: t.artifact, path: t.path, principal: t.client.user ?? t.client.ip, cache: t.cache, bytes: t.bytes_done, duration_ms: duration })
    }
  }

  private fail(sim: Sim, error: string): void {
    const t = sim.t
    t.state = 'failed'
    t.error = error
    this.release(sim)
    this.record(false, 0, 0)
    this.emit({ type: 'transfer.failed', ts: new Date().toISOString(), id: t.id, bytes_done: Math.round(t.bytes_done), error })
    if (!sim.owner) {
      this.addActivity({ kind: 'error', summary: `${t.component ?? t.artifact}: ${error}`, outcome: 'failure', repository: t.repository, format: t.format, artifact: t.artifact, path: t.path, principal: t.client.user ?? t.client.ip, cache: 'ERROR', bytes: Math.round(t.bytes_done), duration_ms: Date.now() - Date.parse(t.started_at) })
    }
  }

  private record(hit: boolean, hitBytes: number, upstreamBytes: number): void {
    const now = Date.now()
    this.completions.push({ at: now, hit })
    const bucketTs = Math.floor(now / 1000 / HISTORY_BUCKET_S) * HISTORY_BUCKET_S * 1000
    let last = this.history[this.history.length - 1]
    if (!last || last.ts < bucketTs) {
      last = { ts: bucketTs, hit: 0, upstream: 0 }
      this.history.push(last)
      if (this.history.length > 90) this.history.shift()
    }
    last.hit += hitBytes
    last.upstream += upstreamBytes
  }

  private statsTick(): void {
    const now = Date.now()
    this.completions = this.completions.filter((c) => now - c.at < 60_000)
    const last10 = this.completions.filter((c) => now - c.at < 10_000).length
    const hits = this.completions.filter((c) => c.hit).length
    const active = [...this.sims.values()]
    const out = active.reduce((a, s) => a + s.t.rate_bps, 0)
    const upstream = active.filter((s) => s.t.cache === 'FETCHING').reduce((a, s) => a + s.t.rate_bps, 0)
    // Steady background of small metadata hits the simulation doesn't model one by one.
    const background = between(2.5, 6.5)
    this.emit({
      type: 'stats.tick',
      ts: new Date(now).toISOString(),
      requests_per_s: Math.round((last10 / 10 + background) * 10) / 10,
      bytes_out_per_s: Math.round(out),
      bytes_upstream_per_s: Math.round(upstream),
      bytes_saved_per_s: Math.round(out - upstream),
      hit_ratio_1m: this.completions.length ? (hits + background * 54) / (this.completions.length + background * 60) : null,
      active_transfers: active.length,
      storage_used_bytes: this.storageUsed,
      artifacts_cached: this.artifacts,
    })
  }

  emitUpstream(repo: string): void {
    const health = upstreamHealth(this).find((h) => h.repository === repo)
    if (health) this.emit({ type: 'upstream.state', ts: new Date().toISOString(), health })
  }

  /** When the auto-blocked upstream (epel) is next retried; each failed retry blocks it for another 10 min. */
  blockedUntil(): number {
    const now = Date.now()
    while (this.epelRetryAt <= now) this.epelRetryAt += 10 * 60_000
    return this.epelRetryAt
  }

  private miscActivity(): void {
    const roll = Math.random()
    const who = pick(['alice', 'bob', 'admin'])
    if (roll < 0.25) this.addActivity({ kind: 'auth', summary: `${who} signed in`, outcome: 'info', repository: null, format: null, artifact: null, principal: who, cache: null, bytes: null, duration_ms: null })
    else if (roll < 0.45) this.addActivity({ kind: 'cleanup', summary: `Cleanup policy "npm-unused-90d" removed ${Math.floor(between(40, 900))} assets`, outcome: 'success', repository: 'npm', format: 'npm', artifact: null, principal: 'scheduler', cache: null, bytes: Math.round(between(0.2, 4) * GiB), duration_ms: Math.round(between(4000, 60000)) })
    else if (roll < 0.7) this.addActivity({ kind: 'upstream_state', summary: `yum.oracle.com responding slowly (p95 ${between(2.1, 3.4).toFixed(1)} s); serving stale repomd.xml`, outcome: 'failure', repository: 'oracle', format: 'yum', artifact: null, principal: null, cache: 'STALE', bytes: null, duration_ms: null })
    else if (roll < 0.85) this.addActivity({ kind: 'upstream_state', summary: 'dl.fedoraproject.org still unreachable (connect timeout after 10 s); epel stays blocked for 10 min', outcome: 'failure', repository: 'epel', format: 'yum', artifact: null, principal: null, cache: null, bytes: null, duration_ms: null })
    else this.addActivity({ kind: 'repository', summary: 'Repository ghcr-proxy updated (metadata max age 6 h)', outcome: 'info', repository: 'ghcr-proxy', format: 'docker', artifact: null, principal: 'admin', cache: null, bytes: null, duration_ms: null })
  }

  private seedActivity(): void {
    const now = Date.now()
    const seed: Omit<ActivityEvent, 'id'>[] = [
      { ts: new Date(now - 42_000).toISOString(), kind: 'cache_miss', summary: 'Cached postgres:18.1 from upstream', outcome: 'success', repository: 'dockerhub-proxy', format: 'docker', artifact: 'v2/library/postgres/manifests/18.1', principal: '192.0.2.41', cache: 'FETCHING', bytes: 151 * MiB, duration_ms: 6120 },
      { ts: new Date(now - 2 * 60_000).toISOString(), kind: 'cache_miss', summary: 'Cached ghcr.io/immich-app/immich-server:v2.2.3 from upstream', outcome: 'success', repository: 'ghcr-proxy', format: 'docker', artifact: 'v2/immich-app/immich-server/manifests/v2.2.3', principal: '198.51.100.14', cache: 'FETCHING', bytes: 735 * MiB, duration_ms: 41_870 },
      { ts: new Date(now - 3 * 60_000).toISOString(), kind: 'error', summary: 'podman-5.4.0-13.0.1.el9_6: 503 Service Unavailable from yum.oracle.com', outcome: 'failure', repository: 'oracle', format: 'yum', artifact: 'repo/OracleLinux/OL9/appstream/x86_64/getPackage/podman-5.4.0-13.0.1.el9_6.x86_64.rpm', principal: '198.51.100.23', cache: 'ERROR', bytes: 0, duration_ms: 2480 },
      { ts: new Date(now - 7 * 60_000).toISOString(), kind: 'upstream_state', summary: 'dl.fedoraproject.org auto-blocked after 5 consecutive failures (retry in 10 min)', outcome: 'failure', repository: 'epel', format: 'yum', artifact: null, principal: null, cache: null, bytes: null, duration_ms: null },
      { ts: new Date(now - 11 * 60_000).toISOString(), kind: 'cache_miss', summary: 'Cached kernel-core-6.12.0-124.8.1.el10_1 from upstream', outcome: 'success', repository: 'rocky', format: 'yum', artifact: '10/BaseOS/x86_64/os/Packages/k/kernel-core-6.12.0-124.8.1.el10_1.x86_64.rpm', principal: '203.0.113.7', cache: 'FETCHING', bytes: 18 * MiB, duration_ms: 2310 },
      { ts: new Date(now - 19 * 60_000).toISOString(), kind: 'cache_miss', summary: 'Cached linux-image-6.17.0-9-generic 6.17.0-9.9 from upstream', outcome: 'success', repository: 'ubuntu-security', format: 'apt', artifact: 'pool/main/l/linux-image-6.17.0-9-generic/linux-image-6.17.0-9-generic_6.17.0-9.9_amd64.deb', principal: '192.0.2.88', cache: 'FETCHING', bytes: 16 * MiB, duration_ms: 1870 },
      { ts: new Date(now - 26 * 60_000).toISOString(), kind: 'cleanup', summary: 'Cleanup policy "docker-untagged-14d" removed 312 assets', outcome: 'success', repository: 'dockerhub-proxy', format: 'docker', artifact: null, principal: 'scheduler', cache: null, bytes: Math.round(7.4 * GiB), duration_ms: 48_200 },
      { ts: new Date(now - 48 * 60_000).toISOString(), kind: 'repository', summary: 'Repository raspberrypi created', outcome: 'info', repository: 'raspberrypi', format: 'apt', artifact: null, principal: 'admin', cache: null, bytes: null, duration_ms: null },
      { ts: new Date(now - 64 * 60_000).toISOString(), kind: 'auth', summary: 'admin signed in', outcome: 'info', repository: null, format: null, artifact: null, principal: 'admin', cache: null, bytes: null, duration_ms: null },
    ]
    const events: ActivityEvent[] = seed.map((e) => ({ id: `${Date.parse(e.ts)}-0`, ...e }))
    events.push(...historicActivity(now, 900))
    events.sort((a, b) => compareIdsDesc(a.id, b.id))
    this.activity.push(...events.slice(0, ACTIVITY_KEEP))
  }
}

/** A day of plausible history: client runs replayed into past activity events (denser in recent hours). */
function historicActivity(now: number, count: number): ActivityEvent[] {
  const plans: (() => JobPlan)[] = [() => dockerPull(Math.random() < 0.3), npmInstall, aptRun, () => dnfRun(), rawDownload, trickle]
  const out: ActivityEvent[] = []
  let seq = 1
  while (out.length < count) {
    const age = 90_000 + Math.random() ** 1.6 * 86_400_000
    let at = now - age
    const plan = pick(plans)()
    const who = plan.client.user ?? plan.client.ip
    for (const spec of plan.specs.slice(0, Math.ceil(between(1, 4)))) {
      at += between(80, 2400)
      const failed = spec.cache === 'ERROR' || Boolean(spec.error)
      const fetched = spec.cache === 'FETCHING' || spec.cache === 'COALESCED'
      const rate = fetched ? between(8, 45) * MiB : between(60, 110) * MiB
      const duration = Math.round(80 + (spec.size / rate) * 1000)
      const base = { repository: spec.repository, format: spec.format, artifact: spec.artifact, path: spec.path, principal: who, duration_ms: duration }
      const ts = new Date(at).toISOString()
      const id = `${Math.floor(at)}-${seq++}`
      if (failed) out.push({ id, ts, kind: 'error', summary: `${spec.component ?? spec.artifact}: ${spec.error ?? 'upstream answered 503'}`, outcome: 'failure', cache: 'ERROR', bytes: 0, ...base })
      else if (fetched) out.push({ id, ts, kind: 'cache_miss', summary: `Cached ${spec.component ?? spec.artifact} from upstream`, outcome: 'success', cache: 'FETCHING', bytes: spec.size, ...base })
      else out.push({ id, ts, kind: 'download', summary: `${who} downloaded ${spec.artifact}`, outcome: 'success', cache: spec.cache, bytes: spec.size, ...base })
    }
  }
  return out
}

/** Upstream health per proxy repo, derived from the world (used by REST and upstream.state events). */
export function upstreamHealth(world: MockWorld) {
  const now = Date.now()
  return world.repos
    .filter((r) => r.type === 'proxy')
    .map((r) => {
      const base = { repository: r.name, format: r.format, upstream_url: r.proxy?.upstream_url ?? '', status: r.status, rate_limit: null as null | { limit: number; remaining: number; window_s: number; reset_at: string; source: string } }
      if (r.name === 'dockerhub-proxy') {
        return { ...base, error_rate: 0.004, requests_1h: 412, p95_latency_ms: 238, last_error: '429 Too Many Requests (pull rate limit)', last_error_at: new Date(now - 26 * 3_600_000).toISOString(), blocked_until: null, rate_limit: { limit: world.rateLimit.limit, remaining: world.rateLimit.remaining, window_s: 21600, reset_at: new Date(world.rateLimit.resetAt).toISOString(), source: 'docker-hub' } }
      }
      if (r.status === 'degraded') return { ...base, error_rate: 0.183, requests_1h: 164, p95_latency_ms: 2870, last_error: '503 Service Unavailable', last_error_at: new Date(now - 3 * 60_000).toISOString(), blocked_until: null }
      if (r.status === 'blocked') return { ...base, error_rate: 1, requests_1h: 5, p95_latency_ms: null, last_error: 'connect timeout after 10 s', last_error_at: new Date(now - 7 * 60_000).toISOString(), blocked_until: new Date(world.blockedUntil()).toISOString() }
      if (r.status === 'offline') return { ...base, error_rate: 0, requests_1h: 0, p95_latency_ms: null, last_error: null, last_error_at: null, blocked_until: null }
      const p95: Record<string, number> = { npm: 182, apt: 96, yum: 121, raw: 341, docker: 205 }
      return { ...base, error_rate: Math.round(Math.random() * 40) / 10000, requests_1h: Math.round(r.stats.requests_24h / 24 / 6), p95_latency_ms: p95[r.format] ?? 150, last_error: null, last_error_at: null, blocked_until: null }
    })
}


export const world = new MockWorld()
