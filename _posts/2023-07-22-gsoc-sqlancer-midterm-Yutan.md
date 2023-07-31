---
title: 'GSoC 2023: Midterm Report of test case reduction'
date: 2023-07-30
author: Yutan Yang
categories:
  - blog
tags:
  - gsoc
---
# Overview

SQLancer generates a large number of statements, but not all of them are relevant to the bug. To automatically reduce the test cases, I implemented two reducers in SQLancer, the statement reducer and the AST-based reducer.
<center>
    <img style="border-radius: 0.3125em;
    box-shadow: 0 2px 4px 0 rgba(34,36,38,.12),0 2px 10px 0 rgba(34,36,38,.08); zoom: 60%;" 
    src="/assets/imgs-yutan-midterm/logs.png" alt="logs">
    <br>
    <div style="
    border-bottom: 1px;
    display: inline-block;
    padding: 2px;
    margin-bottom:5px;">randomly generated statements</div>
</center>

# Statement Reducer

The statement reducer utilizes the delta-debugging technique, which is designed for efficient test case reduction, to reduce the statements. The set of bug-inducing test cases is divided into subsets. Each subset is then individually removed. If the bug is still triggered without a specific subset, this subset is considered to be irrelevant and thus could be eliminated. Conversely, if removing any subset does not reproduce the bug, the reducer will divide test cases into more subsets and repeat the process. This iterative process continues until the set cannot be further divided into more subsets. 

For more details of delta-debugging, you can refer to the paper: [Simplifying and Isolating Failure-Inducing Input](https://www.cs.purdue.edu/homes/xyzhang/fall07/Papers/delta-debugging.pdf). This [video tutorial](https://youtu.be/lGe2-y1xibY) also helps.

Delta-debugging offers the advantage of potentially removing a significant number of statements in one turn, making it particularly efficient when the bug-inducing statements are sparsely distributed. Using the statement reducer, SQLancer could effectively reduce the set of statements to a minimal subset that reproduces the bug. 

An earlier version of sqlite3 (version 3.28.0) are used to test the statement reducer. An illustrative example is provided below. Given a test case containing thousands of statements, the reducer was able to effectively remove irrelevant ones and retained only <B>four </B> bug inducing statements.

<center>
    <img style="border-radius: 0.3125em;
    box-shadow: 0 2px 4px 0 rgba(34,36,38,.12),0 2px 10px 0 rgba(34,36,38,.08); zoom: 60%;" 
    src="/assets/imgs-yutan-midterm/sqlite3-original.png" alt="logs">
    <br>
    <div style="
    border-bottom: 1px;
    display: inline-block;
    padding: 2px;
    margin-bottom:5px;">
    A bug inducing test case for sqlite3 version 3.28.0</div>
</center>

```sql
-- reduced queries:
CREATE VIRTUAL TABLE rt0 USING rtree(c0, c1, c2);
CREATE VIRTUAL TABLE vt1 USING fts4(c0, compress=likely, uncompress=likely);
INSERT OR FAIL INTO vt1 VALUES ('g&'), (0.16215918433687637), ('1帬trBWP?');
INSERT OR IGNORE INTO rt0(c1) VALUES (x'');
```



# AST-Based Reducer

After the statement reducing pass, the rest of the statements could still be long and complex. The AST-based reducer focuses on eliminating redundant or unnecessary parts of queries.
<center>
    <img style="border-radius: 0.3125em;
    box-shadow: 0 2px 4px 0 rgba(34,36,38,.12),0 2px 10px 0 rgba(34,36,38,.08); zoom: 67%;" 
    src="/assets/imgs-yutan-midterm/unreduced.png" alt="unreduced">
    <br>
    <div style="
    border-bottom: 1px;
    display: inline-block;
    padding: 2px;
    margin-bottom:5px;">
    a complicated statement</div>
</center>


This reducer operates by parsing the SQL string into an abstract syntax tree (AST) using [JSQLParser](https://github.com/JSQLParser/JSqlParser), which supports the SQL standard as well as major RDBMS, and recursively visiting each AST node to apply transformations. These transformations include removing unnecessary clauses, irrelevant elements in a list, and replacing complex expressions with simpler ones. The reduced queries are then executed, and if the bug is still triggered, the transformation is retained. 
<center>
    <img style="border-radius: 0.3125em;
    box-shadow: 0 2px 4px 0 rgba(34,36,38,.12),0 2px 10px 0 rgba(34,36,38,.08); zoom: 67%;" 
    src="/assets/imgs-yutan-midterm/ASTreduced.png" alt="ASTreduced" >
    <br>
    <div style="
    border-bottom: 1px;
    display: inline-block;
    padding: 2px;
    margin-bottom:5px;">result of AST-based reduction</div>
</center>

Currently the reducer only performs generic transformations which works for any SQL dialects. However, different dialects may require special handling for unique features. Thus, expanding the capabilities of this reducer to provide support for various SQL dialects would be part of the future work.

# Reducers Testing

A virtual database engine is designed to test the functionality of reducers. Instead of really executing, the engine would record the input statements. The bug inducing condition can be customized to check the recorded statements, making it easy and convenient to observe if the reducers work as expected. For example, we could define that the coexistence of certain lines would result in a bug and see if the reducer isolated them. 
<center>
    <img style="border-radius: 0.3125em;
    box-shadow: 0 2px 4px 0 rgba(34,36,38,.12),0 2px 10px 0 rgba(34,36,38,.08);" 
    src="/assets/imgs-yutan-midterm/test-statement-reducer.png" alt="test-statement-reducer">
    <br>
    <div style="
    border-bottom: 1px;
    display: inline-block;
    padding: 2px;
    margin-bottom:5px;">test statement reduction via virtual engine</div>
</center>
We also apply the reducers to the old version of DBMSs to observe its capabilities in the real world.

# Usage

The statement reducer is now available. The AST-based reducer, however, is still experimental and has not been integrated into the project. 

This feature is disabled by default. To enable the reducer (actually only the statement reducer), you could add `–use-reducer` option when start SQLancer. Also, it supports defining limits of time and reducing steps by using `statement-reducer-max-steps=<limit>` and `statement-reducer-max-time=<limit>` 

# Challenges of Reducing

+ SQL dialects

  The dialects vary among DBMSs. At present, the AST-based reducer only offers support for generic transformations that can be applied to any dialect. However, accommodating distinct SQL syntaxes will be included in the future development plans.

+ Enhancing extendibility for AST-based reducer

  Rules of transformation are defined to shorten an SQL statement. Ensuring the ease of defining and integrating new rules of transformation would be highly advantageous as well as challenging.
