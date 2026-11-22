#!/bin/sh
# Image build only (jvm/Dockerfile): pre-seed what the tools need to build, from the public upstreams, so the harness
# fixtures hold only what the tests resolve through Waystation.
# - Maven 4: its default build plugins (and maven-dependency-plugin) into /opt/m2-seed4, as client-maven does for 3.9.
# - sbt: the launcher's boot directory, sbt itself and the meta-build's artifacts (coursier cache) under /root, from a
#   Java-only project with the settings the test uses (no managed Scala instance: sbt's own Scala compiles it).
# Gradle needs nothing: java-library and maven-publish ship with the distribution.
set -eu

seed=/tmp/seed
mkdir -p "$seed/src/main/java/seed"
cd "$seed"
printf 'package seed; public class Seed {}\n' > src/main/java/seed/Seed.java

# ---- Maven 4
cat > pom.xml <<'EOF'
<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>seed</groupId>
<artifactId>seed</artifactId><version>1</version><properties><maven.compiler.release>21</maven.compiler.release>
<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties></project>
EOF
mvn4 -B -q -Dmaven.repo.local=/opt/m2-seed4 org.apache.maven.plugins:maven-dependency-plugin:3.8.1:resolve deploy \
  -DaltDeploymentRepository=seed::file:///tmp/seed-repo
rm -rf /tmp/seed-repo /opt/m2-seed4/seed target pom.xml
find /opt/m2-seed4 \( -name _remote.repositories -o -name '*.lastUpdated' -o -name resolver-status.properties \
  -o -name 'maven-metadata-*.xml' \) -delete
chmod -R a+rX /opt/m2-seed4

# ---- sbt
mkdir -p project /opt/sbt-harness
echo "sbt.version=$SBT_VERSION" > /opt/sbt-harness/build.properties  # the test's projects use this sbt (seeded)
cp /opt/sbt-harness/build.properties project/
cat > build.sbt <<'EOF'
organization := "seed"
name := "seed"
version := "1"
crossPaths := false
autoScalaLibrary := false
managedScalaInstance := false
EOF
sbt -batch -Dsbt.server.autostart=false update compile package makePom publishLocal
rm -rf /root/.ivy2/local/seed "$seed"
