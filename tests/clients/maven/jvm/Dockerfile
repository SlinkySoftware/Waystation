# client-jvm (maven format, tests/clients/README.md): Maven 4, Gradle 8, Gradle 9 and sbt on the JDK 21 base image
# of client-maven (shared layers). Distributions are pinned by version and verified against their published
# checksums. seed.sh pre-seeds what the tools need to build (Maven 4 plugins, sbt's own artifacts) from the public
# upstreams at image build, so the fixtures hold only what the tests resolve through Waystation.
FROM maven:3.9.11-eclipse-temurin-21

ARG MAVEN4_VERSION=4.0.0-rc-7
ARG MAVEN4_SHA512=17b886c0ffb7ec225e514afa8179b3143d855d196cfbb4607ece6272a9b4f51501a389438ab6841b393a4b1391a1b714f07f450daba1951f53f732b400338184
ARG GRADLE8_VERSION=8.14.8
ARG GRADLE8_SHA256=94da89692fc3ca6d2d0ce6bb48548762ea0f1897ba444286f5c7f3759c7af651
ARG GRADLE9_VERSION=9.9.2
ARG GRADLE9_SHA256=971fcd7150ca4ba04852c0b2ca0d2d0ab9468e14b03473eab19528809914c76b
ARG SBT_VERSION=1.13.1
ARG SBT_SHA256=4a20e36c1642b6d5be34ef641d2ed6c8c0d464e6145283730d5566716e4083be

RUN set -eu; cd /tmp; \
    curl -fsSLo maven4.tgz "https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/${MAVEN4_VERSION}/apache-maven-${MAVEN4_VERSION}-bin.tar.gz"; \
    echo "${MAVEN4_SHA512}  maven4.tgz" | sha512sum -c -; \
    mkdir /opt/maven4; tar -xzf maven4.tgz -C /opt/maven4 --strip-components=1; ln -s /opt/maven4/bin/mvn /usr/local/bin/mvn4; \
    for g in "8 ${GRADLE8_VERSION} ${GRADLE8_SHA256}" "9 ${GRADLE9_VERSION} ${GRADLE9_SHA256}"; do \
      set -- $g; \
      curl -fsSLo gradle.zip "https://services.gradle.org/distributions/gradle-$2-bin.zip"; \
      echo "$3  gradle.zip" | sha256sum -c -; \
      mkdir -p "/opt/gradle$1"; (cd "/opt/gradle$1" && jar xf /tmp/gradle.zip); \
      mv "/opt/gradle$1/gradle-$2"/* "/opt/gradle$1/"; rmdir "/opt/gradle$1/gradle-$2"; \
      chmod +x "/opt/gradle$1/bin/gradle"; ln -s "/opt/gradle$1/bin/gradle" "/usr/local/bin/gradle$1"; \
      rm gradle.zip; \
    done; \
    curl -fsSLo sbt.tgz "https://github.com/sbt/sbt/releases/download/v${SBT_VERSION}/sbt-${SBT_VERSION}.tgz"; \
    echo "${SBT_SHA256}  sbt.tgz" | sha256sum -c -; \
    tar -xzf sbt.tgz -C /opt; ln -s /opt/sbt/bin/sbt /usr/local/bin/sbt; \
    rm -f maven4.tgz sbt.tgz

COPY seed.sh /tmp/seed.sh
RUN SBT_VERSION="${SBT_VERSION}" sh /tmp/seed.sh && rm /tmp/seed.sh
