---
permalink: /supported-databases/
title: "Database systems supported by SQLancer"
---

{%- comment -%}
The list below is derived from _data/impact/dbms.json, which the impact pipeline
regenerates from the provider directories in the SQLancer repository. Adding a
DBMS to SQLancer is enough for it to appear here.
{%- endcomment -%}
{%- assign supported = site.data.impact.dbms.dbms | where: "supported_by_sqlancer", true -%}

SQLancer supports many state-of-the-art database systems. The following
{{ supported.size }} implementations are part of the
[main SQLancer repository](https://github.com/sqlancer/sqlancer/tree/master/src/sqlancer):

{% for entry in supported -%}
* {% if entry.url %}[{{ entry.name }}]({{ entry.url }}){% else %}{{ entry.name }}{% endif %}
{% endfor %}

Supporting a database system is not the same as that project using SQLancer, and
not the same as SQLancer having found bugs in it. The
[impact page]({{ '/impact/#database-systems' | relative_url }}) tracks all three
separately.
