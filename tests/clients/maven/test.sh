#!/bin/sh
# Maven-format clients against one stack (format.env CLIENT="maven jvm"): client-maven runs Maven 3.9, client-jvm
# Maven 4, Gradle 8, Gradle 9 and sbt. Each deploys its own artifactId (hello, hello-mvn4, hello-gradle8,
# hello-gradle9, hello-sbt), so the release repository's disable_redeploy refusals stay per client.
set -eu
. /harness/lib.sh

d=/harness/maven
case ${HARNESS_CLIENT:-maven} in
  maven)
    MVN=mvn M2_SEED=/opt/m2-seed HELLO=hello sh "$d/mvn.sh"
    ;;
  jvm)
    MVN=mvn4 M2_SEED=/opt/m2-seed4 HELLO=hello-mvn4 sh "$d/mvn.sh"
    sh "$d/gradle.sh" 8
    sh "$d/gradle.sh" 9
    sh "$d/sbt.sh"
    ;;
  *) fail "unknown client $HARNESS_CLIENT" ;;
esac
pass
