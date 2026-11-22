#!/bin/sh
# Gradle through Waystation (client-jvm container): gradle.sh <8|9> runs /usr/local/bin/gradle<8|9>.
# Resolves a small dependency set (hamcrest-library 1.3 with hamcrest-core, reactive-streams through a version range)
# through the Maven Central proxy, writing Gradle's dependency verification metadata (sha256 of every artifact and
# POM/module file), then through the group from an empty Gradle home with strict verification against it (and a
# tampered checksum must fail). Publishes with maven-publish (jar, POM, Gradle module metadata, md5/sha1/sha256/sha512)
# a release and two SNAPSHOT builds, checks refusals (redeploy, anonymous publish, a wrong token) and consumes both
# through the group. Credentials: the admin's username and an API token, sent after the 401 challenge (no
# `authentication { basic }`: ADR 0005). Plain HTTP needs `allowInsecureProtocol`.
set -eu
. /harness/lib.sh

v=$1
gradle=gradle$v
art=hello-gradle$v
work=/tmp/gradle$v-harness
base="$WAYSTATION_URL/repository"
rm -rf "$work"
mkdir -p "$work"

ws_login
ws_token '["write"]'
token=$WS_TOKEN

run_gradle() {  # run_gradle <project dir> <Gradle user home> <args...>
  _dir=$1 _home=$2
  shift 2
  (cd "$_dir" && "$gradle" --no-daemon --console=plain --no-configuration-cache --warning-mode=none -g "$_home" \
    -Dorg.gradle.vfs.watch=false -PwsBase="$base" "$@")
}

project() {  # project <dir> <artifactId> <dependencies block body>
  mkdir -p "$1/src/main/java/harness"
  echo "rootProject.name = '$2'" > "$1/settings.gradle"
  cat > "$1/build.gradle" <<EOF
plugins {
    id 'java-library'
    id 'maven-publish'
}
group = 'com.example.harness'
version = findProperty('ver') ?: '1.0.0'
tasks.withType(JavaCompile).configureEach { options.release = 21 }

repositories {
    maven {
        url = "\${wsBase}/\${findProperty('wsRepo') ?: 'maven-public'}/"
        allowInsecureProtocol = true
        if (findProperty('wsToken')) {
            credentials {
                username = 'admin'
                password = findProperty('wsToken')
            }
        }
    }
}
dependencies {
$3
}
tasks.register('resolve', Copy) {
    from configurations.runtimeClasspath
    into layout.buildDirectory.dir('deps')
}
publishing {
    publications { maven(MavenPublication) { from components.java } }
    repositories {
        maven {
            name = 'waystation'
            url = "\${wsBase}/\${findProperty('publishRepo') ?: 'maven-releases'}/"
            allowInsecureProtocol = true
            if (findProperty('wsToken')) {
                credentials {
                    username = 'admin'
                    password = findProperty('wsToken')
                }
            }
        }
    }
}
EOF
  printf 'package harness;\npublic class Hello { public static String hi() { return "%s"; } }\n' "$2" \
    > "$1/src/main/java/harness/Hello.java"
}

has() {  # has <project dir> <file name>: a file the resolve task copied
  [ -s "$1/build/deps/$2" ] || { ls "$1/build/deps" >&2 || true; fail "gradle $v did not resolve $2"; }
}

remote_sha1() {  # remote_sha1 <repository> <path>
  curl -fsS -u "admin:$token" "$base/$1/$2.sha1" | cut -c1-40
}

public_deps="    implementation 'org.hamcrest:hamcrest-library:1.3'
    implementation 'org.reactivestreams:reactive-streams:[1.0.3,1.0.4]'"

step "gradle $v: resolve through the Maven Central proxy (writes the dependency verification metadata)"
project "$work/consumer" consumer "$public_deps"
run_gradle "$work/consumer" "$work/home-central" -PwsRepo=maven-central -PwsToken="$token" \
  --write-verification-metadata sha256 resolve || fail "gradle $v resolve (proxy)"
has "$work/consumer" hamcrest-library-1.3.jar
has "$work/consumer" hamcrest-core-1.3.jar
has "$work/consumer" reactive-streams-1.0.4.jar
[ "$(remote_sha1 maven-central org/hamcrest/hamcrest-library/1.3/hamcrest-library-1.3.jar)" = \
  "$(sha1sum "$work/consumer/build/deps/hamcrest-library-1.3.jar" | cut -c1-40)" ] || fail 'hamcrest-library .sha1'
verification="$work/consumer/gradle/verification-metadata.xml"
grep -q 'hamcrest-library-1.3.jar' "$verification" || fail "no verification metadata written: $(cat "$verification")"

step "gradle $v: resolve through the group from an empty Gradle home, strict dependency verification"
project "$work/consumer-group" consumer "$public_deps"
mkdir -p "$work/consumer-group/gradle"
cp "$verification" "$work/consumer-group/gradle/"
run_gradle "$work/consumer-group" "$work/home-group" -PwsToken="$token" --dependency-verification strict resolve \
  || fail "gradle $v resolve (group, verified)"
has "$work/consumer-group" hamcrest-library-1.3.jar
has "$work/consumer-group" reactive-streams-1.0.4.jar

step "gradle $v: a checksum that does not match fails the verification"
project "$work/consumer-bad" consumer "$public_deps"
mkdir -p "$work/consumer-bad/gradle"
# the first artifact's sha256 (hamcrest-core's jar or POM, in the file's sorted order) replaced with zeros
awk '!done && /<sha256 value="/ { sub(/value="[0-9a-f]+"/, "value=\"" sprintf("%064d", 0) "\""); done = 1 } { print }' \
  "$verification" > "$work/consumer-bad/gradle/verification-metadata.xml"
if run_gradle "$work/consumer-bad" "$work/home-group" -PwsToken="$token" --dependency-verification strict resolve \
  >"$work/bad.log" 2>&1; then
  fail "gradle $v accepted an artifact whose checksum does not match the verification metadata"
fi
grep -q 'Dependency verification failed' "$work/bad.log" || { tail -20 "$work/bad.log"; fail 'expected a verification failure'; }

step "gradle $v: a wrong token is refused (401 after the challenge)"
project "$work/consumer-wrong" consumer "$public_deps"
if run_gradle "$work/consumer-wrong" "$work/home-wrong" -PwsToken=not-a-token resolve >"$work/wrong.log" 2>&1; then
  fail "gradle $v resolved through the group with a wrong token"
fi
grep -Eq '401|Unauthorized' "$work/wrong.log" || { tail -20 "$work/wrong.log"; fail 'wrong token: expected a 401'; }

step "gradle $v: publish a release (maven-publish: jar, POM, module metadata, checksums)"
project "$work/hello" "$art" ''
publish() {  # publish <repository> <version> [gradle args...]
  _repo=$1 _ver=$2
  shift 2
  run_gradle "$work/hello" "$work/home-publish" -PpublishRepo="$_repo" -Pver="$_ver" "$@" publish
}
publish maven-releases 1.0.0 -PwsToken="$token" || fail "gradle $v publish (release)"
meta=$(curl -fsS -u "admin:$token" "$base/maven-releases/com/example/harness/$art/maven-metadata.xml")
printf '%s' "$meta" | grep -q '<release>1.0.0</release>' || fail "release metadata: $meta"
[ "$(remote_sha1 maven-releases "com/example/harness/$art/1.0.0/$art-1.0.0.jar")" = \
  "$(sha1sum "$work/hello/build/libs/$art-1.0.0.jar" | cut -c1-40)" ] || fail 'published jar .sha1'
curl -fsS -u "admin:$token" "$base/maven-releases/com/example/harness/$art/1.0.0/$art-1.0.0.module" \
  | grep -q '"formatVersion"' || fail 'Gradle module metadata not stored'

step "gradle $v: publishing the release again is refused (disable_redeploy)"
if publish maven-releases 1.0.0 -PwsToken="$token" >"$work/redeploy.log" 2>&1; then
  fail "gradle $v published the release twice"
fi
grep -q '400' "$work/redeploy.log" || { tail -20 "$work/redeploy.log"; fail 'redeploy: expected a 400'; }

step "gradle $v: publishing without credentials is refused"
if publish maven-releases 1.0.1 >"$work/anon.log" 2>&1; then
  fail "gradle $v published anonymously"
fi
grep -Eq '401|Unauthorized' "$work/anon.log" || { tail -20 "$work/anon.log"; fail 'anonymous publish: expected a 401'; }

step "gradle $v: publish a SNAPSHOT twice (timestamped builds)"
publish maven-snapshots 1.1.0-SNAPSHOT -PwsToken="$token" || fail "gradle $v publish (snapshot 1)"
sed -i 's/return "[^"]*"/return "snapshot-build-2"/' "$work/hello/src/main/java/harness/Hello.java"
publish maven-snapshots 1.1.0-SNAPSHOT -PwsToken="$token" || fail "gradle $v publish (snapshot 2)"
snap=$(curl -fsS -u "admin:$token" "$base/maven-snapshots/com/example/harness/$art/1.1.0-SNAPSHOT/maven-metadata.xml")
printf '%s' "$snap" | grep -q '<buildNumber>2</buildNumber>' || fail "snapshot metadata: $snap"

step "gradle $v: consume the published release and SNAPSHOT through the group"
project "$work/app" app "    implementation 'com.example.harness:$art:1.0.0'"
run_gradle "$work/app" "$work/home-app" -PwsToken="$token" resolve || fail "gradle $v resolve $art 1.0.0 (group)"
has "$work/app" "$art-1.0.0.jar"
project "$work/app-snap" app-snap "    implementation 'com.example.harness:$art:1.1.0-SNAPSHOT'"
run_gradle "$work/app-snap" "$work/home-app" -PwsToken="$token" resolve \
  || fail "gradle $v resolve $art 1.1.0-SNAPSHOT (group)"
has "$work/app-snap" "$art-1.1.0-SNAPSHOT.jar"  # Gradle names the timestamped build by the base version
mkdir -p "$work/classes"
(cd "$work/classes" && jar xf "$work/app-snap/build/deps/$art-1.1.0-SNAPSHOT.jar" harness/Hello.class)
grep -q 'snapshot-build-2' "$work/classes/harness/Hello.class" || fail 'the group did not serve the newest snapshot build'
