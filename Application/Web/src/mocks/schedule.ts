// SPDX-License-Identifier: GPL-3.0-or-later
// When a mock schedule fires next: @every, @daily / @hourly and the minute and hour fields of a 5-field cron (enough
// for the demo's schedules; the server has the real cron parser). Shared by the tasks and prefetch mocks.

const MINUTE = 60_000
const iso = (ms: number) => new Date(ms).toISOString()

export function nextFireOf(schedule: string, now: number): string {
  const every = /^@every (\d+)([smh])$/.exec(schedule)
  if (every) {
    const step = Number(every[1]) * { s: 1000, m: MINUTE, h: 60 * MINUTE }[every[2] as 's' | 'm' | 'h']
    return iso((Math.floor(now / step) + 1) * step)
  }
  const [mi, hr] = schedule.replace('@daily', '0 0 * * *').replace('@hourly', '0 * * * *').split(' ')
  const at = new Date(now)
  at.setUTCSeconds(0, 0)
  for (let i = 0; i < 60 * 24 * 8; i++) {
    at.setUTCMinutes(at.getUTCMinutes() + 1)
    const ok = (field: string | undefined, value: number) => !field || field === '*' || (field.startsWith('*/') ? value % Number(field.slice(2)) === 0 : field.split(',').includes(String(value)))
    if (ok(mi, at.getUTCMinutes()) && ok(hr, at.getUTCHours())) return at.toISOString()
  }
  return iso(now + 86_400_000)
}
