# Maven clients

How to point Maven, Gradle and sbt at Waystation **Maven** repositories: proxies (Maven Central or any Maven 2
layout repository), hosted repositories for your own releases and SNAPSHOTs, and groups that combine them. The
repository's **Set me up** tab shows the same snippets with your real URL filled in. Examples below use
`https://waystation.example.com` and the repositories `maven-central` (proxy of Maven Central), `maven-releases`
(hosted, release version policy), `maven-snapshots` (hosted, SNAPSHOT version policy) and `maven-public` (a group of
the three, in that order).

Status: proxy, hosted and group repositories (FORMATS.md "Maven"). Verified with Maven 3.9 by the real-client
harness (`tests/clients/maven/`: resolve through the proxy and the group with strict checksums, deploy a release
and SNAPSHOTs, consume them through the group). Gradle and sbt use the same repository layout and are expected to
work but are not yet in the harness.

## Repository options

- **Version policy** (proxy and hosted): `release` (default) holds only release versions, `snapshot` only
  `-SNAPSHOT` versions, `mixed` both. A release proxy answers `404` for SNAPSHOT paths without asking its upstream
  (and a group skips members whose policy excludes the version). Deploying a version the policy excludes answers
  `400`.
- **Layout policy**: `strict` (default) accepts only Maven layout paths (artifacts, signatures, checksums,
  `maven-metadata.xml`); `permissive` accepts any path (hosted: deploy any file; proxy: proxy any path).
- **Write policy** (hosted): `allow_redeploy` (the default), `disable_redeploy` (recommended for release
  repositories: a deployed release file can never be replaced), `read_only`. SNAPSHOT versions can always be
  redeployed.

## Maven

Resolve everything through Waystation with a mirror in `~/.m2/settings.xml`. Use the group, so your own releases,
SNAPSHOTs and Maven Central come from one URL:

```xml
<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0">
  <mirrors>
    <mirror>
      <id>maven-public</id>
      <mirrorOf>*</mirrorOf>
      <url>https://waystation.example.com/repository/maven-public/</url>
    </mirror>
  </mirrors>
  <servers>
    <!-- only when anonymous reads are not allowed -->
    <server>
      <id>maven-public</id>
      <username>your-username</username>
      <password>your-api-token</password>
    </server>
  </servers>
</settings>
```

The `<server>` id must match the mirror id. Use `--strict-checksums` (`-C`) in CI: Waystation serves checksum
files for everything it holds, so a checksum failure is a real problem, not a missing file.

### Deploy (mvn deploy)

In the project's `pom.xml`:

```xml
<distributionManagement>
  <repository>
    <id>maven-releases</id>
    <url>https://waystation.example.com/repository/maven-releases/</url>
  </repository>
  <snapshotRepository>
    <id>maven-snapshots</id>
    <url>https://waystation.example.com/repository/maven-snapshots/</url>
  </snapshotRepository>
</distributionManagement>
```

and a `<server>` per repository id in `settings.xml` with your username and an API token. Without changing the pom:

```sh
mvn deploy -DaltDeploymentRepository=maven-releases::https://waystation.example.com/repository/maven-releases/
```

Waystation keeps `maven-metadata.xml` itself: after every deploy (and delete) it rewrites the artifact's document
from what the repository holds (every version in Maven order, `latest` and `release` recomputed) and, for a
SNAPSHOT version, the version's document (newest timestamped build per classifier and extension). The documents
Maven uploads are checked and then ignored, so parallel deploys of different versions never lose one. Uploaded
checksum files are compared with the stored file (`400` when they differ) and not stored: checksums are generated
on request from the stored file. Groups are read-only: deploy to a hosted repository.

## Gradle

`build.gradle.kts`:

```kotlin
repositories {
    maven {
        url = uri("https://waystation.example.com/repository/maven-public/")
        credentials {  // only when anonymous reads are not allowed
            username = providers.gradleProperty("wsUser").get()
            password = providers.gradleProperty("wsToken").get()
        }
    }
}
```

Publishing (`maven-publish` plugin):

```kotlin
plugins {
    `maven-publish`
}

publishing {
    repositories {
        maven {
            name = "Waystation"
            url = uri(
                if (version.toString().endsWith("SNAPSHOT"))
                    "https://waystation.example.com/repository/maven-snapshots/"
                else "https://waystation.example.com/repository/maven-releases/"
            )
            credentials {
                username = providers.gradleProperty("wsUser").get()
                password = providers.gradleProperty("wsToken").get()
            }
        }
    }
}
```

with `wsUser` and `wsToken` in `~/.gradle/gradle.properties` (never in the project).

## sbt

```scala
resolvers += "maven-public" at "https://waystation.example.com/repository/maven-public/"
credentials += Credentials("Waystation", "waystation.example.com", "your-username", "your-api-token")
```

## Authentication

Maven, Gradle and sbt send HTTP basic credentials after a `401` with `WWW-Authenticate: Basic` (which Waystation
answers when anonymous access is not enough). Use your username and an **API token** as the password (create one on
your profile page: avatar menu → Profile → API tokens; deploying needs the `write` scope, deleting `delete`). Any
username works with a token; the token decides. Proxy repositories are usually readable anonymously; hosted and
group repositories follow your RBAC settings.

## What Waystation verifies (proxies)

- Every artifact is checked while it downloads against the upstream's `.sha1` (or `.sha256` when there is no
  `.sha1`). A mismatch is never cached and the client sees a broken transfer (a 502 when nothing was sent yet).
- `maven-metadata.xml` must parse (no DTDs or entities, bounded size) or it is refused and not cached; it is
  revalidated after the repository's metadata max age (default 30 minutes).
- Checksum files (`.md5`, `.sha1`, `.sha256`, `.sha512`) of cached files are generated from the stored file, so
  they always match what Waystation serves. `.asc` signatures pass through untouched (verify them with your own
  keys, e.g. a PGP verification plugin).

## Troubleshooting

- **`Could not transfer metadata ... (404)` for a SNAPSHOT through a release proxy**: expected; resolve SNAPSHOTs
  through a group that contains your SNAPSHOT repository.
- **`status code: 400` on deploy**: the message names the reason: a release already deployed (write policy), a
  version the version policy does not accept, a path that is not a Maven layout path (strict layout), or a checksum
  that does not match the uploaded file.
- **`status code: 401`**: the `<server>` id does not match the repository (or mirror) id, or the token was revoked
  or lacks the `write` scope.
- **`status code: 405` on deploy**: you deployed to a group; use the hosted repository's URL.
- **Stale versions**: the proxy keeps `maven-metadata.xml` for the repository's metadata max age; lower it for
  upstreams that publish often, or wait.
