# commerce_lab Data Dictionary

| Table | Approx rows | Purpose |
|---|---:|---|
| customer | 50,000 | customer profile / tier |
| customer_address | 50,000 | address FK practice |
| category | 60 | hierarchical category |
| product | 10,000 | catalog, JSON, FULLTEXT |
| warehouse | 8 | warehouse master |
| inventory | 80,000 | composite PK / stock query |
| orders | 200,000 | core transaction table |
| order_item | 600,000 | large join / aggregation |
| payment | 200,000 | payment status / unique key |
| shipment | ~180,000 | fulfillment flow |
| product_review | 120,000 | aggregation / text |
| api_request_log | 500,000 | partition / log analytics |
| account_balance | 50,000 | transaction / lock practice |
| order_status_history | grows during labs | trigger audit |

Total initial rows are roughly 2 million, plus the existing Sakila dataset.
