---
permalink: /bugs/
title: "Bugs found by SQLancer"
---

{%- assign head = site.data.impact.stats.headline -%}

SQLancer has found {{ head.bugs_total }} bugs that we can attribute to it with
evidence, across {{ head.dbms_with_bugs }} database systems. Each one is recorded
with the primary source linking it to SQLancer or to one of its test oracles.

[**Browse the bugs on the impact page**]({{ '/impact/#bugs' | relative_url }}){: .btn .btn--primary}

The underlying records live in
[`_data/impact/bugs.json`](https://github.com/sqlancer/sqlancer.github.io/blob/main/_data/impact/bugs.json).
Reduced test cases for the historic reports are kept in the
[SQLancer bug repository](https://github.com/sqlancer/bugs).

Further lists of database system bugs, not all of them found with SQLancer, are
published by [Jinsheng Ba](http://jinshengba.me/bombs/),
[Manuel Rigger](https://manuelrigger.at/dbms-bugs/), and the
[NUS TEST lab](https://nus-test.github.io/bugs/).
