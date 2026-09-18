ARG HAPROXY_IMAGE=docker.io/library/haproxy:3.0-alpine
FROM ${HAPROXY_IMAGE}
COPY images/proxy/haproxy.cfg /usr/local/etc/haproxy/haproxy.cfg
RUN haproxy -c -f /usr/local/etc/haproxy/haproxy.cfg
