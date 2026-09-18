USE commerce_lab;

-- JSON 값 조회
SELECT product_id, name,
       JSON_VALUE(attributes, '$.color') color,
       JSON_VALUE(attributes, '$.weight_g') weight_g
FROM product
WHERE JSON_VALUE(attributes, '$.color') = 'black'
LIMIT 20;

-- FULLTEXT 검색
SELECT product_id, name,
       MATCH(name, description) AGAINST('catalog index' IN NATURAL LANGUAGE MODE) score
FROM product
WHERE MATCH(name, description) AGAINST('catalog index' IN NATURAL LANGUAGE MODE)
ORDER BY score DESC
LIMIT 20;

-- 과제: JSON_VALUE(attributes,'$.color')를 기반으로 자주 조회한다면
-- generated column + index로 어떻게 바꿀지 설계해본다.
