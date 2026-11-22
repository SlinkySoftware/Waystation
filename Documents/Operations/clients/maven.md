# Maven client guide

How to point Maven, Gradle and sbt at Waystation **Maven** repositories. The repository's **Set me up** tab shows the
same snippets with your real URL filled in. Examples below use `https://waystation.example.com` and the repositories
`maven-central` (proxy of Maven Central), `maven-releases` (hosted, release version policy), `maven-snapshots`
(hosted, SNAPSHOT version policy) and `maven-public` (a group of the three, in that order).

## Overview

Waystation serves the Maven 2 repository layout: proxies of Maven Central or any Maven 2 layout repository, hosted
repositories for your own releases and SNAPSHOTs, and groups that combine them. Waystation maintains
`maven-metadata.xml` itself and generates checksum files (`.md5`, `.sha1`, `.sha256`, `.sha512`) for everything it
holds. Maven components (one per version) are found by artifactId in the global search.

Every repository answers at `https://waystation.example.com/repository/<name>/`, followed by the usual layout path
(`com/example/app/1.0.0/app-1.0.0.jar`).

Tested with Maven 3.9, Maven 4 (4.0.0-rc-7), Gradle 8.14 and 9.9 and sbt 1.13: each resolves through the proxy and the
group with checksum verification (Maven `--strict-checksums`, Gradle strict dependency verification, coursier with
SHA-1 required), publishes a release and SNAPSHOTs with credentials sent after the `401` challenge, is refused a
redeploy of the release and an anonymous publish, and consumes what it published through the group.

## Repository types

- **Proxy**: caches a Maven 2 layout repository (Maven Central by default).
- **Hosted**: your own releases and SNAPSHOTs, deployed with `mvn deploy`, Gradle `maven-publish` or sbt `publish`.
- **Group**: one URL for several Maven repositories, read-only (deploy to a hosted repository). A group skips members
  whose version policy excludes the requested version.

### Repository options

- **Version policy** (proxy and hosted): `release` (default) holds only release versions, `snapshot` only
  `-SNAPSHOT` versions, `mixed` both. A release proxy answers `404` for SNAPSHOT paths without asking its upstream.
  Deploying a version the policy excludes answers `400`.
- **Layout policy**: `strict` (default) accepts only Maven layout paths (artifacts, signatures, checksums,
  `maven-metadata.xml`); `permissive` accepts any path (hosted: deploy any file; proxy: proxy any path).
- **Write policy** (hosted): `allow_redeploy` (the default), `disable_redeploy` (recommended for release
  repositories: a deployed release file can never be replaced), `read_only`. SNAPSHOT versions can always be
  redeployed.

### What Waystation verifies (proxies)

- Every artifact is checked while it downloads against the upstream's `.sha1` (or `.sha256` when there is no
  `.sha1`). A mismatch is never cached and the client sees a broken transfer (a `502` when nothing was sent yet).
- `maven-metadata.xml` must parse (no DTDs or entities, bounded size) or it is refused and not cached; it is
  revalidated after the repository's metadata max age (default 30 minutes).
- Checksum files (`.md5`, `.sha1`, `.sha256`, `.sha512`) of cached files are generated from the stored file, so
  they always match what Waystation serves. `.asc` signatures pass through untouched (verify them with your own
  keys, e.g. a PGP verification plugin).

## Client setup

### Maven

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
  <!-- Maven Central (which the mirror replaces) has SNAPSHOTs turned off: turn them on -->
  <profiles>
    <profile>
      <id>waystation</id>
      <repositories>
        <repository>
          <id>central</id>
          <url>http://central</url>
          <snapshots><enabled>true</enabled></snapshots>
        </repository>
      </repositories>
    </profile>
  </profiles>
  <activeProfiles><activeProfile>waystation</activeProfile></activeProfiles>
</settings>
```

The profile matters only when SNAPSHOTs come through the mirror (a group with a SNAPSHOT member, or a SNAPSHOT
repository): the mirror replaces Maven Central, whose definition in Maven's super POM has SNAPSHOTs disabled, so
without it Maven never asks for a SNAPSHOT (`Could not find artifact ...-SNAPSHOT` within a fraction of a second).

The `<server>` id must match the mirror id. Use `--strict-checksums` (`-C`) in CI: Waystation serves checksum files
for everything it holds, so a checksum failure is a real problem, not a missing file.

### Gradle

`build.gradle.kts`:

```kotlin
repositories {
    maven {
        url = uri("https://waystation.example.com/repository/maven-public/")
        credentials {  // only when anonymous reads are not allowed
            username = providers.gradleProperty("wsUser").get()
            password = providers.gradleProperty("wsToken").get()
        }
        authentication { create<BasicAuthentication>("basic") }  // optional: send the credentials up front
    }
}
```

with `wsUser` and `wsToken` in `~/.gradle/gradle.properties` (never in the project).

Checksums: by default Gradle does not verify downloads against the repository's checksum files. Use its dependency
verification: `gradle --write-verification-metadata sha256 build` once (review and commit
`gradle/verification-metadata.xml`), then every build verifies each artifact and POM/module file against it (strict
by default).

Plain `http://` URLs (a test instance) need `isAllowInsecureProtocol = true` in the `maven { }` block.

### sbt

```scala
resolvers += "maven-public" at "https://waystation.example.com/repository/maven-public/"
credentials += Credentials("Waystation", "waystation.example.com", "your-username", "your-api-token")
// SHA-1 checksums are required instead of "if present"
update / checksums := Vector("sha1")
```

The realm must be `Waystation` (the realm of Waystation's `401` challenge) and the host the bare host name, without
scheme or port. To use only Waystation, set `externalResolvers := Seq(...)` instead of adding to `resolvers`. Plain
`http://` URLs need `("id" at "http://...").withAllowInsecureProtocol(true)`.

## Authentication

Use your username and an **API token** as the password (create one on your profile page: avatar menu → Profile →
API tokens; deploying needs the `write` scope, deleting `delete`). Any username works with a token; the token
decides. Proxy repositories are usually readable anonymously; hosted and group repositories follow your RBAC
settings. Account passwords are not accepted by package clients.

**Plain credentials are enough.** Maven, Gradle and sbt send credentials for a `GET` only after a `401` challenge.
Waystation answers a request that carries no credentials with `401` and `WWW-Authenticate: Basic realm="Waystation"`
when anonymous users cannot see the repository
([ADR 0005](../../Architecture/adr/0005-anonymous-challenge-on-format-routes.md), as Nexus does), so the client
retries with the `settings.xml` (or Gradle `credentials`) username and token. A repository that does not exist
answers the same challenge, so anonymous callers cannot tell the two apart. A request *with* credentials that may not
see the repository gets `404`.

Optional: send the credentials up front to save the extra round trip per request:

- Maven 3.9: `-Daether.connector.http.preemptiveAuth=true`, best in the project's `.mvn/maven.config` (one
  argument per line) or in `MAVEN_OPTS`/`MAVEN_ARGS`. Deploys (`PUT`) already send credentials up front.
- Gradle: `authentication { create<BasicAuthentication>("basic") }` inside the `maven { }` block, next to
  `credentials`.

## Publishing

Deploy to a hosted repository; groups are read-only.

### Maven (mvn deploy)

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

### Gradle (maven-publish)

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
            authentication { create<BasicAuthentication>("basic") }
        }
    }
}
```

Gradle publishes the jar, the POM, Gradle module metadata (`.module`) and `.md5`/`.sha1`/`.sha256`/`.sha512`
checksums; SNAPSHOTs are timestamped. Waystation stores the module metadata like any artifact file and regenerates
`maven-metadata.xml` itself.

### sbt (publish)

```scala
publishMavenStyle := true
publishTo := Some(
  if (isSnapshot.value) "maven-snapshots" at "https://waystation.example.com/repository/maven-snapshots/"
  else "maven-releases" at "https://waystation.example.com/repository/maven-releases/"
)
```

sbt publishes SNAPSHOTs without timestamps (`app-1.1.0-SNAPSHOT.jar`, replaced on every publish), which Waystation
serves as they are. sbt refuses to republish an existing release itself; with
`publishConfiguration ~= (_.withOverwrite(true))` the repository's write policy decides (`400` with
`disable_redeploy`).

### How deploys are processed

Waystation keeps `maven-metadata.xml` itself: after every deploy (and delete) it rewrites the artifact's document
from what the repository holds (every version in Maven order, `latest` and `release` recomputed) and, for a
SNAPSHOT version, the version's document (newest timestamped build per classifier and extension). The documents
clients upload are checked and then ignored, so parallel deploys of different versions never lose one. Uploaded
checksum files are compared with the stored file (`400` when they differ) and not stored: checksums are generated
on request from the stored file.

## Limitations

- Groups are read-only: a deploy to a group answers `405`.
- A release proxy never serves SNAPSHOTs; resolve them through a group containing a SNAPSHOT repository.
- With the `strict` layout policy only Maven layout paths are accepted.
- `.asc` signatures are passed through, not verified.
- Gradle does not verify checksums unless dependency verification is enabled.

## Troubleshooting

- **`Could not transfer metadata ... (404)` for a SNAPSHOT through a release proxy**: expected; resolve SNAPSHOTs
  through a group that contains your SNAPSHOT repository.
- **`status code: 400` on deploy**: the message names the reason: a release already deployed (write policy), a
  version the version policy does not accept, a path that is not a Maven layout path (strict layout), or a checksum
  that does not match the uploaded file.
- **`status code: 401`** that persists: the client has no credentials for this repository; the `<server>` id does
  not match the repository (or mirror) id, or the token was revoked or lacks the `write` scope.
- **`404` with credentials**: your token or account cannot read the repository, or the path does not exist.
- **`status code: 405` on deploy**: you deployed to a group; use the hosted repository's URL.
- **Stale versions**: the proxy keeps `maven-metadata.xml` for the repository's metadata max age; lower it for
  upstreams that publish often, or wait.
