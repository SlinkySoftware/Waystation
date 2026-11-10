#!/bin/sh
# Maven 3.9 through Waystation (client-maven container): resolve through a Maven Central proxy and a group, deploy a
# release and SNAPSHOTs to hosted repositories, consume them through the group. Every mvn run uses a settings.xml
# mirror to Waystation (so mvn never talks to the upstream) with basic auth: the admin's username and an API token.
# No preemptive authentication: mvn sends the credentials after the server's 401 Basic challenge, which Waystation
# answers for anonymous requests to repositories anonymous cannot see (ADR 0005), as Nexus does.
# The build plugins come pre-seeded in /opt/m2-seed (see format.env); each step starts from a copy of it.
set -eu
. /harness/lib.sh

work=/tmp/maven-harness
base="$WAYSTATION_URL/repository"
rm -rf "$work"
mkdir -p "$work"

ws_login
ws_token '["write"]'
token=$WS_TOKEN

settings() {  # settings <file> <mirror repository> [with credentials: yes|no]
  servers=''
  if [ "${3:-yes}" = yes ]; then
    for id in "$2" maven-releases maven-snapshots; do
      servers="$servers<server><id>$id</id><username>admin</username><password>$token</password></server>"
    done
  fi
  cat > "$1" <<EOF
<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0">
  <mirrors><mirror><id>$2</id><mirrorOf>*</mirrorOf><url>$base/$2/</url></mirror></mirrors>
  <servers>$servers</servers>
  <!-- central (mirrored) has SNAPSHOTs turned off in the super POM: turn them on so the group serves them -->
  <profiles><profile><id>waystation</id>
    <repositories><repository><id>central</id><url>http://central</url>
      <snapshots><enabled>true</enabled></snapshots></repository></repositories>
  </profile></profiles>
  <activeProfiles><activeProfile>waystation</activeProfile></activeProfiles>
</settings>
EOF
}

local_repo() {  # local_repo <dir>: a local repository holding only the seeded build plugins
  rm -rf "$1"
  cp -r /opt/m2-seed "$1"
}

run_mvn() {  # run_mvn <project dir> <settings> <local repository> <args...>
  _dir=$1 _settings=$2 _repo=$3
  shift 3
  (cd "$_dir" && mvn -B -ntp --strict-checksums -s "$_settings" -Dmaven.repo.local="$_repo" "$@")
}

resolve='org.apache.maven.plugins:maven-dependency-plugin:3.8.1:resolve'

project() {  # project <dir> <groupId> <artifactId> <version> <dependencies XML>
  mkdir -p "$1/src/main/java/harness"
  cat > "$1/pom.xml" <<EOF
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>$2</groupId>
  <artifactId>$3</artifactId>
  <version>$4</version>
  <properties>
    <maven.compiler.release>21</maven.compiler.release>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
  <dependencies>$5</dependencies>
</project>
EOF
  printf 'package harness;\npublic class Hello { public static String hi() { return "%s"; } }\n' "$4" \
    > "$1/src/main/java/harness/Hello.java"
}

has() {  # has <local repository> <path>: a file mvn downloaded
  [ -s "$1/$2" ] || fail "missing in the local repository: $2"
}

remote_sha1() {  # remote_sha1 <repository> <path>
  curl -fsS -u "admin:$token" "$base/$1/$2.sha1" | cut -c1-40
}

public_deps='
  <dependency><groupId>org.hamcrest</groupId><artifactId>hamcrest-library</artifactId><version>1.3</version></dependency>
  <dependency><groupId>org.reactivestreams</groupId><artifactId>reactive-streams</artifactId>
    <version>[1.0.3,1.0.4]</version></dependency>'
project "$work/consumer" com.example.harness consumer 1.0.0 "$public_deps"

step 'anonymous requests to the group and to a missing repository get the same Basic challenge (ADR 0005)'
challenge() {  # challenge <url>: "<status> <WWW-Authenticate>" of an anonymous GET
  curl -sS -o /dev/null -D - "$1" | tr -d '\r' | awk 'NR == 1 {s = $2} tolower($1) == "www-authenticate:" {
    sub(/^[^:]*: */, ""); h = $0} END {print s, h}'
}
group_answer=$(challenge "$base/maven-public/org/hamcrest/hamcrest-library/1.3/hamcrest-library-1.3.pom")
missing_answer=$(challenge "$base/no-such-maven-repo/org/hamcrest/hamcrest-library/1.3/hamcrest-library-1.3.pom")
case $group_answer in '401 Basic realm="Waystation"'*) ;; *) fail "anonymous group request: $group_answer" ;; esac
[ "$group_answer" = "$missing_answer" ] || fail "hidden and missing repositories differ: $group_answer / $missing_answer"

step 'resolve through the Maven Central proxy (cold cache: checksums verified, version range via maven-metadata.xml)'
settings "$work/central.xml" maven-central
local_repo "$work/m2-central"
run_mvn "$work/consumer" "$work/central.xml" "$work/m2-central" "$resolve" || fail 'mvn dependency:resolve (proxy)'
has "$work/m2-central" org/hamcrest/hamcrest-library/1.3/hamcrest-library-1.3.jar
has "$work/m2-central" org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar
has "$work/m2-central" org/reactivestreams/reactive-streams/1.0.4/reactive-streams-1.0.4.jar
[ "$(remote_sha1 maven-central org/hamcrest/hamcrest-library/1.3/hamcrest-library-1.3.jar)" = \
  "$(sha1sum "$work/m2-central/org/hamcrest/hamcrest-library/1.3/hamcrest-library-1.3.jar" | cut -c1-40)" ] \
  || fail 'hamcrest-library .sha1'

step 'resolve through the group from an empty local repository (served from the Waystation cache)'
settings "$work/public.xml" maven-public
local_repo "$work/m2-public"
run_mvn "$work/consumer" "$work/public.xml" "$work/m2-public" "$resolve" || fail 'mvn dependency:resolve (group)'
has "$work/m2-public" org/hamcrest/hamcrest-library/1.3/hamcrest-library-1.3.jar
has "$work/m2-public" org/reactivestreams/reactive-streams/1.0.4/reactive-streams-1.0.4.jar

step 'browse API: the proxy lists what it cached'
i=0
until curl -fsS -u "admin:$token" "$WAYSTATION_URL/api/v1/repositories/maven-central/maven/artifacts?q=hamcrest" \
  | grep -q '"artifact_id":"hamcrest-library"'; do
  i=$((i + 1)); [ "$i" -lt 20 ] || fail 'hamcrest-library not listed by the browse API'; sleep 0.5
done

step 'deploy a release (mvn deploy: jar, pom, checksums; maven-metadata.xml regenerated by the server)'
project "$work/hello" com.example.harness hello 1.0.0 ''
local_repo "$work/m2-deploy"
deploy_to() {  # deploy_to <repository> <settings> [mvn args...]
  _target=$1 _settings=$2
  shift 2
  run_mvn "$work/hello" "$_settings" "$work/m2-deploy" deploy -DaltDeploymentRepository="$_target::$base/$_target/" "$@"
}
deploy_to maven-releases "$work/public.xml" || fail 'mvn deploy (release)'
meta=$(curl -fsS -u "admin:$token" "$base/maven-releases/com/example/harness/hello/maven-metadata.xml")
printf '%s' "$meta" | grep -q '<release>1.0.0</release>' || fail "release metadata: $meta"
[ "$(remote_sha1 maven-releases com/example/harness/hello/1.0.0/hello-1.0.0.jar)" = \
  "$(sha1sum "$work/hello/target/hello-1.0.0.jar" | cut -c1-40)" ] || fail 'deployed jar .sha1'

step 'redeploying the release is refused (disable_redeploy)'
if deploy_to maven-releases "$work/public.xml" >"$work/redeploy.log" 2>&1; then
  fail 'the release was deployed twice'
fi
grep -q '400' "$work/redeploy.log" || { tail -20 "$work/redeploy.log"; fail 'redeploy: expected a 400 from the server'; }

step 'deploy without credentials is refused'
settings "$work/anonymous.xml" maven-public no
if deploy_to maven-releases "$work/anonymous.xml" >"$work/anon.log" 2>&1; then
  fail 'an anonymous deploy succeeded'
fi
grep -Eq '401|Unauthorized' "$work/anon.log" \
  || { tail -20 "$work/anon.log"; fail 'anonymous deploy: expected the 401 challenge from the server'; }

step 'deploy a SNAPSHOT twice (timestamped builds, version-level maven-metadata.xml)'
sed -i 's#<version>1.0.0</version>#<version>1.1.0-SNAPSHOT</version>#' "$work/hello/pom.xml"
deploy_to maven-snapshots "$work/public.xml" || fail 'mvn deploy (snapshot 1)'
deploy_to maven-snapshots "$work/public.xml" || fail 'mvn deploy (snapshot 2)'
snap=$(curl -fsS -u "admin:$token" "$base/maven-snapshots/com/example/harness/hello/1.1.0-SNAPSHOT/maven-metadata.xml")
printf '%s' "$snap" | grep -q '<buildNumber>2</buildNumber>' || fail "snapshot metadata: $snap"
if deploy_to maven-releases "$work/public.xml" >"$work/policy.log" 2>&1; then
  fail 'a SNAPSHOT was deployed to the release repository'
fi

step 'consume the deployed release and SNAPSHOT through the group'
hello_deps='
  <dependency><groupId>com.example.harness</groupId><artifactId>hello</artifactId><version>1.0.0</version></dependency>'
project "$work/app" com.example.harness app 1.0.0 "$hello_deps"
project "$work/app-snap" com.example.harness app-snap 1.0.0 "$(printf '%s' "$hello_deps" | sed 's/1.0.0/1.1.0-SNAPSHOT/')"
local_repo "$work/m2-app"
run_mvn "$work/app" "$work/public.xml" "$work/m2-app" "$resolve" || fail 'resolve hello 1.0.0 (group)'
has "$work/m2-app" com/example/harness/hello/1.0.0/hello-1.0.0.jar
run_mvn "$work/app-snap" "$work/public.xml" "$work/m2-app" "$resolve" || fail 'resolve hello 1.1.0-SNAPSHOT (group)'
ls "$work/m2-app/com/example/harness/hello/1.1.0-SNAPSHOT/" | grep -q 'hello-1.1.0-.*-2.jar' \
  || fail 'the group did not serve the newest snapshot build'

step 'group metadata merges the members (release and SNAPSHOT versions)'
merged=$(curl -fsS -u "admin:$token" "$base/maven-public/com/example/harness/hello/maven-metadata.xml")
printf '%s' "$merged" | grep -q '<version>1.0.0</version>' || fail "merged metadata: $merged"
printf '%s' "$merged" | grep -q '<version>1.1.0-SNAPSHOT</version>' || fail "merged metadata: $merged"

pass
