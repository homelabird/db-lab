#!/usr/bin/env python3
"""Turn the supplied commerce data templates into small deterministic InnoDB writesets.
Uses MariaDB virtual Sequence tables as READ sources; no non-InnoDB helper tables.
Outputs SQL incrementally: no generated dataset needs to be stored in the archive.
"""
import argparse
from pathlib import Path
import re
import sys

SIZES = {
    'tiny': {'customer': 100, 'product': 100, 'orders': 200, 'product_review': 120, 'api_request_log': 500},
    'small': {'customer': 5000, 'product': 1000, 'orders': 20000, 'product_review': 12000, 'api_request_log': 50000},
    'standard': {'customer': 50000, 'product': 10000, 'orders': 200000, 'product_review': 120000, 'api_request_log': 500000},
    'large': {'customer': 100000, 'product': 20000, 'orders': 400000, 'product_review': 240000, 'api_request_log': 1000000},
}

def expected_counts(size):
    c = SIZES[size]
    # 10% of synthetic orders are cancelled/refunded/pending and have no shipment.
    return dict(c, customer_address=c['customer'], category=60, warehouse=8,
                inventory=8*c['product'], order_item=3*c['orders'], payment=c['orders'],
                shipment=sum(i % 100 >= 10 for i in range(c['orders'])),
                account_balance=c['customer'], order_status_history=0)

def blocks(count, batch):
    for lo in range(0, count, batch):
        yield lo, min(lo + batch, count) - 1

def generate(template, size='standard', batch=500, payload_bytes=256):
    if batch < 1 or batch > 5000: raise ValueError('batch must be 1..5000')
    if not 0 <= payload_bytes <= 8192: raise ValueError('payload must be 0..8192')
    c = SIZES[size]
    text = re.sub(r'--[^\n]*', '', template)
    # Foreign-key references, not every occurrence of a numeric literal (prices also use 50000).
    text = text.replace('(n % 50000)', f"(n % {c['customer']})")
    text = text.replace('((n * 17) % 50000)', f"((n * 17) % {c['customer']})")
    text = text.replace('((o.n * 17 + i.item_no * 997) % 10000)', f"((o.n * 17 + i.item_no * 997) % {c['product']})")
    text = text.replace('((n * 41) % 10000)', f"((n * 41) % {c['product']})")
    text = text.replace("'sg'))", "'sg'), 'payload', RPAD(SHA2(CONCAT('row-',n),256), " + str(payload_bytes) + ", 'x'))")
    yield 'USE commerce_lab;\nSET NAMES utf8mb4;\nSET autocommit=1;\n'
    for original in text.split(';'):
        stmt = original.strip()
        if not stmt or re.match(r'(USE|SET|DROP|CREATE)\b', stmt, re.I): continue
        if stmt.startswith('INSERT INTO _'): continue
        match = re.match(r'INSERT INTO ([a-z_]+)', stmt)
        table = match.group(1) if match else None
        yield '-- Step: ' + (table or stmt.split()[0]) + '\n'
        if '_numbers' in stmt:
            if table == 'customer_address': count = c['customer']
            elif table == 'order_item': count = c['orders']
            elif table == 'category': count = 50
            else: count = c[table]
            stmt = re.sub(r'n < \d+', 'n < ' + str(count), stmt)
            for lo, hi in blocks(count, batch):
                sequence = f'(SELECT seq AS n FROM seq_{lo}_to_{hi}) AS numbers'
                yield stmt.replace('_numbers', sequence) + ';\n'
        elif table == 'inventory':
            for lo, hi in blocks(c['product'], batch):
                yield stmt + f' WHERE p.product_id BETWEEN {lo+1} AND {hi+1};\n'
        elif table in ('payment', 'shipment'):
            for lo, hi in blocks(c['orders'], batch):
                joiner = ' AND ' if table == 'shipment' else ' WHERE '
                yield stmt + joiner + f'o.order_id BETWEEN {lo+1} AND {hi+1};\n'
        elif table == 'account_balance':
            for lo, hi in blocks(c['customer'], batch):
                yield stmt + f' WHERE customer_id BETWEEN {lo+1} AND {hi+1};\n'
        elif stmt.startswith('UPDATE orders'):
            for lo, hi in blocks(c['orders'], batch):
                query = stmt.replace('FROM order_item\n', f'FROM order_item WHERE order_id BETWEEN {lo+1} AND {hi+1}\n')
                yield query + f' WHERE o.order_id BETWEEN {lo+1} AND {hi+1};\n'
        else:
            yield stmt + ';\n'

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--size', choices=SIZES, default='standard')
    p.add_argument('--batch', type=int, default=500)
    p.add_argument('--payload-bytes', type=int, default=256)
    p.add_argument('--template', type=Path, default=Path(__file__).resolve().parent.parent / 'datasets/commerce-data-template.sql')
    a = p.parse_args()
    if not a.template.exists():
        a.template = Path('/opt/lab/datasets/commerce-data-template.sql')
    for sql in generate(a.template.read_text(), a.size, a.batch, a.payload_bytes):
        sys.stdout.write(sql)

if __name__ == '__main__':
    try: main()
    except BrokenPipeError: sys.exit(1)
