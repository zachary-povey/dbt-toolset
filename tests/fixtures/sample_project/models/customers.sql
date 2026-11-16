select customer_id, 'emea' as region, count(*) as order_count from {{ ref('stg_orders') }} group by 1, 2
