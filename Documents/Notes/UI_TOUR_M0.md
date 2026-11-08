# M0 UI tour

Screenshots of the M0 feedback build running against the REAL server (not mocks), taken on 2026-11-09 after
`make e2e-m0` cached real content: Docker images (nginx, debian, node), npm packages (chalk and friends), Debian
trixie packages (curl, jq) and a GitHub release (jq 1.8.1). Both themes, desktop (1440x900) and mobile (390x844 @2x).
Full-page shots where the page scrolls. Compressed with pngquant.

Retake: with the stack running and `make e2e-m0` done, from the repository root:

```sh
docker run --rm --network host --user "$(id -u):$(id -g)" -e HOME=/tmp -e REAL=1 \
  -v /path/to/admin-password:/run/adminpw:ro -v "$PWD:/src" -w /src/Application/Web \
  mcr.microsoft.com/playwright:v1.65.0-noble \
  sh -c 'npm i --no-save --prefix /tmp/pw playwright@1.65.0 >/dev/null && NODE_PATH=/tmp/pw/node_modules node scripts/screenshots.mjs'
```

Give feedback per page in [UI_FEEDBACK.md](UI_FEEDBACK.md).

| Page | Light, desktop | Dark, desktop | Light, mobile | Dark, mobile |
|---|---|---|---|---|
| **Sign in**: The sign-in page (the only page shown without a session). | [light](screenshots/m0/sign-in-light-desktop.png) | [dark](screenshots/m0/sign-in-dark-desktop.png) | [light](screenshots/m0/sign-in-light-mobile.png) | [dark](screenshots/m0/sign-in-dark-mobile.png) |
| **Dashboard**: KPI tiles, live transfers, traffic chart, upstream health (with the Docker Hub pull allowance), recent activity, storage by format. | [light](screenshots/m0/dashboard-light-desktop.png) | [dark](screenshots/m0/dashboard-dark-desktop.png) | [light](screenshots/m0/dashboard-light-mobile.png) | [dark](screenshots/m0/dashboard-dark-mobile.png) |
| **Repositories**: The demo repository set: Docker Hub and GHCR proxies, Debian and Ubuntu (+ security), npm, GitHub releases. | [light](screenshots/m0/repositories-light-desktop.png) | [dark](screenshots/m0/repositories-dark-desktop.png) | [light](screenshots/m0/repositories-light-mobile.png) | [dark](screenshots/m0/repositories-dark-mobile.png) |
| **Create repository**: Create dialog: Docker proxy with a preset applied (Google gcr.io), test-upstream button. | [light](screenshots/m0/create-repository-light-desktop.png) | [dark](screenshots/m0/create-repository-dark-desktop.png) | [light](screenshots/m0/create-repository-light-mobile.png) | [dark](screenshots/m0/create-repository-dark-mobile.png) |
| **Set me up: Docker**: Mirror-mode and prefix-pull instructions for the default Docker Hub proxy. | [light](screenshots/m0/repo-dockerhub-proxy-setup-light-desktop.png) | [dark](screenshots/m0/repo-dockerhub-proxy-setup-dark-desktop.png) | [light](screenshots/m0/repo-dockerhub-proxy-setup-light-mobile.png) | [dark](screenshots/m0/repo-dockerhub-proxy-setup-dark-mobile.png) |
| **Set me up: npm**: `.npmrc` / `npm config` snippets. | [light](screenshots/m0/repo-npm-setup-light-desktop.png) | [dark](screenshots/m0/repo-npm-setup-dark-desktop.png) | [light](screenshots/m0/repo-npm-setup-light-mobile.png) | [dark](screenshots/m0/repo-npm-setup-dark-mobile.png) |
| **Set me up: apt**: deb822 and one-line sources for the Debian proxy. | [light](screenshots/m0/repo-debian-setup-light-desktop.png) | [dark](screenshots/m0/repo-debian-setup-dark-desktop.png) | [light](screenshots/m0/repo-debian-setup-light-mobile.png) | [dark](screenshots/m0/repo-debian-setup-dark-mobile.png) |
| **Set me up: raw**: curl / wget download examples. | [light](screenshots/m0/repo-raw-releases-setup-light-desktop.png) | [dark](screenshots/m0/repo-raw-releases-setup-dark-desktop.png) | [light](screenshots/m0/repo-raw-releases-setup-light-mobile.png) | [dark](screenshots/m0/repo-raw-releases-setup-dark-mobile.png) |
| **Repository overview**: Stats strip, traffic and recent activity for one repository. | [light](screenshots/m0/repo-debian-overview-light-desktop.png) | [dark](screenshots/m0/repo-debian-overview-dark-desktop.png) | [light](screenshots/m0/repo-debian-overview-light-mobile.png) | [dark](screenshots/m0/repo-debian-overview-dark-mobile.png) |
| **Browse: Docker images**: Images cached in the Docker Hub proxy. | [light](screenshots/m0/browse-docker-images-light-desktop.png) | [dark](screenshots/m0/browse-docker-images-dark-desktop.png) | [light](screenshots/m0/browse-docker-images-light-mobile.png) | [dark](screenshots/m0/browse-docker-images-dark-mobile.png) |
| **Browse: Docker tags**: Tags of `library/nginx`. | [light](screenshots/m0/browse-docker-tags-light-desktop.png) | [dark](screenshots/m0/browse-docker-tags-dark-desktop.png) | [light](screenshots/m0/browse-docker-tags-light-mobile.png) | [dark](screenshots/m0/browse-docker-tags-dark-mobile.png) |
| **Browse: Docker manifest**: `nginx:latest`: platforms, layers with their cache state, pull commands. | [light](screenshots/m0/browse-docker-manifest-light-desktop.png) | [dark](screenshots/m0/browse-docker-manifest-dark-desktop.png) | [light](screenshots/m0/browse-docker-manifest-light-mobile.png) | [dark](screenshots/m0/browse-docker-manifest-dark-mobile.png) |
| **Browse: npm versions**: Versions of `chalk`, cached ones marked. | [light](screenshots/m0/browse-npm-versions-light-desktop.png) | [dark](screenshots/m0/browse-npm-versions-dark-desktop.png) | [light](screenshots/m0/browse-npm-versions-light-mobile.png) | [dark](screenshots/m0/browse-npm-versions-dark-mobile.png) |
| **Browse: apt packages**: Packages of the `trixie` suite. | [light](screenshots/m0/browse-apt-packages-light-desktop.png) | [dark](screenshots/m0/browse-apt-packages-dark-desktop.png) | [light](screenshots/m0/browse-apt-packages-light-mobile.png) | [dark](screenshots/m0/browse-apt-packages-dark-mobile.png) |
| **Browse: raw folder**: A GitHub release folder with the cached `jq` files. | [light](screenshots/m0/browse-raw-light-desktop.png) | [dark](screenshots/m0/browse-raw-dark-desktop.png) | [light](screenshots/m0/browse-raw-light-mobile.png) | [dark](screenshots/m0/browse-raw-dark-mobile.png) |
| **Browse (all repositories)**: The repository picker for browsing. | [light](screenshots/m0/browse-all-light-desktop.png) | [dark](screenshots/m0/browse-all-dark-desktop.png) | [light](screenshots/m0/browse-all-light-mobile.png) | [dark](screenshots/m0/browse-all-dark-mobile.png) |
| **Activity**: live tail of requests and events with filters and CSV export. | [light](screenshots/m0/activity-light-desktop.png) | [dark](screenshots/m0/activity-dark-desktop.png) | [light](screenshots/m0/activity-light-mobile.png) | [dark](screenshots/m0/activity-dark-mobile.png) |
| **Storage**: volume usage, usage by format, the largest repositories. | [light](screenshots/m0/storage-light-desktop.png) | [dark](screenshots/m0/storage-dark-desktop.png) | [light](screenshots/m0/storage-light-mobile.png) | [dark](screenshots/m0/storage-dark-mobile.png) |
| **Repository settings**: Settings tab of the Docker Hub proxy. | [light](screenshots/m0/repo-settings-light-desktop.png) | [dark](screenshots/m0/repo-settings-dark-desktop.png) | [light](screenshots/m0/repo-settings-light-mobile.png) | [dark](screenshots/m0/repo-settings-dark-mobile.png) |
| **Auth providers**: sign-in providers (local, LDAP) under Administration. | [light](screenshots/m0/auth-providers-light-desktop.png) | [dark](screenshots/m0/auth-providers-dark-desktop.png) | [light](screenshots/m0/auth-providers-light-mobile.png) | [dark](screenshots/m0/auth-providers-dark-mobile.png) |
| **Profile**: account details and personal API tokens. | [light](screenshots/m0/profile-tokens-light-desktop.png) | [dark](screenshots/m0/profile-tokens-dark-desktop.png) | [light](screenshots/m0/profile-tokens-light-mobile.png) | [dark](screenshots/m0/profile-tokens-dark-mobile.png) |
