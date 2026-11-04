# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
import pytest

from lyst.data.sqlguard import GuardPolicy, QueryRejected, check_read_query

P = GuardPolicy(tables={"sales": "sales__v2", "customers": "customers__v1"},
                restricted_columns={"customers": {"email"}})


@pytest.mark.parametrize("sql", [
    "select region, sum(amount) from sales group by 1",
    "with t as (select region from sales) select * from t",
    "select c.name, s.amount from sales s join customers c on c.id = s.customer_id",
    "select * from sales union all select * from sales",
])
def test_allowed(sql):
    out = check_read_query(sql, P)
    assert 'data."sales__v2"' in out.sql and out.sql.endswith("LIMIT 10000")


@pytest.mark.parametrize("sql,reason", [
    ("delete from sales", "only read"),
    ("drop table sales", "only read"),
    ("select 1; select 2", "exactly one"),
    ("select * from read_csv('/etc/passwd')", "table functions"),
    ("select read_text('/etc/passwd')", "read_text"),
    ("select getenv('HOME')", "getenv"),
    ("select * from secret_table", "unknown dataset"),
    ("select * from data.sales__v1", "by name only"),
    ("attach 'x.db' as x", ""),
    ("copy sales to '/tmp/out.csv'", ""),
    ("select email from customers", "restricted"),
    ("select * from customers", "restricted"),
    ("select c.* from customers c", "restricted"),
    ("pragma database_list", ""),
])
def test_rejected(sql, reason):
    with pytest.raises(QueryRejected, match=reason):
        check_read_query(sql, P)
