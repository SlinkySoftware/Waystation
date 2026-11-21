// SPDX-License-Identifier: GPL-3.0-or-later
// End-to-end flows on the mock build (T-U09): sign in, the repository list and create flow, every repository tab,
// browse (tree, packages, referrers, README), Set me up OS variants, density and the showcase. Every test fails on
// a console error or an uncaught page error.
import { expect, type Page, test } from '@playwright/test'

import { open, prepare, watchConsole } from './support'

let problems: string[] = []
test.beforeEach(({ page }) => {
  problems = watchConsole(page)
})
test.afterEach(() => {
  expect(problems, 'console errors').toEqual([])
})

const tab = (page: Page, name: string) => page.getByRole('tab', { name, exact: true })

test('signs in through the form and lands on the dashboard', async ({ page }) => {
  await prepare(page, { user: null })
  await page.goto('sign-in')
  await page.waitForLoadState('networkidle')
  problems.splice(0) // the session check answers 401 before signing in, by design
  await page.locator('input[autocomplete=username]').fill('admin')
  await page.locator('input[autocomplete=current-password]').fill('mock-password')
  await page.getByRole('button', { name: /sign in/i }).click()
  await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible()
  await expect(page.getByText('Live transfers')).toBeVisible()
})

test('repository list: filters, card grid kept across visits', async ({ page }) => {
  await prepare(page)
  await open(page, 'repositories')
  await page.getByRole('button', { name: 'Cards' }).click()
  await expect(page).toHaveURL(/view=grid/)
  await expect(page.locator('.repo-card')).toHaveCount(13)
  await page.getByPlaceholder('Search by name or description').fill('debian')
  await expect(page.locator('.repo-card')).toHaveCount(2)
  await open(page, 'repositories')
  await expect(page.locator('.repos__grid')).toBeVisible() // remembered on this browser
  await page.getByRole('button', { name: 'Table' }).click()
  await expect(page.locator('.ws-table')).toBeVisible()
})

test('creates an npm proxy and opens it', async ({ page }) => {
  await prepare(page)
  await open(page, 'repositories')
  await page.getByRole('button', { name: 'Create repository' }).first().click()
  await page.getByRole('button', { name: 'npm', exact: true }).click()
  await page.getByRole('button', { name: /^Proxy/ }).click()
  await page.getByLabel('Name', { exact: true }).fill('npm-e2e')
  await page.getByLabel(/Upstream URL/).fill('https://registry.npmjs.org')
  await page.getByRole('dialog').getByRole('button', { name: 'Create repository' }).click()
  await expect(page).toHaveURL(/repositories\/npm-e2e/)
  await expect(page.getByRole('heading', { name: /npm-e2e/ })).toBeVisible()
})

test('repository tabs follow the URL and old browse links reach Packages', async ({ page }) => {
  await prepare(page)
  await open(page, 'repositories/dockerhub-proxy/browse?image=library%2Fnginx')
  await expect(page).toHaveURL(/dockerhub-proxy\/packages\?image=library(%2F|\/)nginx/)
  await expect(tab(page, 'Packages')).toHaveAttribute('aria-selected', 'true')
  for (const name of ['Overview', 'Browse', 'Activity', 'Cleanup', 'Members', 'Settings']) {
    await tab(page, name).click()
    await expect(page).toHaveURL(new RegExp(`dockerhub-proxy${name === 'Overview' ? '/overview' : `/${name.toLowerCase()}`}`))
  }
  await tab(page, 'Members').click()
  await expect(page.getByText('Administrators can do everything')).toBeVisible()
  await tab(page, 'Activity').click()
  await expect(page.locator('.act-row--item').first()).toBeVisible()
})

test('raw repositories have no Packages tab', async ({ page }) => {
  await prepare(page)
  await open(page, 'repositories/raw-releases')
  await expect(tab(page, 'Browse')).toBeVisible()
  await expect(tab(page, 'Packages')).toHaveCount(0)
})

test('the file tree works with the keyboard and deep links', async ({ page }) => {
  await prepare(page)
  await open(page, 'repositories/raw-releases/browse')
  const tree = page.getByRole('tree')
  await tree.focus()
  await page.keyboard.press('ArrowDown') // astral-sh
  await page.keyboard.press('ArrowDown') // cli
  await page.keyboard.press('ArrowRight') // open cli
  await expect(page.getByRole('treeitem', { name: /^cli/ }).first()).toHaveAttribute('aria-expanded', 'true')
  await expect(page.getByRole('treeitem', { name: /^cli/ })).toHaveCount(2) // cli/cli loaded
  await page.keyboard.press('ArrowRight') // to its child
  await page.keyboard.press('Enter')
  await expect(page).toHaveURL(/path=cli(%2F|\/)cli$/)
  await open(page, 'repositories/raw-releases/browse?path=jqlang%2Fjq%2Freleases%2Fdownload%2Fjq-1.8.1%2Fjq-linux-amd64')
  await expect(page.getByRole('treeitem', { name: 'jq-linux-amd64' })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByText('https://registry.example.com/repository/raw-releases/jqlang/jq/releases/download/jq-1.8.1/jq-linux-amd64')).toBeVisible()
})

test('docker manifest shows its referrers', async ({ page }) => {
  await prepare(page)
  await open(page, 'repositories/ghcr-proxy/packages?image=immich-app%2Fimmich-server&ref=v2.2.3')
  const refs = page.getByRole('region', { name: 'Referrers' })
  await expect(refs.getByText('Signature')).toBeVisible()
  await expect(refs.getByText('SBOM')).toBeVisible()
  await expect(refs.getByText('Attestation')).toBeVisible()
  await open(page, 'repositories/dockerhub-proxy/packages?image=library%2Fnginx&ref=1.29')
  await expect(page.getByText('No signatures, SBOMs or attestations point at this image here.')).toBeVisible()
})

test('README renders as safe Markdown', async ({ page }) => {
  await prepare(page)
  await open(page, 'repositories/npm/packages?package=vue')
  await page.getByRole('tab', { name: 'README' }).click()
  const readme = page.locator('.markdown')
  await expect(readme.getByRole('heading', { name: 'Install' })).toBeVisible()
  await expect(readme.locator('table')).toBeVisible()
  await expect(readme.locator('script, img, [onclick]')).toHaveCount(0)
  await expect(readme).toContainText('<script>alert("never runs")</script>')
  await expect(readme.locator('a[href^="javascript"]')).toHaveCount(0)
})

test('Set me up shows the snippets of the chosen system', async ({ page }) => {
  await prepare(page)
  await open(page, 'repositories/dockerhub-proxy?setup=1')
  const panel = page.getByRole('dialog')
  await panel.getByRole('button', { name: 'Linux' }).click()
  await expect(panel.getByText('/etc/docker/daemon.json')).toBeVisible()
  await panel.getByRole('button', { name: 'Windows' }).click()
  await expect(panel.getByText('Docker Desktop: Settings > Docker Engine')).toBeVisible()
  await expect(panel.getByText('/etc/docker/daemon.json')).toHaveCount(0)
})

test('compact density is saved and applied app-wide', async ({ page }) => {
  await prepare(page)
  await open(page, 'profile')
  await page.getByRole('button', { name: 'Compact' }).click()
  await expect(page.locator('body')).toHaveClass(/ws-compact/)
  await open(page, 'repositories')
  await expect(page.locator('body')).toHaveClass(/ws-compact/)
  await open(page, 'profile')
  await page.getByRole('button', { name: 'Comfortable' }).click()
  await expect(page.locator('body')).not.toHaveClass(/ws-compact/)
})

test('profile: edit the display name, see sessions', async ({ page }) => {
  await prepare(page)
  await open(page, 'profile')
  await page.getByRole('button', { name: 'Edit' }).click()
  await page.getByLabel('Display name').fill('Ada Admin')
  await page.getByRole('button', { name: 'Save' }).click()
  await expect(page.getByText('Profile saved')).toBeVisible()
  await expect(page.locator('.account__facts')).toContainText('Ada Admin')
  await expect(page.getByRole('region', { name: 'Sessions' })).toContainText('This session')
})

test('the component showcase shows both themes', async ({ page }) => {
  await prepare(page)
  await open(page, 'dev/showcase')
  await expect(page.locator('[data-theme=light] .gauge')).toHaveCount(2)
  await expect(page.locator('[data-theme=dark] .gauge')).toHaveCount(2)
  const bg = await page.locator('[data-theme=dark]').evaluate((el) => getComputedStyle(el).backgroundColor)
  expect(bg).toBe('rgb(28, 28, 30)')
})
