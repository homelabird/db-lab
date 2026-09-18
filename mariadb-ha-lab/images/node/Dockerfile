ARG MARIADB_IMAGE=docker.io/library/mariadb:11.8
FROM ${MARIADB_IMAGE}
USER root
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      galera-4 mariadb-backup socat python3 python3-pymysql \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /opt/lab /var/lib/labctl \
 && p="$(find /usr/lib -name libgalera_smm.so -print -quit)" \
 && test -n "$p" && ln -s "$p" /opt/lab/libgalera_smm.so \
 && command -v mariadb-backup && command -v socat \
 && python3 -c 'import pymysql'
COPY images/node/common.cnf /etc/mysql/conf.d/80-lab.cnf
COPY images/node/entrypoint.sh /usr/local/bin/lab-entrypoint.sh
COPY images/node/init-users.sh /docker-entrypoint-initdb.d/10-lab-users.sh
COPY scripts/ /opt/lab/
COPY datasets/ /opt/lab/datasets/
RUN chmod 644 /etc/mysql/conf.d/80-lab.cnf \
 && chmod 755 /usr/local/bin/lab-entrypoint.sh /docker-entrypoint-initdb.d/10-lab-users.sh \
 && find /opt/lab -type f -exec chmod 644 {} + \
 && bash -n /usr/local/bin/lab-entrypoint.sh \
 && bash -n /docker-entrypoint-initdb.d/10-lab-users.sh \
 && python3 -m compileall -q /opt/lab \
 && python3 -c 'import sys; sys.path.insert(0,"/opt/lab"); import health, state, seed, workload, simulator, scenario_worker'
EXPOSE 3306 4567 4568 4444 9200
ENTRYPOINT ["/usr/local/bin/lab-entrypoint.sh"]
CMD ["mariadbd"]
