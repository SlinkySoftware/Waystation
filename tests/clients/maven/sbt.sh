#!/bin/sh
# sbt through Waystation (client-jvm container). A Java-only build (no managed Scala instance: sbt compiles it with
# its own Scala, pre-seeded with sbt at image build, see jvm/seed.sh) whose only resolver is a Waystation repository.
# Resolves hamcrest-library 1.3 (with hamcrest-core) and reactive-streams through a version range with coursier,
# SHA-1 checksums required, through the Maven Central proxy and then the group; publishes (Maven style, with
# checksums) a release and a SNAPSHOT to the hosted repositories, checks refusals (redeploy, anonymous publish) and
# consumes both through the group. Credentials: the admin's username and an API token for realm "Waystation" (the
# realm of the server's 401 challenge, which sbt's publisher answers; ADR 0005).
set -eu
. /harness/lib.sh

work=/tmp/sbt-harness
base="$WAYSTATION_URL/repository"
art=hello-sbt
rm -rf "$work"
mkdir -p "$work"

ws_login
ws_token '["write"]'
token=$WS_TOKEN

run_sbt() {  # run_sbt <project dir> <java properties and sbt commands...>
  _dir=$1
  shift
  (cd "$_dir" && sbt -batch -Dsbt.server.autostart=false -Dsbt.supershell=false -Dsbt.color=false \
    -Dsbt.log.noformat=true -DwsBase="$base" "$@")
}

project() {  # project <dir> <name> <libraryDependencies entries>
  mkdir -p "$1/src/main/java/harness" "$1/project"
  cp /opt/sbt-harness/build.properties "$1/project/build.properties"
  cat > "$1/build.sbt" <<EOF
organization := "com.example.harness"
name := "$2"
version := sys.props.getOrElse("ver", "1.0.0")
crossPaths := false
autoScalaLibrary := false
managedScalaInstance := false
javacOptions ++= Seq("--release", "21")

// plain HTTP inside the harness network: sbt refuses it unless allowed per resolver
def waystation(id: String, repo: String) = (id at s"\${sys.props("wsBase")}/\$repo/").withAllowInsecureProtocol(true)
externalResolvers := Seq(waystation("waystation", sys.props.getOrElse("wsRepo", "maven-public")))
// a cache per project: every resolve below goes to Waystation
csrCacheDirectory := baseDirectory.value / "coursier-cache"
checksums := Vector("sha1")
update / checksums := Vector("sha1")
credentials ++= sys.props.get("wsToken").toSeq.map(t => Credentials("Waystation", "waystation-web", "admin", t))
publishMavenStyle := true
publishTo := Some(waystation("waystation-publish", sys.props.getOrElse("publishRepo", "maven-releases")))
// let the server decide about redeploys (sbt would refuse an existing release before uploading)
publishConfiguration := publishConfiguration.value.withOverwrite(true)

libraryDependencies ++= Seq($3)

lazy val resolve = taskKey[Unit]("copy the resolved jars to target/deps")
resolve := {
  val dir = target.value / "deps"
  IO.createDirectory(dir)
  (Compile / externalDependencyClasspath).value.foreach(f => IO.copyFile(f.data, dir / f.data.getName))
}
EOF
  printf 'package harness;\npublic class Hello { public static String hi() { return "%s"; } }\n' "$2" \
    > "$1/src/main/java/harness/Hello.java"
}

has() {  # has <project dir> <file name>: a jar the resolve task copied
  [ -s "$1/target/deps/$2" ] || { ls "$1/target/deps" >&2 || true; fail "sbt did not resolve $2"; }
}

cached_sha1() {  # cached_sha1 <repository> <path>: the .sha1 coursier fetched (and checked) for a file
  _f=$(find "$work" -path "*/coursier-cache/*/repository/$1/$2.sha1" | head -1)
  [ -n "$_f" ] || fail "coursier did not fetch $1/$2.sha1"
  cut -c1-40 "$_f"
}

public_deps='"org.hamcrest" % "hamcrest-library" % "1.3", "org.reactivestreams" % "reactive-streams" % "[1.0.3,1.0.4]"'

step 'sbt: resolve through the Maven Central proxy (coursier, SHA-1 checksums required)'
project "$work/consumer" consumer "$public_deps"
run_sbt "$work/consumer" -DwsRepo=maven-central -DwsToken="$token" resolve || fail 'sbt resolve (proxy)'
has "$work/consumer" hamcrest-library-1.3.jar
has "$work/consumer" hamcrest-core-1.3.jar
has "$work/consumer" reactive-streams-1.0.4.jar
[ "$(cached_sha1 maven-central org/hamcrest/hamcrest-library/1.3/hamcrest-library-1.3.jar)" = \
  "$(sha1sum "$work/consumer/target/deps/hamcrest-library-1.3.jar" | cut -c1-40)" ] || fail 'hamcrest-library .sha1'

step 'sbt: resolve through the group'
project "$work/consumer-group" consumer "$public_deps"
run_sbt "$work/consumer-group" -DwsToken="$token" resolve || fail 'sbt resolve (group)'
has "$work/consumer-group" hamcrest-library-1.3.jar
has "$work/consumer-group" reactive-streams-1.0.4.jar
cached_sha1 maven-public org/reactivestreams/reactive-streams/1.0.4/reactive-streams-1.0.4.jar >/dev/null

step 'sbt: a wrong token is refused'
project "$work/consumer-wrong" consumer-wrong "$public_deps"
if run_sbt "$work/consumer-wrong" -DwsToken=not-a-token resolve >"$work/wrong.log" 2>&1; then
  fail 'sbt resolved through the group with a wrong token'
fi
grep -Eqi 'unauthorized' "$work/wrong.log" || { tail -20 "$work/wrong.log"; fail 'wrong token: expected a refusal'; }

step 'sbt: publish a release (Maven style: jar, POM, checksums)'
project "$work/hello" "$art" ''
publish() {  # publish <repository> <version> [properties...]
  _repo=$1 _ver=$2
  shift 2
  run_sbt "$work/hello" -DpublishRepo="$_repo" -Dver="$_ver" "$@" publish
}
publish maven-releases 1.0.0 -DwsToken="$token" || fail 'sbt publish (release)'
meta=$(curl -fsS -u "admin:$token" "$base/maven-releases/com/example/harness/$art/maven-metadata.xml")
printf '%s' "$meta" | grep -q '<release>1.0.0</release>' || fail "release metadata: $meta"
[ "$(curl -fsS -u "admin:$token" "$base/maven-releases/com/example/harness/$art/1.0.0/$art-1.0.0.jar.sha1" | cut -c1-40)" = \
  "$(sha1sum "$work/hello/target/$art-1.0.0.jar" | cut -c1-40)" ] || fail 'published jar .sha1'

step 'sbt: publishing the release again is refused (disable_redeploy)'
if publish maven-releases 1.0.0 -DwsToken="$token" >"$work/redeploy.log" 2>&1; then
  fail 'sbt published the release twice'
fi
grep -q '400' "$work/redeploy.log" || { tail -20 "$work/redeploy.log"; fail 'redeploy: expected a 400'; }

step 'sbt: publishing without credentials is refused'
if publish maven-releases 1.0.1 >"$work/anon.log" 2>&1; then
  fail 'sbt published anonymously'
fi
grep -Eqi '401|unauthorized' "$work/anon.log" || { tail -20 "$work/anon.log"; fail 'anonymous publish: expected a 401'; }

step 'sbt: publish a SNAPSHOT (twice: SNAPSHOTs redeploy)'
publish maven-snapshots 1.1.0-SNAPSHOT -DwsToken="$token" || fail 'sbt publish (snapshot 1)'
sed -i 's/return "[^"]*"/return "snapshot-build-2"/' "$work/hello/src/main/java/harness/Hello.java"
publish maven-snapshots 1.1.0-SNAPSHOT -DwsToken="$token" || fail 'sbt publish (snapshot 2)'
curl -fsS -o /dev/null -u "admin:$token" \
  "$base/maven-snapshots/com/example/harness/$art/1.1.0-SNAPSHOT/$art-1.1.0-SNAPSHOT.jar" || fail 'SNAPSHOT jar missing'

step 'sbt: consume the published release and SNAPSHOT through the group'
project "$work/app" app "\"com.example.harness\" % \"$art\" % \"1.0.0\""
run_sbt "$work/app" -DwsToken="$token" resolve || fail "sbt resolve $art 1.0.0 (group)"
has "$work/app" "$art-1.0.0.jar"
project "$work/app-snap" app-snap "\"com.example.harness\" % \"$art\" % \"1.1.0-SNAPSHOT\""
run_sbt "$work/app-snap" -DwsToken="$token" resolve || fail "sbt resolve $art 1.1.0-SNAPSHOT (group)"
has "$work/app-snap" "$art-1.1.0-SNAPSHOT.jar"
mkdir -p "$work/classes"
(cd "$work/classes" && jar xf "$work/app-snap/target/deps/$art-1.1.0-SNAPSHOT.jar" harness/Hello.class)
grep -q 'snapshot-build-2' "$work/classes/harness/Hello.class" || fail 'the group did not serve the redeployed SNAPSHOT'
