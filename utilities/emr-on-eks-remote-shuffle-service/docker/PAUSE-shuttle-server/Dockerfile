FROM amazoncorretto:8
ARG MAVEN_VERSION=3.8.6
ARG BASE_URL=https://apache.osuosl.org/maven/maven-3/${MAVEN_VERSION}/binaries


# install maven
RUN yum update -y && yum install -y git tar procps
RUN mkdir -p /usr/share/maven /usr/share/maven/ref \
 && curl -fsSL -o /tmp/apache-maven.tar.gz ${BASE_URL}/apache-maven-${MAVEN_VERSION}-bin.tar.gz \
 && tar -xzf /tmp/apache-maven.tar.gz -C /usr/share/maven --strip-components=1 \
 && rm -f /tmp/apache-maven.tar.gz \
 && ln -s /usr/share/maven/bin/mvn /usr/bin/mvn

ENV MAVEN_HOME /usr/share/maven
ENV MAVEN_CONFIG "$USER_HOME_DIR/.m2"
ENV RSS_HOME=/usr/local/shuttle-rss

# compile server
RUN git clone -b sp31 --single-branch https://github.com/cubefs/shuttle.git /tmp/shuttle
WORKDIR /tmp/shuttle
RUN mvn clean package -Pserver -DskipTests -Dmaven.javadoc.skip=true \
    && mv /tmp/shuttle $RSS_HOME
    
WORKDIR $RSS_HOME
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh bin/*

EXPOSE 19189
EXPOSE 19188
EXPOSE 19190
EXPOSE 19191

ENTRYPOINT ["entrypoint.sh"]