# Backup / Restore Lab

## Logical backup

```bash
podman exec mariadb-learning mariadb-dump \
  -uroot -prootpassword \
  --single-transaction --routines --triggers --events \
  commerce_lab > commerce_lab.sql
```

복원 연습용 DB를 만든 뒤:

```bash
podman exec mariadb-learning mariadb -uroot -prootpassword \
  -e "DROP DATABASE IF EXISTS commerce_restore; CREATE DATABASE commerce_restore;"

cat commerce_lab.sql | podman exec -i mariadb-learning \
  mariadb -uroot -prootpassword commerce_restore
```

## 확인할 것

- `--single-transaction`이 InnoDB에서 왜 중요한가?
- 대용량 logical dump의 단점은?
- binlog와 full backup을 조합하면 PITR(Point-in-Time Recovery)를 어떻게 구성할 수 있는가?
- `mariadb-backup` 같은 physical backup은 logical dump와 무엇이 다른가?
